# ================================================
# app/momo.py
# ------------------------------------------------
# Payment gateway: Yo Uganda Limited
# Licensed and Regulated by Bank of Uganda
#
# API format: XML over HTTP POST
#
# TWO MAIN OPERATIONS:
# 1. charge_mobile_money → parent tops up wallet
#    (acdepositfunds — asynchronous, NonBlocking=TRUE)
#
# 2. disburse_to_merchant → pay vendor at end of day
#    (acwithdrawfunds)
#
# DOCS: https://payments.yo.co.ug
# SANDBOX: https://sandbox.yo.co.ug
# SUPPORT: support@yo.co.ug | +256 788 238665
#
# ------------------------------------------------
# FIELD NAMES — these come from Yo! Payments API
# Specification v3.48 §6.1.1 and must match EXACTLY.
# Yo silently ignores unrecognised fields, so a typo
# here means callbacks never fire and transactions
# sit PENDING forever with no error anywhere.
#
#   <InstantNotificationUrl>  → success callbacks (§6.3)
#   <FailureNotificationUrl>  → failure callbacks (§6.4)
#
# Both are handled in app/routes/webhook.py.
# ------------------------------------------------
#
# ⚠️ TEST MODE WARNING
# If YO_USERNAME is unset OR APP_ENV != "production",
# every function below returns a FAKE SUCCESS without
# contacting Yo Uganda at all. That means:
#   - top-ups appear to succeed with no real money moving
#   - merchant payouts report success with nothing sent
# Before going live, confirm BOTH are set correctly in
# Railway → Variables, or the app will silently fake
# every transaction.
#
# ⚠️ OUTSTANDING: disburse_to_merchant does NOT sign its
# requests. Per API spec §4, withdraw requests require an
# RSA signature generated with YOUR private key (public
# half shared with Yo support). Yo will only process signed
# withdraw requests, so real payouts will fail until this
# is implemented.
# ================================================

import httpx
import os
import uuid
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

YO_USERNAME = os.getenv("YO_USERNAME", "")
YO_PASSWORD = os.getenv("YO_PASSWORD", "")
YO_API_URL  = os.getenv(
    "YO_API_URL",
    "https://sandbox.yo.co.ug/services/yopaymentsdev/"
)

# Success callbacks (IPN) — see webhook.py POST /webhook/yo
YO_IPN_URL = os.getenv(
    "YO_IPN_URL",
    "https://web-production-454a5.up.railway.app/webhook/yo"
)

# Failure callbacks — see webhook.py POST /webhook/yo/failure
YO_FAILURE_URL = os.getenv(
    "YO_FAILURE_URL",
    "https://web-production-454a5.up.railway.app/webhook/yo/failure"
)

APP_ENV = os.getenv("APP_ENV", "development")


# ================================================
# HELPER: Parse Yo Uganda XML response
# ================================================
def parse_yo_response(xml_text: str) -> dict:
    """
    Parse Yo Uganda XML response into a flat dict.

    Yo Uganda returns XML like:
    <AutoCreate>
      <Response>
        <Status>OK</Status>
        <StatusCode>0</StatusCode>
        <TransactionStatus>PENDING</TransactionStatus>
        <TransactionReference>YO-REF-123</TransactionReference>
      </Response>
    </AutoCreate>
    """
    try:
        root = ET.fromstring(xml_text)
        result = {}
        for child in root.iter():
            if child.text and child.text.strip():
                result[child.tag] = child.text.strip()
        return result
    except Exception as e:
        print(f"XML parse error: {e}")
        return {"Status": "ERROR", "StatusMessage": str(e)}


def _is_test_mode() -> bool:
    """
    True when we should fake responses instead of calling Yo Uganda.
    Kept as one function so the condition can't drift between callers.
    """
    return (not YO_USERNAME) or (APP_ENV != "production")


# ================================================
# MAIN FUNCTION 1: Charge parent's mobile money
# ================================================
async def charge_mobile_money(
    phone: str,
    amount: int,
    network: str = "MTN",
    tx_ref: str = "",
    customer_name: str = "School Parent"
) -> dict:
    """
    Request payment from parent's MTN or Airtel wallet.

    Uses Yo Uganda acdepositfunds with NonBlocking=TRUE (§6.1).
    Parent receives an on-screen/USSD prompt to approve with their PIN.
    Yo Uganda then calls:
      - YO_IPN_URL      on success  (see webhook.py)
      - YO_FAILURE_URL  on failure  (see webhook.py)

    Args:
        phone         → e.g. "256771234567" (no "+", country code required)
        amount        → in UGX e.g. 20000
        network       → "MTN" or "AIRTEL" (informational only)
        tx_ref        → your unique reference; comes back as external_ref
                        in the IPN, and is how webhook.py routes the
                        callback (USSD-TOPUP-…, USSD-REG-…, or a UUID)
        customer_name → parent name, used in the narrative

    Returns:
        dict with Status, TransactionReference, etc.
    """

    # ── TEST MODE ──────────────────────────────
    if _is_test_mode():
        print(f"\n⚠️  TEST MODE — Yo Uganda fake charge (NO REAL MONEY)")
        print(f"  Phone:  {phone}")
        print(f"  Amount: UGX {amount:,}")
        print(f"  Ref:    {tx_ref}")
        return {
            "Status":               "OK",
            "StatusCode":           "0",
            "TransactionStatus":    "PENDING",
            "TransactionReference": tx_ref,
            "StatusMessage":        "TEST MODE — no real transaction",
        }

    # ── Format phone ────────────────────────────
    phone = phone.strip().replace("+", "").replace(" ", "")

    # ── Build XML request (§6.1.1) ───────────────
    xml_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{YO_USERNAME}</APIUsername>
    <APIPassword>{YO_PASSWORD}</APIPassword>
    <Method>acdepositfunds</Method>
    <NonBlocking>TRUE</NonBlocking>
    <Amount>{amount}</Amount>
    <Account>{phone}</Account>
    <Narrative>School Wallet top-up for {customer_name}</Narrative>
    <ExternalReference>{tx_ref}</ExternalReference>
    <ProviderReferenceText>{tx_ref}</ProviderReferenceText>
    <InstantNotificationUrl>{YO_IPN_URL}</InstantNotificationUrl>
    <FailureNotificationUrl>{YO_FAILURE_URL}</FailureNotificationUrl>
  </Request>
