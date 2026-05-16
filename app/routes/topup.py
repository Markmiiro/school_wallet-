# ================================================
# routes/topup.py
# ------------------------------------------------
# Handles parent wallet top-ups via DGateway.
# DGateway supports MTN MoMo and Airtel Uganda.
#
# FLOW:
# 1. Parent sends amount + phone + network + wallet_id
# 2. Server validates everything
# 3. Server calls DGateway to charge parent phone
# 4. DGateway sends USSD prompt to parent
# 5. Parent enters MoMo PIN to approve
# 6. Poll /topup/{reference} to check status
# 7. When completed → wallet is credited
# ================================================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime
import uuid

from app.database import get_db
from app.models import Transaction, Wallet, Student
from app.momo import charge_mobile_money, verify_transaction

router = APIRouter()


# ================================================
# SCHEMAS
# ================================================

class TopUpRequest(BaseModel):
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
        v = v.replace(" ", "").replace("+", "")
        if not v.startswith("256"):
            raise ValueError("Phone must start with 256. Example: 256771234567")
        if not v.isdigit():
            raise ValueError("Phone must contain digits only")
        if len(v) != 12:
            raise ValueError(f"Phone must be 12 digits. Got {len(v)}: {v}")
        return v

    @field_validator("network")
    def network_must_be_valid(cls, v):
        v = v.upper().strip()
        if v not in ["MTN", "AIRTEL"]:
            raise ValueError("Network must be MTN or AIRTEL")
        return v


class TopUpResponse(BaseModel):
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
@router.post("/", response_model=TopUpResponse)
async def initiate_topup(
    topup_data: TopUpRequest,
    db: Session = Depends(get_db)
):
    """
    Initiate a wallet top-up via MTN or Airtel Money.
    Parent receives a USSD prompt to approve with their PIN.
    Poll GET /topup/{reference_id} to check payment status.
    """

    # ── Check wallet exists ─────────────────────────────────
    wallet = db.query(Wallet).filter(
        Wallet.id == topup_data.wallet_id
    ).first()

    if not wallet:
        raise HTTPException(
            status_code=404,
            detail=f"Wallet not found: {topup_data.wallet_id}"
        )

    if not wallet.is_active:
        raise HTTPException(
            status_code=403,
            detail="This wallet is deactivated. Contact school admin."
        )

    # ── Get student name ────────────────────────────────────
    student = db.query(Student).filter(
        Student.id == wallet.student_id
    ).first()
    student_name = student.name if student else "Student"

    # ── Generate internal reference ID ─────────────────────
    our_ref = str(uuid.uuid4())

    # ── Call DGateway ───────────────────────────────────────
    # Do this BEFORE saving to DB so we get DGateway's reference
    try:
        dg_response = await charge_mobile_money(
            phone=topup_data.phone_number,
            amount=topup_data.amount,
            network=topup_data.network,
            tx_ref=our_ref,
            customer_name=f"Parent of {student_name}",
        )

        # Extract DGateway's reference from response
        # In test mode this returns our_ref
        # In production DGateway returns their own reference
        dg_data = dg_response.get("data", {})
        dg_reference = dg_data.get("reference", our_ref)

        print(f"\n💳 Top-up initiated:")
        print(f"   Student:      {student_name}")
        print(f"   Amount:       UGX {topup_data.amount:,}")
        print(f"   Phone:        {topup_data.phone_number}")
        print(f"   DG Reference: {dg_reference}")

    except Exception as e:
        print(f"   ❌ DGateway error: {str(e)}")
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach payment provider. Try again. Error: {str(e)}"
        )

    # ── Save PENDING transaction with DGateway's reference ──
    # We save AFTER getting DGateway's reference
    # so webhook can match by reference
    txn = Transaction(
        wallet_id=topup_data.wallet_id,
        amount=topup_data.amount,
        type="topup",
        status="pending",
        reference=dg_reference,      # ← DGateway's reference
        momo_phone=topup_data.phone_number,
        description=topup_data.note or f"Top-up for {student_name}",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    return TopUpResponse(
        reference_id=dg_reference,
        wallet_id=txn.wallet_id,
        amount=txn.amount,
        status="pending",
        phone_number=topup_data.phone_number,
        network=topup_data.network,
        message=(
            f"Payment request sent to {topup_data.phone_number}. "
            f"Enter your {topup_data.network} PIN to approve. "
            f"Wallet updates automatically when approved."
        ),
        created_at=txn.timestamp,
    )


# ================================================
# ENDPOINT 2 — Check status of a top-up
# ================================================
@router.get("/{reference_id}")
async def check_topup_status(
    reference_id: str,
    db: Session = Depends(get_db)
):
    """
    Check the current status of a top-up.
    Also polls DGateway directly for latest status.

    Returns:
    - pending   → parent has not approved yet
    - completed → wallet has been credited
    - failed    → rejected or timed out
    """
    # Check our database first
    txn = db.query(Transaction).filter(
        Transaction.reference == reference_id
    ).first()

    if not txn:
        raise HTTPException(
            status_code=404,
            detail=f"No transaction found: {reference_id}"
        )

    # If still pending — check DGateway for latest status
    if txn.status == "pending":
        try:
            dg_status = await verify_transaction(reference_id)
            latest_status = dg_status.get("data", {}).get("status", "pending")

            # DGateway uses "completed" — update our DB if changed
            if latest_status == "completed" and txn.status != "completed":
                from app.models import Wallet as WalletModel
                wallet = db.query(WalletModel).filter(
                    WalletModel.id == txn.wallet_id
                ).first()
                if wallet:
                    wallet.balance += txn.amount
                txn.status = "completed"
                db.commit()
                print(f"✅ Top-up confirmed via polling: {reference_id}")

            elif latest_status == "failed" and txn.status != "failed":
                txn.status = "failed"
                db.commit()

        except Exception as e:
            print(f"⚠️  Could not poll DGateway: {e}")

    status_messages = {
        "pending":   "Waiting for parent to approve on their phone",
        "completed": "Payment approved — wallet has been credited ✅",
        "failed":    "Payment failed or was rejected",
    }

    return {
        "reference_id": txn.reference,
        "amount":       txn.amount,
        "status":       txn.status,
        "message":      status_messages.get(txn.status, "Unknown status"),
        "wallet_id":    txn.wallet_id,
        "phone":        txn.momo_phone,
        "date":         txn.timestamp,
    }


# ================================================
# ENDPOINT 3 — Top-up history for a wallet
# ================================================
@router.get("/history/{wallet_id}")
def get_topup_history(
    wallet_id: int,
    limit: int = 10,
    db: Session = Depends(get_db)
):
    """Get all top-ups for a student's wallet. Newest first."""
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail=f"Wallet not found: {wallet_id}")

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

    total_credited = sum(t.amount for t in topups if t.status == "completed")

    return {
        "wallet_id":            wallet_id,
        "current_balance_ugx":  wallet.balance,
        "total_topped_up_ugx":  total_credited,
        "number_of_topups":     len(topups),
        "topups": [
            {
                "reference_id": t.reference,
                "amount_ugx":   t.amount,
                "status":       t.status,
                "phone":        t.momo_phone,
                "note":         t.description,
                "date":         t.timestamp,
            }
            for t in topups
        ]
    }