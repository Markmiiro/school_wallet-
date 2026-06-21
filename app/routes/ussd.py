# ================================================
# app/routes/ussd.py
# ------------------------------------------------
# Yo Uganda Custom USSD Callout Handler
#
# FINAL MENU FLOW (agreed with Mark, reviewed against
# Yo Uganda's actual production menus for reference):
#
#   SCREEN 0 — Main menu
#     Welcome to School Wallet
#     1. Enter card or account number
#     2. Register New Student
#     0. Exit
#
#   SCREEN 1 — (if 1) Identify student
#     Enter card or account number:
#
#   SCREEN 2 — Personalized menu
#     {Student Name} - {School}
#     1. Top Up Wallet
#     2. Exit
#
#   SCREEN 3 — (if 1) Amount entry — FREE TEXT, not a fixed list
#     Enter amount to top up:
#     (validated: UGX 1,000 - 5,000,000)
#
#   SCREEN 4 — Confirmation
#     Confirm: Top up {Name}'s wallet by UGX {amount}?
#     1. Confirm
#     2. Cancel
#
#   SCREEN 5 — Result (Yo Uganda shows this after IPN)
#
#   Registration (Option 2 at Screen 0) — ON HOLD.
#   NFC stock-pool design isn't finalized yet, so this
#   currently returns a "coming soon" message rather than
#   collecting name/DOB/card color. Revisit once the NFC
#   stock-pool decision (see app/nfc.py, currently unused
#   in production) is made.
#
# IMPORTANT — FIELD NAMES TO GIVE ALEX:
# Yo Uganda's dashboard lets their staff name the variables
# however they configure each step. Tell Alex our server
# expects these exact field names in the JSON body at each
# custom step:
#
#   account_number   → what the parent types at Screen 1
#   topup_amount     → what the parent types at Screen 3
#   confirm_choice   → "1" or "2" from Screen 4
#
# Static text-only menus (Screen 0, Screen 1's prompt text,
# Screen 3's prompt text) likely don't need to call our
# server at all — Alex may be able to configure those as
# plain static menus on Yo's side. Confirm with him which
# steps actually route to our URL vs. which are native.
#
# GIVE ALEX THIS URL: https://web-production-454a5.up.railway.app/ussd/yo
# ================================================

import os
import re
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

MIN_TOPUP = 1_000
MAX_TOPUP = 5_000_000

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
    return urllib.parse.quote(f"{APP_BASE_URL}{path}", safe="")


def build_topup_reference(student_id: int, amount: int) -> str:
    """USSD-{student_id}-{amount}-{uuid8} — parsed by webhook.py on IPN."""
    short_id = uuid.uuid4().hex[:8]
    return f"USSD-{student_id}-{amount}-{short_id}"


