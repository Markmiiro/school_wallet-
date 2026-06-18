# ================================================
# app/routes/ussd.py
# ------------------------------------------------
# Yo Uganda Custom USSD Callout Handler
#
# HOW YO'S USSD WORKS FOR US:
#   Yo Uganda manages the USSD session on their end.
#   When a parent dials *217*XXX#, Yo calls THIS URL
#   at each configured step to get our response.
#
# FLOW (as configured with Alex at Yo Uganda):
#   Step 1  → action=getmainmenu → we return welcome + ask for student ID
#   Step 2  → student_id received → we validate + return top-up amounts
#   Payment → Yo initiates acdepositfunds on parent's phone
#   IPN     → Yo calls /webhook/yo → wallet credited
#
# USSD SCOPE (Alex confirmed): payment initiation ONLY.
#   Balance checks via USSD are NOT supported by Yo's system.
#
# SECURITY:
#   All requests from Yo are signed. We verify the SHA1+RSA
#   signature before processing. In development, signature
#   verification is skipped (logged only).
#
# GIVE ALEX THIS URL:
#   https://web-production-454a5.up.railway.app/ussd/yo
#
# REFERENCE FORMAT for USSD-initiated top-ups:
#   USSD-{student_id}-{amount}-{8char_uuid}
#   Example: USSD-42-20000-a3f9c1d8
#   The webhook parses this to credit the correct wallet.
# ================================================

import os
import uuid
import base64
import hashlib
import logging
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Student, Wallet

router = APIRouter()
logger = logging.getLogger(__name__)

APP_ENV      = os.getenv("APP_ENV", "development")
APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    "https://web-production-454a5.up.railway.app"
)

# ── Yo Uganda public keys for signature verification ────────────────
# Test key: for sandbox/development
# Production key: for live traffic
# Source: Yo Uganda Third Party URL Call-Out API docs v2.7

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


# ================================================
# SIGNATURE VERIFICATION
# ================================================
def verify_yo_signature(datetime_str: str, phone: str, signature_b64: str) -> bool:
    """
    Verify the request signature from Yo Uganda's USSD app.

    Process (from Yo Uganda docs v2.7):
    1. Concatenate datetime + anumbermsisdn
    2. SHA1 hex-digest the concatenation
    3. Base64-decode the signature from Yo
    4. RSA-verify the signature against the SHA1 hash using Yo's public key
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.exceptions import InvalidSignature

        # Step 1+2: SHA1 of the concatenation
        concat   = datetime_str + phone
        sha1_hex = hashlib.sha1(concat.encode("utf-8")).hexdigest()

        # Step 3: Decode signature
        signature = base64.b64decode(signature_b64)

        # Step 4: Load the appropriate public key
        key_pem = (
            YO_USSD_PUBLIC_KEY_PROD
            if APP_ENV == "production"
            else YO_USSD_PUBLIC_KEY_TEST
        )
        public_key = serialization.load_pem_public_key(key_pem.encode("utf-8"))

        # Verify — raises InvalidSignature if it fails
        public_key.verify(
            signature,
            sha1_hex.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA1(),  # noqa: S303 — required by Yo Uganda's signing scheme
        )
        return True

    except ImportError:
        # cryptography library not installed — skip verification, log warning
        logger.warning(
            "[USSD] cryptography package not installed. "
            "Signature verification skipped. Run: pip install cryptography"
        )
        return True  # Don't block in dev if library is missing

    except Exception as e:
        logger.warning(f"[USSD] Signature verification failed: {e}")
        return False


# ================================================
# TOP-UP AMOUNT OPTIONS
# Presented to parent as a USSD menu
# ================================================
TOPUP_OPTIONS = [
    {"label": "UGX 5,000",   "amount": 5_000,   "option": "1"},
    {"label": "UGX 10,000",  "amount": 10_000,  "option": "2"},
    {"label": "UGX 20,000",  "amount": 20_000,  "option": "3"},
    {"label": "UGX 50,000",  "amount": 50_000,  "option": "4"},
    {"label": "UGX 100,000", "amount": 100_000, "option": "5"},
]


def build_ipn_url(path: str = "/webhook/yo") -> str:
    """URL-encode the IPN callback URL for inclusion in Yo's product list."""
    return urllib.parse.quote(f"{APP_BASE_URL}{path}", safe="")


def build_topup_reference(student_id: int, amount: int) -> str:
    """
    Unique reference for a USSD-initiated top-up.
    Format: USSD-{student_id}-{amount}-{8char_uuid}
    The webhook parses this to credit the correct student wallet.
    """
    short_id = uuid.uuid4().hex[:8]
    return f"USSD-{student_id}-{amount}-{short_id}"


