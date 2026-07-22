# ================================================
# app/routes/ussd.py
# ------------------------------------------------
# Yo Uganda Custom USSD Callout Handler
#
# REBUILT TO MATCH: "MIIRO TECHNOLOGIES USSD Menu Flow V2
# for *217*XXX#" — the document actually submitted to and
# stamped/approved for Yo Uganda (11/7/26).
#
# KEY CONTRACT FACTS FROM THE APPROVED DOC (do not "fix"
# these back to the old assumptions — they were wrong):
#
#   - The selector field is "product_key" ("1" = Send Upkeep
#     / top-up, "2" = Register for Smart Card), NOT
#     "main_menu_choice".
#   - The amount field is "amount", NOT "topup_amount".
#   - Cancel is displayed as "00. Cancel" in our message text,
#     but Yo's engine handles Confirm/Cancel NATIVELY once we
#     return ussd_processor_params + success/failure IPN URLs.
#     We are NEVER called again for the confirm/cancel tap
#     itself — the next thing we hear about the transaction is
#     the /webhook/yo IPN callback.
#   - Registration (product_key == "2") is NOT on hold. It's
#     fully specified: student_name -> dob -> class -> school
#     -> card_color (1=Blue, 2=Green, 3=Yellow, 4=Red), fixed
#     fee UGX 25,000, single callout once all 5 fields are
#     collected (the individual prompts are static/native on
#     Yo's side).
#
# CALLOUTS WE ACTUALLY RECEIVE (everything else is native/
# static on Yo's dashboard):
#
#   1. product_key=1, account_number only
#        -> look up student, return their details + "1. Top
#           up / 00. Cancel" menu as plain text.
#   2. product_key=1, account_number + amount
#        -> validate amount, return confirm message +
#           ussd_processor_params.payment_external_reference
#           + success/failure IPN URLs. (amount itself is
#           NOT included here — the user already typed it and
#           Yo's engine already knows it.)
#   3. product_key=2, student_name + dob + class + school +
#      card_color all present
#        -> persist a PendingUssdRegistration row, return
#           confirm message + ussd_processor_params (amount,
#           amount_formatted, payment_external_reference) +
#           success/failure IPN URLs.
#
# GIVE ALEX THIS URL: https://web-production-454a5.up.railway.app/ussd/yo
#
# TODO (backend, not covered in this file): webhook.py needs
# to handle a "USSD-REG-..." reference by looking up the
# PendingUssdRegistration row, creating the real Student +
# Wallet + Card records, and only then deleting the pending
# row. A "USSD-TOPUP-..." reference continues to just credit
# the existing student's wallet, as before.
# ================================================

import os
import re
import uuid
import base64
import hashlib
import logging
import urllib.parse
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime

from app.database import get_db, Base
from app.models import Student, Wallet

router = APIRouter()
logger = logging.getLogger(__name__)

APP_ENV      = os.getenv("APP_ENV", "development")
APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    "https://web-production-454a5.up.railway.app"
)

MIN_TOPUP = 1_000
MAX_TOPUP = 5_000_000
REGISTRATION_FEE = 25_000  # fixed price for a new Smart Card, per the approved doc

CARD_COLORS = {
    "1": "Blue",
    "2": "Green",
    "3": "Yellow",
    "4": "Red",
}

# ── TODO: move this into app/models.py once confirmed ───────────────
# Persists what the parent typed during USSD registration until the
# mobile money payment succeeds and webhook.py can create the real
# Student/Wallet/Card records. Keyed by the same payment_external_
# reference we hand to Yo, so the webhook can look it back up.
class PendingUssdRegistration(Base):
    __tablename__ = "pending_ussd_registrations"

    reference    = Column(String, primary_key=True)
    phone        = Column(String, nullable=False)
    student_name = Column(String, nullable=False)
    dob          = Column(String, nullable=False)
    class_name   = Column(String, nullable=False)
    school_name  = Column(String, nullable=False)
    card_color   = Column(String, nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)


