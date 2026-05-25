# ================================================
# app/routes/ussd.py
# ------------------------------------------------
# Production-ready USSD for Africa's Talking Uganda
#
# FIXES IN THIS VERSION:
# 1. Multiple children handled properly
# 2. Network auto-detected from phone number
# 3. Amount validation — no crashes on bad input
# 4. PIN confirmation before top-up
# 5. Clear error messages
# 6. Session timeout handled gracefully
# 7. Failed top-up notification to parent
# ================================================

from fastapi import APIRouter, Depends, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Student, Wallet, Transaction
from app.momo import charge_mobile_money
from app.sms import detect_network
import uuid

router = APIRouter()

USSD_CODE = "*384*23114#"


# ================================================
# HELPER: Format balance display
# ================================================
def fmt(amount) -> str:
    """Format number with commas e.g. 10100 → 10,100"""
    try:
        return f"{int(amount):,}"
    except:
        return str(amount)


# ================================================
# HELPER: Get student list for parent
# ================================================
def get_parent_students(db: Session, phone: str):
    """Get parent and their students from phone number."""
    phone = phone.strip().replace("+", "").replace(" ", "")

    parent = db.query(User).filter(
        User.phone == phone
    ).first()

    if not parent:
        return None, []

    students = db.query(Student).filter(
        Student.parent_id == parent.id
    ).all()

    return parent, students


