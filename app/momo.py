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
#    (acwithdrawfunds — RSA-signed, see below)
#
# DOCS: https://payments.yo.co.ug
# SANDBOX: https://sandbox.yo.co.ug
# SUPPORT: support@yo.co.ug | +256 788 238665
#
# ------------------------------------------------
# FIELD NAMES — from Yo! Payments API Specification
# v3.48 §4.1 and §6.1.1. These must match EXACTLY.
# Yo silently ignores unrecognised fields, so a typo
# means callbacks never fire and transactions sit
# PENDING forever with no error anywhere.
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
# Railway → Variables.
#
# ------------------------------------------------
# WITHDRAW SIGNING (§4.1)
#
# Yo only processes acwithdrawfunds requests signed with
# YOUR private key, once your public key is registered on
# your Yo account profile.
#
# Setup (one time):
#   openssl genpkey -algorithm RSA -out private_key.pem \
#       -pkeyopt rsa_keygen_bits:2048
#   openssl rsa -pubout -in private_key.pem -out public_key.pem
#
#   1. Send ONLY public_key.pem to your Yo account rep.
#   2. Put the contents of private_key.pem in the
#      YO_PRIVATE_KEY environment variable (Railway).
#   3. NEVER commit private_key.pem — keep it in .gitignore.
#      Do not share it with anyone, including Yo support.
#
# Signature construction, in this exact order:
#   concat = APIUsername + Amount + Account + Narrative
#            + ExternalReference + PublicKeyAuthenticationNonce
#   sig    = base64( RSA_sign_SHA1( SHA1_hex(concat) ) )
#
# Narrative / ExternalReference / Nonce are each truncated
# to their first 255 chars before concatenation.
# ================================================

import base64
import hashlib
import httpx
import os
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

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

# PEM-encoded RSA private key used to sign withdraw requests.
# Set this in Railway → Variables. Never commit the key itself.
YO_PRIVATE_KEY = os.getenv("YO_PRIVATE_KEY", "")

APP_ENV = os.getenv("APP_ENV", "development")

# Per §4.1, these fields are truncated before signing.
_SIGNATURE_FIELD_MAX = 255


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


def _generate_nonce() -> str:
    """
    PublicKeyAuthenticationNonce (§4.1).

    MUST be unique for every single API call — including calls that
    fail. Deliberately NOT derived from any transaction reference,
    since those can be retried or reused. Alphanumeric only, well
    under the 255-char limit.
    """
    return uuid.uuid4().hex


