# ================================================
# tests/test_merchant_payout_idempotency.py
# ------------------------------------------------
# Money-critical: app/routes/reports.py's trigger_manual_payout() /
# automated_daily_payout() had NO idempotency tracking at all — every
# call re-sums a merchant's completed Transaction rows for the day and
# calls disburse_to_merchant(), with nothing recording "this merchant
# was already paid for this date." Two admin clicks, a retried cron, or
# manual+auto both firing for the same day all pay the merchant twice.
#
# Fix: a Payout row, unique on (merchant_id, payout_date), inserted and
# committed BEFORE calling Yo — not just a lock. The first call to
# reserve the (merchant, date) slot wins; a second caller either loses
# the race at the database's unique constraint (never calls Yo at all)
# or, if a prior row already exists with status "pending"/"sent", sees
# that and skips. Only a "failed" row is retried (status flips back to
# "pending", same row reused — the unique constraint means there can
# never be more than one row per merchant per day regardless of retries).
#
# This first test doesn't reference Payout at all — it spies on
# disburse_to_merchant's call count, so it can meaningfully run (and
# fail) against the pre-fix code, which has no Payout model yet.
# ================================================

import concurrent.futures
import os
from datetime import datetime

import pytest

from app.models import Payout, Transaction
from tests.conftest import headers_for, make_admin


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


def test_triggering_payout_twice_only_pays_once(
    client, db_session, school, parent_user, merchant, monkeypatch,
):
    from tests.conftest import make_student_with_wallet
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=0)
    _add_completed_payment(db_session, merchant, wallet, 5000, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999030")
    headers = headers_for(admin)

    call_count = {"n": 0}
    real_disburse = __import__("app.momo", fromlist=["disburse_to_merchant"]).disburse_to_merchant

    async def counting_disburse(*args, **kwargs):
        call_count["n"] += 1
        return await real_disburse(*args, **kwargs)

    monkeypatch.setattr("app.routes.reports.disburse_to_merchant", counting_disburse)

    res1 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    res2 = client.post(f"/reports/school/{school.id}/payout", headers=headers)

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert call_count["n"] == 1, (
        f"disburse_to_merchant was called {call_count['n']} times for two triggers "
        f"of the same school/day — must be called exactly once"
    )
    assert res1.json()["total_paid_ugx"] == 5000
    assert res2.json()["total_paid_ugx"] == 0  # nothing new to pay — already sent


def test_exactly_one_payout_row_exists_after_double_trigger(
    client, db_session, school, parent_user, merchant,
):
    from tests.conftest import make_student_with_wallet
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=0)
    _add_completed_payment(db_session, merchant, wallet, 3000, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999031")
    headers = headers_for(admin)

    client.post(f"/reports/school/{school.id}/payout", headers=headers)
    client.post(f"/reports/school/{school.id}/payout", headers=headers)

    rows = db_session.query(Payout).filter_by(merchant_id=merchant.id).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].amount == 3000


def test_failed_payout_can_be_retried(
    client, db_session, school, parent_user, merchant, monkeypatch,
):
    from tests.conftest import make_student_with_wallet
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=0)
    _add_completed_payment(db_session, merchant, wallet, 2000, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999032")
    headers = headers_for(admin)

    async def failing_disburse(*args, **kwargs):
        return {"Status": "ERROR", "StatusMessage": "simulated gateway failure"}

    monkeypatch.setattr("app.routes.reports.disburse_to_merchant", failing_disburse)
    res1 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res1.status_code == 200
    assert res1.json()["payouts_failed"] == 1

    row = db_session.query(Payout).filter_by(merchant_id=merchant.id).first()
    assert row.status == "failed"

    # Gateway recovers — retrying the same merchant/day should now succeed,
    # reusing the same row (not inserting a second one).
    monkeypatch.undo()
    res2 = client.post(f"/reports/school/{school.id}/payout", headers=headers)
    assert res2.status_code == 200
    assert res2.json()["total_paid_ugx"] == 2000

    # The `row` object loaded above is still in this session's identity
    # map with its pre-retry "failed" status — expire it so this re-query
    # actually reflects what the request's own (separate) session just
    # committed, instead of handing back the stale cached copy.
    db_session.expire_all()
    rows = db_session.query(Payout).filter_by(merchant_id=merchant.id).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"


def test_scoped_admin_cannot_trigger_payout_for_other_school(
    client, db_session, school, second_school,
):
    admin = make_admin(db_session, school, phone="256700999033")
    res = client.post(f"/reports/school/{second_school.id}/payout", headers=headers_for(admin))
    assert res.status_code == 403


def test_scoped_admin_can_trigger_payout_for_own_school(
    client, db_session, school, parent_user, merchant,
):
    from tests.conftest import make_student_with_wallet
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=0)
    _add_completed_payment(db_session, merchant, wallet, 1500, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999034")
    res = client.post(f"/reports/school/{school.id}/payout", headers=headers_for(admin))
    assert res.status_code == 200
    assert res.json()["total_paid_ugx"] == 1500


# ══════════════════════════════════════════════════════
# Genuine concurrency: two truly-simultaneous triggers, not just two
# sequential calls. Proves the reservation (unique constraint + row
# lock on retry) actually serializes under real interleaving, not just
# under the "check status first" ordering the sequential tests above
# exercise. Postgres-only: with_for_update() silently no-ops on SQLite
# (see test_topup_status_race.py for the same reasoning) — though here
# the *first-attempt* case (no existing row) is protected by the plain
# unique constraint regardless of backend, since that's enforced on
# INSERT even without a lock. What specifically needs Postgres is the
# retry-a-failed-row path's with_for_update().
# ══════════════════════════════════════════════════════
def test_concurrent_double_trigger_only_pays_once(
    client, db_session, school, parent_user, merchant, monkeypatch,
):
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip(
            "Needs TEST_DATABASE_URL set to a throwaway Postgres db to genuinely "
            "prove the concurrent path — see test_topup_status_race.py."
        )

    from tests.conftest import make_student_with_wallet
    import asyncio

    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=0)
    _add_completed_payment(db_session, merchant, wallet, 9000, datetime.utcnow())

    admin = make_admin(db_session, school, phone="256700999035")
    headers = headers_for(admin)

    async def slow_disburse(*args, **kwargs):
        await asyncio.sleep(0.05)
        return {"Status": "OK", "TransactionStatus": "SUCCEEDED", "ExternalReference": "test-ref"}

    monkeypatch.setattr("app.routes.reports.disburse_to_merchant", slow_disburse)

    def fire():
        return client.post(f"/reports/school/{school.id}/payout", headers=headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fire(), range(2)))

    assert all(r.status_code == 200 for r in results)
    total_paid_across_both_calls = sum(r.json()["total_paid_ugx"] for r in results)
    assert total_paid_across_both_calls == 9000, (
        f"two concurrent triggers must pay the merchant's 9000 UGX in sales "
        f"exactly once between them, got {total_paid_across_both_calls}"
    )

    rows = db_session.query(Payout).filter_by(merchant_id=merchant.id).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"
