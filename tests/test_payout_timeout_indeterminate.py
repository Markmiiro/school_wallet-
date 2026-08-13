# ================================================
# tests/test_payout_timeout_indeterminate.py
# ------------------------------------------------
# Money-critical RED test — Challenge A.
#
# THE BUG
# A payout that TIMES OUT is in an UNKNOWN state: Yo may well have
# executed the disbursement and simply not answered us in time. The
# current code cannot tell that apart from a clean rejection:
#
#   app/momo.py:455-460   catches every exception (incl. httpx timeouts)
#                         and flattens it to {"Status": "ERROR"}
#   app/routes/reports.py:118-121  reads Status != "OK" and writes
#                         status="failed"
#   app/routes/reports.py:73       "failed" is NOT in the ("pending",
#                         "sent") skip set, so the next trigger reuses
#                         the row and calls Yo AGAIN → double payout
#
# It compounds: app/momo.py:404 mints a fresh uuid4() ExternalReference
# on every call, so the second request is unrecognisable to Yo as a
# duplicate of the first — Yo cannot dedupe it either.
#
# app/momo.py:375-379 already prescribes the mitigation in its own
# docstring ("use verify_transaction() ... to resolve INDETERMINATE
# cases"). reports.py never calls it.
#
# WHY THIS TEST EXISTS ALONGSIDE test_failed_payout_can_be_retried
# That test passes, and should: it simulates a CLEAN gateway rejection,
# where retrying is genuinely correct. It is testing the wrong failure
# mode for this bug. This file tests the other one.
#
# WHY IT DRIVES THE REAL momo.py CODE
# Patching app.routes.reports.disburse_to_merchant to raise would test
# a path that never happens in production — momo.py catches the timeout
# internally, so reports.py never sees an exception. To be honest about
# the bug the test must run momo.py's actual except-branch. That means
# taking momo.py out of test mode, which is done here with THREE
# independent guarantees that nothing reaches Yo:
#   1. momo.httpx is replaced wholesale (only momo's reference, not the
#      real httpx module) with a shim whose post() always raises.
#   2. momo.YO_API_URL is repointed at 127.0.0.1:9 (discard port), so
#      even a total patch failure cannot leave the machine.
#   3. Throwaway RSA key material only — the real private_key.pem /
#      YO_PRIVATE_KEY are never read (CLAUDE.md: "mock the signer").
# ================================================

import types
from datetime import date, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import momo
from app.models import Payout, Transaction
from tests.conftest import headers_for, make_admin, make_student_with_wallet


def _add_completed_payment(db_session, merchant, wallet, amount, when):
    db_session.add(Transaction(
        wallet_id=wallet.id,
        merchant_id=merchant.id,
        amount=amount,
        type="payment",
        status="completed",
        timestamp=when,
    ))
    db_session.commit()


@pytest.fixture()
def timing_out_yo(monkeypatch):
    """
    Take app.momo out of test mode so its REAL network-error handling
    runs, with every outbound request raising a genuine
    httpx.ReadTimeout. Records each attempted POST so a test can assert
    how many times we actually reached out to Yo.
    """
    attempts = {"posts": [], "payloads": []}

    class _TimingOutAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            attempts["posts"].append(url)
            attempts["payloads"].append(kwargs.get("content", ""))
            raise httpx.ReadTimeout(
                "simulated network timeout — request sent, response never arrived"
            )

    # Replace ONLY momo's reference to httpx, never the real module.
    monkeypatch.setattr(
        momo, "httpx", types.SimpleNamespace(AsyncClient=_TimingOutAsyncClient)
    )

    # Throwaway key material so sign_withdraw_request() succeeds and we
    # actually reach the HTTP call. Never the real key.
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
    # Belt-and-braces: discard port, unroutable to Yo by construction.
    monkeypatch.setattr(momo, "YO_API_URL", "http://127.0.0.1:9/yo-must-never-be-reached")

    return attempts