# ── Yo Uganda public keys for signature verification ────────────────
YO_USSD_PUBLIC_KEY_TEST = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqSnqDR6VU2evVZL36S78
fYdQTarny91nMH2+sSV2n8+hQhZFrExkORVaITao2ogFtCvcnEfMBcAP64inazYo
+30k+pylvv2rszGBRKs/Z9Cgw2G8fLRPlaU0EWdDRigIHuvriYUajZ9XTVOtpoGd
sqT/CfGvSNQw2p6fu9t+n6Jny0Imj+vWZ3gwVkSl0Oma4O6pa7KlokUh2EfKFXRP
KrYGT5oEYQ9mBBdZZQfuBj7Au39p0ylGYUqzRqKXOrZJMHmNBPGpD4obEGwqUy6Y
9/6ghWnsWbn+9rKJRPHHO6PWO7ju/Szz4vogGiwgXOKbo+1xK0+IvwOnlqD9FIw8
oQIDAQAB
-----END PUBLIC KEY-----"""

YO_USSD_PUBLIC_KEY_PROD = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnJyVFThbsKzs4nTsJF/A
nNQMXuCby13HauEPYNE4mymVi9GJF7HdHqGnhnksxwaH9jkBDJveYA+SkMWRi9yS
DJMrwTOPaJYZPnhVBXMnm6aUHSFTeh+oRqMLgAP10vReV3o0ISUuFEzGHvU/r1+d
i9GQnysamCSzarbkkSKo+IO04tQLEaJrWwnoeu+C4Oo5mevfngjAn3zUavWR9jIi
3oq9d9OZbG3jsMueuRvc0q2MqnY7VuxMTZlq81DTsd0zEL3J1gdWfgUpoCIHAIuy
xffm3XUoyGgjdtcXeAG5TFpywUr/5yJVk8WBe066b/k1mmi5MaqkmKdSUgazQQJn
PQIDAQAB
-----END PUBLIC KEY-----"""


