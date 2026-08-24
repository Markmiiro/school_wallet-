# ================================================
# tests/test_yo_api_conformance.py
# ------------------------------------------------
# Covers three fixes applied against the Yo! Payments API spec, all of
# which are invisible in normal operation and only show up as money that
# silently does not move:
#
#   A. §3.2.1 request headers — Yo requires Content-Type: text/xml AND
#      Content-transfer-encoding: text on EVERY request. We were sending
#      application/xml and no transfer-encoding. Adding both is what
#      produced our first successful sandbox call.
#
#   B. The YO_API_URL fallback was missing the /task.php path segment, so
#      it addressed a host that answers but routes nothing. Worse, because
#      _is_test_mode() suppresses all HTTP anywhere but production, that
#      fallback is reachable ONLY in production — where it would post real
#      credentials to sandbox and return plausible success while no money
#      moved. It must now hard-fail at import instead.
#
#   D. The numeric StatusCode was parsed and then thrown away everywhere.
#      It is the field that distinguishes "Yo refused a duplicate, an
#      earlier payout exists" from "nothing happened, send again" — the
#      two cases that differ by one merchant being paid twice.
#
# Nothing here touches the network: momo.httpx is replaced with a shim and
# YO_API_URL is repointed at the discard port, the same two guarantees
# used by test_payout_timeout_indeterminate.py.
# ================================================

import asyncio
import types
from datetime import date, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import momo
from app.routes import reports
from app.models import Payout, Transaction
from tests.conftest import headers_for, make_admin, make_student_with_wallet


# ================================================
# A. §3.2.1 HEADERS
# ================================================

REQUIRED_YO_HEADERS = {
    "Content-Type": "text/xml",
    "Content-transfer-encoding": "text",
}


@pytest.fixture()
def recording_yo(monkeypatch):
    """
    Take app.momo out of test mode so the real HTTP branch runs, and record
    every outbound request's URL, headers and body. The response body is
    settable per-test so StatusCode handling can be driven from here too.
    """
    calls = []
    box = {"body": "<?xml version='1.0'?><AutoCreate><Response>"
                   "<Status>OK</Status><StatusCode>0</StatusCode>"
                   "<TransactionStatus>PENDING</TransactionStatus>"
                   "</Response></AutoCreate>"}

    class _Response:
        def __init__(self, text):
            self.text = text

    class _RecordingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            calls.append({
                "url": url,
                "headers": kwargs.get("headers") or {},
                "body": kwargs.get("content", ""),
            })
            return _Response(box["body"])

    monkeypatch.setattr(
        momo, "httpx", types.SimpleNamespace(AsyncClient=_RecordingAsyncClient)
    )

    private_pem = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    monkeypatch.setattr(momo, "YO_PRIVATE_KEY", private_pem)
    monkeypatch.setattr(momo, "YO_USERNAME", "test-api-username")
    monkeypatch.setattr(momo, "YO_PASSWORD", "test-api-password")
    monkeypatch.setattr(momo, "APP_ENV", "production")  # leaves test mode
    monkeypatch.setattr(
        momo, "YO_API_URL", "http://127.0.0.1:9/yo-must-never-be-reached"
    )

    return types.SimpleNamespace(calls=calls, box=box)


def _assert_spec_headers(call, method_name):
    sent = {k.lower(): v for k, v in call["headers"].items()}
    for key, value in REQUIRED_YO_HEADERS.items():
        assert sent.get(key.lower()) == value, (
            f"{method_name} was sent without the §3.2.1 header "
            f"{key}: {value!r} (got {sent.get(key.lower())!r}). Yo does not "
            f"error on this — it just never parses the request, so the call "
            f"appears to work and no money moves.\nHeaders sent: {call['headers']}"
        )


def test_charge_mobile_money_sends_spec_headers(recording_yo):
    asyncio.run(momo.charge_mobile_money("256700000001", 5000, tx_ref="T-1"))
    assert len(recording_yo.calls) == 1
    _assert_spec_headers(recording_yo.calls[0], "acdepositfunds")


