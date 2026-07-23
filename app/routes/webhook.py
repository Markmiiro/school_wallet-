# ================================================
# app/routes/webhook.py
# ------------------------------------------------
# Webhook callbacks from Yo Uganda.
#
# Per Yo! Payments API Specification v3.48:
#   - Successful deposits  -> POST /webhook/yo          (§6.3, "IPN")
#   - Failed deposits      -> POST /webhook/yo/failure   (§6.4)
#
# Both are FORM-ENCODED POSTs (not JSON, not XML), and both include a
# signed field that MUST be verified using Yo Uganda's public certificate
# before any database write happens. Signature scheme: RSA + SHA1,
# base64-encoded. Never trust an unverified request.
# ================================================

import base64
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.database import get_db
from app.models import Transaction, Wallet, Student, User
from app.sms import sms_topup_confirmation

router = APIRouter()

APP_ENV = os.getenv("APP_ENV", "development")
CERT_DIR = Path(__file__).resolve().parent.parent / "certs"

_public_key_cache = None


def _load_yo_public_key():
    """
    Loads Yo Uganda's public certificate for signature verification.
    Uses the SANDBOX cert unless APP_ENV=production.
    """
    cert_file = (
        "Yo_Uganda_Public_Certificate.crt"
        if APP_ENV == "production"
        else "Yo_Uganda_Public_Sandbox_Certificate.crt"
    )
    cert_path = CERT_DIR / cert_file
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return cert.public_key()


def get_yo_public_key():
    global _public_key_cache
    if _public_key_cache is None:
        _public_key_cache = _load_yo_public_key()
    return _public_key_cache


