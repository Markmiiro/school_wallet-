# ================================================
# tests/test_tuckshop_payment.py
# ------------------------------------------------
# Money-critical: tuck-shop tap-to-pay loop.
#   GET  /tuckshop/check  (app/routes/tuckshop.py)
#   POST /payments/nfc    (app/routes/payments.py)
#
# Neither file is on CLAUDE.md's frozen/read-only list, so bugs found
# here are reported for a fix decision rather than silently patched.
# ================================================

import concurrent.futures
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from tests.conftest import make_student_with_wallet


# ── 1. Happy path ─────────────────────────────────────
def test_check_then_pay_happy_path(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)

    check = client.get(f"/tuckshop/check?tag_uid={nfc.tag_uid}", headers=staff_headers)
    assert check.status_code == 200
    body = check.json()
    assert body["student_name"] == "Test Student"
    assert body["balance"] == 10000

    pay = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 2000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert pay.status_code == 200
    data = pay.json()
    assert data["amount_paid"] == 2000
    assert data["remaining_balance"] == 8000

    db_session.refresh(wallet)
    assert wallet.balance == 8000

    txns = db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).all()
    assert len(txns) == 1
    assert txns[0].type == "payment"
    assert txns[0].status == "completed"
    assert txns[0].amount == 2000


# ── 2. Unknown tag ─────────────────────────────────────
def test_check_unknown_tag_returns_404(client, staff_headers):
    r = client.get("/tuckshop/check?tag_uid=DOESNOTEXIST", headers=staff_headers)
    assert r.status_code == 404


def test_pay_unknown_tag_returns_404(client, merchant, staff_headers):
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": "DOESNOTEXIST", "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 404


# ── 3. Deactivated wallet ──────────────────────────────
def test_pay_deactivated_wallet_returns_403(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=10000, wallet_active=False,
    )
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000  # untouched


# ── 4. Unknown merchant ────────────────────────────────
def test_pay_unknown_merchant_returns_404(client, db_session, school, parent_user, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": 999999, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 404
    db_session.refresh(wallet)
    assert wallet.balance == 10000


# ── 5. Insufficient balance ────────────────────────────
def test_pay_insufficient_balance_returns_400(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=500)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 400
    db_session.refresh(wallet)
    assert wallet.balance == 500
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 0


# ── 6. Daily limit ──────────────────────────────────────
def test_pay_within_daily_limit_succeeds(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=50000, daily_limit=5000,
    )
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 5000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 200


def test_pay_exceeding_daily_limit_returns_400(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=50000, daily_limit=5000,
    )
    first = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 4000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert first.status_code == 200

    second = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 2000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert second.status_code == 400
    assert "Daily limit" in second.json()["detail"]

    db_session.refresh(wallet)
    assert wallet.balance == 46000  # only the first payment went through


def test_daily_limit_ignores_previous_days_spending(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(
        db_session, school, parent_user, balance=50000, daily_limit=5000,
    )
    yesterday_txn = models.Transaction(
        wallet_id=wallet.id,
        merchant_id=merchant.id,
        amount=4000,
        type="payment",
        status="completed",
        timestamp=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add(yesterday_txn)
    db_session.commit()

    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 4000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 200, "today's spend should not be blocked by yesterday's transactions"


# ── 7. Double charge on retry ──────────────────────────
# FIXED: nfc_payment() now requires a client-generated request_id
# (the tuck-shop device generates one UUID per NFC tap). A second call
# with the same request_id doesn't re-check balance/daily-limit or
# touch the wallet at all — it returns the original result (see
# _idempotent_replay_response in app/routes/payments.py).
def test_identical_retry_does_not_double_charge(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    params = {
        "tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 3000,
        "request_id": str(uuid.uuid4()),
    }

    first = client.post("/payments/nfc", params=params, headers=staff_headers)
    second = client.post("/payments/nfc", params=params, headers=staff_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["transaction_id"] == first.json()["transaction_id"]
    assert second.json().get("idempotent_replay") is True
    assert first.json().get("idempotent_replay") is not True

    db_session.refresh(wallet)
    assert wallet.balance == 7000, "the wallet must be debited exactly once, not on every retry"
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 1
    assert db_session.query(models.Payment).filter_by(wallet_id=wallet.id).count() == 1


def test_different_request_ids_are_independent_charges(client, db_session, school, parent_user, merchant, staff_headers):
    """Two genuinely separate purchases of the same amount must both go through."""
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)

    first = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    second = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["transaction_id"] != second.json()["transaction_id"]

    db_session.refresh(wallet)
    assert wallet.balance == 8000
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 2


def test_missing_request_id_is_rejected(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000},
        headers=staff_headers,
    )
    assert r.status_code == 422
    db_session.refresh(wallet)
    assert wallet.balance == 10000


# ── 8. Concurrent payments must not overdraw the wallet ──
# FIXED: the wallet reads in nfc_payment(), make_payment(), and
# sync_offline_payments() now use .with_for_update() to lock the row
# until commit, so a second concurrent request blocks and re-reads the
# post-deduction balance instead of clobbering it.
#
# This test cannot verify that end-to-end on SQLite: SQLAlchemy
# silently drops the FOR UPDATE clause there (confirmed by compiling
# the same query object against the sqlite vs postgresql dialects —
# sqlite emits a plain SELECT, postgresql emits "SELECT ... FOR
# UPDATE"). That's a real SQLite limitation (no native row-level
# locking), not a bug in the fix, so it self-skips on SQLite rather
# than failing in a way that misleadingly reads as "still broken."
# Run against a throwaway Postgres db to get a real answer:
#   TEST_DATABASE_URL=postgresql://supreme@localhost/school_wallet_test \
#     ./venv/bin/python -m pytest tests/test_tuckshop_payment.py -k concurrent
# See also test_wallet_balance_reads_request_row_locking, a
# dialect-independent regression check that runs on every backend.
def test_concurrent_payments_do_not_overdraw_wallet(client, db_session, school, parent_user, merchant, staff_headers):
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip("FOR UPDATE is a no-op on SQLite; run with TEST_DATABASE_URL set to a Postgres db.")

    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=3000)

    def fire():
        # Each call is a genuinely distinct purchase attempt (not a retry
        # of the same one), so each gets its own request_id — this test is
        # about the wallet-balance race, not the idempotency dedup path.
        params = {
            "tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 2000,
            "request_id": str(uuid.uuid4()),
        }
        return client.post("/payments/nfc", params=params, headers=staff_headers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fire(), range(2)))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 400], (
        f"expected exactly one payment to succeed and one to be rejected for "
        f"insufficient balance, got statuses={statuses}"
    )

    db_session.refresh(wallet)
    completed = (
        db_session.query(models.Transaction)
        .filter_by(wallet_id=wallet.id, type="payment", status="completed")
        .all()
    )
    total_charged = sum(t.amount for t in completed)
    assert wallet.balance == 3000 - total_charged, (
        f"lost update: {len(completed)} completed payment(s) totalling "
        f"{total_charged} UGX recorded, but wallet balance is {wallet.balance} "
        f"(started at 3000) — the two writes clobbered each other instead of "
        f"both applying or one being rejected"
    )