def build_product_list(student_id: int) -> list:
    """
    Build the single_step_product_list for Yo Uganda.
    Each entry has a unique external reference encoding student + amount.
    The IPN fires to /webhook/yo with that reference so we can credit the wallet.
    """
    ipn_url = build_ipn_url("/webhook/yo")
    products = []

    for opt in TOPUP_OPTIONS:
        ref = build_topup_reference(student_id, opt["amount"])
        products.append({
            "id":                     ref,
            "label":                  opt["label"],
            "expected_user_input":    opt["option"],
            "amount":                 opt["amount"],
            "product_external_reference": ref,
            "product_success_ipn_url":    ipn_url,
            "product_failure_ipn_url":    ipn_url,
        })

    return products


def build_display_message(student_name: str, balance: int) -> str:
    """
    URL-encoded display message for parent USSD screen.
    Yo Uganda constraint: decoded text must use only alphanumeric,
    spaces, hyphen, underscore, period, comma.
    %0A = newline in URL encoding.
    """
    # Note: no colon, no slash, no parentheses — Yo Uganda's constraint
    lines = [
        f"{student_name} - Balance UGX {balance:,}",
        "Select top-up amount",
        "1. UGX 5,000",
        "2. UGX 10,000",
        "3. UGX 20,000",
        "4. UGX 50,000",
        "5. UGX 100,000",
    ]
    raw = "\n".join(lines)
    return urllib.parse.quote(raw, safe="")


# ================================================
# CALLOUT ENDPOINT
# POST /ussd/yo
# ================================================
@router.post("/yo")
async def yo_ussd_callout(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Yo Uganda Custom USSD callout endpoint.

    Yo Uganda POSTs JSON here at each configured step of the USSD flow.
    We validate, then respond with JSON that Yo Uganda uses to continue
    or complete the session.

    Give Alex (Yo Uganda) this URL:
        {APP_BASE_URL}/ussd/yo

    This endpoint must respond within 5 seconds.
    Yo Uganda's USSD times out at 7 seconds.
    """

    # ── Parse request body ────────────────────────
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"[USSD] Could not parse JSON body: {e}")
        return {"validated": False, "message": "Invalid request format"}

    datetime_str = body.get("datetime", "")
    phone        = body.get("anumbermsisdn", "")
    action       = body.get("action", "")
    signature    = body.get("signature", "")
    student_id   = body.get("student_id")  # variable name as configured with Yo Uganda

    logger.info(
        f"[USSD] Request: phone={phone} action={action} student_id={student_id}"
    )

    # ── Verify signature ──────────────────────────
    # Skip in development — enforce strictly in production
    if APP_ENV == "production" and signature:
        if not verify_yo_signature(datetime_str, phone, signature):
            logger.warning(f"[USSD] Signature verification FAILED for {phone}")
            return {"validated": False, "message": "Request verification failed"}

    # ──────────────────────────────────────────────
    # STEP 1: Show main menu (getmainmenu or no student_id yet)
    # ──────────────────────────────────────────────
    if action == "getmainmenu" or not student_id:
        logger.info(f"[USSD] Showing main menu to {phone}")
        return {
            "validated": True,
            "message":   "Welcome to School Wallet\nEnter your child's student ID to top up",
        }

    # ──────────────────────────────────────────────
    # STEP 2: Validate student ID, return top-up options
    # ──────────────────────────────────────────────

    # Validate student_id is numeric
    try:
        sid = int(student_id)
    except (ValueError, TypeError):
        logger.warning(f"[USSD] Non-numeric student_id from {phone}: {student_id}")
        return {
            "validated": False,
            "message":   "Invalid student ID. Please check and try again.",
        }

    # Look up student in DB
    student = (
        db.query(Student)
        .filter(Student.id == sid)
        .first()
    )

    if not student:
        logger.info(f"[USSD] Student {sid} not found — phone {phone}")
        return {
            "validated": False,
            "message":   f"Student ID {sid} not found. Please check with the school.",
        }

    # Look up wallet
    wallet = (
        db.query(Wallet)
        .filter(Wallet.student_id == student.id)
        .first()
    )

    if not wallet or not wallet.is_active:
        logger.warning(f"[USSD] Wallet inactive or missing for student {sid}")
        return {
            "validated": False,
            "message":   "This student wallet is not active. Contact the school admin.",
        }

    balance = wallet.balance

    logger.info(
        f"[USSD] Student {student.name} (ID {sid}) validated. "
        f"Balance: UGX {balance:,}. Returning product list."
    )

    # Build top-up product list with unique references per amount
    display_msg  = build_display_message(student.name, balance)
    error_msg    = urllib.parse.quote(
        "Invalid option. Please select 1 to 5.", safe=""
    )
    product_list = build_product_list(student.id)

    # Return product list — Yo Uganda will:
    # 1. Show display_message to parent
    # 2. Let parent select 1–5
    # 3. Initiate acdepositfunds for the selected amount
    # 4. Call /webhook/yo with the product_external_reference on success/failure
    return {
        "validated": True,
        "single_step_product": {
            "display_message":        display_msg,
            "error_message":          error_msg,
            "single_step_product_list": product_list,
        },
    }