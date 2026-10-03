# ================================================
# tests/test_parent_links_card.py
# ------------------------------------------------
# Linking a card to a child by card number.
#
# Decision (3 Oct 2026, replacing 1 Oct): only the school links cards.
# A card number is printed on the card and readable by any NFC phone,
# so it proves nothing about who is asking. The school links each card
# from its roster at handout (/issue/), and parents get their children
# by phone number instead (tests/test_family_linking.py).
#
#   PUT  /students/{id}/assign-nfc?tag_uid=<card number>
#     admin (own school)  -> link, or replace a working card
#     parent              -> 403, "ask the school"
#   POST /students/{id}/undo-card-link
#     admin (own school)  -> free a card linked by mistake, if nothing
#                            has been bought with it since
# ================================================

import uuid

from app import models
from app.auth import hash_pin
from tests.conftest import (
    headers_for, make_admin, make_student_with_wallet,
)


def _student_without_card(db_session, school, parent):
    """As registration leaves it: an empty placeholder slot, no tag_uid."""
    return make_student_with_wallet(db_session, school, parent, tag_uid=None)


def test_school_links_card_and_it_pays(
    client, db_session, school, parent_user, merchant, admin_headers, staff_headers,
):
    student, wallet, slot = _student_without_card(db_session, school, parent_user)

    res = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "04A21B55"},
        headers=admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["tag_uid"] == "04A21B55"

    # The placeholder slot was filled in place, not duplicated.
    db_session.expire_all()
    tags = db_session.query(models.NFCTag).filter_by(student_id=student.id).all()
    assert len(tags) == 1
    assert tags[0].tag_uid == "04A21B55"
    assert tags[0].is_active is True

    pay = client.post(
        "/payments/nfc",
        params={
            "tag_uid": "04A21B55", "merchant_id": merchant.id,
            "amount": 2000, "request_id": str(uuid.uuid4()),
        },
        headers=staff_headers,
    )
    assert pay.status_code == 200
    assert pay.json()["remaining_balance"] == 8000


def test_typed_card_number_is_normalised(client, db_session, school, parent_user, admin_headers):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    res = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "04:a2:1b:55"},
        headers=admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["tag_uid"] == "04A21B55"


def test_school_gets_a_clear_error_for_a_malformed_card_number(
    client, db_session, school, parent_user, admin_headers,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    for bad in ("123", "ZZZZZZZZ", "04A21B5"):
        res = client.put(
            f"/students/{student.id}/assign-nfc", params={"tag_uid": bad}, headers=admin_headers,
        )
        assert res.status_code == 422, bad


def test_parent_cannot_link_a_card_to_someone_elses_child(
    client, db_session, school, parent_user,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    intruder = models.User(
        name="Intruder", phone="256700555920", role="parent", pin_hash=hash_pin("1234"),
    )
    db_session.add(intruder)
    db_session.commit()

    res = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "04A21B55"},
        headers=headers_for(intruder),
    )
    assert res.status_code == 403
    db_session.expire_all()
    assert db_session.query(models.NFCTag).filter_by(tag_uid="04A21B55").count() == 0


def test_parent_cannot_link_any_card_even_to_their_own_child(
    client, db_session, school, parent_user, auth_headers,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    res = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "04A21B55"},
        headers=auth_headers,
    )
    assert res.status_code == 403
    assert "school" in res.json()["detail"].lower()
    db_session.expire_all()
    assert db_session.query(models.NFCTag).filter_by(tag_uid="04A21B55").count() == 0


def test_after_a_lost_card_the_school_links_the_replacement(
    client, db_session, student_with_wallet, auth_headers, admin_headers,
):
    student, _, nfc = student_with_wallet
    old_uid = nfc.tag_uid

    assert client.post(
        f"/students/{student.id}/report-stolen", params={"reason": "lost"}, headers=auth_headers,
    ).status_code == 200

    assert client.put(
        f"/students/{student.id}/assign-nfc", params={"tag_uid": "04A21B55"},
        headers=auth_headers,
    ).status_code == 403
    res = client.put(
        f"/students/{student.id}/assign-nfc", params={"tag_uid": "04A21B55"},
        headers=admin_headers,
    )
    assert res.status_code == 200

    # The lost card stays on record and can never be linked again.
    again = client.put(
        f"/students/{student.id}/assign-nfc", params={"tag_uid": old_uid}, headers=admin_headers,
    )
    assert again.status_code in (400, 409)

    db_session.expire_all()
    by_uid = {
        t.tag_uid: t
        for t in db_session.query(models.NFCTag).filter_by(student_id=student.id).all()
    }
    assert by_uid[old_uid].is_active is False
    assert by_uid[old_uid].status == "lost"
    assert by_uid["04A21B55"].is_active is True


def test_a_card_number_already_linked_to_another_child_is_refused(
    client, db_session, school, parent_user, admin_headers,
):
    make_student_with_wallet(db_session, school, parent_user, tag_uid="AAAA1111")
    sibling, _, _ = _student_without_card(db_session, school, parent_user)

    res = client.put(
        f"/students/{sibling.id}/assign-nfc",
        params={"tag_uid": "AAAA1111"},
        headers=admin_headers,
    )
    assert res.status_code == 400