def verify_yo_signature(datetime_str: str, phone: str, signature_b64: str) -> bool:
    """SHA1 + RSA verification of Yo Uganda's request signature."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        concat   = datetime_str + phone
        sha1_hex = hashlib.sha1(concat.encode("utf-8")).hexdigest()
        signature = base64.b64decode(signature_b64)

        key_pem = (
            YO_USSD_PUBLIC_KEY_PROD if APP_ENV == "production"
            else YO_USSD_PUBLIC_KEY_TEST
        )
        public_key = serialization.load_pem_public_key(key_pem.encode("utf-8"))
        public_key.verify(
            signature, sha1_hex.encode("utf-8"),
            padding.PKCS1v15(), hashes.SHA1(),  # noqa: S303
        )
        return True
    except ImportError:
        logger.warning("[USSD] cryptography not installed — signature check skipped.")
        return True
    except Exception as e:
        logger.warning(f"[USSD] Signature verification failed: {e}")
        return False


def build_ipn_url(path: str = "/webhook/yo") -> str:
    return f"{APP_BASE_URL}{path}"


def build_topup_reference(student_id: int, amount: int) -> str:
    """USSD-TOPUP-{student_id}-{amount}-{uuid8} — parsed by webhook.py on IPN."""
    short_id = uuid.uuid4().hex[:8]
    return f"USSD-TOPUP-{student_id}-{amount}-{short_id}"


def build_registration_reference() -> str:
    """USSD-REG-{uuid8} — webhook.py looks this up in PendingUssdRegistration."""
    return f"USSD-REG-{uuid.uuid4().hex[:8]}"


def validate_amount(raw: str) -> Optional[int]:
    """
    Validate a free-typed top-up amount.
    Accepts digits only (parent may type "20000" or "20,000" — strip commas).
    """
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+", cleaned):
        return None
    amount = int(cleaned)
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        return None
    return amount


def find_student_by_account_number(db: Session, account_number: str):
    """
    Look up a student by their card/account number (e.g. "S123").
    Returns the Student, or None if not found / input is malformed.
    """
    if not account_number:
        return None
    cleaned = account_number.strip().replace(" ", "")
    return db.query(Student).filter(Student.account_number == cleaned).first()


@router.post("/yo")
async def yo_ussd_callout(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Yo Uganda Custom USSD callout endpoint.
    Give Alex this URL: {APP_BASE_URL}/ussd/yo
    Must respond within 5 seconds (Yo's USSD times out at 7s).
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[USSD] Could not parse JSON body: {e}")
        return {"validated": False, "message": "Invalid request format"}

    datetime_str = body.get("datetime", "")
    phone        = body.get("anumbermsisdn", "")
    signature    = body.get("signature", "")
    product_key  = body.get("product_key")

    # Top-up (Send Upkeep) fields
    account_number = body.get("account_number")
    amount_raw     = body.get("amount")

    # Registration (Smart Card) fields
    student_name = body.get("student_name")
    dob          = body.get("dob")
    class_name   = body.get("class")
    school_name  = body.get("school")
    card_color   = body.get("card_color")

    logger.info(
        f"[USSD] phone={phone} product_key={product_key} "
        f"account_number={account_number} amount={amount_raw} "
        f"student_name={student_name}"
    )

    if APP_ENV == "production" and signature:
        if not verify_yo_signature(datetime_str, phone, signature):
            logger.warning(f"[USSD] Signature verification FAILED for {phone}")
            return {"validated": False, "message": "Request verification failed"}

    # ──────────────────────────────────────────────
    # Main menu — likely configured as a static menu on
    # Yo's dashboard, but included here as a safe fallback.
    # ──────────────────────────────────────────────
    if not any([account_number, student_name]):
        return {
            "validated": True,
            "message": (
                "Welcome to School wallet\n"
                "1. Send Upkeep\n"
                "2. Register for Smart card"
            ),
        }

    # ──────────────────────────────────────────────
    # PRODUCT 2 — Register for Smart Card
    # Single callout, only fires once all 5 fields are
    # collected (the individual prompts are native/static
    # on Yo's side).
    # ──────────────────────────────────────────────
    if product_key == "2" and all([student_name, dob, class_name, school_name, card_color]):
        color_label = CARD_COLORS.get(card_color)
        if color_label is None:
            return {"validated": False, "message": "Invalid card color selected."}

        reference = build_registration_reference()

        pending = PendingUssdRegistration(
            reference=reference,
            phone=phone,
            student_name=student_name.strip(),
            dob=dob.strip(),
            class_name=class_name.strip(),
            school_name=school_name.strip(),
            card_color=color_label,
        )
        db.add(pending)
        db.commit()

        logger.info(f"[USSD] Pending registration created: {reference}")

        return {
            "validated": True,
            "message": (
                f"Confirm payment of UGX {REGISTRATION_FEE:,} to "
                f"Register {student_name} for a {color_label} NFC card\n"
                f"1. Confirm\n00. Cancel"
            ),
            "ussd_processor_params": {
                "amount": str(REGISTRATION_FEE),
                "amount_formatted": f"UGX {REGISTRATION_FEE:,}",
                "payment_external_reference": reference,
            },
            "success_ipn_url": build_ipn_url("/webhook/yo"),
            "failure_ipn_url": build_ipn_url("/webhook/yo"),
        }

    # ──────────────────────────────────────────────
    # PRODUCT 1, callout 2 — amount just typed. Validate
    # and hand off the confirm screen + payment params.
    # We are NOT called again for the confirm/cancel tap —
    # Yo's engine handles that natively from here.
    # ──────────────────────────────────────────────
    if product_key == "1" and account_number is not None and amount_raw is not None:
        amount = validate_amount(amount_raw)
        if amount is None:
            return {
                "validated": False,
                "message": f"Invalid amount. Must be between "
                            f"UGX {MIN_TOPUP:,} and UGX {MAX_TOPUP:,}.",
            }

        student = find_student_by_account_number(db, account_number)
        if not student:
            return {"validated": False, "message": "Student not found."}

        reference = build_topup_reference(student.id, amount)
        logger.info(f"[USSD] Top-up confirm: student {student.id} amount UGX {amount:,}")

        return {
            "validated": True,
            "message": (
                f"Confirm a top up of UGX {amount:,} for {student.name}\n"
                f"1. Confirm\n00. Cancel"
            ),
            "ussd_processor_params": {
                "payment_external_reference": reference,
            },
            "success_ipn_url": build_ipn_url("/webhook/yo"),
            "failure_ipn_url": build_ipn_url("/webhook/yo"),
        }

    # ──────────────────────────────────────────────
    # PRODUCT 1, callout 1 — account/card number just
    # entered. Identify the student, show their personalized
    # menu.
    # ──────────────────────────────────────────────
    if product_key == "1" and account_number is not None:
        student = find_student_by_account_number(db, account_number)
        if not student:
            return {
                "validated": False,
                "message": f"Account {account_number} not found. Please check with the school.",
            }

        wallet = db.query(Wallet).filter(Wallet.student_id == student.id).first()
        if not wallet or not wallet.is_active:
            return {"validated": False, "message": "This wallet is not active. Contact the school admin."}

        return {
            "validated": True,
            "message": (
                f"Student Name: {student.name}\n"
                f"Class: {getattr(student, 'class_name', 'N/A')}\n"
                f"School: {student.school.name}\n"
            ),
        }

    # Fallback — shouldn't normally be reached
    return {"validated": False, "message": "Session error. Please try again."}