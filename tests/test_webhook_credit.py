# ================================================
# tests/test_webhook_credit.py
# ------------------------------------------------
# Money-critical: wallet crediting via the Yo Uganda IPN callback.
#   POST /webhook/yo  (app/routes/webhook.py)
#
# app/routes/webhook.py is on CLAUDE.md's READ-ONLY list — bugs found
# here are reported, never silently patched.
#
# Real Yo IPNs carry an RSA signature we can't forge; signature
# verification is mocked to "valid" via the autouse mock_yo_signature
# fixture in conftest.py, so these tests focus on reference routing,
# amount matching, and idempotency — the actual money-movement logic.
# ================================================

from datetime import datetime

import pytest

from app import models
from app.routes.ussd import build_topup_reference, build_registration_reference, REGISTRATION_FEE
from tests.conftest import make_student_with_wallet


def ipn_payload(external_ref, amount, **overrides):
    payload = {
        "date_time": "2026-07-28 10:00:00",
        "amount": str(amount),
        "narrative": "School Wallet top-up",
        "network_ref": "YO-TESTREF-1",
        "external_ref": external_ref,
        "msisdn": "256771234567",
        "signature": "mocked",  # verify_yo_signature is monkeypatched to True
    }
    payload.update(overrides)
    return payload


# ══════════════════════════════════════════════════════
# USSD-TOPUP-{student_id}-{amount}-{uuid8}
# ══════════════════════════════════════════════════════
def test_ussd_topup_credits_existing_wallet(client, db_session, school, parent_user):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    ref = build_topup_reference(student.id, 5000)

    r = client.post("/webhook/yo", data=ipn_payload(ref, 5000))
    assert r.status_code == 200
    assert "credited" in r.json()["message"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 6000

    txns = db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).all()
    assert len(txns) == 1
    assert txns[0].type == "topup"
    assert txns[0].status == "completed"
    assert txns[0].amount == 5000
    assert txns[0].reference == ref