def test_linking_requires_a_token_and_till_staff_cannot_link(
    client, db_session, school, parent_user, staff_headers,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    url = f"/students/{student.id}/assign-nfc"
    assert client.put(url, params={"tag_uid": "04A21B55"}).status_code == 401
    assert client.put(url, params={"tag_uid": "04A21B55"}, headers=staff_headers).status_code == 403


def test_school_admin_can_still_link_and_replace(
    client, db_session, school, second_school, student_with_wallet, admin_headers,
):
    student, _, nfc = student_with_wallet
    res = client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "04A21B55"},
        headers=admin_headers,
    )
    assert res.status_code == 200          # admin MAY replace a working card

    db_session.expire_all()
    db_session.refresh(nfc)
    assert nfc.is_active is False
    assert nfc.status == "replaced"

    other_admin = make_admin(db_session, second_school, phone="256700999040")
    assert client.put(
        f"/students/{student.id}/assign-nfc",
        params={"tag_uid": "0499AA11"},
        headers=headers_for(other_admin),
    ).status_code == 403


def test_parents_still_cannot_create_students(client, school, parent_user, auth_headers):
    res = client.post(
        "/students/",
        params={"name": "Self Made", "school_id": school.id, "parent_id": parent_user.id},
        headers=auth_headers,
    )
    assert res.status_code == 403


def test_issue_page_offers_typed_card_number(client):
    from app.routes import issue
    assert 'id="oManualUid"' in issue.PAGE
    assert "function manualAssign()" in issue.PAGE


# ── Undoing a link made by mistake ───────────────────
def _link(client, student, uid, headers):
    return client.put(f"/students/{student.id}/assign-nfc",
                      params={"tag_uid": uid}, headers=headers)


def _undo(client, student, headers):
    return client.post(f"/students/{student.id}/undo-card-link", headers=headers)


def test_school_can_undo_a_fresh_link_and_the_card_is_reusable(
    client, db_session, school, parent_user, admin_headers,
):
    wrong, _, _ = _student_without_card(db_session, school, parent_user)
    right, _, _ = _student_without_card(db_session, school, parent_user)
    assert _link(client, wrong, "04A21B55", admin_headers).status_code == 200

    res = _undo(client, wrong, admin_headers)
    assert res.status_code == 200, res.text

    db_session.expire_all()
    slot = db_session.query(models.NFCTag).filter_by(student_id=wrong.id).one()
    assert slot.tag_uid is None and slot.is_active is True
    assert _link(client, right, "04A21B55", admin_headers).status_code == 200


def test_undo_is_refused_once_something_was_bought_with_the_card(
    client, db_session, school, parent_user, merchant, admin_headers, staff_headers,
):
    student, wallet, _ = _student_without_card(db_session, school, parent_user)
    _link(client, student, "04A21B55", admin_headers)
    pay = client.post("/payments/nfc", params={
        "tag_uid": "04A21B55", "merchant_id": merchant.id, "amount": 500,
        "request_id": str(uuid.uuid4()),
    }, headers=staff_headers)
    assert pay.status_code == 200

    res = _undo(client, student, admin_headers)
    assert res.status_code == 409
    db_session.expire_all()
    assert db_session.query(models.NFCTag).filter_by(tag_uid="04A21B55").one().is_active


def test_undo_is_refused_for_a_link_older_than_the_check(
    client, db_session, student_with_wallet, admin_headers,
):
    student, _, nfc = student_with_wallet          # linked with no linked_at
    assert _undo(client, student, admin_headers).status_code == 409


def test_undo_puts_a_fulfilled_card_order_back_to_owed(
    client, db_session, school, parent_user, admin_headers,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    db_session.add(models.CardOrder(
        student_id=student.id, ordered_by=parent_user.id, card_color="Blue",
        amount=25000, status="paid", reference="CARD-undo", momo_phone="256771234567",
        network="MTN",
    ))
    db_session.commit()
    _link(client, student, "04A21B55", admin_headers)
    _undo(client, student, admin_headers)
    db_session.expire_all()
    order = db_session.query(models.CardOrder).filter_by(reference="CARD-undo").one()
    assert order.status == "paid" and order.fulfilled_at is None


def test_only_the_childs_school_can_undo(
    client, db_session, school, second_school, parent_user, admin_headers, auth_headers,
):
    student, _, _ = _student_without_card(db_session, school, parent_user)
    _link(client, student, "04A21B55", admin_headers)
    other = make_admin(db_session, second_school, phone="256700999041")
    assert _undo(client, student, headers_for(other)).status_code == 403
    assert _undo(client, student, auth_headers).status_code == 403


def test_issue_page_offers_guardian_phone_and_undo(client):
    from app.routes import issue
    assert "function editPhone(" in issue.PAGE and "/guardian-phone" in issue.PAGE
    assert "function undoLink(" in issue.PAGE and "/undo-card-link" in issue.PAGE
