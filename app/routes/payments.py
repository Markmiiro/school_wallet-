from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, date

from app.database import get_db
from app.models import Wallet, Merchant, Transaction
from app.sms import sms_payment_alert, sms_low_balance_alert
from app.models import Student, User
router = APIRouter()




# ================================================
# POST /payments/
# Student pays at tuck shop
# ================================================
@router.post("/")
def make_payment(
    wallet_id: int,
    merchant_id: int,
    amount: int,
    description: str = None,
    db: Session = Depends(get_db)
):
    """
    Student pays a merchant at the tuck shop.

    This is INSTANT — no MoMo call needed.
    Money moves inside your system immediately.

    SAFETY CHECKS:
    1. Wallet must exist and be active
    2. Merchant must exist
    3. Balance must cover the amount
    4. Amount must not exceed daily limit
    """

    # ── CHECK 1: Wallet exists ──────────────────
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # ── CHECK 2: Wallet is active ───────────────
    if not wallet.is_active:
        raise HTTPException(
            status_code=403,
            detail="This wallet is deactivated. Contact school admin."
        )

    # ── CHECK 3: Merchant exists ────────────────
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # ── CHECK 4: Enough balance ─────────────────
    if wallet.balance < amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. "
                   f"Available: UGX {wallet.balance:,}, "
                   f"Required: UGX {amount:,}"
        )

    # ── CHECK 5: Daily limit ────────────────────
    # Check how much has been spent today already
    today = date.today()
    spent_today = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id == wallet_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )

    # Add up today's spending only
    total_spent_today = sum(
        t.amount for t in spent_today
        if t.timestamp and t.timestamp.date() == today
    )

    # Check if this payment would exceed the daily limit
    if wallet.daily_limit and (total_spent_today + amount) > wallet.daily_limit:
        remaining = wallet.daily_limit - total_spent_today
        raise HTTPException(
            status_code=400,
            detail=f"Daily limit exceeded. "
                   f"Daily limit: UGX {wallet.daily_limit:,}, "
                   f"Already spent today: UGX {total_spent_today:,}, "
                   f"Remaining: UGX {remaining:,}"
        )

    # ── DEDUCT from wallet ──────────────────────
    wallet.balance -= amount

    # ── RECORD the transaction ──────────────────
    txn = Transaction(
        wallet_id=wallet_id,
        merchant_id=merchant_id,
        amount=amount,
        type="payment",
        status="completed",      # payments are instant — no pending
        description=description or f"Payment at {merchant.name}",
        timestamp=datetime.utcnow(),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    print(f"\n💸 Payment made:")
    print(f"   Student wallet: {wallet_id}")
    print(f"   Merchant:       {merchant.name}")
    print(f"   Amount:         UGX {amount:,}")
    print(f"   New balance:    UGX {wallet.balance:,}")

    # ── SEND SMS TO PARENT ──────────────────────
    print("🔍 DEBUG: Starting SMS block...")
    try:
        print(f"🔍 DEBUG: Looking for student with wallet_id={wallet.student_id}")
        student = db.query(Student).filter(
            Student.id == wallet.student_id
        ).first()

        print(f"🔍 DEBUG: Student found = {student}")

        if student:
            print(f"🔍 DEBUG: Looking for parent with id={student.parent_id}")
            parent = db.query(User).filter(
                User.id == student.parent_id
            ).first()

            print(f"🔍 DEBUG: Parent found = {parent}")

            if parent:
                print(f"🔍 DEBUG: Sending SMS to {parent.phone}")
                sms_payment_alert(
                    parent_phone=parent.phone,
                    student_name=student.name,
                    amount=amount,
                    merchant_name=merchant.name,
                    remaining_balance=wallet.balance,
                    timestamp=txn.timestamp.strftime("%d %b %I:%M%p"),
                )

                if wallet.balance < 2000:
                    sms_low_balance_alert(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        remaining_balance=wallet.balance,
                    )
        else:
            print("🔍 DEBUG: No student found — SMS skipped")

    except Exception as e:
        print(f"⚠️  SMS notification failed: {e}")
        import traceback
        traceback.print_exc()

    print("🔍 DEBUG: SMS block finished")

    return {
        "message": "Payment successful ✅",
        "transaction_id": txn.id,
        "wallet_id": wallet_id,
        "merchant": merchant.name,
        "amount_paid": amount,
        "remaining_balance": wallet.balance,
        "currency": "UGX",
        "description": txn.description,
        "timestamp": txn.timestamp,
    }


# ================================================
# GET /payments/merchant/{merchant_id}
# All payments received by a merchant today
# Useful for the tuck shop to see their sales
# ================================================
@router.get("/merchant/{merchant_id}")
def get_merchant_payments(
    merchant_id: int,
    db: Session = Depends(get_db)
):
    """
    Get all payments received by a merchant.
    Shows their total sales and each transaction.
    Useful for end of day reconciliation.
    """
    merchant = db.query(Merchant).filter(Merchant.id == merchant_id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    payments = (
        db.query(Transaction)
        .filter(
            Transaction.merchant_id == merchant_id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .order_by(Transaction.timestamp.desc())
        .all()
    )

    total_sales = sum(p.amount for p in payments)

    return {
        "merchant": merchant.name,
        "merchant_id": merchant_id,
        "total_sales_ugx": total_sales,
        "number_of_payments": len(payments),
        "payments": [
            {
                "transaction_id": p.id,
                "amount": p.amount,
                "description": p.description,
                "date": p.timestamp,
            }
            for p in payments
        ]
    }