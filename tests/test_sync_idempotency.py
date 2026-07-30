# ================================================
# tests/test_sync_idempotency.py
# ------------------------------------------------
# Money-critical: POST /payments/sync (app/routes/payments.py).
#
# Before this fix, /sync had no idempotency key at all — resending a
# batch (e.g. because the device lost the response after the server
# already committed) double-charged every item in it. This suite proves
# the fix: /sync now uses the same request_id + Payment.reference
# pattern as /payments/nfc, and also now enforces wallet.daily_limit,
# which it silently skipped before.
# ================================================

import concurrent.futures
import uuid

import pytest

from app import models
from tests.conftest import make_student_with_wallet


# ── 1. Resending the exact same batch must not double-charge ──
def test_resent_batch_does_not_double_charge(client, db_session, school, parent_user, merchant, auth_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    request_id = str(uuid.uuid4())
    batch = [{"tag_uid": nfc.tag_uid, "amount": 2000, "request_id": request_id, "description": "Lunch"}]

    first = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=batch,
        headers=auth_headers,
    )
    assert first.status_code == 200
    assert first.json()["processed"] == 1

    # Device didn't get the response (or resent out of caution) — same
    # batch, same request_id, sent again.
    second = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=batch,
        headers=auth_headers,
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["processed"] == 1  # reported as processed again...
    assert second_body["details"]["processed"][0]["already_synced"] is True  # ...but flagged, not re-charged

    db_session.refresh(wallet)
    assert wallet.balance == 8000, "wallet must be debited exactly once across both sync attempts"
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 1
    assert db_session.query(models.Payment).filter_by(wallet_id=wallet.id).count() == 1


# ── 2. Partial resend: one item already synced, one genuinely new ──
def test_mixed_batch_only_charges_the_new_item(client, db_session, school, parent_user, merchant, auth_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    already_synced_id = str(uuid.uuid4())

    first = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 1000, "request_id": already_synced_id}],
        headers=auth_headers,
    )
    assert first.json()["processed"] == 1

    new_id = str(uuid.uuid4())
    second = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[
            {"tag_uid": nfc.tag_uid, "amount": 1000, "request_id": already_synced_id},  # resent, already charged
            {"tag_uid": nfc.tag_uid, "amount": 500, "request_id": new_id},              # genuinely new
        ],
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert second.json()["processed"] == 2  # both reported processed...

    db_session.refresh(wallet)
    assert wallet.balance == 8500, "only the genuinely new item (500) should have been charged on the resend"
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 2


# ── 3. A missing request_id can't be synced at all ──
def test_sync_item_without_request_id_is_rejected(client, db_session, school, parent_user, merchant, auth_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 1000}],
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 0
    assert body["failed"] == 1
    assert "request_id" in body["details"]["failed"][0]["reason"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 10000


# ── 4. /sync now enforces the daily limit, matching /payments/nfc ──
def test_sync_respects_daily_limit(client, db_session, school, parent_user, merchant, auth_headers):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=50000, daily_limit=5000,
    )
    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[
            {"tag_uid": nfc.tag_uid, "amount": 4000, "request_id": str(uuid.uuid4())},
            {"tag_uid": nfc.tag_uid, "amount": 2000, "request_id": str(uuid.uuid4())},
        ],
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 1
    assert body["failed"] == 1
    assert "daily limit" in body["details"]["failed"][0]["reason"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 46000  # only the 4000 item went through


def test_sync_daily_limit_ignores_previous_days_spending(client, db_session, school, parent_user, merchant, auth_headers):
    from datetime import datetime, timedelta

    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=50000, daily_limit=5000,
    )
    yesterday_txn = models.Transaction(
        wallet_id=wallet.id, merchant_id=merchant.id, amount=4000,
        type="payment", status="completed",
        timestamp=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(yesterday_txn)
    db_session.commit()

    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 4000, "request_id": str(uuid.uuid4())}],
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["processed"] == 1, "today's sync should not be blocked by yesterday's spending"


# ── 5. Concurrent resync of the identical request_id must not double-charge ──
# Same rationale/limitation as test_concurrent_payments_do_not_overdraw_wallet
# in test_tuckshop_payment.py: SQLite silently drops FOR UPDATE, so this only
# proves anything real against Postgres.
def test_concurrent_resync_same_request_id_does_not_double_charge(
    client, db_session, school, parent_user, merchant, auth_headers,
):
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip("FOR UPDATE is a no-op on SQLite; run with TEST_DATABASE_URL set to a Postgres db.")

    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    request_id = str(uuid.uuid4())
    batch = [{"tag_uid": nfc.tag_uid, "amount": 3000, "request_id": request_id}]

    def fire():
        return client.post(
            "/payments/sync",
            params={"merchant_id": merchant.id, "device_id": "device-1"},
            json=batch,
            headers=auth_headers,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fire(), range(2)))

    assert all(r.status_code == 200 for r in results)

    db_session.refresh(wallet)
    assert wallet.balance == 7000, "wallet must be debited exactly once even when two syncs race on the same request_id"
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 1
    assert db_session.query(models.Payment).filter_by(wallet_id=wallet.id).count() == 1
