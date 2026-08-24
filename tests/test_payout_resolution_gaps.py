# ================================================
# tests/test_payout_resolution_gaps.py
# ------------------------------------------------
# Two gaps at opposite ends of the same state machine. Both are about the
# one word that matters in app/routes/reports.py:
#
#     "failed" is the ONLY status that unlocks another send.
#
# GAP 1 (classifier, too eager to say failed)
#   A response that reached Yo but could not be read is classified
#   "failed" and becomes retryable. httpx does not raise for HTTP status,
#   and disburse_to_merchant() (momo.py:566-568) never checks
#   response.status_code — it parses whatever came back and sets
#   _Delivery="responded" unconditionally. A proxy 502 page or an empty
#   body therefore arrives with no TransactionStatus and no StatusCode
#   and falls through to `return "failed", msg or "Payout failed"`
#   (reports.py:244). The merchant may already have been paid.
#
# GAP 2 (resolver, too reluctant to say failed)
#   The mirror image. A definitive "transaction not found" from a status
#   check is a clean negative — Yo has no record of the reference — but
#   it arrives as Status="ERROR", so _resolve_payout() returns at its
#   early guard (reports.py:267) with outcome "unresolved" and never
#   consults the StatusCode below. The row sits "indeterminate" forever
#   and the merchant is never paid.
#
# Nothing here touches the network. Gap 2 uses the in-memory test db.
# ================================================

import asyncio
import types
from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import momo
from app.models import Payout
from app.routes import reports
from app.routes.reports import _classify_payout_result


# ================================================
# GAP 1 — unreadable response must not unlock a re-send
# ================================================

@pytest.fixture(scope="module")
def throwaway_key():
    """Generated per test run. Never the real YO_PRIVATE_KEY."""
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _responded(body: str, monkeypatch, throwaway_key) -> dict:
    """
    Drive the REAL disburse_to_merchant() against a stubbed transport and
    return what it hands the classifier.

    Deliberately not a hand-rolled mirror of momo.py's delivery marking:
    the marking is the thing under test, and a copy of it here would keep
    passing after the real one regressed. Nothing touches the network —
    momo.httpx is replaced and YO_API_URL points at the discard port.
    """
    class _Response:
        text = body

    class _StubAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            return _Response()

    monkeypatch.setattr(
        momo, "httpx", types.SimpleNamespace(AsyncClient=_StubAsyncClient)
    )
    monkeypatch.setattr(momo, "YO_PRIVATE_KEY", throwaway_key)
    monkeypatch.setattr(momo, "YO_USERNAME", "test-api-username")
    monkeypatch.setattr(momo, "YO_PASSWORD", "test-api-password")
    monkeypatch.setattr(momo, "APP_ENV", "production")  # leaves test mode
    monkeypatch.setattr(
        momo, "YO_API_URL", "http://127.0.0.1:9/yo-must-never-be-reached"
    )

    return asyncio.run(
        momo.disburse_to_merchant(
            phone="256700000002",
            amount=7000,
            merchant_name="Test Tuck Shop",
            external_reference="SW-PAYOUT-1-20260822-1",
            narrative="School Wallet payout Test Tuck Shop 2026-08-22",
        )
    )


NOT_A_YO_ANSWER = [
    pytest.param(
        "<html><head><title>502 Bad Gateway</title></head>"
        "<body><center>nginx</center></body></html>",
        id="proxy-502-html-page",
    ),
    # Closed by the _ParseFailed -> _DELIVERY_UNKNOWN marker in momo.py
    # (parse_yo_response + disburse_to_merchant). Before that, an empty
    # body reached the classifier disguised as a Yo-shaped ERROR and was
    # recorded "failed" — the one status that unlocks a re-send.
    pytest.param("", id="empty-body-connection-closed"),
    pytest.param(
        "<?xml version='1.0'?><AutoCreate><Response><Status>O",
        id="truncated-mid-response",
    ),
    pytest.param(
        "upstream connect error or disconnect/reset before headers",
        id="load-balancer-plain-text",
    ),
]


@pytest.mark.parametrize("body", NOT_A_YO_ANSWER)
def test_unreadable_response_must_not_unlock_a_resend(
    body, monkeypatch, throwaway_key
):
    """
    THE DOUBLE-PAYMENT CASE.

    The request left this process and reached something. Whether Yo
    processed it is unknown — which is the definition of indeterminate.
    Recording it "failed" tells _process_merchant_payout it may send the
    merchant's daily total a second time, with a bumped attempt number
    that Yo's duplicate net will not recognise.
    """
    status, reason = _classify_payout_result(
        _responded(body, monkeypatch, throwaway_key)
    )

    assert status != "failed", (
        f"an unreadable response classified {status!r} (reason: {reason!r}) — "
        f"'failed' is the one status that unlocks another send, and this "
        f"request may already have moved money"
    )
    assert status == "indeterminate", (
        f"expected 'indeterminate' (resolve by polling), got {status!r}"
    )


# ── Regression guards for gap 1: the retry path must survive a fix ──
# A genuinely failed payout only ever gets paid BECAUSE "failed" unlocks
# a re-send. Widening "indeterminate" too far strands real money.

def test_yo_confirmed_failed_is_still_failed(monkeypatch, throwaway_key):
    status, _ = _classify_payout_result(
        _responded(
            "<?xml version='1.0'?><AutoCreate><Response>"
            "<Status>OK</Status><StatusCode>0</StatusCode>"
            "<TransactionStatus>FAILED</TransactionStatus>"
            "</Response></AutoCreate>",
            monkeypatch, throwaway_key,
        )
    )
    assert status == "failed", "Yo-confirmed FAILED must remain retryable"


