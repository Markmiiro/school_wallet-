# ================================================
# app/sms.py
# ------------------------------------------------
# Handles all SMS notifications to parents.
# Uses Africa's Talking API.
#
# HOW IT WORKS:
# 1. Payment happens
# 2. Your server calls send_sms()
# 3. Africa's Talking sends SMS to parent's phone
# 4. Parent receives message on any basic phone
#
# COST: About UGX 50 per SMS
# SANDBOX: Free for testing — no real SMS sent
# ================================================

import africastalking
import os
from dotenv import load_dotenv

load_dotenv()

AT_USERNAME  = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY   = os.getenv("AT_API_KEY", "")
AT_SENDER_ID = os.getenv("AT_SENDER_ID", "SchoolWallet")

# Initialise Africa's Talking
# This runs once when the file is imported
if AT_API_KEY:
    africastalking.initialize(AT_USERNAME, AT_API_KEY)
    sms = africastalking.SMS
else:
    sms = None
    print("⚠️  AT_API_KEY not set — SMS will run in print-only mode")


def send_sms(phone: str, message: str) -> bool:
    """
    Send an SMS to a phone number.

    Args:
        phone   → recipient number e.g. "256771234567"
        message → the SMS text

    Returns:
        True  → SMS sent successfully
        False → SMS failed (logged but does not crash the app)

    IMPORTANT:
    SMS failures should NEVER crash a payment.
    The payment already succeeded — SMS is just a notification.
    We wrap everything in try/except for this reason.
    """

    # ── SANDBOX / TEST MODE ──────────────────────
    # If no API key — just print the SMS to terminal
    # Useful during development so you can see what
    # the parent would receive without sending real SMS
    if not AT_API_KEY or AT_USERNAME == "sandbox":
        print(f"\n📱 SMS (TEST MODE — not really sent):")
        print(f"   To:      {phone}")
        print(f"   Message: {message}")
        print(f"   ─────────────────────────────────")
        return True

    # ── PRODUCTION MODE ──────────────────────────
    try:
        # Format phone for Africa's Talking
        # Must start with + e.g. +256771234567
        if not phone.startswith("+"):
            phone = f"+{phone}"

        response = sms.send(
            message=message,
            recipients=[phone],
            sender_id=AT_SENDER_ID,
        )

        # Check if it was delivered
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])
        if recipients and recipients[0].get("status") == "Success":
            print(f"✅ SMS sent to {phone}")
            return True
        else:
            print(f"⚠️  SMS may have failed: {response}")
            return False

    except Exception as e:
        # Never crash the app because of an SMS failure
        print(f"⚠️  SMS error (payment still succeeded): {e}")
        return False


# ================================================
# PRE-BUILT SMS TEMPLATES
# ================================================
# These functions build the SMS messages.
# Consistent format so parents always know
# exactly what each message means.

def sms_payment_alert(
    parent_phone: str,
    student_name: str,
    amount: int,
    merchant_name: str,
    remaining_balance: int,
    timestamp: str,
) -> bool:
    """
    Send payment alert to parent after student buys something.

    Example SMS:
    School Wallet 🏫
    Amara spent UGX 2,000 at Main Tuck Shop.
    Balance: UGX 3,000
    10 May 2026 8:05pm
    """
    message = (
        f"School Wallet Alert\n"
        f"{student_name} spent UGX {amount:,} "
        f"at {merchant_name}.\n"
        f"Balance: UGX {remaining_balance:,}\n"
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
    Send top-up confirmation to parent after wallet is credited.

    Example SMS:
    School Wallet 🏫
    UGX 20,000 added to Amara's wallet.
    New balance: UGX 20,000
    """
    message = (
        f"School Wallet Alert\n"
        f"UGX {amount:,} added to "
        f"{student_name}'s wallet.\n"
        f"New balance: UGX {new_balance:,}"
    )
    return send_sms(parent_phone, message)


def sms_low_balance_alert(
    parent_phone: str,
    student_name: str,
    remaining_balance: int,
) -> bool:
    """
    Warn parent when balance drops below UGX 2,000.

    Example SMS:
    School Wallet 🏫
    ⚠️ Low balance alert!
    Amara's wallet has UGX 1,000 remaining.
    Top up now to avoid disruption.
    """
    message = (
        f"School Wallet Alert\n"
        f"Low balance warning!\n"
        f"{student_name}'s wallet has "
        f"UGX {remaining_balance:,} remaining.\n"
        f"Top up now to avoid disruption."
    )
    return send_sms(parent_phone, message)


def sms_wallet_deactivated(
    parent_phone: str,
    student_name: str,
) -> bool:
    """Tell parent their child's wallet was deactivated."""
    message = (
        f"School Wallet Alert\n"
        f"{student_name}'s wallet has been deactivated.\n"
        f"Contact school admin for assistance."
    )
    return send_sms(parent_phone, message)