</AutoCreate>"""

    # ── Send to Yo Uganda ────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                YO_API_URL,
                content=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=30.0,
            )

        result    = parse_yo_response(response.text)
        status    = result.get("Status", "ERROR")
        tx_status = result.get("TransactionStatus", "UNKNOWN")

        print(f"Yo Uganda charge: Status={status} TxStatus={tx_status} Ref={tx_ref}")
        return result

    except Exception as e:
        print(f"Yo Uganda error: {e}")
        return {
            "Status":        "ERROR",
            "StatusMessage": str(e),
        }


# ================================================
# MAIN FUNCTION 2: Check transaction status
# ================================================
async def verify_transaction(tx_ref: str) -> dict:
    """
    Check the current status of a transaction (§7).

    Uses Yo Uganda actransactioncheckstatus.
    Poll roughly every 15 seconds for transactions still PENDING.

    TransactionStatus values:
      PENDING       → waiting for the payer to approve
      SUCCEEDED     → payment confirmed
      FAILED        → rejected or timed out
      INDETERMINATE → unknown; resolves within ~1 hour, check again
    """

    # ── TEST MODE ──────────────────────────────
    if _is_test_mode():
        print(f"⚠️  TEST MODE — Yo Uganda fake status check for {tx_ref}")
        return {
            "Status":               "OK",
            "TransactionStatus":    "SUCCEEDED",
            "TransactionReference": tx_ref,
            "StatusMessage":        "TEST MODE — not a real status",
        }

    xml_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{YO_USERNAME}</APIUsername>
    <APIPassword>{YO_PASSWORD}</APIPassword>
    <Method>actransactioncheckstatus</Method>
    <PrivateTransactionReference>{tx_ref}</PrivateTransactionReference>
  </Request>
</AutoCreate>"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                YO_API_URL,
                content=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=30.0,
            )
        return parse_yo_response(response.text)
    except Exception as e:
        print(f"Yo Uganda status check error: {e}")
        return {"Status": "ERROR", "StatusMessage": str(e)}


# ================================================
# MAIN FUNCTION 3: Pay merchant (end of day payout)
# ================================================
async def disburse_to_merchant(
    phone: str,
    amount: int,
    merchant_name: str = "Merchant"
) -> dict:
    """
    Send end-of-day sales money to a merchant's mobile money wallet (§4).

    Uses Yo Uganda acwithdrawfunds. Money leaves your Yo Uganda float
    account and lands in the merchant's MTN/Airtel wallet.

    ⚠️ NOT PRODUCTION READY: per §4, withdraw requests must carry an
    RSA signature in <AuthenticationSignatureBase64>, generated with a
    private key whose public half is registered with Yo support. That
    is not implemented here, so live payouts will be rejected.

    Recommended flow (§4.1): debit the merchant's balance on your side
    FIRST, then call this; if Yo reports failure, reverse the debit.

    Args:
        phone         → merchant's MoMo e.g. "256700000001"
        amount        → daily sales total in UGX
        merchant_name → used in the payment narrative
    """

    # ── TEST MODE ──────────────────────────────
    if _is_test_mode():
        print(f"\n⚠️  TEST MODE — Yo Uganda fake payout (NO REAL MONEY SENT)")
        print(f"  Merchant: {merchant_name}")
        print(f"  Phone:    {phone}")
        print(f"  Amount:   UGX {amount:,}")
        return {
            "Status":            "OK",
            "TransactionStatus": "SUCCEEDED",
            "StatusMessage":     "TEST MODE — no real payout",
        }

    phone   = phone.strip().replace("+", "").replace(" ", "")
    ext_ref = str(uuid.uuid4())

    xml_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{YO_USERNAME}</APIUsername>
    <APIPassword>{YO_PASSWORD}</APIPassword>
    <Method>acwithdrawfunds</Method>
    <Amount>{amount}</Amount>
    <Account>{phone}</Account>
    <Narrative>Daily payout to {merchant_name}</Narrative>
    <ExternalReference>{ext_ref}</ExternalReference>
  </Request>
</AutoCreate>"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                YO_API_URL,
                content=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=30.0,
            )
        result = parse_yo_response(response.text)
        print(f"Yo Uganda payout: {result.get('Status')} — {merchant_name} — ref {ext_ref}")
        return result
    except Exception as e:
        print(f"Yo Uganda payout error: {e}")
        return {"Status": "ERROR", "StatusMessage": str(e)}