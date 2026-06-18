# ================================================
# app/routes/webhook.py
# ------------------------------------------------
# Webhook callbacks from Yo Uganda (IPN).
#
# POST /webhook/yo
#   Yo Uganda calls this after a parent approves
#   a top-up payment on their phone — whether
#   initiated via /topup (API) or via USSD.
#
#   Yo Uganda sends XML or form data with:
#     ExternalReference  -> our tx_ref (UUID or USSD ref)
#     TransactionStatus  -> SUCCEEDED or FAILED  (uppercase)
#     Amount             -> amount paid in UGX
#
#   TWO reference formats handled here:
#   1. UUID (e.g. "a3f9c1d8-...")
#      -> Regular top-up from /topup endpoint
#      -> Pre-created Transaction row exists in DB
#
#   2. USSD-{student_id}-{amount}-{uuid8} (e.g. "USSD-42-20000-a3f9c1d8")
#      -> USSD-initiated top-up from routes/ussd.py
#      -> No pre-created Transaction row — we create it here
#
#   This endpoint must return 200 quickly.
#   Yo Uganda retries if it does not get 200.
# ================================================

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

    # ──────────────────────────────────────────────
    # USSD-INITIATED TOP-UP BRANCH
    # Reference format: USSD-{student_id}-{amount}-{uuid8}
    # No pre-created Transaction exists — we create it here on success.
    # ──────────────────────────────────────────────
    if tx_ref.startswith("USSD-"):
        parts = tx_ref.split("-")
        # Expected: ["USSD", student_id, amount, uuid8]
        if len(parts) < 4:
            print(f"[Yo Webhook] Malformed USSD ref: {tx_ref}")
            return {"message": "Malformed USSD reference"}

        try:
            ussd_student_id = int(parts[1])
            ussd_amount     = int(parts[2])
        except ValueError:
            print(f"[Yo Webhook] Could not parse USSD ref parts: {tx_ref}")
            return {"message": "Could not parse USSD reference"}

        # Idempotency: check if this USSD ref was already processed
        existing = db.query(Transaction).filter(
            Transaction.reference == tx_ref
        ).first()
        if existing:
            return {"message": f"USSD payment already processed: {existing.status}"}

        if status != "SUCCEEDED":
            print(f"[Yo Webhook] USSD payment failed: {status} — Ref: {tx_ref}")
            return {"message": f"USSD payment not completed: {status}"}

        # Credit the wallet
        wallet = (
            db.query(Wallet)
            .filter(Wallet.student_id == ussd_student_id)
            .first()
        )
        if not wallet:
            print(f"[Yo Webhook] USSD: wallet not found for student {ussd_student_id}")
            return {"message": "Wallet not found"}

        wallet.balance += ussd_amount

        # Create the transaction record (didn't exist before payment confirmed)
        ussd_txn = Transaction(
            wallet_id=wallet.id,
            amount=ussd_amount,
            type="topup",
            status="completed",
            reference=tx_ref,
            momo_phone="",
            description="USSD top-up via School Wallet",
        )
        db.add(ussd_txn)
        db.commit()

        print(
            f"[Yo Webhook] USSD top-up: "
            f"student {ussd_student_id} credited UGX {ussd_amount:,}"
        )

        # Send SMS to parent
        try:
            student = db.query(Student).filter(
                Student.id == ussd_student_id
            ).first()
            if student:
                parent = db.query(User).filter(
                    User.id == student.parent_id
                ).first()
                if parent and parent.phone:
                    await sms_topup_confirmation(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        amount=ussd_amount,
                        new_balance=wallet.balance,
                    )
        except Exception as e:
            print(f"[Yo Webhook] USSD SMS error (non-fatal): {e}")

        return {"message": "USSD top-up credited successfully"}

    # ──────────────────────────────────────────────
    # REGULAR TOP-UP BRANCH (UUID reference from /topup endpoint)
    # ──────────────────────────────────────────────

    # ── Find pre-created transaction ──────────────
    txn = db.query(Transaction).filter(
        Transaction.reference == tx_ref
    ).first()

    if not txn:
        # Return 200 so Yo Uganda stops retrying
        return {"message": f"Transaction not found: {tx_ref}"}

    # ── Idempotency — never process twice ─────────
    if txn.status != "pending":
        return {"message": f"Already processed: {txn.status}"}

    # ── SUCCEEDED -> credit wallet ─────────────────
    if status == "SUCCEEDED":
        wallet = db.query(Wallet).filter(
            Wallet.id == txn.wallet_id
        ).first()

        if not wallet:
            return {"message": "Wallet not found"}

        wallet.balance += txn.amount
        txn.status      = "completed"
        db.commit()

        print(f"[Yo Webhook] Wallet {wallet.id} credited UGX {txn.amount:,}")

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
                    await sms_topup_confirmation(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        amount=txn.amount,
                        new_balance=wallet.balance,
                    )
        except Exception as e:
            # SMS failure must never block wallet credit
            print(f"[Yo Webhook] SMS error (non-fatal): {e}")

        return {"message": "Wallet credited successfully"}

    # ── FAILED -> mark failed ──────────────────────
    else:
        txn.status = "failed"
        db.commit()
        print(f"[Yo Webhook] Payment failed: {status} — Ref: {tx_ref}")
        return {"message": f"Payment failed: {status}"}