def test_verify_transaction_sends_spec_headers(recording_yo):
    asyncio.run(momo.verify_transaction("SW-PAYOUT-1-20260821-1"))
    assert len(recording_yo.calls) == 1
    _assert_spec_headers(recording_yo.calls[0], "actransactioncheckstatus")


def test_disburse_to_merchant_sends_spec_headers(recording_yo):
    asyncio.run(momo.disburse_to_merchant(
        "256700000002", 7000, external_reference="SW-PAYOUT-1-20260821-1"
    ))
    assert len(recording_yo.calls) == 1
    _assert_spec_headers(recording_yo.calls[0], "acwithdrawfunds")


def test_all_three_call_sites_share_one_header_dict():
    """
    Guards against the fix rotting one call site at a time: a future edit
    that inlines headers at one of the three POSTs would drift from the
    other two silently.
    """
    import inspect
    source = inspect.getsource(momo)
    assert source.count("headers=_YO_HEADERS") == 3, (
        "expected all three Yo POSTs to use the shared _YO_HEADERS dict"
    )
    assert momo._YO_HEADERS == REQUIRED_YO_HEADERS


# ================================================
# B. URL + PRODUCTION GUARD
# ================================================

def test_sandbox_fallback_url_includes_task_php():
    assert momo._YO_SANDBOX_API_URL == (
        "https://sandbox.yo.co.ug/services/yopaymentsdev/task.php"
    ), (
        "the sandbox endpoint without /task.php resolves to a host that "
        "answers but routes nothing to the payments service"
    )


def test_import_hard_fails_when_production_has_no_yo_api_url(monkeypatch):
    """
    Re-import app.momo with APP_ENV=production and YO_API_URL unset. The
    module must refuse to load rather than quietly aiming production
    traffic — with real credentials — at the sandbox.
    """
    import importlib
    import sys

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("YO_API_URL", raising=False)
    # load_dotenv() at import time must not be allowed to put YO_API_URL back.
    monkeypatch.setattr(momo, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "app.momo", momo)

    spec = importlib.util.find_spec("app.momo")
    fresh = importlib.util.module_from_spec(spec)
    # Neutralise dotenv inside the fresh module too.
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

    with pytest.raises(RuntimeError) as exc:
        spec.loader.exec_module(fresh)

    assert "YO_API_URL" in str(exc.value)


def test_non_production_still_gets_the_sandbox_fallback(monkeypatch):
    import importlib
    import sys
    import dotenv

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("YO_API_URL", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

    spec = importlib.util.find_spec("app.momo")
    fresh = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "app.momo", momo)
    spec.loader.exec_module(fresh)

    assert fresh.YO_API_URL == fresh._YO_SANDBOX_API_URL


# ================================================
# D. StatusCode MAPPING
# ================================================
#
# The classifier's output vocabulary is the whole point: only "failed"
# unlocks another send. Every case below is really asking "does this
# response permit us to send money again?"

def _resp(**kw):
    base = {"_Delivery": "responded"}
    base.update(kw)
    return base


@pytest.mark.parametrize("code,expected", [
    ("6",   "sent"),
    ("4",   "sent"),
    ("-30", "failed"),
    ("-13", "failed"),
    ("-21", "failed"),
    ("-8",  "indeterminate"),
    ("-6",  "indeterminate"),
    ("-22", "indeterminate"),
    ("3",   "indeterminate"),
    ("5",   "indeterminate"),
    ("9",   "indeterminate"),
])
def test_status_code_alone_maps_correctly_on_request_level_rejection(code, expected):
    """
    No TransactionStatus at all = Yo threw the request out before it ever
    became a transaction. This is the ONLY context where a numeric code may
    produce "failed" by itself.
    """
    status, reason = reports._classify_payout_result(
        _resp(Status="ERROR", StatusCode=code, StatusMessage="whatever")
    )
    assert status == expected, f"StatusCode {code} → {status!r}, expected {expected!r}"
    assert code in reason