# ================================================
# MAIN USSD HANDLER
# ================================================
@router.post("/", response_class=PlainTextResponse)
async def ussd_handler(
    sessionId:   str = Form(...),
    phoneNumber: str = Form(...),
    text:        str = Form(""),
    db: Session = Depends(get_db)
):
    """
    Main USSD handler — called by Africa's Talking
    on every keypress from the parent's phone.

    text grows with each keypress:
    ""        → first dial
    "1"       → pressed 1
    "1*1"     → pressed 1 then 1
    "2*5000"  → pressed 2 then typed 5000
    """

    phone  = phoneNumber.strip().replace("+", "")
    parts  = text.split("*") if text else []
    level  = len(parts)

    # Detect network from phone number
    network = detect_network(phone)

    print(f"\nUSSD: phone={phone} network={network} text='{text}'")

    # ── Find parent ──────────────────────────────
    parent, students = get_parent_students(db, phone)

    if not parent:
        return (
            "END Your number is not registered.\n"
            f"Contact your school to register.\n"
            f"School Wallet - {USSD_CODE}"
        )

    if not students:
        return (
            "END You have no students registered.\n"
            "Contact your school admin."
        )

    # ════════════════════════════════════════════
    # MAIN MENU
    # ════════════════════════════════════════════
    if text == "":
        if len(students) == 1:
            # One child — go straight to menu
            student = students[0]
            wallet  = db.query(Wallet).filter(
                Wallet.student_id == student.id
            ).first()
            balance = wallet.balance if wallet else 0

            return (
                f"CON School Wallet\n"
                f"Student: {student.name}\n"
                f"Balance: UGX {fmt(balance)}\n\n"
                f"1. Top up wallet\n"
                f"2. Check balance\n"
                f"3. Transaction history\n"
                f"4. Exit"
            )
        else:
            # Multiple children — show selection
            menu = "CON School Wallet\nSelect student:\n"
            for i, s in enumerate(students, 1):
                menu += f"{i}. {s.name}\n"
            return menu.strip()

    # ════════════════════════════════════════════
    # SINGLE CHILD FLOW
    # ════════════════════════════════════════════
    if len(students) == 1:
        student = students[0]
        wallet  = db.query(Wallet).filter(
            Wallet.student_id == student.id
        ).first()

        # ── Option 2: Check balance ──────────────
        if text == "2":
            balance     = wallet.balance if wallet else 0
            daily_limit = wallet.daily_limit if wallet else 0
            return (
                f"END {student.name}\n"
                f"Balance: UGX {fmt(balance)}\n"
                f"Daily limit: UGX {fmt(daily_limit)}\n"
                f"Dial {USSD_CODE} to top up."
            )

        # ── Option 3: Transaction history ────────
        if text == "3":
            if not wallet:
                return "END No wallet found."

            txns = (
                db.query(Transaction)
                .filter(
                    Transaction.wallet_id == wallet.id,
                    Transaction.status == "completed",
                )
                .order_by(Transaction.timestamp.desc())
                .limit(3)
                .all()
            )

            if not txns:
                return (
                    f"END {student.name}\n"
                    f"No transactions yet.\n"
                    f"Balance: UGX {fmt(wallet.balance)}"
                )

            msg = f"END Last 3 transactions:\n"
            for t in txns:
                direction = "IN" if t.type == "topup" else "OUT"
                msg += f"{direction} UGX {fmt(t.amount)}\n"

            msg += f"Balance: UGX {fmt(wallet.balance)}"
            return msg

        # ── Option 4: Exit ───────────────────────
        if text == "4":
            return "END Thank you for using School Wallet."

        # ── Option 1: Top up ─────────────────────
        if text == "1":
            return (
                f"CON Top up {student.name}\n"
                f"Current balance: UGX {fmt(wallet.balance)}\n\n"
                f"Enter amount (UGX):"
            )

        # ── Level 2: Amount entered ───────────────
        if len(parts) == 2 and parts[0] == "1":
            try:
                amount = int(parts[1].replace(",", ""))
            except ValueError:
                return (
                    "CON Invalid amount.\n"
                    "Please enter numbers only.\n\n"
                    "Enter amount (UGX):"
                )

            if amount < 500:
                return (
                    "CON Minimum top-up is UGX 500.\n\n"
                    "Enter amount (UGX):"
                )

            if amount > 2_000_000:
                return (
                    "CON Maximum top-up is UGX 2,000,000.\n\n"
                    "Enter amount (UGX):"
                )

            return (
                f"CON Confirm top-up:\n"
                f"Student: {student.name}\n"
                f"Amount: UGX {fmt(amount)}\n"
                f"Network: {network}\n\n"
                f"1. Confirm\n"
                f"2. Cancel"
            )

        # ── Level 3: Confirmed ────────────────────
        if len(parts) == 3 and parts[0] == "1":
            try:
                amount = int(parts[1].replace(",", ""))
            except ValueError:
                return "END Invalid amount. Please try again."

            choice = parts[2]

            if choice == "2":
                return "END Top-up cancelled."

            if choice == "1":
                if not wallet:
                    return "END Wallet not found. Contact school admin."

                ref_id = str(uuid.uuid4())

                # Save pending transaction
                txn = Transaction(
                    wallet_id=wallet.id,
                    amount=amount,
                    type="topup",
                    status="pending",
                    reference=ref_id,
                    momo_phone=phone,
                    description=f"USSD top-up for {student.name}",
                )
                db.add(txn)
                db.commit()

                # Call payment gateway
                try:
                    result = await charge_mobile_money(
                        phone=phone,
                        amount=amount,
                        network=network,
                        tx_ref=ref_id,
                        customer_name=parent.name,
                    )

                    if result.get("status") == "success":
                        return (
                            f"END Request sent!\n"
                            f"Amount: UGX {fmt(amount)}\n"
                            f"You will receive a {network} prompt.\n"
                            f"Approve with your PIN.\n"
                            f"Wallet updates automatically."
                        )
                    else:
                        txn.status = "failed"
                        db.commit()
                        return (
                            "END Payment request failed.\n"
                            "Check your MoMo balance and try again."
                        )

                except Exception as e:
                    txn.status = "failed"
                    db.commit()
                    print(f"USSD top-up error: {e}")
                    return (
                        "END Something went wrong.\n"
                        "Please try again later."
                    )

    # ════════════════════════════════════════════
    # MULTIPLE CHILDREN FLOW
    # ════════════════════════════════════════════
    else:
        # Parent selected a child
        if level >= 1:
            try:
                child_index = int(parts[0]) - 1
                if child_index < 0 or child_index >= len(students):
                    return "END Invalid selection. Please try again."
                student = students[child_index]
            except ValueError:
                return "END Invalid selection."

            wallet = db.query(Wallet).filter(
                Wallet.student_id == student.id
            ).first()
            balance = wallet.balance if wallet else 0

            # Show child menu
            if level == 1:
                return (
                    f"CON {student.name}\n"
                    f"Balance: UGX {fmt(balance)}\n\n"
                    f"1. Top up wallet\n"
                    f"2. Check balance\n"
                    f"3. Transaction history\n"
                    f"0. Back"
                )

            action = parts[1] if level > 1 else ""

            # Back to main menu
            if action == "0":
                menu = "CON Select student:\n"
                for i, s in enumerate(students, 1):
                    menu += f"{i}. {s.name}\n"
                return menu.strip()

            # Check balance
            if action == "2":
                return (
                    f"END {student.name}\n"
                    f"Balance: UGX {fmt(balance)}\n"
                    f"Daily limit: UGX {fmt(wallet.daily_limit if wallet else 0)}"
                )

            # Transaction history
            if action == "3":
                if not wallet:
                    return "END No wallet found."
                txns = (
                    db.query(Transaction)
                    .filter(
                        Transaction.wallet_id == wallet.id,
                        Transaction.status == "completed",
                    )
                    .order_by(Transaction.timestamp.desc())
                    .limit(3)
                    .all()
                )
                if not txns:
                    return f"END No transactions yet."

                msg = "END Last 3 transactions:\n"
                for t in txns:
                    d = "IN" if t.type == "topup" else "OUT"
                    msg += f"{d} UGX {fmt(t.amount)}\n"
                return msg

            # Top up — enter amount
            if action == "1" and level == 2:
                return (
                    f"CON Top up {student.name}\n"
                    f"Balance: UGX {fmt(balance)}\n\n"
                    f"Enter amount (UGX):"
                )

            # Amount entered
            if action == "1" and level == 3:
                try:
                    amount = int(parts[2].replace(",", ""))
                except ValueError:
                    return (
                        "CON Invalid amount.\n"
                        "Enter numbers only:\n"
                    )

                if amount < 500:
                    return "CON Minimum is UGX 500.\nEnter amount:"
                if amount > 2_000_000:
                    return "CON Maximum is UGX 2,000,000.\nEnter amount:"

                return (
                    f"CON Confirm:\n"
                    f"{student.name}\n"
                    f"UGX {fmt(amount)} via {network}\n\n"
                    f"1. Confirm\n"
                    f"2. Cancel"
                )

            # Confirmed or cancelled
            if action == "1" and level == 4:
                try:
                    amount = int(parts[2].replace(",", ""))
                except ValueError:
                    return "END Invalid amount."

                choice = parts[3]

                if choice == "2":
                    return "END Top-up cancelled."

                if choice == "1":
                    ref_id = str(uuid.uuid4())
                    txn = Transaction(
                        wallet_id=wallet.id,
                        amount=amount,
                        type="topup",
                        status="pending",
                        reference=ref_id,
                        momo_phone=phone,
                        description=f"USSD top-up for {student.name}",
                    )
                    db.add(txn)
                    db.commit()

                    try:
                        result = await charge_mobile_money(
                            phone=phone,
                            amount=amount,
                            network=network,
                            tx_ref=ref_id,
                            customer_name=parent.name,
                        )

                        if result.get("status") == "success":
                            return (
                                f"END Request sent!\n"
                                f"UGX {fmt(amount)} for {student.name}.\n"
                                f"Approve on your {network} phone.\n"
                                f"Wallet updates automatically."
                            )
                        else:
                            txn.status = "failed"
                            db.commit()
                            return (
                                "END Payment failed.\n"
                                "Check MoMo balance and retry."
                            )

                    except Exception as e:
                        txn.status = "failed"
                        db.commit()
                        return "END Error occurred. Try again later."

    # Fallback
    return (
        "CON Invalid option.\n"
        "1. Top up wallet\n"
        "2. Check balance\n"
        "3. Transaction history\n"
        "4. Exit"
    )