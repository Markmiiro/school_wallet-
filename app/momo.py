# ================================================
# app/momo.py
# ------------------------------------------------
# Handles all payment API calls via DGateway.
# DGateway is a unified Uganda payment gateway
# supporting MTN MoMo and Airtel Money.
#
# DOCS: https://dgateway.desispay.com/docs
#
# HOW IT WORKS:
# 1. Your server calls charge_mobile_money()
# 2. DGateway sends USSD prompt to parent's phone
# 3. Parent enters MTN/Airtel PIN to approve
# 4. You poll verify_transaction() for the result
# 5. When status = "completed" → credit the wallet
#
# TEST MODE:
# Use test API key (dgw_test_...) with test numbers:
# Success → 256111777771
# Failed  → 256111777991
# Pending → 256111777781
# ================================================

import httpx
import os
from dotenv import load_dotenv

load_dotenv()

DGATEWAY_API_URL = os.getenv("DGATEWAY_API_URL", "https://dgatewayapi.desispay.com")
DGATEWAY_API_KEY = os.getenv("DGATEWAY_API_KEY", "")
APP_ENV          = os.getenv("APP_ENV", "development")


async def charge_mobile_money(
    phone: str,
    amount: int,
    network: str = "MTN",
    tx_ref: str = "",
    customer_name: str = "School Parent"
) -> dict:
    """
    Sends a payment request to a parent's MTN or Airtel number.

    DGateway sends a USSD prompt to the parent's phone.
    Parent enters their PIN to approve.
    Poll verify_transaction() to check the result.

    Args:
        phone         → e.g. "256771234567"
        amount        → in UGX e.g. 20000
        network       → "MTN" or "AIRTEL" (informational only)
        tx_ref        → your unique reference ID
        customer_name → parent's name

    Returns:
        dict → DGateway response with reference and status
    """

    # ── TEST MODE ──────────────────────────────────────────
    # No API key set → simulate a successful payment
    if not DGATEWAY_API_KEY or APP_ENV == "development":
        print(f"\n🧪 TEST MODE — skipping real DGateway call")
        print(f"   Phone:     {phone}")
        print(f"   Amount:    UGX {amount:,}")
        print(f"   Reference: {tx_ref}")
        print(f"   Result:    FAKE SUCCESS ✅")
        return {
            "data": {
                "reference": tx_ref,
                "status":    "pending",
                "amount":    amount,
                "currency":  "UGX",
                "provider":  "iotec",
            }
        }

    # ── PRODUCTION MODE ────────────────────────────────────
    # Format phone — DGateway accepts 256XXXXXXXXX or 0XXXXXXXXX
    phone = phone.strip().replace("+", "").replace(" ", "")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{DGATEWAY_API_URL}/v1/payments/collect",
            headers={
                "X-Api-Key":    DGATEWAY_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "amount":       amount,
                "currency":     "UGX",
                "phone_number": phone,
                "provider":     "iotec",   # handles both MTN and Airtel in Uganda
                "description":  f"School wallet top-up - {customer_name}",
                "metadata": {
                    "tx_ref":   tx_ref,
                    "customer": customer_name,
                    "network":  network,
                }
            },
            timeout=30.0,
        )

    result = response.json()

    # DGateway returns data.reference — map it to our tx_ref format
    if "data" in result and "reference" in result["data"]:
        # Store DGateway's reference alongside our tx_ref
        result["data"]["tx_ref"] = tx_ref
        print(f"📡 DGateway charge initiated: {result['data'].get('status')} — ref: {result['data']['reference']}")
    else:
        print(f"⚠️  DGateway error: {result}")

    return result


async def verify_transaction(reference: str) -> dict:
    """
    Check the current status of a payment by its reference.

    Use this to poll for payment completion after calling
    charge_mobile_money(). Poll every 5 seconds until
    status is "completed" or "failed".

    Args:
        reference → DGateway reference from charge_mobile_money()

    Returns:
        dict → { data: { reference, status, amount, currency } }
        status values: "pending" | "completed" | "failed"
    """

    # ── TEST MODE ──────────────────────────────────────────
    if not DGATEWAY_API_KEY or APP_ENV == "development":
        return {
            "data": {
                "reference": reference,
                "status":    "completed",
                "amount":    0,
                "currency":  "UGX",
            }
        }

    # ── PRODUCTION MODE ────────────────────────────────────
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{DGATEWAY_API_URL}/v1/webhooks/verify",
            headers={
                "X-Api-Key":    DGATEWAY_API_KEY,
                "Content-Type": "application/json",
            },
            json={"reference": reference},
            timeout=30.0,
        )

    return response.json()


async def disburse_to_merchant(
    phone: str,
    amount: int,
    merchant_name: str = "Merchant"
) -> dict:
    """
    Send money to a merchant's mobile money account.
    Used for end-of-day vendor payouts.

    Args:
        phone         → merchant's MTN/Airtel number e.g. "256700000001"
        amount        → amount in UGX
        merchant_name → merchant's name for the payout note
    """

    # ── TEST MODE ──────────────────────────────────────────
    if not DGATEWAY_API_KEY or APP_ENV == "development":
        print(f"\n🧪 TEST MODE — fake payout to {phone} UGX {amount:,}")
        return {"data": {"status": "completed", "amount": amount}}

    # ── PRODUCTION MODE ────────────────────────────────────
    phone = phone.strip().replace("+", "").replace(" ", "")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{DGATEWAY_API_URL}/v1/payments/disburse",
            headers={
                "X-Api-Key":    DGATEWAY_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "amount":           amount,
                "currency":         "UGX",
                "phone_number":     phone,
                "provider":         "iotec",
                "description":      f"Daily payout to {merchant_name}",
            },
            timeout=30.0,
        )

    result = response.json()
    print(f"💸 Payout to {merchant_name}: {result.get('data', {}).get('status')}")
    return result