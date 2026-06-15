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
#    (acdepositfunds — asynchronous)
#
# 2. disburse_to_merchant → pay vendor at end of day
#    (acwithdrawfunds)
#
# DOCS: https://payments.yo.co.ug
# SANDBOX: https://sandbox.yo.co.ug
# SUPPORT: support@yo.co.ug | +256 788 238665
# ================================================

import httpx
import os
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

YO_USERNAME = os.getenv("YO_USERNAME", "")
YO_PASSWORD = os.getenv("YO_PASSWORD", "")
YO_API_URL  = os.getenv(
    "YO_API_URL",
    "https://sandbox.yo.co.ug/services/yopaymentsdev/"
)
YO_IPN_URL  = os.getenv(
    "YO_IPN_URL",
    "https://web-production-454a5.up.railway.app/webhook/yo"
)
APP_ENV = os.getenv("APP_ENV", "development")


# ================================================
# HELPER: Parse Yo Uganda XML response
# ================================================
def parse_yo_response(xml_text: str) -> dict:
    """
    Parse Yo Uganda XML response into a dict.

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

    Uses Yo Uganda acdepositfunds (asynchronous).
    Parent receives USSD prompt to approve with PIN.
    Yo Uganda calls your /webhook/yo when approved.

    Args:
        phone         → e.g. "256771234567"
        amount        → in UGX e.g. 20000
        network       → "MTN" or "AIRTEL" (informational)
        tx_ref        → your unique reference ID
        customer_name → parent name for the USSD prompt

    Returns:
        dict with Status, TransactionReference, etc.
    """

    # ── TEST MODE ──────────────────────────────
    if not YO_USERNAME or APP_ENV == "development":
        print(f"\nTEST MODE — Yo Uganda fake charge")
        print(f"  Phone:  {phone}")
        print(f"  Amount: UGX {amount:,}")
        print(f"  Ref:    {tx_ref}")
        return {
            "Status":               "OK",
            "StatusCode":           "0",
            "TransactionStatus":    "PENDING",
            "TransactionReference": tx_ref,
            "StatusMessage":        "TEST MODE — auto approved",
        }

    # ── Format phone ────────────────────────────
    phone = phone.strip().replace("+", "").replace(" ", "")

    # ── Build XML request ────────────────────────
    # Yo Uganda uses XML for all API calls
    xml_request = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <AutoCreate>
      <Request>
        <APIUsername>{YO_USERNAME}</APIUsername>
        <APIPassword>{YO_PASSWORD}</APIPassword>
        <Method>acdepositfunds</Method>
        <Amount>{amount}</Amount>
        <Account>{phone}</Account>
        <Currency>UGX</Currency>
        <Narrative>School Wallet top-up for {customer_name}</Narrative>
        <ExternalReference>{tx_ref}</ExternalReference>
        <ProviderReferenceText>{tx_ref}</ProviderReferenceText>
        <NonBlocking>TRUE</NonBlocking>
        <InstantPaymentNotificationURL>{YO_IPN_URL}</InstantPaymentNotificationURL>
      </Request>
    </AutoCreate>
    """.strip()

    # ── Send to Yo Uganda ────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                YO_API_URL,
                content=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=30.0,
            )

        result = parse_yo_response(response.text)
        status = result.get("Status", "ERROR")
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
    Check the current status of a transaction.

    Uses Yo Uganda actransactioncheckstatus API.
    Poll this every 15 seconds for pending transactions.

    Status values from Yo Uganda:
    PENDING       → waiting for parent to approve
    SUCCEEDED     → payment confirmed
    FAILED        → rejected or timed out
    INDETERMINATE → unknown — check again later
    """

    # ── TEST MODE ──────────────────────────────
    if not YO_USERNAME or APP_ENV == "development":
        return {
            "Status":            "OK",
            "TransactionStatus": "SUCCEEDED",
            "TransactionReference": tx_ref,
        }

    xml_request = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <AutoCreate>
      <Request>
        <APIUsername>{YO_USERNAME}</APIUsername>
        <APIPassword>{YO_PASSWORD}</APIPassword>
        <Method>actransactioncheckstatus</Method>
        <PrivateTransactionReference>{tx_ref}</PrivateTransactionReference>
      </Request>
    </AutoCreate>
    """.strip()

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
        return {"Status": "ERROR", "StatusMessage": str(e)}


# ================================================
# MAIN FUNCTION 3: Pay vendor (end of day payout)
# ================================================
async def disburse_to_merchant(
    phone: str,
    amount: int,
    merchant_name: str = "Merchant"
) -> dict:
    """
    Send end-of-day sales money to merchant's MoMo.

    Uses Yo Uganda acwithdrawfunds API.
    Money leaves your Yo Uganda float account
    and goes to the merchant's MTN/Airtel wallet.

    Args:
        phone         → merchant's MoMo e.g. "256700000001"
        amount        → daily sales total in UGX
        merchant_name → for the payment narrative
    """

    # ── TEST MODE ──────────────────────────────
    if not YO_USERNAME or APP_ENV == "development":
        print(f"\nTEST MODE — Yo Uganda fake payout")
        print(f"  Merchant: {merchant_name}")
        print(f"  Phone:    {phone}")
        print(f"  Amount:   UGX {amount:,}")
        return {
            "Status":            "OK",
            "TransactionStatus": "SUCCEEDED",
            "StatusMessage":     "TEST MODE — payout simulated",
        }

    phone = phone.strip().replace("+", "").replace(" ", "")

    import uuid
    ext_ref = str(uuid.uuid4())

    xml_request = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <AutoCreate>
      <Request>
        <APIUsername>{YO_USERNAME}</APIUsername>
        <APIPassword>{YO_PASSWORD}</APIPassword>
        <Method>acwithdrawfunds</Method>
        <Amount>{amount}</Amount>
        <Account>{phone}</Account>
        <Currency>UGX</Currency>
        <Narrative>Daily payout to {merchant_name}</Narrative>
        <ExternalReference>{ext_ref}</ExternalReference>
      </Request>
    </AutoCreate>
    """.strip()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                YO_API_URL,
                content=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=30.0,
            )
        result = parse_yo_response(response.text)
        print(f"Yo Uganda payout: {result.get('Status')} — {merchant_name}")
        return result
    except Exception as e:
        print(f"Yo Uganda payout error: {e}")
        return {"Status": "ERROR", "StatusMessage": str(e)}