def test_known_failure_code_rejection_is_still_failed(monkeypatch, throwaway_key):
    status, _ = _classify_payout_result(
        _responded(
            "<?xml version='1.0'?><AutoCreate><Response>"
            "<Status>ERROR</Status><StatusCode>-13</StatusCode>"
            "<StatusMessage>Insufficient funds</StatusMessage>"
            "</Response></AutoCreate>",
            monkeypatch, throwaway_key,
        )
    )
    assert status == "failed", (
        "a Yo-shaped rejection carrying a known failure code must stay retryable"
    )


def test_signing_failure_is_still_failed():
    """not_sent: nothing left this process, so a retry is definitionally safe."""
    status, _ = _classify_payout_result(
        {"_Delivery": "not_sent", "StatusMessage": "Signing failed: no key"}
    )
    assert status == "failed"


# ================================================
# GAP 2 — a definitive not-found must resolve, not stall
# ================================================

def _unresolved_payout(db_session, merchant, amount=7000):
    row = Payout(
        merchant_id=merchant.id,
        payout_date=date.today(),
        amount=amount,
        status="indeterminate",
        yo_reference=f"SW-PAYOUT-{merchant.id}-{date.today():%Y%m%d}-1",
    )
    db_session.add(row)
    db_session.commit()
    return row


# Yo answered cleanly. This is not a network failure — it is a definitive
# negative: Yo holds no transaction under this reference, so the
# withdrawal never became a transaction and no money moved.
NOT_FOUND_RESPONSE = {
    "_Delivery": "responded",
    "Status": "ERROR",
    "StatusCode": "-30",
    "StatusMessage": "Transaction not found",
}


GAP_2_REASON = (
    "GAP 2 — OPEN, blocked on two unanswered questions about the Yo! "
    "Payments API v3.48 spec, which is not checked into this repo:\n"
    "  (1) Does StatusCode -30 on actransactioncheckstatus mean 'no such "
    "transaction' (a definitive negative, safe to re-send) or 'not found "
    "YET' (indeterminate, must keep polling)? Reading it as definitive "
    "when Yo means the latter re-sends money that is still in flight.\n"
    "  (2) Is PrivateTransactionReference the field Yo indexes OUR "
    "ExternalReference under? momo.py:418 polls with our SW-PAYOUT-... "
    "reference in that field. If it is Yo's own reference instead, every "
    "status check returns not-found — and 'not-found => failed => re-send' "
    "would re-send EVERY indeterminate payout in the table on the next "
    "sweep.\n"
    "Until both are confirmed, resolving not-found to 'failed' is a "
    "double-payment engine, not a fix. strict=True: this flips red the "
    "moment gap 2 is closed, so it cannot rot into a silent pass."
)


@pytest.mark.xfail(strict=True, reason=GAP_2_REASON)
def test_definitive_not_found_resolves_to_failed(db_session, merchant, monkeypatch):
    """
    A row whose reference Yo has never heard of is a row whose money never
    moved. It must resolve to "failed" so the payout can be re-sent.

    Today it hits the early guard at reports.py:267 (Status != "OK") and
    returns "unresolved" without ever consulting StatusCode -30 — which
    _classify_payout_result already maps to "failed" on the send path.
    The row then sits "indeterminate" forever: every sweep re-polls it,
    gets the same clean negative, and the merchant is never paid.
    """
    row = _unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        assert ref == row.yo_reference
        return dict(NOT_FOUND_RESPONSE)

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    out = asyncio.run(reports._resolve_payout(db_session, row))

    assert out["outcome"] == "failed", (
        f"got {out!r} — a definitive not-found leaves this row unresolvable "
        f"by any automated path, so the merchant is never paid"
    )

    db_session.expire_all()
    assert db_session.get(Payout, row.id).status == "failed", (
        "the row must be written down as failed, or the next trigger will "
        "skip it again"
    )


@pytest.mark.xfail(strict=True, reason=GAP_2_REASON)
def test_not_found_row_becomes_retryable_by_the_trigger(
    db_session, merchant, monkeypatch
):
    """
    The point of resolving to "failed" is that _process_merchant_payout
    reuses a failed row for a re-send. This asserts the end state the
    payout trigger actually keys off, not just the return value.
    """
    row = _unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        return dict(NOT_FOUND_RESPONSE)

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    asyncio.run(reports._resolve_payout(db_session, row))

    db_session.expire_all()
    resolved = db_session.get(Payout, row.id)
    assert resolved.status not in ("pending", "sent", "indeterminate"), (
        f"status {resolved.status!r} is not a state the payout trigger will "
        f"ever re-send from"
    )


# ── Regression guard for gap 2: a fix must not read every non-OK
#    status check as a licence to re-send. ──

def test_unreachable_status_check_still_resolves_nothing(
    db_session, merchant, monkeypatch
):
    """
    The opposite error. A status check that could not be delivered tells
    us nothing at all, and must never be mistaken for a clean negative —
    that inversion is how you pay twice.
    """
    row = _unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        return {
            "Status": "ERROR",
            "StatusMessage": "timeout",
            "_Delivery": "unknown",
        }

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    out = asyncio.run(reports._resolve_payout(db_session, row))

    assert out["outcome"] == "unresolved"
    db_session.expire_all()
    assert db_session.get(Payout, row.id).status == "indeterminate"
