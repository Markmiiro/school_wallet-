# ================================================
# app/sms.py
# ------------------------------------------------
# SMS Gateway: Yo Uganda SMS Gateway
# Endpoint: https://smgw1.yo.co.ug:8100/sendsms
# Protocol: HTTP GET (URL params) — simpler than XML POST
#
# Default sender ID: 6969 (free, no monthly fee)
# Custom sender ID : requires separate setup + MTN fee
#
# COST CONTROL STRATEGY (boarding school context):
#   - Top-up confirmations  → send immediately (parent needs reassurance)
#   - Purchase receipts     → batch at 6PM daily (see reports.py)
#   - Low balance alerts    → send immediately (threshold: UGX 2,000)
#   - Daily summary         → called from 6PM cron in reports.py
#
# ENV VARS REQUIRED:
#   YO_SMS_ACCOUNT   → YBS account number (from Yo Uganda)
#   YO_SMS_PASSWORD  → SMS gateway password (from Yo Uganda)
#   YO_SMS_SENDER    → sender ID (default: 6969)
#   YO_SMS_URL       → gateway URL (default: https://smgw1.yo.co.ug:8100/sendsms)
# ================================================

import os
import httpx
import logging
import urllib.parse
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

YO_SMS_ACCOUNT = os.getenv("YO_SMS_ACCOUNT", "")
YO_SMS_PASSWORD = os.getenv("YO_SMS_PASSWORD", "")
YO_SMS_SENDER   = os.getenv("YO_SMS_SENDER", "6969")
YO_SMS_URL      = os.getenv("YO_SMS_URL", "https://smgw1.yo.co.ug:8100/sendsms")
APP_ENV         = os.getenv("APP_ENV", "development")

# Yo Uganda numbers must be in full format: 256XXXXXXXXX
def _clean_phone(phone: str) -> str:
    phone = phone.strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone.startswith("256"):
        phone = "256" + phone.lstrip("0")
    return phone


# ================================================
# CORE SEND FUNCTIONS
# Two versions exist on purpose:
#   - send_sms()      → async, used by webhook.py (async def routes, awaited)
#   - send_sms_sync() → sync,  used by payments.py (plain def routes, no await)
# Both talk to the same Yo Uganda SMS Gateway endpoint.
# ================================================
def send_sms_sync(phone: str, message: str) -> dict:
    """
    Synchronous version of send_sms — for use inside plain `def` route
    handlers (e.g. payments.py) that call SMS functions without `await`.
    """
    phone = _clean_phone(phone)

    if not YO_SMS_ACCOUNT or APP_ENV == "development":
        print(f"\n[Yo SMS TEST] To: {phone}")
        print(f"  Sender:  {YO_SMS_SENDER}")
        print(f"  Message: {message}")
        logger.info(f"[Yo SMS TEST] To: {phone} | {message[:60]}")
        return {"success": True, "message": "TEST MODE — SMS not sent to real gateway"}

    params = {
        "ybsacctno":    YO_SMS_ACCOUNT,
        "password":     YO_SMS_PASSWORD,
        "origin":       YO_SMS_SENDER,
        "sms_content":  message,
        "destinations": phone,
    }

    try:
        with httpx.Client() as client:
            response = client.get(YO_SMS_URL, params=params, timeout=15.0)

        result = urllib.parse.parse_qs(response.text.strip())
        status = result.get("ybs_autocreate_status", ["ERROR"])[0]

        if status == "OK":
            logger.info(f"[Yo SMS] Sent ✓ to {phone}")
            return {"success": True, "message": "SMS sent"}
        else:
            error_raw = result.get("ybs_autocreate_message", ["Unknown error"])[0]
            error_msg = urllib.parse.unquote_plus(error_raw)
            logger.error(f"[Yo SMS] Failed to {phone}: {error_msg}")
            return {"success": False, "message": error_msg}

    except httpx.TimeoutException:
        logger.error(f"[Yo SMS] Timeout — could not reach gateway for {phone}")
        return {"success": False, "message": "SMS gateway timeout"}
    except Exception as e:
        logger.error(f"[Yo SMS] Unexpected error: {e}")
        return {"success": False, "message": str(e)}


