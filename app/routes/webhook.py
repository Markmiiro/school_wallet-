# ================================================
# app/routes/webhook.py
# ------------------------------------------------
# Webhook callbacks from Yo Uganda.
#
# Per Yo! Payments API Specification v3.48:
#   - Successful deposits  -> POST /webhook/yo          (§6.3, "IPN")
#   - Failed deposits      -> POST /webhook/yo/failure   (§6.4)
#
# Both are FORM-ENCODED POSTs, and both include a signed field that
# MUST be verified using Yo Uganda's public certificate before any
# database write happens. Signature scheme: RSA + SHA1, base64.
#
# Three kinds of external_ref land here:
#   1. "USSD-TOPUP-{student_id}-{amount}-{uuid8}" -> credit existing
#      student's wallet. (Reference format per app/routes/ussd.py —
#      NOTE: this changed from the old "USSD-{id}-{amount}-{uuid8}"
#      format; both the prefix and this parser must stay in sync
#      with ussd.py's build_topup_reference().)
#   2. "USSD-REG-{uuid8}" -> look up the PendingUssdRegistration row,
#      create the real Student + Wallet + NFCTag (+ parent User if
#      needed), then delete the pending row.
#   3. A plain UUID -> regular /topup-initiated top-up, pre-created
#      Transaction row already exists.
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
from app.models import Transaction, Wallet, Student, User, School, NFCTag
from app.sms import sms_topup_confirmation
from app.account_number import generate_account_number
from app.routes.ussd import PendingUssdRegistration, REGISTRATION_FEE

router = APIRouter()

APP_ENV = os.getenv("APP_ENV", "development")
CERT_DIR = Path(__file__).resolve().parent.parent / "certs"

_public_key_cache = None


def _load_yo_public_key():
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


def _find_or_create_parent(db: Session, phone: str) -> User:
    """
    Look up a parent User by phone. If none exists, create one with
    role="parent" and no pin_hash yet — they'll need to set a PIN
    through the app before they can log in (out of scope here).
    """
    phone = (phone or "").strip().replace(" ", "").replace("+", "")
    parent = db.query(User).filter(User.phone == phone).first()
    if parent:
        return parent

    parent = User(
        name=f"Parent {phone}",  # placeholder; can be updated later in-app
        phone=phone,
        role="parent",
        pin_hash=None,
    )
    db.add(parent)
    db.flush()  # get parent.id without a full commit yet
    return parent


