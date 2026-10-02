# ================================================
# tests/test_payout_attempt_cap.py
# ------------------------------------------------
# Money-critical — the guards on automated payout re-sending.
#
# THE RULE BEING TESTED
# The unattended cron may re-send a payout ONLY when Yo has explicitly
# confirmed the previous attempt FAILED (money definitively did not move),
# and only up to _MAX_AUTOMATED_ATTEMPTS times per (merchant, payout_date).
# Past that the row is left alone and surfaced as needing a human. It may
# NEVER re-send on "indeterminate", at any attempt number.
#
# WHY A CAP AT ALL
# Every automated re-send is safe in isolation — Yo said the money did not
# move. But a merchant whose sends keep failing has something wrong that
# retrying will not fix (dead MoMo number, blocked account), and an
# uncapped cron would keep hammering it every day forever with nobody
# looking. The cap converts an invisible loop into a visible exception.
#
# WHAT WOULD FAIL BEFORE THE FIX
# test_automated_path_stops_after_the_attempt_cap asserts exactly 3
# outbound sends across 4 triggers. Pre-fix there was no cap and no
# attempt counter, so the 4th trigger re-sent and the count was 4.
#
# The attempt counter is derived from Payout.yo_reference (which is why
# these tests assert on the reference format too) so that the cap needed
# no new column on the live payouts table.
# ================================================

from datetime import date, datetime

import pytest
from sqlalchemy import text

from app.models import Payout, Transaction
from app.routes import reports
from tests.conftest import headers_for, make_admin, make_student_with_wallet

SETTLEMENT_SECRET = "test-only-settlement-secret"

# Hardcoded rather than read off reports._MAX_AUTOMATED_ATTEMPTS, so these
# tests fail on the BEHAVIOUR (four triggers sent four times) rather than on
# the constant being absent, and so a silent change to the cap is caught.
EXPECTED_CAP = 3


def _add_completed_payment(db_session, merchant, wallet, amount):
    db_session.add(Transaction(
        wallet_id=wallet.id,
        merchant_id=merchant.id,
        amount=amount,
        type="payment",
        status="completed",
        timestamp=datetime.utcnow(),
    ))
    db_session.commit()


@pytest.fixture()
def settlement_secret(monkeypatch):
    """
    Set SETTLEMENT_SECRET explicitly rather than leaning on reports.py's
    hardcoded fallback (which is in git history and must be treated as
    public — see CLAUDE.md).
    """
    monkeypatch.setenv("SETTLEMENT_SECRET", SETTLEMENT_SECRET)
    return SETTLEMENT_SECRET


@pytest.fixture()
def spy_disburse(monkeypatch):
    """
    Replace disburse_to_merchant with a spy that records every send and
    returns whatever the test queues up. Nothing reaches Yo.

    Default response is a Yo-confirmed FAILED — the one and only status
    that permits a re-send.
    """
    state = {
        "calls": [],
        "response": {
            "Status":            "OK",
            "TransactionStatus": "FAILED",
            "StatusMessage":     "simulated Yo-confirmed failure",
            "_Delivery":         "responded",
        },
    }

    async def fake_disburse(**kwargs):
        state["calls"].append(kwargs)
        result = dict(state["response"])
        result.setdefault("ExternalReference", kwargs.get("external_reference"))
        return result

    monkeypatch.setattr("app.routes.reports.disburse_to_merchant", fake_disburse)
    return state


def _refs(state):
    # .get, not [], so a failure message stays readable even when the code
    # under test never passed an external_reference at all.
    return [c.get("external_reference") for c in state["calls"]]


# ================================================
# THE CAP
# ================================================
def test_cap_constant_matches_what_these_tests_assume():
    """
    Kept separate from the behavioural tests on purpose: if this assertion
    lived inside them it would trip first and mask the real failure message
    (how many times the payout actually went out).
    """
    assert reports._MAX_AUTOMATED_ATTEMPTS == EXPECTED_CAP, (
        f"these tests are written against a cap of {EXPECTED_CAP}; the code "
        f"now says {reports._MAX_AUTOMATED_ATTEMPTS}"
    )


