# ================================================
# routes/topup.py
# ------------------------------------------------
# Handles parent wallet top-ups via Flutterwave.
#
# FLOW:
# 1. Parent sends amount + phone + network + wallet_id
# 2. Server validates everything
# 3. Server saves PENDING transaction to DB
# 4. Server calls Flutterwave to charge parent phone
# 5. Flutterwave sends USSD prompt to parent
# 6. Parent enters MoMo PIN to approve
# 7. Flutterwave calls /webhook with result
# 8. Webhook credits wallet if successful
#
# ENDPOINTS:
# POST /topup/                      → initiate top-up
# GET  /topup/{reference_id}        → check top-up status
# GET  /topup/history/{wallet_id}   → all top-ups for a wallet
# ================================================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime
import uuid

from app.database import get_db
from app.models import Transaction, Wallet, Student
from app.momo import charge_mobile_money

router = APIRouter()


# ================================================
# REQUEST / RESPONSE SCHEMAS
# ================================================

class TopUpRequest(BaseModel):
    """
    What the parent sends to initiate a top-up.

    Example:
    {
        "wallet_id": 1,
        "amount": 20000,
        "phone_number": "256771234567",
        "network": "MTN",
        "note": "For lunch this week"
    }
    """
    wallet_id: int
    amount: int
    phone_number: str
    network: str
    note: Optional[str] = None

    @field_validator("amount")
    def amount_must_be_valid(cls, v):
        if v < 500:
            raise ValueError("Minimum top-up is UGX 500")
        if v > 5_000_000:
            raise ValueError("Maximum top-up is UGX 5,000,000")
        return v

    @field_validator("phone_number")
    def phone_must_be_valid(cls, v):
        v = v.replace(" ", "")
        if not v.startswith("256"):
            raise ValueError("Phone must start with 256. Example: 256771234567")
        if not v.isdigit():
            raise ValueError("Phone must contain digits only — no spaces, dashes, or + signs")
        if len(v) != 12:
            raise ValueError(f"Phone must be exactly 12 digits. Got {len(v)}: {v}")
        return v

    @field_validator("network")
    def network_must_be_valid(cls, v):
        v = v.upper().strip()
        if v not in ["MTN", "AIRTEL"]:
            raise ValueError("Network must be MTN or AIRTEL")
        return v


class TopUpResponse(BaseModel):
    """What the API returns after initiating a top-up."""
    reference_id: str
    wallet_id: int
    amount: int
    status: str
    phone_number: str
    network: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True


