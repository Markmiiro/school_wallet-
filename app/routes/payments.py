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
# ================================================
# POST /payments/nfc
# Student taps NFC bracelet at tuck shop
# ------------------------------------------------
# This is called by the tuck shop NFC reader device.
# It looks up the student by their bracelet UID
# and deducts the payment from their wallet.
# ================================================
@router.post("/nfc")
def nfc_payment(
    tag_uid: str,       # the NFC bracelet's unique ID
    merchant_id: int,
    amount: int,
    description: str = None,
    db: Session = Depends(get_db)
):
    """
    Process a payment when a student taps their NFC bracelet.

    The tuck shop device sends:
    - tag_uid    → the bracelet's unique ID e.g. "A3F2B1C4"
    - merchant_id → which tuck shop
    - amount      → how much to charge

    Your server:
    1. Looks up which student owns this bracelet
    2. Finds their wallet
    3. Checks balance and daily limit
    4. Deducts the amount
    5. Sends SMS to parent
    """
    from app.models import NFCTag

    # ── FIND STUDENT BY NFC TAG ─────────────────
    nfc = db.query(NFCTag).filter(
        NFCTag.tag_uid == tag_uid
    ).first()

    if not nfc:
        raise HTTPException(
            status_code=404,
            detail=f"NFC tag {tag_uid} not registered. Contact school admin."
        )

    # ── FIND WALLET ─────────────────────────────
    wallet = db.query(Wallet).filter(
        Wallet.student_id == nfc.student_id
    ).first()

    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    if not wallet.is_active:
        raise HTTPException(
            status_code=403,
            detail="Wallet is deactivated. Contact school admin."
        )

    # ── FIND MERCHANT ────────────────────────────
    merchant = db.query(Merchant).filter(
        Merchant.id == merchant_id
    ).first()

    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # ── CHECK BALANCE ────────────────────────────
    if wallet.balance < amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. "
                   f"Available: UGX {wallet.balance:,}, "
                   f"Required: UGX {amount:,}"
        )

    # ── CHECK DAILY LIMIT ────────────────────────
    today = date.today()
    spent_today = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id == wallet.id,
            Transaction.type == "payment",
            Transaction.status == "completed",
        )
        .all()
    )
    total_spent_today = sum(
        t.amount for t in spent_today
        if t.timestamp and t.timestamp.date() == today
    )

    if wallet.daily_limit and (total_spent_today + amount) > wallet.daily_limit:
        remaining = wallet.daily_limit - total_spent_today
        raise HTTPException(
            status_code=400,
            detail=f"Daily limit exceeded. Remaining: UGX {remaining:,}"
        )

    # ── DEDUCT BALANCE ───────────────────────────
    wallet.balance -= amount

    # ── RECORD TRANSACTION ───────────────────────
    txn = Transaction(
        wallet_id=wallet.id,
        merchant_id=merchant_id,
        amount=amount,
        type="payment",
        status="completed",
        description=description or f"NFC payment at {merchant.name}",
        timestamp=datetime.utcnow(),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    print(f"\n📡 NFC Payment:")
    print(f"   Tag:      {tag_uid}")
    print(f"   Merchant: {merchant.name}")
    print(f"   Amount:   UGX {amount:,}")
    print(f"   Balance:  UGX {wallet.balance:,}")

    # ── SEND SMS TO PARENT ───────────────────────
    try:
        student = db.query(Student).filter(
            Student.id == wallet.student_id
        ).first()
        if student:
            parent = db.query(User).filter(
                User.id == student.parent_id
            ).first()
            if parent:
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
    except Exception as e:
        print(f"⚠️  SMS failed: {e}")

    return {
        "message": "NFC payment successful ✅",
        "tag_uid": tag_uid,
        "merchant": merchant.name,
        "amount_paid": amount,
        "remaining_balance": wallet.balance,
        "currency": "UGX",
        "transaction_id": txn.id,
        "timestamp": txn.timestamp,
    }
# ================================================
# POST /payments/sync
# Tuck shop device syncs offline payments
# ------------------------------------------------
# When internet returns, the device sends all
# payments it stored locally while offline.
# Your server processes each one and returns
# success or failure for each.
# ================================================
@router.post("/sync")
def sync_offline_payments(
    payments: list[dict],
    merchant_id: int,
    device_id: str,
    db: Session = Depends(get_db)
):
    """
    Receives a batch of offline payments from a tuck shop device.

    Device sends a list like:
    [
        {
            "tag_uid": "A3F2B1C4",
            "amount": 2000,
            "description": "Lunch",
            "timestamp": "2026-05-14T10:30:00"
        },
        ...
    ]

    Server processes each one and returns:
    - processed: list of successful payments
    - failed: list of failed payments with reasons
    """
    from app.models import NFCTag

    processed = []
    failed = []

    print(f"\n🔄 Sync received from device {device_id}")
    print(f"   {len(payments)} payments to process")

    for payment in payments:
        tag_uid     = payment.get("tag_uid")
        amount      = payment.get("amount")
        description = payment.get("description", "Offline payment")
        offline_time = payment.get("timestamp")

        try:
            # Find NFC tag
            nfc = db.query(NFCTag).filter(
                NFCTag.tag_uid == tag_uid
            ).first()

            if not nfc:
                failed.append({
                    "tag_uid": tag_uid,
                    "amount": amount,
                    "reason": "NFC tag not registered"
                })
                continue

            # Find wallet
            wallet = db.query(Wallet).filter(
                Wallet.student_id == nfc.student_id
            ).first()

            if not wallet or not wallet.is_active:
                failed.append({
                    "tag_uid": tag_uid,
                    "amount": amount,
                    "reason": "Wallet not found or deactivated"
                })
                continue

            # Check balance
            if wallet.balance < amount:
                failed.append({
                    "tag_uid": tag_uid,
                    "amount": amount,
                    "reason": f"Insufficient balance: UGX {wallet.balance:,}"
                })
                continue

            # Deduct and record
            wallet.balance -= amount

            txn = Transaction(
                wallet_id=wallet.id,
                merchant_id=merchant_id,
                amount=amount,
                type="payment",
                status="completed",
                description=f"[OFFLINE] {description}",
                timestamp=datetime.utcnow(),
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)

            processed.append({
                "tag_uid": tag_uid,
                "amount": amount,
                "transaction_id": txn.id,
                "status": "completed"
            })

            # Send SMS to parent
            try:
                student = db.query(Student).filter(
                    Student.id == wallet.student_id
                ).first()
                if student:
                    parent = db.query(User).filter(
                        User.id == student.parent_id
                    ).first()
                    if parent:
                        sms_payment_alert(
                            parent_phone=parent.phone,
                            student_name=student.name,
                            amount=amount,
                            merchant_name=f"Tuck Shop (offline sync)",
                            remaining_balance=wallet.balance,
                            timestamp=offline_time or "earlier today",
                        )
            except Exception:
                pass

        except Exception as e:
            failed.append({
                "tag_uid": tag_uid,
                "amount": amount,
                "reason": str(e)
            })

    print(f"   ✅ Processed: {len(processed)}")
    print(f"   ❌ Failed:    {len(failed)}")

    return {
        "message": "Sync complete",
        "device_id": device_id,
        "total_received": len(payments),
        "processed": len(processed),
        "failed": len(failed),
        "details": {
            "processed": processed,
            "failed": failed,
        }
    }


# ================================================
# GET /payments/sync/status/{device_id}
# Check last sync status for a device
# ================================================
@router.get("/sync/status/{device_id}")
def get_sync_status(device_id: str):
    """
    Returns the last sync status for a tuck shop device.
    Useful for the admin to see when each device last synced.
    """
    return {
        "device_id": device_id,
        "message": "Sync status endpoint ready",
        "note": "Full sync tracking comes in Stage 4"
    }