def test_timeout_during_payout_is_not_retried_as_a_clean_failure(
    client, db_session, school, parent_user, merchant, timing_out_yo,
):
    """
    Two triggers, where the first one TIMES OUT. Yo must be called
    exactly ONCE — the first payment's fate is unknown, and retrying it
    risks paying the merchant twice.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    _add_completed_payment(db_session, merchant, wallet, 7000, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999040")
    headers = headers_for(admin)

    # ── First trigger: the request reaches Yo, then times out ──
    res1 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res1.status_code == 200

    posts_after_first = len(timing_out_yo["posts"])
    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    status_after_first = row.status

    # Precondition — proves the test reached the network layer at all,
    # rather than bailing out early in signing (which would make the
    # call-count assertion below pass for entirely the wrong reason).
    assert posts_after_first == 1, (
        f"setup problem, not the bug under test: expected exactly 1 outbound "
        f"attempt on the first trigger, got {posts_after_first}. If this is 0, "
        f"sign_withdraw_request() failed and momo.py returned early without "
        f"ever calling Yo — fix the fixture before trusting this test."
    )

    # ── Second trigger: must NOT re-send ──
    res2 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res2.status_code == 200

    # Count WITHDRAWALS, not all traffic to Yo. The second trigger now
    # polls actransactioncheckstatus, which is a read and is the required
    # resolution path (withdrawals have no callbacks) — counting that as a
    # re-send would fail this test for the opposite of the reason it exists.
    withdrawals   = [p for p in timing_out_yo["payloads"] if "acwithdrawfunds" in p]
    status_checks = [p for p in timing_out_yo["payloads"] if "actransactioncheckstatus" in p]

    total_posts = len(withdrawals)
    refs = withdrawals

    assert total_posts == 1, (
        f"DOUBLE PAYOUT: Yo was called {total_posts} times for the same "
        f"(merchant={merchant.id}, date=today) after the first attempt timed "
        f"out.\n"
        f"  Payout row status after the timeout: {status_after_first!r}\n"
        f"  A timeout means UNKNOWN, not failed — the first disbursement may "
        f"have succeeded at Yo.\n"
        f"  Because the row was written {status_after_first!r} (not in the "
        f"skip set at reports.py:73), the second trigger reused it and "
        f"re-sent.\n"
        f"  Distinct ExternalReferences across the {total_posts} attempts: "
        f"{len(set(refs))} — a fresh uuid4() per call (momo.py:404) means Yo "
        f"cannot dedupe these either."
    )

    # The row must be parked as UNKNOWN, not failed — that status is what
    # keeps the next trigger from re-sending.
    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "indeterminate", (
        f"a timed-out payout must be recorded 'indeterminate', got "
        f"{row.status!r} — 'failed' invites a re-send of money that may "
        f"already have moved"
    )

    # The reference is deterministic AND was written before the send, so the
    # row names exactly what to poll even though the send never answered.
    expected_ref = f"SW-PAYOUT-{merchant.id}-{date.today():%Y%m%d}-1"
    assert row.yo_reference == expected_ref, (
        f"expected the deterministic attempt-1 reference {expected_ref!r} "
        f"recorded before the send, got {row.yo_reference!r}"
    )

    # Resolution must actually have been attempted — the whole point is that
    # the second trigger POLLS instead of re-sending.
    assert len(status_checks) == 1, (
        f"the second trigger must resolve by polling actransactioncheckstatus "
        f"(withdrawals have no callbacks), got {len(status_checks)} status checks"
    )

    # And the operator must be told it is unresolved, not told it failed.
    assert res2.json()["payouts_unresolved"] == 1, (
        f"an unresolved payout must be reported as unresolved, not failed: "
        f"{res2.json()}"
    )
    assert res2.json()["payouts_failed"] == 0
    assert res2.json()["total_paid_ugx"] == 0