def _find_or_create_school(db: Session, school_name: str) -> School:
    """Case-insensitive match on school name; create if not found."""
    school_name = (school_name or "").strip()
    school = (
        db.query(School)
        .filter(School.name.ilike(school_name))
        .first()
    )
    if school:
        return school

    school = School(name=school_name, location=None)
    db.add(school)
    db.flush()
    return school


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

    concatenated = f"{date_time}{amount_str}{narrative}{network_ref}{external_ref}{msisdn}"

    if not verify_yo_signature(concatenated, signature):
        print(f"[Yo IPN] REJECTED — could not verify signature for external_ref={external_ref}")
        return {"message": "Signature verification failed"}

    try:
        amount = int(float(amount_str))
    except ValueError:
        return {"message": "Invalid amount"}

    tx_ref = external_ref

    # ---------------- USSD registration (new Smart Card) ----------------
    if tx_ref.startswith("USSD-REG-"):
        existing = db.query(Transaction).filter(Transaction.reference == tx_ref).first()
        if existing:
            return {"message": f"Registration already processed: {existing.status}"}

        pending = (
            db.query(PendingUssdRegistration)
            .filter(PendingUssdRegistration.reference == tx_ref)
            .first()
        )
        if not pending:
            print(f"[Yo IPN] No pending registration found for {tx_ref}")
            return {"message": "Pending registration not found"}

        if amount != REGISTRATION_FEE:
            print(f"[Yo IPN] Registration amount mismatch for {tx_ref}: got {amount}, expected {REGISTRATION_FEE}")
            return {"message": "Amount mismatch — not processed"}

        # 1. Parent — find or create by phone
        parent = _find_or_create_parent(db, pending.phone)

        # 2. School — find or create by name
        school = _find_or_create_school(db, pending.school_name)

        # 3. Student
        account_number = generate_account_number(db, school.id)
        student = Student(
            name=pending.student_name,
            school_id=school.id,
            parent_id=parent.id,
            account_number=account_number,
            dob=pending.dob,
            class_name=pending.class_name,
        )
        db.add(student)
        db.flush()  # get student.id

        # 4. Wallet
        wallet = Wallet(student_id=student.id, balance=0.0, is_active=True)
        db.add(wallet)
        db.flush()  # get wallet.id

        # 5. NFC card record (physical tag_uid assigned later when card is issued)
        nfc_tag = NFCTag(
            student_id=student.id,
            tag_uid=None,
            is_active=True,
            card_color=pending.card_color,
        )
        db.add(nfc_tag)

        # 6. Transaction record for the registration fee
        db.add(Transaction(
            wallet_id=wallet.id,
            amount=amount,
            type="registration",
            status="completed",
            reference=tx_ref,
            momo_phone=msisdn,
            description=f"Smart card registration — {pending.student_name}",
        ))

        # 7. Clean up the pending row
        db.delete(pending)
        db.commit()
        db.refresh(wallet)

        print(
            f"[Yo IPN] Registration complete: student={student.id} "
            f"account_number={account_number} card_color={pending.card_color}"
        )

        try:
            if parent.phone:
                await sms_topup_confirmation(
                    parent_phone=parent.phone,
                    student_name=student.name,
                    amount=0,
                    new_balance=wallet.balance,
                )
        except Exception as e:
            print(f"[Yo IPN] Registration SMS error (non-fatal): {e}")

        return {
            "message": "Registration completed successfully",
            "student_id": student.id,
            "account_number": account_number,
        }

    # ---------------- USSD top-up (existing student) ----------------
    if tx_ref.startswith("USSD-TOPUP-"):
        # Format: USSD-TOPUP-{student_id}-{amount}-{uuid8}
        remainder = tx_ref[len("USSD-TOPUP-"):]
        parts = remainder.split("-")
        if len(parts) < 3:
            print(f"[Yo IPN] Malformed USSD-TOPUP ref: {tx_ref}")
            return {"message": "Malformed USSD-TOPUP reference"}

        try:
            ussd_student_id = int(parts[0])
            ussd_amount = int(parts[1])
        except ValueError:
            print(f"[Yo IPN] Could not parse USSD-TOPUP ref parts: {tx_ref}")
            return {"message": "Could not parse USSD-TOPUP reference"}

        existing = db.query(Transaction).filter(Transaction.reference == tx_ref).first()
        if existing:
            return {"message": f"USSD top-up already processed: {existing.status}"}

        if amount != ussd_amount:
            print(f"[Yo IPN] Amount mismatch for {tx_ref}: IPN={amount} ref={ussd_amount}")
            return {"message": "Amount mismatch — not credited"}

        wallet = db.query(Wallet).filter(Wallet.student_id == ussd_student_id).first()
        if not wallet:
            print(f"[Yo IPN] USSD-TOPUP: wallet not found for student {ussd_student_id}")
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
@router.post("/yo/failure")
async def yo_uganda_failure_notification(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    failed_ref    = form.get("failed_transaction_reference", "")
    init_date     = form.get("transaction_init_date", "")
    verification  = form.get("verification", "")

    print(f"[Yo Failure] ref={failed_ref}")

    if not failed_ref or not verification:
        return {"message": "Missing required fields"}

    concatenated = f"{failed_ref}{init_date}"

    if not verify_yo_signature(concatenated, verification):
        print(f"[Yo Failure] REJECTED — could not verify signature for ref={failed_ref}")
        return {"message": "Signature verification failed"}

    # USSD registrations that fail just leave the pending row in place —
    # nothing to mark failed since no Transaction was ever created. It'll
    # sit unused unless/until a cleanup job is added (see follow-ups).
    if failed_ref.startswith("USSD-REG-"):
        print(f"[Yo Failure] Registration payment failed for {failed_ref} — pending row left as-is")
        return {"message": "Registration failure noted"}

    txn = db.query(Transaction).filter(Transaction.reference == failed_ref).first()
    if txn and txn.status == "pending":
        txn.status = "failed"
        db.commit()
        print(f"[Yo Failure] Marked {failed_ref} as failed")

    return {"message": "Failure notification processed"}