def verify_yo_signature(concatenated: str, signature_b64: str) -> bool:
    """
    Verifies an RSA-SHA1 signature from Yo Uganda.
    `concatenated` must be built in the exact field order Yo Uganda specifies
    (see API Spec §6.3.4 for IPN, §6.4.3 for failure notifications).
    """
    try:
        signature = base64.b64decode(signature_b64)
        public_key = get_yo_public_key()
        public_key.verify(
            signature,
            concatenated.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
        return True
    except (InvalidSignature, ValueError, Exception) as e:
        print(f"[Yo Webhook] Signature verification FAILED: {e}")
        return False


# ================================================
# SUCCESSFUL PAYMENT — Instant Payment Notification (§6.3)
# ================================================
@router.post("/yo")
async def yo_uganda_ipn(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    date_time    = form.get("date_time", "")
    amount_str   = form.get("amount", "")
    narrative    = form.get("narrative", "")
    network_ref  = form.get("network_ref", "")
    external_ref = form.get("external_ref", "")
    msisdn       = form.get("msisdn", "")
    signature    = form.get("signature", "")

    print(f"[Yo IPN] external_ref={external_ref} amount={amount_str} msisdn={msisdn}")

    if not external_ref or not signature:
        return {"message": "Missing required fields"}

    # Signature covers, in order: date_time, amount, narrative, network_ref,
    # external_ref, msisdn (concatenated with no separators — per §6.3.3)
    concatenated = f"{date_time}{amount_str}{narrative}{network_ref}{external_ref}{msisdn}"

    if not verify_yo_signature(concatenated, signature):
        print(f"[Yo IPN] REJECTED — could not verify signature for external_ref={external_ref}")
        # Return 200 so Yo doesn't retry a request we've already rejected,
        # but credit NOTHING.
        return {"message": "Signature verification failed"}

    try:
        amount = int(float(amount_str))
    except ValueError:
        return {"message": "Invalid amount"}

    tx_ref = external_ref

    # ---------------- USSD-initiated top-up ----------------
    if tx_ref.startswith("USSD-"):
        parts = tx_ref.split("-")
        if len(parts) < 4:
            print(f"[Yo IPN] Malformed USSD ref: {tx_ref}")
            return {"message": "Malformed USSD reference"}

        try:
            ussd_student_id = int(parts[1])
            ussd_amount = int(parts[2])
        except ValueError:
            print(f"[Yo IPN] Could not parse USSD ref parts: {tx_ref}")
            return {"message": "Could not parse USSD reference"}

        existing = db.query(Transaction).filter(Transaction.reference == tx_ref).first()
        if existing:
            return {"message": f"USSD payment already processed: {existing.status}"}

        # Cross-check the VERIFIED amount from Yo against what the reference
        # string claims. A verified signature proves Yo really sent this
        # notification, but this guards against the amount portion of the
        # reference being tampered with before it ever reached Yo.
        if amount != ussd_amount:
            print(
                f"[Yo IPN] Amount mismatch for {tx_ref}: "
                f"IPN says {amount}, reference says {ussd_amount}"
            )
            return {"message": "Amount mismatch — not credited"}

        wallet = db.query(Wallet).filter(Wallet.student_id == ussd_student_id).first()
        if not wallet:
            print(f"[Yo IPN] USSD: wallet not found for student {ussd_student_id}")
            return {"message": "Wallet not found"}

        wallet.balance += ussd_amount
        db.add(Transaction(
            wallet_id=wallet.id,
            amount=ussd_amount,
            type="topup",
            status="completed",
            reference=tx_ref,
            momo_phone=msisdn,
            description="USSD top-up via School Wallet",
        ))
        db.commit()

        print(f"[Yo IPN] USSD top-up: student {ussd_student_id} credited UGX {ussd_amount:,}")

        try:
            student = db.query(Student).filter(Student.id == ussd_student_id).first()
            if student:
                parent = db.query(User).filter(User.id == student.parent_id).first()
                if parent and parent.phone:
                    await sms_topup_confirmation(
                        parent_phone=parent.phone,
                        student_name=student.name,
                        amount=ussd_amount,
                        new_balance=wallet.balance,
                    )
        except Exception as e:
            print(f"[Yo IPN] USSD SMS error (non-fatal): {e}")

        return {"message": "USSD top-up credited successfully"}

    # ---------------- Regular top-up (pre-created Transaction) ----------------
    txn = db.query(Transaction).filter(Transaction.reference == tx_ref).first()
    if not txn:
        return {"message": f"Transaction not found: {tx_ref}"}

    if txn.status != "pending":
        return {"message": f"Already processed: {txn.status}"}

    wallet = db.query(Wallet).filter(Wallet.id == txn.wallet_id).first()
    if not wallet:
        return {"message": "Wallet not found"}

    wallet.balance += txn.amount
    txn.status = "completed"
    db.commit()

    print(f"[Yo IPN] Wallet {wallet.id} credited UGX {txn.amount:,}")

    try:
        student = db.query(Student).filter(Student.id == wallet.student_id).first()
        if student:
            parent = db.query(User).filter(User.id == student.parent_id).first()
            if parent and parent.phone:
                await sms_topup_confirmation(
                    parent_phone=parent.phone,
                    student_name=student.name,
                    amount=txn.amount,
                    new_balance=wallet.balance,
                )
    except Exception as e:
        print(f"[Yo IPN] SMS error (non-fatal): {e}")

    return {"message": "Wallet credited successfully"}


# ================================================
# FAILED PAYMENT — Transaction Failure Notification (§6.4)
# ================================================
# NOTE: this requires momo.py to also send a <FailureNotificationUrl>
# pointing here when initiating deposits — currently it doesn't (see below).
@router.post("/yo/failure")
async def yo_uganda_failure_notification(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    failed_ref    = form.get("failed_transaction_reference", "")
    init_date     = form.get("transaction_init_date", "")
    verification  = form.get("verification", "")

    print(f"[Yo Failure] ref={failed_ref}")

    if not failed_ref or not verification:
        return {"message": "Missing required fields"}

    # Signature covers, in order: failed_transaction_reference, transaction_init_date
    concatenated = f"{failed_ref}{init_date}"

    if not verify_yo_signature(concatenated, verification):
        print(f"[Yo Failure] REJECTED — could not verify signature for ref={failed_ref}")
        return {"message": "Signature verification failed"}

    txn = db.query(Transaction).filter(Transaction.reference == failed_ref).first()
    if txn and txn.status == "pending":
        txn.status = "failed"
        db.commit()
        print(f"[Yo Failure] Marked {failed_ref} as failed")

    return {"message": "Failure notification processed"}