def validate_amount(raw: str) -> Optional[int]:
    """
    Validate a free-typed top-up amount.
    Returns the integer amount if valid, or None if invalid.
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


def build_single_amount_product(student_id: int, amount: int) -> dict:
    """
    Build a single_step_product with ONE item whose amount matches
    exactly what the parent typed. Yo Uganda's mechanism expects a
    list of fixed options — we just give it a list of one, built
    fresh each time around whatever number the parent entered.
    """
    ref = build_topup_reference(student_id, amount)
    ipn_url = build_ipn_url("/webhook/yo")

    return {
        "single_step_product_list": [
            {
                "id":                          ref,
                "label":                       f"UGX {amount:,}",
                "expected_user_input":         "1",
                "amount":                      amount,
                "product_external_reference":  ref,
                "product_success_ipn_url":     ipn_url,
                "product_failure_ipn_url":     ipn_url,
            }
        ],
        "display_message": urllib.parse.quote(
            f"Confirm payment of UGX {amount:,}", safe=""
        ),
        "error_message": urllib.parse.quote(
            "Invalid selection. Please try again.", safe=""
        ),
    }


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

    datetime_str    = body.get("datetime", "")
    phone           = body.get("anumbermsisdn", "")
    action          = body.get("action", "")
    signature       = body.get("signature", "")

    # Field names to give Alex — see header comment for details
    account_number  = body.get("account_number")
    main_menu_choice = body.get("main_menu_choice")
    topup_amount_raw = body.get("topup_amount")
    confirm_choice  = body.get("confirm_choice")

    logger.info(
        f"[USSD] phone={phone} action={action} "
        f"account_number={account_number} topup_amount={topup_amount_raw} "
        f"confirm_choice={confirm_choice}"
    )

    if APP_ENV == "production" and signature:
        if not verify_yo_signature(datetime_str, phone, signature):
            logger.warning(f"[USSD] Signature verification FAILED for {phone}")
            return {"validated": False, "message": "Request verification failed"}

    # ──────────────────────────────────────────────
    # SCREEN 0 — Main menu (likely handled as a static
    # menu on Yo's dashboard, but included here in case
    # it's routed to us)
    # ──────────────────────────────────────────────
    if action == "getmainmenu" or not any([account_number, topup_amount_raw, confirm_choice]):
        return {
            "validated": True,
            "message": (
                "Welcome to School Wallet\n"
                "1. Enter card or account number\n"
                "2. Register New Student\n"
                "0. Exit"
            ),
        }

    # ──────────────────────────────────────────────
    # Registration — ON HOLD. NFC stock-pool design
    # isn't finalized, so this is a stub for now.
    # ──────────────────────────────────────────────
    if main_menu_choice == "2":
        return {
            "validated": True,
            "message": "Student registration via USSD is coming soon. "
                        "Please contact your school admin to register a new student.",
        }

    # ──────────────────────────────────────────────
    # SCREEN 4 — Confirm/Cancel (must come before the
    # amount check below, since confirm_choice and
    # topup_amount are both present by this step)
    # ──────────────────────────────────────────────
    if confirm_choice is not None:
        if confirm_choice == "2":
            return {"validated": True, "message": "Top-up cancelled."}

        if confirm_choice != "1":
            return {"validated": False, "message": "Invalid option. Please select 1 or 2."}

        # confirm_choice == "1" → build the actual payment product
        amount = validate_amount(topup_amount_raw)
        if amount is None:
            return {
                "validated": False,
                "message": f"Invalid amount. Must be between "
                            f"UGX {MIN_TOPUP:,} and UGX {MAX_TOPUP:,}.",
            }

        try:
            sid = int(account_number)
        except (ValueError, TypeError):
            return {"validated": False, "message": "Invalid account number."}

        student = db.query(Student).filter(Student.id == sid).first()
        if not student:
            return {"validated": False, "message": "Student not found."}

        product = build_single_amount_product(student.id, amount)
        logger.info(
            f"[USSD] Confirmed top-up: student {student.id} amount UGX {amount:,}"
        )
        return {
            "validated": True,
            "single_step_product": product,
        }

    # ──────────────────────────────────────────────
    # SCREEN 3 — Amount typed, not yet confirmed.
    # Show the confirmation screen.
    # ──────────────────────────────────────────────
    if topup_amount_raw is not None and account_number is not None:
        amount = validate_amount(topup_amount_raw)
        if amount is None:
            return {
                "validated": False,
                "message": f"Invalid amount. Must be between "
                            f"UGX {MIN_TOPUP:,} and UGX {MAX_TOPUP:,}.",
            }

        try:
            sid = int(account_number)
        except (ValueError, TypeError):
            return {"validated": False, "message": "Invalid account number."}

        student = db.query(Student).filter(Student.id == sid).first()
        if not student:
            return {"validated": False, "message": "Student not found."}

        return {
            "validated": True,
            "message": (
                f"Confirm: Top up {student.name}'s wallet "
                f"by UGX {amount:,}?\n1. Confirm\n2. Cancel"
            ),
        }

    # ──────────────────────────────────────────────
    # SCREEN 1/2 — Account number just entered.
    # Identify student, show personalized menu.
    # ──────────────────────────────────────────────
    if account_number is not None:
        try:
            sid = int(account_number)
        except (ValueError, TypeError):
            return {"validated": False, "message": "Invalid account number. Please check and try again."}

        student = db.query(Student).filter(Student.id == sid).first()
        if not student:
            return {"validated": False, "message": f"Account {sid} not found. Please check with the school."}

        wallet = db.query(Wallet).filter(Wallet.student_id == student.id).first()
        if not wallet or not wallet.is_active:
            return {"validated": False, "message": "This wallet is not active. Contact the school admin."}

        return {
            "validated": True,
            "message": (
                f"{student.name} - {student.school.name}\n"
                f"1. Top Up Wallet\n"
                f"2. Exit"
            ),
        }

    # Fallback — shouldn't normally be reached
    return {"validated": False, "message": "Session error. Please try again."}