def test_automated_path_stops_after_the_attempt_cap(
    client, db_session, school, parent_user, merchant, settlement_secret, spy_disburse,
):
    """
    Four cron triggers, every send confirmed FAILED by Yo. Sends must stop
    at the cap, and the row must be surfaced as needing a human rather than
    quietly retried forever.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    _add_completed_payment(db_session, merchant, wallet, 4000)

    admin = make_admin(db_session, school, phone="256700999050")
    headers = headers_for(admin)

    responses = []
    for _ in range(4):
        res = client.post(
            f"/reports/settlements/auto?secret={settlement_secret}", headers=headers
        )
        assert res.status_code == 200
        responses.append(res.json())

    cap = EXPECTED_CAP

    assert len(spy_disburse["calls"]) == cap, (
        f"the automated path sent {len(spy_disburse['calls'])} times across 4 "
        f"triggers; the cap is {cap}. An uncapped cron re-sends to a broken "
        f"MoMo number every day with nobody watching.\n"
        f"  references used: {_refs(spy_disburse)}"
    )

    # Each retry must use a FRESH tuple. Yo rejects a withdrawal whose
    # (msisdn, amount, narrative, external reference) all four match an
    # earlier one, so a reused reference would get the legitimate retry
    # bounced as a duplicate.
    today = date.today()
    assert _refs(spy_disburse) == [
        f"SW-PAYOUT-{merchant.id}-{today:%Y%m%d}-{n}" for n in range(1, cap + 1)
    ], f"expected one deterministic reference per attempt, got {_refs(spy_disburse)}"

    # ...while narrative and amount stay byte-identical across attempts, so
    # an accidental re-send of the SAME attempt is still a Yo duplicate.
    narratives = {c["narrative"] for c in spy_disburse["calls"]}
    assert narratives == {f"School Wallet payout {merchant.name} {today.isoformat()}"}, (
        f"narrative must be stable and contain nothing volatile, got {narratives}"
    )
    assert {c["amount"] for c in spy_disburse["calls"]} == {4000}

    # The capped trigger must leave the row alone, not park it somewhere new.
    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "failed"
    assert row.yo_reference == f"SW-PAYOUT-{merchant.id}-{today:%Y%m%d}-{cap}"

    # And the cron output must say a human is needed.
    last = responses[-1]
    school_block = last["results"][0]
    assert school_block["needs_human"] == [merchant.name], (
        f"a capped payout must be surfaced as needing a human, got {school_block}"
    )
    assert last["grand_total_ugx"] == 0
    statuses = [p["status"] for p in school_block["payouts"]]
    assert statuses == ["needs_human"], (
        f"'needs_human' must not be flattened into 'failed' — an operator who "
        f"reads 'failed' re-triggers. got {statuses}"
    )


def test_manual_trigger_is_not_capped(
    client, db_session, school, parent_user, merchant, settlement_secret, spy_disburse,
):
    """
    The cap is a guard on unattended repetition, not a hard stop on the
    money. Once a human is looking at the row, they can still send.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    _add_completed_payment(db_session, merchant, wallet, 4000)

    admin = make_admin(db_session, school, phone="256700999051")
    headers = headers_for(admin)

    cap = EXPECTED_CAP

    # Burn through the automated cap.
    for _ in range(cap + 1):
        client.post(
            f"/reports/settlements/auto?secret={settlement_secret}", headers=headers
        )
    assert len(spy_disburse["calls"]) == cap

    # A human triggers it. This one goes out.
    spy_disburse["response"] = {
        "Status":            "OK",
        "TransactionStatus": "SUCCEEDED",
        "StatusMessage":     "simulated success",
        "_Delivery":         "responded",
    }
    res = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res.status_code == 200
    assert res.json()["total_paid_ugx"] == 4000

    assert len(spy_disburse["calls"]) == cap + 1, (
        "the manual admin trigger must not be subject to the automated cap"
    )
    today = date.today()
    assert _refs(spy_disburse)[-1] == f"SW-PAYOUT-{merchant.id}-{today:%Y%m%d}-{cap + 1}"

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "sent"


