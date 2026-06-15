from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any, Dict
import xml.etree.ElementTree as ET

from app.database import get_db
from app.models import Transaction, Wallet, Student, User
from app.sms import sms_topup_confirmation

router = APIRouter()


class WebhookPayload(BaseModel):
    event: str
    data: Dict[str, Any]


# ================================================
# POST /webhook/yo
# ------------------------------------------------
# Yo Uganda calls this URL (IPN) after a parent
# approves a top-up payment on their phone.
#
# Yo Uganda sends XML or form data with:
#   ExternalReference  → your tx_ref (our UUID)
#   TransactionStatus  → SUCCEEDED or FAILED
#   Amount             → amount paid in UGX
#
# This endpoint must return 200 quickly.
# Yo Uganda retries if it does not get 200.
# ================================================

@router.post("/yo")
async def yo_uganda_webhook(
    request: Request,
    db: Session = Depends(get_db)
):
    # ── Read raw body ─────────────────────────────
    body      = await request.body()
    body_text = body.decode("utf-8")

    print(f"\n[Yo Webhook] Received: {body_text[:200]}")

    tx_ref = None
    status = None
    amount = None

    # ── Try XML first ─────────────────────────────
    try:
        root = ET.fromstring(body_text)
        for child in root.iter():
            if child.tag == "ExternalReference":
                tx_ref = child.text
            if child.tag == "TransactionStatus":
                status = child.text
            if child.tag == "Amount":
                amount = child.text
    except Exception:
        # ── Fall back to form data ─────────────────
        try:
            form   = await request.form()
            tx_ref = form.get("ExternalReference") or form.get("tx_ref")
            status = form.get("TransactionStatus") or form.get("status")
            amount = form.get("Amount") or form.get("amount")
        except Exception as e:
            print(f"[Yo Webhook] Could not parse body: {e}")
            return {"message": "Could not parse request body"}

    print(f"[Yo Webhook] Ref={tx_ref} Status={status} Amount={amount}")

    # ── Validate reference ────────────────────────
    if not tx_ref:
        return {"message": "No transaction reference found"}

    # ── Find transaction ──────────────────────────
    txn = db.query(Transaction).filter(
        Transaction.reference == tx_ref
    ).first()

    if not txn:
        # Return 200 so Yo Uganda stops retrying
        return {"message": f"Transaction not found: {tx_ref}"}

    # ── Idempotency — never process twice ─────────
    if txn.status != "pending":
        return {"message": f"Already processed: {txn.status}"}

    # ── SUCCEEDED → credit wallet ─────────────────
    if status == "SUCCEEDED":
        wallet = db.query(Wallet).filter(
            Wallet.id == txn.wallet_id
        ).first()

        if not wallet:
            return {"message": "Wallet not found"}

        wallet.balance += txn.amount
        txn.status      = "completed"
        db.commit()

        print(f"[Yo Webhook] ✅ Wallet {wallet.id} credited UGX {txn.amount:,}")

        # ── Send SMS to parent ─────────────────────
        try:
            student = db.query(Student).filter(
                Student.id == wallet.student_id
            ).first()
            if student:
                parent = db.query(User).filter(
                    User.id == student.parent_id
                ).first()
                if parent and parent.phone:
                    sms_topup_confirmation(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        amount=txn.amount,
                        new_balance=wallet.balance,
                    )
        except Exception as e:
            # SMS failure must never block wallet credit
            print(f"[Yo Webhook] SMS error (non-fatal): {e}")

        return {"message": "Wallet credited successfully"}

    # ── FAILED → mark failed ──────────────────────
    else:
        txn.status = "failed"
        db.commit()
        print(f"[Yo Webhook] ❌ Payment failed: {status} — Ref: {tx_ref}")
        return {"message": f"Payment failed: {status}"}