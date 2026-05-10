# ================================================
# app/momo.py
# ------------------------------------------------
# TEST MODE: When FLW_SECRET_KEY is not set or
# APP_ENV=development, all payments are auto-approved
# without calling Flutterwave at all.
# ================================================

import httpx
import os
from dotenv import load_dotenv

load_dotenv()

FLW_SECRET_KEY = os.getenv("FLW_SECRET_KEY")
APP_ENV        = os.getenv("APP_ENV", "development")


async def charge_mobile_money(
    phone: str,
    amount: int,
    network: str,
    tx_ref: str,
    customer_name: str = "School Parent"
) -> dict:
    """
    In development mode — returns a fake success instantly.
    In production mode — calls real Flutterwave API.
    """

    # ── TEST MODE ──────────────────────────────────────────
    # If no Flutterwave key is set OR we are in development,
    # skip the real API and return a fake success immediately.
    # This lets you test the entire flow without Flutterwave.
    if not FLW_SECRET_KEY or APP_ENV == "development":
        print(f"\n🧪 TEST MODE — skipping real Flutterwave call")
        print(f"   Pretending to charge {phone} ({network}) UGX {amount:,}")
        print(f"   Reference: {tx_ref}")
        print(f"   Result: FAKE SUCCESS ✅")

        # Return the same shape Flutterwave would return
        return {
            "status": "success",
            "message": "TEST MODE — payment auto-approved",
            "data": {
                "tx_ref":       tx_ref,
                "amount":       amount,
                "currency":     "UGX",
                "network":      network,
                "phone_number": phone,
                "status":       "successful",
            }
        }

    # ── PRODUCTION MODE ────────────────────────────────────
    # Only runs when FLW_SECRET_KEY is set
    payload = {
        "tx_ref":       tx_ref,
        "amount":       str(amount),
        "currency":     "UGX",
        "network":      network,
        "email":        "parent@schoolwallet.ug",
        "phone_number": phone,
        "fullname":     customer_name,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.flutterwave.com/v3/charges?type=mobile_money_uganda",
            headers={
                "Authorization": f"Bearer {FLW_SECRET_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30.0,
        )

    result = response.json()
    print(f"📡 Flutterwave: {result.get('status')} — ref: {tx_ref}")
    return result


async def verify_transaction(transaction_id: str) -> dict:
    """Verify a transaction with Flutterwave (production only)."""
    if not FLW_SECRET_KEY or APP_ENV == "development":
        return {"status": "success", "data": {"status": "successful"}}

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers={"Authorization": f"Bearer {FLW_SECRET_KEY}"},
            timeout=30.0,
        )
    return response.json()