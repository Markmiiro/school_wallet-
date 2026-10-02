# ================================================
# tests/test_payment_amount_validation.py
# ------------------------------------------------
# app/routes/payments.py has three endpoints that all shared the same
# missing `amount > 0` validation (see tests/test_tuckshop_payment.py
# for the /payments/nfc coverage and the fix). This file confirms the
# fix landed identically on the other two: POST /payments/ and
# POST /payments/sync.
# ================================================

import uuid

from tests.conftest import make_student_with_wallet


# ── POST /payments/ (make_payment) ─────────────────────
def test_make_payment_rejects_negative_amount(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/",
        params={"wallet_id": wallet.id, "merchant_id": merchant.id, "amount": -5000},
        headers=staff_headers,
    )
    assert r.status_code == 400
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_make_payment_rejects_zero_amount(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/",
        params={"wallet_id": wallet.id, "merchant_id": merchant.id, "amount": 0},
        headers=staff_headers,
    )
    assert r.status_code == 400
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_make_payment_accepts_positive_amount(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/",
        params={"wallet_id": wallet.id, "merchant_id": merchant.id, "amount": 2000},
        headers=staff_headers,
    )
    assert r.status_code == 200
    db_session.refresh(wallet)
    assert wallet.balance == 8000


# ── POST /payments/sync (sync_offline_payments) ────────
def test_sync_rejects_negative_amount_for_that_item_only(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": -3000, "request_id": str(uuid.uuid4()), "description": "bad"}],
        headers=staff_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 0
    assert body["failed"] == 1
    assert "amount" in body["details"]["failed"][0]["reason"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_sync_rejects_zero_amount_for_that_item_only(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 0, "request_id": str(uuid.uuid4()), "description": "bad"}],
        headers=staff_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 0
    assert body["failed"] == 1

    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_sync_processes_good_items_even_when_batch_has_a_bad_one(client, db_session, school, parent_user, merchant, staff_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    r = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "device-1"},
        json=[
            {"tag_uid": nfc.tag_uid, "amount": -3000, "request_id": str(uuid.uuid4()), "description": "bad"},
            {"tag_uid": nfc.tag_uid, "amount": 1500, "request_id": str(uuid.uuid4()), "description": "good"},
        ],
        headers=staff_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 1
    assert body["failed"] == 1

    db_session.refresh(wallet)
    assert wallet.balance == 8500