# ================================================
# THE OTHER HALF OF THE RULE: never re-send on unknown
# ================================================
def test_automated_path_never_resends_an_indeterminate_payout(
    client, db_session, school, parent_user, merchant, settlement_secret, spy_disburse,
):
    """
    An unresolved payout must not be re-sent by the cron at ANY attempt
    number — the cap is irrelevant here, because the money may already have
    moved. The row resolves by polling or not at all.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    _add_completed_payment(db_session, merchant, wallet, 4000)

    admin = make_admin(db_session, school, phone="256700999052")
    headers = headers_for(admin)

    spy_disburse["response"] = {
        "Status":            "OK",
        "TransactionStatus": "INDETERMINATE",
        "StatusMessage":     "interrupted at the mobile money provider",
        "_Delivery":         "responded",
    }

    # verify_transaction is in test mode and now defaults to PENDING, i.e.
    # still unresolved — so nothing here can resolve the row.
    for _ in range(3):
        res = client.post(
            f"/reports/settlements/auto?secret={settlement_secret}", headers=headers
        )
        assert res.status_code == 200

    assert len(spy_disburse["calls"]) == 1, (
        f"an INDETERMINATE payout was sent {len(spy_disburse['calls'])} times. "
        f"Unknown means the merchant may already have the money — only a "
        f"Yo-confirmed FAILED unlocks a re-send."
    )

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "indeterminate"
    assert row.completed_at is None

    school_block = res.json()["results"][0]
    assert [p["status"] for p in school_block["payouts"]] == ["indeterminate"]
    assert school_block["unresolved"] == [merchant.name]
    assert res.json()["grand_total_ugx"] == 0


def test_test_mode_status_check_defaults_to_unresolved_not_paid(
    client, db_session, school, parent_user, merchant, settlement_secret,
    spy_disburse, monkeypatch,
):
    """
    Failing safe in test mode must mean UNRESOLVED, not PAID. With the
    default an unknown payout stays unknown; only an explicit
    TEST_YO_TX_STATUS=SUCCEEDED resolves it — and then it is reported as
    already paid, never re-sent.
    """
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=0
    )
    _add_completed_payment(db_session, merchant, wallet, 4000)

    admin = make_admin(db_session, school, phone="256700999053")
    headers = headers_for(admin)

    spy_disburse["response"] = {
        "Status":            "OK",
        "TransactionStatus": "INDETERMINATE",
        "StatusMessage":     "interrupted at the mobile money provider",
        "_Delivery":         "responded",
    }
    res = client.post(
        f"/reports/settlements/auto?secret={settlement_secret}", headers=headers
    )
    assert res.status_code == 200

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "indeterminate", (
        "the test-mode status check must not silently mark unknown payouts paid"
    )

    # Now let the test say what Yo would say.
    monkeypatch.setenv("TEST_YO_TX_STATUS", "SUCCEEDED")

    res = client.post(f"/reports/school/{school.id}/payouts/resolve", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["checked"] == 1
    assert body["resolved_sent"] == 1
    assert body["resolved_failed"] == 0

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "sent"
    assert row.completed_at is not None

    # A resolved-SUCCEEDED row is never sent again.
    sends_before = len(spy_disburse["calls"])
    res = client.post(
        f"/reports/settlements/auto?secret={settlement_secret}", headers=headers
    )
    assert res.status_code == 200
    assert len(spy_disburse["calls"]) == sends_before


# ================================================
# THE RESOLVER SWEEP
# ================================================
def test_resolver_sweeps_a_stuck_pending_row_with_no_created_at(
    client, db_session, school, parent_user, merchant, monkeypatch,
):
    """
    A "pending" row whose send died mid-flight is the stuck case the resolve
    endpoint exists for. created_at is a Python-side default, so a row
    inserted outside the ORM can have none — and a NULL there is MORE
    suspicious than an old timestamp, not less. Excluding it would make the
    row permanently invisible to the very endpoint built to catch it.
    """
    admin = make_admin(db_session, school, phone="256700999054")
    headers = headers_for(admin)

    today = date.today()
    stuck = Payout(
        merchant_id=merchant.id,
        payout_date=today,
        amount=4000,
        status="pending",
        yo_reference=f"SW-PAYOUT-{merchant.id}-{today:%Y%m%d}-1",
    )
    db_session.add(stuck)
    db_session.commit()

    # created_at has a Python-side default, so the ORM fills it in even when
    # told None. NULL it with Core — which is also how such a row arises for
    # real: written by something that is not this model.
    db_session.execute(
        text("UPDATE payouts SET created_at = NULL WHERE id = :i"), {"i": stuck.id}
    )
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(Payout).filter_by(id=stuck.id).one().created_at is None, (
        "fixture must actually have a NULL created_at, or this test proves nothing"
    )

    monkeypatch.setenv("TEST_YO_TX_STATUS", "FAILED")

    res = client.post(f"/reports/school/{school.id}/payouts/resolve", headers=headers)
    assert res.status_code == 200
    body = res.json()

    assert body["checked"] == 1, (
        f"a stuck 'pending' row with NULL created_at must be swept, got {body}"
    )
    assert body["resolved_failed"] == 1

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "failed", (
        "Yo confirmed FAILED, so the row becomes retryable — the only route "
        "out of limbo for a send that died mid-flight"
    )


def test_resolver_leaves_a_fresh_pending_row_alone(
    client, db_session, school, parent_user, merchant, monkeypatch,
):
    """
    The other side of the same rule: a "pending" row created just now may
    still have its send genuinely in flight, so the sweep must not touch it.
    """
    admin = make_admin(db_session, school, phone="256700999055")
    headers = headers_for(admin)

    today = date.today()
    fresh = Payout(
        merchant_id=merchant.id,
        payout_date=today,
        amount=4000,
        status="pending",
        yo_reference=f"SW-PAYOUT-{merchant.id}-{today:%Y%m%d}-1",
        created_at=datetime.utcnow(),
    )
    db_session.add(fresh)
    db_session.commit()

    monkeypatch.setenv("TEST_YO_TX_STATUS", "FAILED")

    res = client.post(f"/reports/school/{school.id}/payouts/resolve", headers=headers)
    assert res.status_code == 200
    assert res.json()["checked"] == 0, (
        "a send made seconds ago may still be in flight — do not adjudicate it"
    )

    db_session.expire_all()
    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).one()
    assert row.status == "pending"


def test_auto_payout_refuses_without_a_configured_secret(
    client, db_session, school, monkeypatch,
):
    """The old hardcoded default must not open the endpoint when the
    variable is unset, and a wrong secret is still a 403 when it is set."""
    from tests.conftest import headers_for, make_admin

    headers = headers_for(make_admin(db_session, school, phone="256700999021"))

    monkeypatch.delenv("SETTLEMENT_SECRET", raising=False)
    r = client.post(
        "/reports/settlements/auto?secret=school_wallet_settle_2026", headers=headers
    )
    assert r.status_code == 503

    monkeypatch.setenv("SETTLEMENT_SECRET", SETTLEMENT_SECRET)
    r = client.post("/reports/settlements/auto?secret=wrong", headers=headers)
    assert r.status_code == 403
