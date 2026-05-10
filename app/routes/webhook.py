from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict

from app.database import get_db
from app.models import Transaction, Wallet

router = APIRouter()


# ── Schema — tells Swagger UI what body to expect ──
class WebhookPayload(BaseModel):
    event: str
    data: Dict[str, Any]


@router.post("/flutterwave")
def flutterwave_webhook(
    payload: WebhookPayload,
    db: Session = Depends(get_db)
):
    """
    Flutterwave calls this after every payment attempt.

    In test mode — call this manually to simulate approval.

    Send this body to simulate a successful payment:
    {
        "event": "charge.completed",
        "data": {
            "tx_ref": "your-reference-id-here",
            "status": "successful",
            "amount": 50000
        }
    }
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

    # Credit or fail the wallet
    if status == "successful":

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

        return {
            "message": "Wallet credited successfully ✅",
            "wallet_id": wallet.id,
            "amount_credited": txn.amount,
            "new_balance": wallet.balance,
            "currency": "UGX"
        }

    else:
        txn.status = "failed"
        db.commit()

        return {
            "message": "Payment failed ❌",
            "tx_ref": tx_ref,
            "note": "Wallet was not credited"
        }