def sign_withdraw_request(
    amount: int,
    account: str,
    narrative: str,
    external_reference: str,
    nonce: str,
) -> str:
    """
    Build PublicKeyAuthenticationSignatureBase64 for acwithdrawfunds (§4.1).

    Concatenation order is fixed by the spec and must not change:
        APIUsername, Amount, Account, Narrative,
        ExternalReference, PublicKeyAuthenticationNonce

    The signed payload is the SHA1 *hex digest* of that string, which is
    then RSA-signed using SHA1 as the signature algorithm (§4.2.3), and
    base64-encoded. This mirrors how app/routes/ussd.py verifies Yo's
    own signatures, and how Yo's reference PHP library signs.

    Raises RuntimeError if YO_PRIVATE_KEY is not configured.
    """
    if not YO_PRIVATE_KEY:
        raise RuntimeError(
            "YO_PRIVATE_KEY is not set. Withdraw requests cannot be signed. "
            "Generate a keypair (see header of this file), register the "
            "public key with Yo Uganda, and set the private key in Railway."
        )

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    concat = (
        f"{YO_USERNAME}"
        f"{amount}"
        f"{account}"
        f"{narrative[:_SIGNATURE_FIELD_MAX]}"
        f"{external_reference[:_SIGNATURE_FIELD_MAX]}"
        f"{nonce[:_SIGNATURE_FIELD_MAX]}"
    )

    sha1_hex = hashlib.sha1(concat.encode("utf-8")).hexdigest()  # noqa: S324

    private_key = serialization.load_pem_private_key(
        YO_PRIVATE_KEY.encode("utf-8"),
        password=None,
    )
    signature = private_key.sign(
        sha1_hex.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA1(),  # noqa: S303 — required by Yo Uganda spec §4.2.3
    )
    return base64.b64encode(signature).decode("utf-8")


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

    narrative = f"School Wallet top-up for {customer_name}"

    # ── Build XML request (§6.1.1) ───────────────
    xml_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{xml_escape(YO_USERNAME)}</APIUsername>
    <APIPassword>{xml_escape(YO_PASSWORD)}</APIPassword>
    <Method>acdepositfunds</Method>
    <NonBlocking>TRUE</NonBlocking>
    <Amount>{amount}</Amount>
    <Account>{phone}</Account>
    <Narrative>{xml_escape(narrative)}</Narrative>
    <ExternalReference>{xml_escape(tx_ref)}</ExternalReference>
    <ProviderReferenceText>{xml_escape(tx_ref)}</ProviderReferenceText>
    <InstantNotificationUrl>{xml_escape(YO_IPN_URL)}</InstantNotificationUrl>
    <FailureNotificationUrl>{xml_escape(YO_FAILURE_URL)}</FailureNotificationUrl>
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
    <APIUsername>{xml_escape(YO_USERNAME)}</APIUsername>
    <APIPassword>{xml_escape(YO_PASSWORD)}</APIPassword>
    <Method>actransactioncheckstatus</Method>
    <PrivateTransactionReference>{xml_escape(tx_ref)}</PrivateTransactionReference>
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

    Signed with your private key per §4.1 — Yo will reject unsigned
    requests once public key authentication is enabled on your account.

    IMPORTANT (§4.1 guidance): debit the merchant's balance on YOUR side
    first, then call this. If Yo reports failure, reverse that debit.
    Do not credit-on-success only, or a network error mid-call leaves
    you unable to tell whether the money moved — use verify_transaction()
    with the returned reference to resolve INDETERMINATE cases.

    Args:
        phone         → merchant's MoMo e.g. "256700000001"
        amount        → daily sales total in UGX
        merchant_name → used in the payment narrative

    Returns:
        dict with Status, TransactionStatus, and (on success) the
        ExternalReference we generated, so the caller can reconcile.
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

    phone     = phone.strip().replace("+", "").replace(" ", "")
    ext_ref   = str(uuid.uuid4())
    nonce     = _generate_nonce()
    narrative = f"Daily payout to {merchant_name}"

    # ── Sign the request (§4.1) ──────────────────
    try:
        signature = sign_withdraw_request(
            amount=amount,
            account=phone,
            narrative=narrative,
            external_reference=ext_ref,
            nonce=nonce,
        )
    except Exception as e:
        # Do NOT send an unsigned request — Yo would reject it anyway,
        # and a clear error here is easier to diagnose than a -x code.
        print(f"Yo Uganda payout signing error: {e}")
        return {"Status": "ERROR", "StatusMessage": f"Signing failed: {e}"}

    xml_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<AutoCreate>
  <Request>
    <APIUsername>{xml_escape(YO_USERNAME)}</APIUsername>
    <APIPassword>{xml_escape(YO_PASSWORD)}</APIPassword>
    <Method>acwithdrawfunds</Method>
    <Amount>{amount}</Amount>
    <Account>{phone}</Account>
    <Narrative>{xml_escape(narrative)}</Narrative>
    <ExternalReference>{xml_escape(ext_ref)}</ExternalReference>
    <PublicKeyAuthenticationNonce>{xml_escape(nonce)}</PublicKeyAuthenticationNonce>
    <PublicKeyAuthenticationSignatureBase64>{signature}</PublicKeyAuthenticationSignatureBase64>
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
        result.setdefault("ExternalReference", ext_ref)

        print(
            f"Yo Uganda payout: {result.get('Status')} "
            f"({result.get('TransactionStatus', 'n/a')}) — "
            f"{merchant_name} — ref {ext_ref}"
        )
        return result
    except Exception as e:
        print(f"Yo Uganda payout error: {e}")
        return {
            "Status":            "ERROR",
            "StatusMessage":     str(e),
            "ExternalReference": ext_ref,
        }