def test_duplicate_rejection_is_indeterminate_not_failed():
    """
    THE double-payment case. -8/-6 mean an EARLIER identical withdrawal
    already exists at Yo. Recording that "failed" and retrying with a fresh
    reference tuple pays the merchant twice.
    """
    for code in ("-8", "-6"):
        status, reason = reports._classify_payout_result(
            _resp(Status="ERROR", StatusCode=code)
        )
        assert status == "indeterminate", (
            f"StatusCode {code} (duplicate) classified {status!r} — 'failed' "
            f"would unlock a re-send of money that already moved"
        )


def test_code_3_deviates_from_spec_and_stays_indeterminate():
    """
    DELIBERATE DEVIATION (approved): Yo says consider code 3 FAILED. We
    poll instead. Being wrong slow costs one cycle; being wrong fast costs
    a duplicate payout.
    """
    status, _ = reports._classify_payout_result(_resp(Status="ERROR", StatusCode="3"))
    assert status == "indeterminate"


def test_code_22_never_resubmits():
    status, reason = reports._classify_payout_result(
        _resp(Status="ERROR", StatusCode="-22")
    )
    assert status == "indeterminate"
    assert "RE-SUBMIT" in reason or "authorization" in reason


def test_unmapped_status_code_fails_closed():
    status, _ = reports._classify_payout_result(
        _resp(Status="ERROR", StatusCode="-999")
    )
    assert status == "indeterminate", "an unknown code must not unlock a re-send"


@pytest.mark.parametrize("code", ["-30", "-13", "-21"])
def test_status_code_may_never_turn_a_live_transaction_into_failed(code):
    """
    THE HARD RULE. A failure code arriving ALONGSIDE a TransactionStatus is
    commentary on an attempt that exists — Yo disagreeing with itself. It
    must degrade to indeterminate, never to the one status that permits
    sending money again.
    """
    for tx in ("PENDING", "INDETERMINATE", "SUCCEEDED"):
        status, reason = reports._classify_payout_result(
            _resp(Status="OK", TransactionStatus=tx, StatusCode=code)
        )
        assert status == "indeterminate", (
            f"TransactionStatus={tx} + StatusCode={code} → {status!r}; a code "
            f"may only produce 'failed' when there is no TransactionStatus"
        )
        assert "not retried" in reason


def test_confirmed_failed_plus_failure_code_stays_failed():
    """The one agreement case: Yo said FAILED and the code agrees."""
    status, _ = reports._classify_payout_result(
        _resp(Status="OK", TransactionStatus="FAILED", StatusCode="-13")
    )
    assert status == "failed"


@pytest.mark.parametrize("code", ["4", "6"])
def test_success_codes_promote_a_pending_send(code):
    """
    4 and 6 both mean the money moved (6: balance lagging; 4: Yo's own
    guidance is to consider it SUCCEEDED). Promoting to "sent" is safe by
    construction — "sent" is not a status that permits another send.
    """
    status, reason = reports._classify_payout_result(
        _resp(Status="OK", TransactionStatus="PENDING", StatusCode=code)
    )
    assert status == "sent"
    assert code in reason


def test_status_code_zero_leaves_transaction_status_untouched():
    for tx, expected in [
        ("SUCCEEDED", "sent"),
        ("FAILED", "failed"),
        ("PENDING", "indeterminate"),
        ("INDETERMINATE", "indeterminate"),
    ]:
        status, _ = reports._classify_payout_result(
            _resp(Status="OK", TransactionStatus=tx, StatusCode="0")
        )
        assert status == expected, f"StatusCode 0 must not perturb {tx}"


def test_delivery_markers_still_outrank_status_code():
    """StatusCode must not override what we know about delivery."""
    assert reports._classify_payout_result(
        {"_Delivery": "not_sent", "StatusCode": "6", "StatusMessage": "sign fail"}
    )[0] == "failed"
    assert reports._classify_payout_result(
        {"_Delivery": "unknown", "StatusCode": "-13", "StatusMessage": "timeout"}
    )[0] == "indeterminate"