async def send_sms(phone: str, message: str) -> dict:
    """
    Send a single SMS via Yo Uganda SMS Gateway (HTTP GET).

    Response from Yo Uganda is URL-encoded:
        ybs_autocreate_status=OK
        ybs_autocreate_status=ERROR&ybs_autocreate_message=...

    Args:
        phone   → Uganda number e.g. "256771234567"
        message → SMS content (160 chars per SMS segment)

    Returns:
        {"success": True/False, "message": "..."}
    """
    phone = _clean_phone(phone)

    # ── TEST MODE ─────────────────────────────────
    if not YO_SMS_ACCOUNT or APP_ENV == "development":
        print(f"\n[Yo SMS TEST] To: {phone}")
        print(f"  Sender:  {YO_SMS_SENDER}")
        print(f"  Message: {message}")
        logger.info(f"[Yo SMS TEST] To: {phone} | {message[:60]}")
        return {"success": True, "message": "TEST MODE — SMS not sent to real gateway"}

    # ── Build request params ───────────────────────
    params = {
        "ybsacctno":    YO_SMS_ACCOUNT,
        "password":     YO_SMS_PASSWORD,
        "origin":       YO_SMS_SENDER,
        "sms_content":  message,
        "destinations": phone,
    }

    # ── Send via HTTP GET ──────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                YO_SMS_URL,
                params=params,
                timeout=15.0,
            )

        # Parse URL-encoded response body
        # e.g. "ybs_autocreate_status=OK"
        result = urllib.parse.parse_qs(response.text.strip())
        status = result.get("ybs_autocreate_status", ["ERROR"])[0]

        if status == "OK":
            logger.info(f"[Yo SMS] Sent ✓ to {phone}")
            return {"success": True, "message": "SMS sent"}
        else:
            error_raw = result.get("ybs_autocreate_message", ["Unknown error"])[0]
            # Yo returns + instead of spaces in error messages
            error_msg = urllib.parse.unquote_plus(error_raw)
            logger.error(f"[Yo SMS] Failed to {phone}: {error_msg}")
            return {"success": False, "message": error_msg}

    except httpx.TimeoutException:
        logger.error(f"[Yo SMS] Timeout — could not reach gateway for {phone}")
        return {"success": False, "message": "SMS gateway timeout"}
    except Exception as e:
        logger.error(f"[Yo SMS] Unexpected error: {e}")
        return {"success": False, "message": str(e)}


# ================================================
# NAMED MESSAGE FUNCTIONS
# Keep business logic out of route handlers.
# All callers use these — never call send_sms directly from routes.
# ================================================

async def sms_topup_confirmation(
    parent_phone: str,
    student_name: str,
    amount: int,
    new_balance: int,
) -> None:
    """
    Immediate SMS after a successful top-up.
    Called from routes/webhook.py when Yo Uganda IPN fires SUCCEEDED.
    """
    message = (
        f"School Wallet: UGX {amount:,} added for {student_name}. "
        f"New balance: UGX {new_balance:,}."
    )
    await send_sms(parent_phone, message)


async def sms_daily_summary(
    parent_phone: str,
    student_name: str,
    total_spent: int,
    purchase_count: int,
    balance: int,
) -> None:
    """
    6PM daily batch SMS — ONE message per active student per day.
    Called from routes/reports.py 6PM cron job.
    This is the primary cost-control mechanism: replaces per-purchase SMS.
    """
    if purchase_count == 0:
        message = (
            f"School Wallet: {student_name} had no purchases today. "
            f"Balance: UGX {balance:,}."
        )
    elif purchase_count == 1:
        message = (
            f"School Wallet: {student_name} spent UGX {total_spent:,} today "
            f"(1 purchase). Balance: UGX {balance:,}."
        )
    else:
        message = (
            f"School Wallet: {student_name} spent UGX {total_spent:,} today "
            f"({purchase_count} purchases). Balance: UGX {balance:,}."
        )
    await send_sms(parent_phone, message)


def sms_payment_alert(
    parent_phone: str,
    student_name: str,
    amount: int,
    merchant_name: str,
    remaining_balance: int,
    timestamp: str,
) -> None:
    """
    Per-purchase SMS sent immediately after a tuck shop payment.
    SYNC — called directly from payments.py's plain `def` routes
    (make_payment, nfc_payment, sync_offline_payments), no await.

    WARNING: Each SMS costs UGX 35. If purchase volume is high,
    consider switching this to the 6PM batched sms_daily_summary
    instead and dropping per-purchase alerts.
    """
    message = (
        f"School Wallet: {student_name} paid UGX {amount:,} at {merchant_name} "
        f"on {timestamp}. Balance: UGX {remaining_balance:,}."
    )
    send_sms_sync(parent_phone, message)


def sms_low_balance_alert(
    parent_phone: str,
    student_name: str,
    remaining_balance: int,
) -> None:
    """
    Immediate alert when student balance falls below UGX 2,000.
    SYNC — called directly from payments.py's plain `def` routes, no await.
    """
    message = (
        f"School Wallet: Low balance for {student_name}. "
        f"Balance: UGX {remaining_balance:,}. "
        f"Please top up to avoid disruption."
    )
    send_sms_sync(parent_phone, message)


async def sms_payment_receipt(
    parent_phone: str,
    student_name: str,
    amount: int,
    vendor_name: str,
    balance: int,
) -> None:
    """
    Per-purchase receipt SMS.
    WARNING: Use sparingly — each SMS costs UGX 35.
    Default strategy: batch these into sms_daily_summary at 6PM.
    Only call this directly if explicitly needed (e.g. amounts above UGX 10,000).
    """
    message = (
        f"School Wallet: {student_name} paid UGX {amount:,} at {vendor_name}. "
        f"Balance: UGX {balance:,}."
    )
    await send_sms(parent_phone, message)