def test_ussd_topup_amount_mismatch_not_credited(client, db_session, school, parent_user):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    ref = build_topup_reference(student.id, 5000)  # ref says 5000...

    r = client.post("/webhook/yo", data=ipn_payload(ref, 9999))  # ...IPN says 9999
    assert r.status_code == 200
    assert "mismatch" in r.json()["message"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 1000
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 0


def test_ussd_topup_unknown_student_no_crash(client):
    ref = build_topup_reference(999999, 5000)
    r = client.post("/webhook/yo", data=ipn_payload(ref, 5000))
    assert r.status_code == 200
    assert "wallet not found" in r.json()["message"].lower()


def test_ussd_topup_idempotent_on_duplicate_callback(client, db_session, school, parent_user):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    ref = build_topup_reference(student.id, 5000)
    payload = ipn_payload(ref, 5000)

    first = client.post("/webhook/yo", data=payload)
    second = client.post("/webhook/yo", data=payload)

    assert first.status_code == 200
    assert "credited" in first.json()["message"].lower()
    assert second.status_code == 200
    assert "already processed" in second.json()["message"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 6000, "duplicate IPN must not double-credit"
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 1


# ══════════════════════════════════════════════════════
# USSD-REG-{uuid8}
# ══════════════════════════════════════════════════════
def make_pending_registration(db_session, **overrides):
    ref = build_registration_reference()
    from app.routes.ussd import PendingUssdRegistration

    fields = dict(
        reference=ref,
        phone="256772000111",
        student_name="New Kid",
        dob="2015-01-12",
        class_name="P4",
        school_name="Brand New School",
        card_color="Blue",
    )
    fields.update(overrides)
    pending = PendingUssdRegistration(**fields)
    db_session.add(pending)
    db_session.commit()
    return pending


def test_ussd_reg_creates_student_wallet_and_card(client, db_session):
    pending = make_pending_registration(db_session)

    r = client.post("/webhook/yo", data=ipn_payload(pending.reference, REGISTRATION_FEE))
    assert r.status_code == 200
    body = r.json()
    assert "completed" in body["message"].lower()
    student_id = body["student_id"]

    student = db_session.query(models.Student).filter_by(id=student_id).first()
    assert student is not None
    assert student.name == "New Kid"
    assert student.account_number is not None

    wallet = db_session.query(models.Wallet).filter_by(student_id=student.id).first()
    assert wallet is not None
    assert wallet.balance == 0  # registration fee funds the card, not the wallet
    assert wallet.is_active is True

    nfc = db_session.query(models.NFCTag).filter_by(student_id=student.id).first()
    assert nfc is not None
    assert nfc.tag_uid is None  # physical card assigned later at issuance
    assert nfc.card_color == "Blue"

    parent = db_session.query(models.User).filter_by(phone="256772000111").first()
    assert parent is not None
    assert parent.role == "parent"

    school_row = db_session.query(models.School).filter_by(name="Brand New School").first()
    assert school_row is not None
    assert student.school_id == school_row.id

    reg_txn = db_session.query(models.Transaction).filter_by(reference=pending.reference).first()
    assert reg_txn is not None
    assert reg_txn.type == "registration"
    assert reg_txn.status == "completed"
    assert reg_txn.amount == REGISTRATION_FEE

    # pending row cleaned up
    from app.routes.ussd import PendingUssdRegistration
    assert db_session.query(PendingUssdRegistration).filter_by(reference=pending.reference).first() is None


def test_ussd_reg_reuses_existing_parent_and_school(client, db_session, school, parent_user):
    pending = make_pending_registration(
        db_session, phone=parent_user.phone, school_name=school.name,
    )

    r = client.post("/webhook/yo", data=ipn_payload(pending.reference, REGISTRATION_FEE))
    assert r.status_code == 200
    student_id = r.json()["student_id"]

    student = db_session.query(models.Student).filter_by(id=student_id).first()
    assert student.parent_id == parent_user.id, "should reuse the existing parent by phone, not create a duplicate"
    assert student.school_id == school.id, "should reuse the existing school by name, not create a duplicate"

    assert db_session.query(models.User).filter_by(phone=parent_user.phone).count() == 1
    assert db_session.query(models.School).filter(models.School.name.ilike(school.name)).count() == 1


def test_ussd_reg_amount_mismatch_leaves_pending_row_intact(client, db_session):
    pending = make_pending_registration(db_session)

    r = client.post("/webhook/yo", data=ipn_payload(pending.reference, REGISTRATION_FEE - 1))
    assert r.status_code == 200
    assert "mismatch" in r.json()["message"].lower()

    from app.routes.ussd import PendingUssdRegistration
    still_pending = db_session.query(PendingUssdRegistration).filter_by(reference=pending.reference).first()
    assert still_pending is not None, "a mismatched IPN should not consume the pending registration"
    assert db_session.query(models.Student).count() == 0


def test_ussd_reg_unknown_reference_no_crash(client):
    ref = build_registration_reference()
    r = client.post("/webhook/yo", data=ipn_payload(ref, REGISTRATION_FEE))
    assert r.status_code == 200
    assert "not found" in r.json()["message"].lower()


def test_ussd_reg_idempotent_on_duplicate_callback(client, db_session):
    pending = make_pending_registration(db_session)
    payload = ipn_payload(pending.reference, REGISTRATION_FEE)

    first = client.post("/webhook/yo", data=payload)
    assert first.status_code == 200
    assert "completed" in first.json()["message"].lower()

    second = client.post("/webhook/yo", data=payload)
    assert second.status_code == 200
    assert "already processed" in second.json()["message"].lower()

    assert db_session.query(models.Student).count() == 1, "duplicate registration IPN must not create a second student"
    assert db_session.query(models.Wallet).count() == 1


# ══════════════════════════════════════════════════════
# Plain UUID — regular pre-created /topup Transaction
# ══════════════════════════════════════════════════════
def make_pending_topup_txn(db_session, wallet, amount, ref="a1b2c3d4-e5f6-4a5b-8c9d-000000000001"):
    txn = models.Transaction(
        wallet_id=wallet.id, amount=amount, type="topup", status="pending", reference=ref,
    )
    db_session.add(txn)
    db_session.commit()
    db_session.refresh(txn)
    return txn


def test_regular_topup_credits_wallet_and_completes_transaction(client, db_session, school, parent_user):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    txn = make_pending_topup_txn(db_session, wallet, 3000)

    r = client.post("/webhook/yo", data=ipn_payload(txn.reference, 3000))
    assert r.status_code == 200
    assert "credited" in r.json()["message"].lower()

    db_session.refresh(wallet)
    db_session.refresh(txn)
    assert wallet.balance == 4000
    assert txn.status == "completed"


def test_regular_topup_idempotent_on_duplicate_callback(client, db_session, school, parent_user):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    txn = make_pending_topup_txn(db_session, wallet, 3000)
    payload = ipn_payload(txn.reference, 3000)

    first = client.post("/webhook/yo", data=payload)
    second = client.post("/webhook/yo", data=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert "already processed" in second.json()["message"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 4000, "duplicate IPN must not double-credit"


def test_regular_topup_unknown_reference_no_crash(client):
    r = client.post("/webhook/yo", data=ipn_payload("some-uuid-that-does-not-exist", 3000))
    assert r.status_code == 200
    assert "not found" in r.json()["message"].lower()


# ══════════════════════════════════════════════════════
# Cross-cutting: signature + missing fields
# ══════════════════════════════════════════════════════
def test_missing_external_ref_rejected(client):
    r = client.post("/webhook/yo", data=ipn_payload("", 3000))
    assert r.status_code == 200
    assert "missing" in r.json()["message"].lower()


def test_invalid_signature_rejected(client, db_session, school, parent_user, monkeypatch):
    monkeypatch.setattr("app.routes.webhook.verify_yo_signature", lambda *a, **k: False)
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=1000)
    ref = build_topup_reference(student.id, 5000)

    r = client.post("/webhook/yo", data=ipn_payload(ref, 5000))
    assert r.status_code == 200
    assert "signature verification failed" in r.json()["message"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 1000, "wallet must not be credited when the signature check fails"