# ================================================
# D (cont). The same refinement inside _resolve_payout
# ================================================

def _make_unresolved_payout(db_session, merchant, amount=7000):
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


def test_resolve_payout_promotes_on_success_code(
    db_session, merchant, monkeypatch
):
    row = _make_unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        return {
            "_Delivery": "responded",
            "Status": "OK",
            "TransactionStatus": "PENDING",
            "StatusCode": "6",
        }

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    out = asyncio.run(reports._resolve_payout(db_session, row))

    assert out["outcome"] == "sent"
    db_session.expire_all()
    assert db_session.get(Payout, row.id).status == "sent"


def test_resolve_payout_never_unlocks_resend_on_a_failure_code(
    db_session, merchant, monkeypatch
):
    """
    A status check that comes back PENDING with a failure code resolves
    NOTHING. If this ever returned "failed" the caller would re-send.
    """
    row = _make_unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        return {
            "_Delivery": "responded",
            "Status": "OK",
            "TransactionStatus": "PENDING",
            "StatusCode": "-13",
        }

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    out = asyncio.run(reports._resolve_payout(db_session, row))

    assert out["outcome"] == "unresolved", (
        f"got {out!r} — only a Yo-confirmed FAILED may unlock a re-send"
    )
    db_session.expire_all()
    assert db_session.get(Payout, row.id).status == "indeterminate"


def test_resolve_payout_still_confirms_a_clean_failure(
    db_session, merchant, monkeypatch
):
    """The retry path must survive the refinement — this is the only way a
    genuinely failed payout ever gets paid."""
    row = _make_unresolved_payout(db_session, merchant)

    async def fake_verify(ref):
        return {
            "_Delivery": "responded",
            "Status": "OK",
            "TransactionStatus": "FAILED",
            "StatusCode": "0",
        }

    monkeypatch.setattr(reports, "verify_transaction", fake_verify)
    out = asyncio.run(reports._resolve_payout(db_session, row))

    assert out["outcome"] == "failed"
    db_session.expire_all()
    assert db_session.get(Payout, row.id).status == "failed"


# ================================================
# END-TO-END: a duplicate rejection must not become a second payout
# ================================================

def test_duplicate_status_code_end_to_end_does_not_double_send(
    client, db_session, school, parent_user, merchant, recording_yo,
):
    """
    Yo answers the withdrawal with StatusCode -8 (duplicate). The row must
    park as indeterminate and the next trigger must poll, not re-send.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    db_session.add(Transaction(
        wallet_id=wallet.id, merchant_id=merchant.id, amount=7000,
        type="payment", status="completed", timestamp=datetime.utcnow(),
    ))
    db_session.commit()

    recording_yo.box["body"] = (
        "<?xml version='1.0'?><AutoCreate><Response>"
        "<Status>ERROR</Status><StatusCode>-8</StatusCode>"
        "<StatusMessage>Duplicate transaction</StatusMessage>"
        "</Response></AutoCreate>"
    )

    admin = make_admin(db_session, school, phone="256700999041")
    headers = headers_for(admin)

    res1 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res1.status_code == 200

    withdrawals = [c for c in recording_yo.calls if "acwithdrawfunds" in c["body"]]
    assert len(withdrawals) == 1

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "indeterminate", (
        f"a duplicate rejection recorded {row.status!r} — 'failed' would let "
        f"the next trigger send a SECOND payout for money Yo already holds a "
        f"withdrawal for"
    )

    # Second trigger: poll only.
    res2 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res2.status_code == 200

    withdrawals = [c for c in recording_yo.calls if "acwithdrawfunds" in c["body"]]
    assert len(withdrawals) == 1, (
        f"DOUBLE PAYOUT: {len(withdrawals)} withdrawals sent for one "
        f"(merchant, date) after a duplicate rejection"
    )
