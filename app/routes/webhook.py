from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict

from app.database import get_db
from app.models import Transaction, Wallet, Student, User
from app.sms import sms_topup_confirmation

router = APIRouter()


class WebhookPayload(BaseModel):
    event: str
    data: Dict[str, Any]


# ================================================
# POST /webhook/flutterwave
# ------------------------------------------------
# Works for both Flutterwave AND DGateway.
# We keep the same endpoint name so your
# existing tests still work.
#
# DGateway payload:
# {
#     "event": "charge.completed",
#     "data": {
#         "tx_ref": "your-reference-id",
#         "status": "completed",
#         "amount": 50000
#     }
# }
# ================================================
@router.post("/flutterwave")
def payment_webhook(
    payload: WebhookPayload,
    db: Session = Depends(get_db)
):
    """
    Receives payment confirmation from DGateway.
    In test mode — call this manually to simulate approval.
    """

    event = payload.event
    data  = payload.data

    print(f"\n📩 Webhook received: event={event}")

    # Only handle charge completed events
    if event != "charge.completed":
        return {"message": f"Event '{event}' ignored"}

    # Extract fields
    tx_ref = data.get("tx_ref")
    status = data.get("status")

    if not tx_ref:
        raise HTTPException(
            status_code=400,
            detail="Missing tx_ref in webhook data"
        )

    # Find the matching transaction
    txn = db.query(Transaction).filter(
        Transaction.reference == tx_ref
    ).first()

    if not txn:
        return {"message": f"No transaction found for reference: {tx_ref}"}

    # Idempotency — ignore if already processed
    if txn.status != "pending":
        return {"message": f"Already processed — status is {txn.status}"}

    # ── SUCCESSFUL PAYMENT ───────────────────────
    if status == "completed":

        # Find the wallet
        wallet = db.query(Wallet).filter(
            Wallet.id == txn.wallet_id
        ).first()

        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found")

        # Credit the wallet
        wallet.balance += txn.amount
        txn.status = "completed"
        db.commit()

        print(f"   ✅ Wallet credited UGX {txn.amount:,}")
        print(f"   New balance: UGX {wallet.balance:,}")

        # ── SEND TOP-UP CONFIRMATION SMS ─────────
        try:
            student = db.query(Student).filter(
                Student.id == wallet.student_id
            ).first()
            if student:
                parent = db.query(User).filter(
                    User.id == student.parent_id
                ).first()
                if parent:
                    sms_topup_confirmation(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        amount=txn.amount,
                        new_balance=wallet.balance,
                    )
        except Exception as e:
            print(f"⚠️  SMS notification failed: {e}")

        return {
            "message": "Wallet credited successfully ✅",
            "wallet_id": wallet.id,
            "amount_credited": txn.amount,
            "new_balance": wallet.balance,
            "currency": "UGX"
        }

    # ── FAILED PAYMENT ───────────────────────────
    else:
        txn.status = "failed"
        db.commit()

        print(f"   ❌ Payment failed: {status}")

        return {
            "message": "Payment failed ❌",
            "tx_ref": tx_ref,
            "note": "Wallet was not credited"
        }