# ================================================
# tests/test_stolen_card.py
# ------------------------------------------------
# Money-critical: lost/stolen card handling.
#   POST /students/{id}/report-stolen  (app/routes/students.py)
#   PUT  /students/{id}/assign-nfc     (app/routes/students.py)
#   POST /payments/nfc                 (app/routes/payments.py)
#
# The feature promise: reporting a card stolen must immediately stop it
# from spending, must never touch the wallet balance, and the retired
# tag_uid must never be reassignable to anyone — including its original
# owner.
# ================================================

import uuid

import pytest

from app import models
from tests.conftest import make_student_with_wallet, make_admin, headers_for


# ── 1. Report stolen blocks the card, wallet balance untouched ──
def test_report_stolen_blocks_payment_and_preserves_balance(
    client, db_session, school, parent_user, merchant, admin_headers,
):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    old_uid = nfc.tag_uid

    r = client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stolen"
    assert body["wallet_untouched"] is True

    db_session.refresh(nfc)
    assert nfc.is_active is False
    assert nfc.status == "stolen"
    assert nfc.deactivated_at is not None

    db_session.refresh(wallet)
    assert wallet.balance == 10000  # untouched

    pay = client.post(
        "/payments/nfc",
        params={"tag_uid": old_uid, "merchant_id": merchant.id, "amount": 1000, "request_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert pay.status_code == 403
    assert "deactivated" in pay.json()["detail"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 10000  # still untouched after the blocked attempt


# ── 2. Offline /sync path is blocked the same way ──
def test_report_stolen_blocks_offline_sync_payment(
    client, db_session, school, parent_user, merchant, admin_headers,
):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    old_uid = nfc.tag_uid

    client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)

    r = client.post(
        "/payments/sync",
        params={"device_id": "tuckshop-1", "merchant_id": merchant.id},
        json=[{"tag_uid": old_uid, "amount": 1000, "request_id": str(uuid.uuid4()), "timestamp": "2026-07-30T10:00:00"}],
        headers=admin_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["processed"] == 0
    assert data["failed"] == 1
    assert "deactivated" in data["details"]["failed"][0]["reason"].lower()

    db_session.refresh(wallet)
    assert wallet.balance == 10000


# ── 3. Reissue after stolen: new card works, old tag_uid stays dead ──
def test_reissue_after_stolen_new_card_works_old_stays_blocked(
    client, db_session, school, parent_user, merchant, admin_headers,
):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=10000)
    old_uid = nfc.tag_uid

    client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)

    new_uid = "FFEEDDCC"
    assign = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": new_uid},
        headers=admin_headers,
    )
    assert assign.status_code == 200
    assert assign.json()["tag_uid"] == new_uid

    # New card pays fine, balance from before is intact.
    pay = client.post(
        "/payments/nfc",
        params={"tag_uid": new_uid, "merchant_id": merchant.id, "amount": 2000, "request_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert pay.status_code == 200
    assert pay.json()["remaining_balance"] == 8000

    # Old (stolen) tag_uid can never come back to life.
    old_pay = client.post(
        "/payments/nfc",
        params={"tag_uid": old_uid, "merchant_id": merchant.id, "amount": 500, "request_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert old_pay.status_code == 403

    # And it can never be reassigned again, even to the same student.
    reassign = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": old_uid},
        headers=admin_headers,
    )
    assert reassign.status_code == 400

    db_session.expire_all()
    tags = db_session.query(models.NFCTag).filter_by(student_id=student.id).all()
    assert len(tags) == 2
    by_uid = {t.tag_uid: t for t in tags}
    assert by_uid[old_uid].is_active is False
    assert by_uid[old_uid].status == "stolen"
    assert by_uid[new_uid].is_active is True


# ── 4. A tag_uid already on file (any status) can't go to a different student either ──
def test_tag_uid_cannot_move_to_another_student(
    client, db_session, school, parent_user, admin_headers,
):
    student_a, _, nfc_a = make_student_with_wallet(db_session, school, parent_user, balance=0, tag_uid="AAAA1111")
    student_b, _, _ = make_student_with_wallet(
        db_session, school, parent_user, balance=0, tag_uid=None,
    )

    r = client.put(
        f"/students/{student_b.id}/assign-nfc",
        params={"tag_uid": "AAAA1111"},
        headers=admin_headers,
    )
    assert r.status_code == 400


# ── 5. Parent can report their own child's card stolen ──
def test_parent_can_report_own_childs_card_stolen(client, db_session, school, parent_user, auth_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)

    r = client.post(f"/students/{student.id}/report-stolen", headers=auth_headers)
    assert r.status_code == 200
    db_session.refresh(nfc)
    assert nfc.is_active is False


# ── 6. Parent cannot report another parent's child ──
def test_parent_cannot_report_someone_elses_child(client, db_session, school, admin_headers):
    other_parent = models.User(
        name="Other Parent", phone="256700555777", role="parent",
        pin_hash=None,
    )
    db_session.add(other_parent)
    db_session.commit()
    db_session.refresh(other_parent)

    student, wallet, nfc = make_student_with_wallet(db_session, school, other_parent, balance=5000)

    intruder = models.User(
        name="Intruder Parent", phone="256700888999", role="parent", pin_hash=None,
    )
    db_session.add(intruder)
    db_session.commit()
    db_session.refresh(intruder)

    r = client.post(f"/students/{student.id}/report-stolen", headers=headers_for(intruder))
    assert r.status_code == 403

    db_session.refresh(nfc)
    assert nfc.is_active is True  # untouched


# ── 7. Admin from a different school cannot touch this student's card ──
def test_admin_from_other_school_cannot_report_stolen(client, db_session, school, parent_user):
    other_school = models.School(name="Other School", location="Jinja")
    db_session.add(other_school)
    db_session.commit()
    db_session.refresh(other_school)

    other_admin = make_admin(db_session, other_school, phone="256700111000")
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)

    r = client.post(f"/students/{student.id}/report-stolen", headers=headers_for(other_admin))
    assert r.status_code == 403
    db_session.refresh(nfc)
    assert nfc.is_active is True


# ── 8. A merchant cannot report a card stolen ──
def test_merchant_cannot_report_stolen(client, db_session, school, parent_user, merchant):
    merchant_user = models.User(
        name="Merchant User", phone="256700222333", role="merchant",
        pin_hash=None, school_id=school.id,
    )
    db_session.add(merchant_user)
    db_session.commit()
    db_session.refresh(merchant_user)

    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)

    r = client.post(f"/students/{student.id}/report-stolen", headers=headers_for(merchant_user))
    assert r.status_code == 403


# ── 9. Can't report stolen twice with no card in between ──
def test_report_stolen_with_no_active_card_returns_400(client, db_session, school, parent_user, admin_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)

    first = client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)
    assert first.status_code == 200

    second = client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)
    assert second.status_code == 400


# ── 10. "lost" is accepted as a reason, arbitrary strings are not ──
def test_report_lost_reason_accepted_invalid_rejected(client, db_session, school, parent_user, admin_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)

    r = client.post(f"/students/{student.id}/report-stolen", params={"reason": "lost"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "lost"

    student2, wallet2, nfc2 = make_student_with_wallet(
        db_session, school, parent_user, balance=5000, tag_uid="ZZZZ9999",
    )
    bad = client.post(
        f"/students/{student2.id}/report-stolen", params={"reason": "misplaced"}, headers=admin_headers,
    )
    assert bad.status_code == 422


# ── 11. GET /students/ surfaces "stolen" status instead of a generic empty slot ──
def test_student_list_surfaces_stolen_status(client, db_session, school, parent_user, admin_headers):
    student, wallet, nfc = make_student_with_wallet(db_session, school, parent_user, balance=5000)
    client.post(f"/students/{student.id}/report-stolen", headers=admin_headers)

    r = client.get("/students/", headers=admin_headers)
    assert r.status_code == 200
    payload = next(s for s in r.json() if s["id"] == student.id)
    assert payload["nfc"]["status"] == "stolen"
    assert payload["nfc"]["tag_uid"] is None