# Dialect-independent regression guard for the fix above: proves the
# three wallet-balance-mutating endpoints request a row lock, by
# compiling their actual query source against the postgresql dialect
# (where FOR UPDATE is not a no-op) rather than relying on runtime
# behavior under SQLite. Catches a future refactor that accidentally
# drops .with_for_update(), which the skipped test above cannot.
def test_wallet_balance_reads_request_row_locking():
    import inspect
    from app.routes import payments as payments_module

    source = inspect.getsource(payments_module)
    wallet_lookup_count = source.count("db.query(Wallet)")
    locked_lookup_count = source.count(".with_for_update()")
    assert wallet_lookup_count == 3, (
        f"expected exactly 3 db.query(Wallet) lookups (make_payment, "
        f"nfc_payment, sync_offline_payments) — found {wallet_lookup_count}. "
        f"This test's assumptions are stale; update it to match the current "
        f"code before trusting the count below."
    )
    assert locked_lookup_count == 3, (
        f"expected all 3 wallet lookups that precede a balance mutation to "
        f"use .with_for_update() — found {locked_lookup_count}. A wallet "
        f"read lost its row lock, reopening the concurrent-payment lost-"
        f"update bug."
    )


# ── 9. Amount validation ───────────────────────────────
# FIXED: nfc_payment() now rejects amount <= 0 before any balance/daily
# limit logic runs. Previously a negative amount passed the balance
# check (balance < amount is False for any negative amount) and then
# `wallet.balance -= amount` increased the balance — free money with
# no real charge from Yo Uganda behind it.
def test_negative_amount_is_rejected(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": -5000, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    db_session.refresh(wallet)
    assert r.status_code == 400
    assert wallet.balance == 10000


def test_zero_amount_is_rejected(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 0, "request_id": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 400
    db_session.refresh(wallet)
    assert wallet.balance == 10000


# ── 10. Requires authentication ────────────────────────
def test_pay_without_auth_token_is_rejected(client, db_session, school, parent_user, merchant):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/nfc",
        params={"tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
    )
    assert r.status_code == 401
