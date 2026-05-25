# ================================================
# app/sms.py
# ------------------------------------------------
# Production-ready SMS for Africa's Talking Uganda
#
# RULES FOR UGANDA SMS:
# 1. Max 160 characters per SMS (plain text)
# 2. No emojis (they reduce limit to 70 chars)
# 3. No special Unicode characters
# 4. Keep messages short and clear
# 5. Always include school name for context
# ================================================

import os
import africastalking
from dotenv import load_dotenv

load_dotenv()

AT_USERNAME  = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY   = os.getenv("AT_API_KEY", "")
AT_SENDER_ID = os.getenv("AT_SENDER_ID", "SchoolWlt")
APP_ENV      = os.getenv("APP_ENV", "development")

# ── Initialise Africa's Talking ──────────────────
sms_client = None

if AT_API_KEY:
    try:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        sms_client = africastalking.SMS
        print(f"SMS ready — user: {AT_USERNAME}")
    except Exception as e:
        print(f"SMS init failed: {e}")
else:
    print("AT_API_KEY not set — SMS in print-only mode")


# ================================================
# CORE SEND FUNCTION
# ================================================

def send_sms(phone: str, message: str) -> bool:
    """
    Send an SMS to a Uganda phone number.

    NEVER crashes the app — always wrapped in try/except.
    SMS failure should never block a payment.

    Args:
        phone   → Uganda number e.g. "256771234567"
        message → Plain text, max 160 chars, no emojis
    """
    # Format phone
    phone = phone.strip().replace(" ", "").replace("+", "")
    if not phone.startswith("256"):
        print(f"Invalid phone: {phone}")
        return False

    # Add + prefix for AT
    formatted = f"+{phone}"

    # Enforce 160 char limit
    if len(message) > 160:
        message = message[:157] + "..."

    # Print-only mode (no API key or sandbox)
    if not sms_client or AT_USERNAME == "sandbox":
        print(f"\nSMS to {formatted}:")
        print(f"  {message}")
        print(f"  ({len(message)} chars)")
        return True

    # Send real SMS
    try:
        response = sms_client.send(
            message=message,
            recipients=[formatted],
            sender_id=AT_SENDER_ID,
        )

        recipients = response.get(
            "SMSMessageData", {}
        ).get("Recipients", [])

        if recipients:
            status = recipients[0].get("status", "")
            cost   = recipients[0].get("cost", "")
            if status == "Success":
                print(f"SMS sent to {formatted} — cost: {cost}")
                return True
            else:
                print(f"SMS failed: {status}")
                return False
        return False

    except Exception as e:
        print(f"SMS error: {e}")
        return False


# ================================================
# DETECT NETWORK FROM PHONE NUMBER
# ================================================

def detect_network(phone: str) -> str:
    """
    Detect MTN or Airtel from Uganda phone prefix.

    MTN Uganda prefixes:   076, 077, 078, 039
    Airtel Uganda prefixes: 070, 075, 074, 020
    """
    phone = phone.replace("+", "").replace(" ", "")
    if phone.startswith("256"):
        phone = phone[3:]  # remove country code

    mtn_prefixes    = ["76", "77", "78", "39"]
    airtel_prefixes = ["70", "75", "74", "20"]

    for prefix in mtn_prefixes:
        if phone.startswith(prefix):
            return "MTN"

    for prefix in airtel_prefixes:
        if phone.startswith(prefix):
            return "AIRTEL"

    return "MTN"  # default


# ================================================
# SMS TEMPLATES
# All messages:
# - Max 160 characters
# - No emojis
# - No special characters
# - Clear and simple
# ================================================

def sms_payment_alert(
    parent_phone: str,
    student_name: str,
    amount: int,
    merchant_name: str,
    remaining_balance: int,
    timestamp: str,
) -> bool:
    """
    Alert parent when child makes a payment.

    Example (98 chars):
    SW: Amara spent UGX 2,000 at Main Canteen.
    Balance: UGX 8,100. 19 May 10:02am
    """
    message = (
        f"SW: {student_name} spent UGX {amount:,} "
        f"at {merchant_name}. "
        f"Balance: UGX {remaining_balance:,}. "
        f"{timestamp}"
    )
    return send_sms(parent_phone, message)


def sms_topup_confirmation(
    parent_phone: str,
    student_name: str,
    amount: int,
    new_balance: int,
) -> bool:
    """
    Confirm top-up to parent.

    Example (75 chars):
    SW: UGX 20,000 added to Amara's wallet.
    New balance: UGX 20,000.
    """
    message = (
        f"SW: UGX {amount:,} added to "
        f"{student_name}'s wallet. "
        f"New balance: UGX {new_balance:,}."
    )
    return send_sms(parent_phone, message)


def sms_topup_failed(
    parent_phone: str,
    student_name: str,
    amount: int,
) -> bool:
    """
    Tell parent their top-up was rejected.

    Example:
    SW: Top-up of UGX 20,000 for Amara failed.
    Please check your MoMo balance and try again.
    """
    message = (
        f"SW: Top-up of UGX {amount:,} for "
        f"{student_name} failed. "
        f"Check your MoMo balance and try again."
    )
    return send_sms(parent_phone, message)


def sms_low_balance_alert(
    parent_phone: str,
    student_name: str,
    remaining_balance: int,
) -> bool:
    """
    Warn parent when balance drops below UGX 2,000.

    Example (84 chars):
    SW: Low balance. Amara's wallet has UGX 800.
    Top up now to avoid disruption.
    """
    message = (
        f"SW: Low balance. "
        f"{student_name}'s wallet has UGX {remaining_balance:,}. "
        f"Top up now to avoid disruption."
    )
    return send_sms(parent_phone, message)


def sms_wallet_deactivated(
    parent_phone: str,
    student_name: str,
    reason: str = "Contact school admin",
) -> bool:
    """
    Tell parent their child's wallet was deactivated.
    """
    message = (
        f"SW: {student_name}'s wallet has been deactivated. "
        f"{reason}."
    )
    return send_sms(parent_phone, message)


def sms_daily_summary(
    parent_phone: str,
    student_name: str,
    total_spent: int,
    remaining_balance: int,
    date: str,
) -> bool:
    """
    Daily spending summary sent to parent at end of day.
    Optional — school can enable this feature.
    """
    message = (
        f"SW Daily Summary ({date}): "
        f"{student_name} spent UGX {total_spent:,}. "
        f"Balance: UGX {remaining_balance:,}."
    )
    return send_sms(parent_phone, message)


def sms_welcome(
    parent_phone: str,
    parent_name: str,
    student_name: str,
    ussd_code: str = "*384*23114#",
) -> bool:
    """
    Welcome SMS sent when parent is first registered.
    """
    message = (
        f"Welcome to School Wallet, {parent_name}. "
        f"{student_name} is registered. "
        f"Top up by dialing {ussd_code} "
        f"on any MTN or Airtel phone."
    )
    return send_sms(parent_phone, message)