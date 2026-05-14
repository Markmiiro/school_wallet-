# ================================================
# app/routes/ussd.py
# ------------------------------------------------
# Handles USSD sessions from Africa's Talking.
#
# HOW USSD WORKS:
# - Parent dials *284#
# - AT sends POST request to /ussd with:
#     sessionId  → unique session ID
#     phoneNumber → parent's phone e.g. +256771234567
#     text        → what parent has typed so far
#                   e.g. "" = first dial
#                        "1" = pressed 1
#                        "1*2000" = pressed 1 then typed 2000
#
# - Your server responds with:
#     "CON Your menu text here"  → continue (show more)
#     "END Your final message"   → end session
#
# CON = keep session open (parent can type more)
# END = close session (parent sees final message)
# ================================================

from fastapi import APIRouter, Depends, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Student, Wallet, Transaction
from app.momo import charge_mobile_money
import uuid

router = APIRouter()


# ================================================
# POST /ussd
# Africa's Talking calls this on every keypress
# ================================================
@router.post("/", response_class=PlainTextResponse)
async def ussd_handler(
    sessionId:   str = Form(...),
    phoneNumber: str = Form(...),
    text:        str = Form(""),
    db: Session = Depends(get_db)
):
    """
    Main USSD handler.
    Africa's Talking calls this every time a parent
    presses a key on their phone.

    text is cumulative — it grows with each keypress:
    First dial  → text = ""
    Press 1     → text = "1"
    Type 20000  → text = "1*20000"
    Press 1     → text = "1*20000*1"
    """

    # Clean the phone number
    # AT sends +256771234567 — remove the +
    phone = phoneNumber.strip().replace("+", "")

    # Split text into parts based on * separator
    # "1*20000*1" → ["1", "20000", "1"]
    parts = text.split("*") if text else []

    print(f"\n📞 USSD: phone={phone} text='{text}' parts={parts}")

    # ── FIND PARENT ──────────────────────────────
    # Look up parent by their phone number
    parent = db.query(User).filter(
        User.phone == phone
    ).first()

    # ── LEVEL 0: Parent not registered ──────────
    if not parent:
        return "END Sorry, your number is not registered.\nContact the school to register."

    # ── FIND PARENT'S STUDENTS ───────────────────
    students = db.query(Student).filter(
        Student.parent_id == parent.id
    ).all()

    if not students:
        return "END You have no students registered.\nContact the school admin."

    # For simplicity use first student
    # Later we can add a student selection menu
    student = students[0]

    # Get student's wallet
    wallet = db.query(Wallet).filter(
        Wallet.student_id == student.id
    ).first()

    # ════════════════════════════════════════════
    # LEVEL 1 — MAIN MENU (parent just dialed)
    # ════════════════════════════════════════════
    if text == "":
        return (
            f"CON Welcome to School Wallet\n"
            f"Student: {student.name}\n"
            f"1. Check balance\n"
            f"2. Top up wallet\n"
            f"3. Transaction history\n"
            f"4. Exit"
        )

    # ════════════════════════════════════════════
    # LEVEL 2 — PARENT PRESSED A NUMBER
    # ════════════════════════════════════════════

    # ── Option 1: Check balance ──────────────────
    if text == "1":
        if wallet:
            return (
                f"END {student.name}'s wallet\n"
                f"Balance: UGX {wallet.balance:,.0f}\n"
                f"Daily limit: UGX {wallet.daily_limit:,.0f}"
            )
        else:
            return "END No wallet found for this student."

    # ── Option 2: Top up wallet ──────────────────
    if text == "2":
        return (
            f"CON Top up {student.name}'s wallet\n"
            f"Current balance: UGX {wallet.balance:,.0f}\n"
            f"Enter amount (UGX):"
        )

    # ── Option 3: Transaction history ────────────
    if text == "3":
        if not wallet:
            return "END No wallet found."

        # Get last 3 transactions
        transactions = (
            db.query(Transaction)
            .filter(Transaction.wallet_id == wallet.id)
            .order_by(Transaction.timestamp.desc())
            .limit(3)
            .all()
        )

        if not transactions:
            return "END No transactions yet."

        lines = [f"Last transactions for {student.name}:"]
        for t in transactions:
            direction = "IN" if t.type == "topup" else "OUT"
            lines.append(
                f"{direction} UGX {t.amount:,.0f} "
                f"({t.status})"
            )

        return "END " + "\n".join(lines)

    # ── Option 4: Exit ───────────────────────────
    if text == "4":
        return "END Thank you for using School Wallet."

    # ════════════════════════════════════════════
    # LEVEL 3 — PARENT ENTERED TOP-UP AMOUNT
    # text = "2*20000" means pressed 2 then typed 20000
    # ════════════════════════════════════════════
    if len(parts) == 2 and parts[0] == "2":
        try:
            amount = int(parts[1])
        except ValueError:
            return "END Invalid amount. Please enter numbers only."

        if amount < 500:
            return "END Minimum top-up is UGX 500."
        if amount > 5_000_000:
            return "END Maximum top-up is UGX 5,000,000."

        return (
            f"CON Top up UGX {amount:,} for {student.name}?\n"
            f"1. Confirm\n"
            f"2. Cancel"
        )

    # ════════════════════════════════════════════
    # LEVEL 4 — PARENT CONFIRMED OR CANCELLED
    # text = "2*20000*1" means confirmed
    # text = "2*20000*2" means cancelled
    # ════════════════════════════════════════════
    if len(parts) == 3 and parts[0] == "2":
        try:
            amount = int(parts[1])
        except ValueError:
            return "END Invalid amount."

        choice = parts[2]

        # ── Cancelled ────────────────────────────
        if choice == "2":
            return "END Top up cancelled."

        # ── Confirmed ────────────────────────────
        if choice == "1":
            if not wallet:
                return "END Wallet not found. Contact school admin."

            # Generate reference ID
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

            # Call Flutterwave to charge parent
            try:
                result = await charge_mobile_money(
                    phone=phone,
                    amount=amount,
                    network="MTN",  # detect from phone later
                    tx_ref=ref_id,
                    customer_name=parent.name,
                )

                if result.get("status") == "success":
                    return (
                        f"END Request sent!\n"
                        f"You will receive a MoMo prompt\n"
                        f"to approve UGX {amount:,}.\n"
                        f"Wallet updates automatically."
                    )
                else:
                    txn.status = "failed"
                    db.commit()
                    return "END Payment request failed. Try again."

            except Exception as e:
                print(f"⚠️  USSD top-up error: {e}")
                txn.status = "failed"
                db.commit()
                return "END Something went wrong. Try again later."

    # ── Fallback for unrecognised input ──────────
    return (
        "CON Invalid option.\n"
        "1. Check balance\n"
        "2. Top up wallet\n"
        "3. Transaction history\n"
        "4. Exit"
    )