# ================================================
# ENDPOINT 1 — Initiate a top-up
# ================================================
# POST /topup/
#
# IMPORTANT:
# This only STARTS the payment — it does not credit
# the wallet. The wallet is credited only after
# Flutterwave confirms via /webhook.
# ================================================
@router.post("/", response_model=TopUpResponse)
async def initiate_topup(
    topup_data: TopUpRequest,
    db: Session = Depends(get_db)
):
    """
    Initiate a wallet top-up via MTN or Airtel Money.
    The parent will receive a USSD prompt to approve with their PIN.
    Wallet is credited automatically after approval.
    """

    # ── STEP 1: Check wallet exists ────────────────────────
    wallet = db.query(Wallet).filter(
        Wallet.id == topup_data.wallet_id
    ).first()

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found: {topup_data.wallet_id}"
        )

    # ── STEP 2: Check wallet is active ─────────────────────
    if not wallet.is_active:
        raise HTTPException(
            status_code=403,
            detail="This wallet is deactivated. Contact school admin."
        )

    # ── STEP 3: Get student name for the payment note ──────
    student = db.query(Student).filter(
        Student.id == wallet.student_id
    ).first()

    student_name = student.name if student else "Student"

    # ── STEP 4: Generate unique reference ID ───────────────
    # This links your DB record to Flutterwave's record.
    # Flutterwave sends this back in the webhook callback.
    reference_id = str(uuid.uuid4())

    # ── STEP 5: Save PENDING transaction FIRST ─────────────
    # Always save before calling Flutterwave.
    # If the server crashes mid-call, the record exists
    # and can be recovered or investigated.
    txn = Transaction(
        wallet_id=topup_data.wallet_id,
        amount=topup_data.amount,
        type="topup",
        status="pending",
        reference=reference_id,
        momo_phone=topup_data.phone_number,
        description=topup_data.note or f"Top-up for {student_name}",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    print(f"\n💳 Top-up initiated:")
    print(f"   Student:   {student_name}")
    print(f"   Amount:    UGX {topup_data.amount:,}")
    print(f"   Phone:     {topup_data.phone_number} ({topup_data.network})")
    print(f"   Reference: {reference_id}")
    print(f"   Status:    PENDING")

    # ── STEP 6: Call Flutterwave ────────────────────────────
    # Sends USSD prompt to parent's phone.
    # If this fails we mark the transaction as failed.
    try:
        flw_response = await charge_mobile_money(
            phone=topup_data.phone_number,
            amount=topup_data.amount,
            network=topup_data.network,
            tx_ref=reference_id,
            customer_name=f"Parent of {student_name}",
        )

        if flw_response.get("status") != "success":
            txn.status = "failed"
            db.commit()
            error_msg = flw_response.get("message", "Unknown Flutterwave error")
            print(f"   ❌ Flutterwave rejected: {error_msg}")
            raise HTTPException(
                status_code=400,
                detail=f"Payment request failed: {error_msg}"
            )

        print(f"   ✅ USSD prompt sent to {topup_data.phone_number}")

    except HTTPException:
        raise

    except Exception as e:
        txn.status = "failed"
        db.commit()
        print(f"   ❌ Error: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach payment provider. Try again. Error: {str(e)}"
        )

    # ── STEP 7: Return pending transaction ─────────────────
    return TopUpResponse(
        reference_id=reference_id,
        wallet_id=txn.wallet_id,
        amount=txn.amount,
        status="pending",
        phone_number=topup_data.phone_number,
        network=topup_data.network,
        message=(
            f"Payment request sent to {topup_data.phone_number}. "
            f"Enter your {topup_data.network} PIN to approve. "
            f"Wallet will be credited automatically."
        ),
        created_at=txn.timestamp,
    )


# ================================================
# ENDPOINT 2 — Check status of a top-up
# ================================================
# GET /topup/{reference_id}
#
# Use the reference_id returned from POST /topup/
# to check if the payment was approved or failed.
# ================================================
@router.get("/{reference_id}")
def check_topup_status(
    reference_id: str,
    db: Session = Depends(get_db)
):
    """
    Check the current status of a top-up.

    Returns one of:
    - pending   → parent has not approved yet
    - completed → wallet has been credited
    - failed    → rejected or timed out
    """
    txn = db.query(Transaction).filter(
        Transaction.reference == reference_id
    ).first()

    if not txn:
        raise HTTPException(
            status_code=404,
            detail=f"No transaction found with reference: {reference_id}"
        )

    # Human-readable status messages
    status_messages = {
        "pending":   "Waiting for parent to approve on their phone",
        "completed": "Payment approved — wallet has been credited",
        "failed":    "Payment failed or was rejected by the parent",
    }

    return {
        "reference_id": txn.reference,
        "amount": txn.amount,
        "status": txn.status,
        "message": status_messages.get(txn.status, "Unknown status"),
        "wallet_id": txn.wallet_id,
        "phone": txn.momo_phone,
        "date": txn.timestamp,
    }


# ================================================
# ENDPOINT 3 — Top-up history for a wallet
# ================================================
# GET /topup/history/{wallet_id}?limit=10
#
# Returns all top-up transactions for a wallet.
# Ordered newest first.
# ================================================
@router.get("/history/{wallet_id}")
def get_topup_history(
    wallet_id: int,
    limit: int = 10,
    db: Session = Depends(get_db)
):
    """
    Get the top-up history for a student's wallet.
    Useful for the parent to track how much they have added.
    """
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found: {wallet_id}"
        )

    topups = (
        db.query(Transaction)
        .filter(
            Transaction.wallet_id == wallet_id,
            Transaction.type == "topup"
        )
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
        .all()
    )

    total_credited = sum(
        t.amount for t in topups if t.status == "completed"
    )

    return {
        "wallet_id": wallet_id,
        "current_balance_ugx": wallet.balance,
        "total_topped_up_ugx": total_credited,
        "number_of_topups": len(topups),
        "topups": [
            {
                "reference_id": t.reference,
                "amount_ugx": t.amount,
                "status": t.status,
                "phone": t.momo_phone,
                "note": t.description,
                "date": t.timestamp,
            }
            for t in topups
        ]
    }