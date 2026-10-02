# ================================================
# tests/test_card_orders.py
# ------------------------------------------------
# Buying a Smart Card from the parent app (app/routes/cards.py).
#
#   POST /cards/orders                     charge UGX 25,000 for a card
#   GET  /cards/orders/{reference}         poll: pending | paid | failed
#   GET  /cards/orders/student/{id}        a child's orders
#   GET  /cards/orders/school/{id}         cards the school owes (admin)
#
# The one thing that must never happen: the card fee landing in the
# child's wallet. webhook.py credits any pending Transaction it can match
# to a confirmed reference, so card orders are kept out of `transactions`.
# ================================================

from unittest.mock import patch

import pytest

from app import models
from app.routes.cards import CARD_PRICE_UGX
from tests.conftest import headers_for, make_admin, make_student_with_wallet
from tests.test_webhook_credit import ipn_payload


def _child_without_card(db_session, school, parent, **kw):
    return make_student_with_wallet(db_session, school, parent, tag_uid=None, **kw)


def _order(client, headers, student_id, **overrides):
    body = {
        "student_id": student_id,
        "card_color": "Green",
        "phone_number": "256771234567",
        "network": "MTN",
    }
    body.update(overrides)
    return client.post("/cards/orders", json=body, headers=headers)


@pytest.fixture()
def yo_says(monkeypatch):
    """Set what Yo's status check answers in test mode."""
    def _set(status):
        monkeypatch.setenv("TEST_YO_TX_STATUS", status)
    return _set


def test_parent_buys_card_and_it_is_paid_once_yo_confirms(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, wallet, slot = _child_without_card(db_session, school, parent_user, balance=3000)

    res = _order(client, auth_headers, student.id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending"
    assert body["amount"] == CARD_PRICE_UGX
    assert body["reference_id"].startswith("CARD-")
    ref = body["reference_id"]

    # Not approved yet.
    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "pending"

    yo_says("SUCCEEDED")
    paid = client.get(f"/cards/orders/{ref}", headers=auth_headers).json()
    assert paid["status"] == "paid"
    assert paid["card_color"] == "Green"

    db_session.expire_all()
    # The colour is on the child's card slot, still waiting for a card.
    tags = db_session.query(models.NFCTag).filter_by(student_id=student.id).all()
    assert len(tags) == 1
    assert tags[0].tag_uid is None
    assert tags[0].card_color == "Green"
    # The fee did not touch the wallet, and is not a wallet transaction.
    assert db_session.get(models.Wallet, wallet.id).balance == 3000
    assert db_session.query(models.Transaction).count() == 0


def test_webhook_for_a_card_reference_never_credits_the_wallet(
    client, db_session, school, parent_user, auth_headers,
):
    student, wallet, _ = _child_without_card(db_session, school, parent_user, balance=3000)
    ref = _order(client, auth_headers, student.id).json()["reference_id"]

    with patch("app.routes.webhook.verify_yo_signature", return_value=True):
        r = client.post("/webhook/yo", data=ipn_payload(ref, CARD_PRICE_UGX))
    assert r.status_code == 200

    db_session.expire_all()
    assert db_session.get(models.Wallet, wallet.id).balance == 3000
    assert db_session.query(models.Transaction).count() == 0


def test_repeated_polls_after_success_change_nothing(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    yo_says("SUCCEEDED")
    first = client.get(f"/cards/orders/{ref}", headers=auth_headers).json()
    second = client.get(f"/cards/orders/{ref}", headers=auth_headers).json()
    assert first["status"] == second["status"] == "paid"
    assert first["paid_at"] == second["paid_at"]
    assert db_session.query(models.NFCTag).filter_by(student_id=student.id).count() == 1


def test_failed_payment_marks_order_failed_and_parent_can_try_again(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    yo_says("FAILED")
    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "failed"

    yo_says("PENDING")
    assert _order(client, auth_headers, student.id).status_code == 200


def test_indeterminate_stays_pending(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    yo_says("INDETERMINATE")
    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "pending"


def test_cannot_pay_twice_for_the_same_card(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    _order(client, auth_headers, student.id)

    # The parent closed the app before it confirmed, then came back. The
    # second attempt settles the first with Yo and refuses to charge again.
    yo_says("SUCCEEDED")
    again = _order(client, auth_headers, student.id)
    assert again.status_code == 409
    assert "already paid" in again.json()["detail"]
    assert db_session.query(models.CardOrder).count() == 1


def test_child_with_a_working_card_cannot_buy_another(
    client, db_session, school, parent_user, auth_headers,
):
    student, _, _ = make_student_with_wallet(db_session, school, parent_user)
    res = _order(client, auth_headers, student.id)
    assert res.status_code == 409
    assert db_session.query(models.CardOrder).count() == 0


def test_child_whose_card_was_lost_can_buy_a_replacement(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = make_student_with_wallet(db_session, school, parent_user)
    lost = client.post(
        f"/students/{student.id}/report-stolen", params={"reason": "lost"}, headers=auth_headers,
    )
    assert lost.status_code == 200, lost.text

    ref = _order(client, auth_headers, student.id, card_color="red").json()["reference_id"]
    yo_says("SUCCEEDED")
    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "paid"

    db_session.expire_all()
    slot = db_session.get(models.Student, student.id).active_nfc_tag
    assert slot is not None and slot.tag_uid is None and slot.card_color == "Red"


def test_linking_a_card_fulfils_the_order(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    admin = make_admin(db_session, school, phone="256700999011")
    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    yo_says("SUCCEEDED")
    client.get(f"/cards/orders/{ref}", headers=auth_headers)

    owed = client.get(f"/cards/orders/school/{school.id}", headers=headers_for(admin)).json()
    assert [o["reference_id"] for o in owed["orders"]] == [ref]

    link = client.put(
        f"/students/{student.id}/assign-nfc", params={"tag_uid": "04A21B55"},
        headers=headers_for(admin),
    )
    assert link.status_code == 200

    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "fulfilled"
    owed = client.get(f"/cards/orders/school/{school.id}", headers=headers_for(admin)).json()
    assert owed["orders"] == []

    db_session.expire_all()
    tag = db_session.query(models.NFCTag).filter_by(student_id=student.id).one()
    assert tag.tag_uid == "04A21B55" and tag.card_color == "Green"


def test_school_list_picks_up_a_payment_the_app_never_confirmed(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    admin = make_admin(db_session, school, phone="256700999012")
    ref = _order(client, auth_headers, student.id).json()["reference_id"]

    yo_says("SUCCEEDED")  # approved, but the parent's app never polled
    owed = client.get(f"/cards/orders/school/{school.id}", headers=headers_for(admin)).json()
    assert [o["reference_id"] for o in owed["orders"]] == [ref]


def test_charge_refused_by_yo_leaves_a_failed_order(
    client, db_session, school, parent_user, auth_headers, monkeypatch,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)

    async def refused(**kwargs):
        return {"Status": "ERROR", "StatusMessage": "Insufficient funds"}

    monkeypatch.setattr("app.routes.cards.charge_mobile_money", refused)
    res = _order(client, auth_headers, student.id)
    assert res.status_code == 400
    assert "Insufficient funds" in res.json()["detail"]
    assert db_session.query(models.CardOrder).one().status == "failed"


def test_price_is_fixed_by_the_server(client, db_session, school, parent_user, auth_headers):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    res = _order(client, auth_headers, student.id, amount=1)
    assert res.status_code == 200
    assert res.json()["amount"] == CARD_PRICE_UGX


@pytest.mark.parametrize("field,value", [
    ("card_color", "Purple"),
    ("phone_number", "0771234567"),
    ("network", "VODAFONE"),
])
def test_bad_input_is_rejected(client, db_session, school, parent_user, auth_headers, field, value):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    assert _order(client, auth_headers, student.id, **{field: value}).status_code == 422
    assert db_session.query(models.CardOrder).count() == 0


def test_only_the_childs_parent_or_school_admin_may_order_or_look(
    client, db_session, school, parent_user, auth_headers, staff_headers,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)

    stranger = models.User(name="Other Parent", phone="256700555666", role="parent")
    other_school = models.School(name="Other School", location="Jinja")
    db_session.add_all([stranger, other_school])
    db_session.commit()
    other_admin = make_admin(db_session, other_school, phone="256700999013")

    for headers in (headers_for(stranger), headers_for(other_admin), staff_headers):
        assert _order(client, headers, student.id).status_code == 403
    assert client.post("/cards/orders", json={}).status_code in (401, 403)

    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    for headers in (headers_for(stranger), headers_for(other_admin), staff_headers):
        assert client.get(f"/cards/orders/{ref}", headers=headers).status_code == 403
        assert client.get(f"/cards/orders/student/{student.id}", headers=headers).status_code == 403
        assert client.get(f"/cards/orders/school/{school.id}", headers=headers).status_code == 403
    assert client.get(f"/cards/orders/school/{school.id}", headers=auth_headers).status_code == 403

    listed = client.get(f"/cards/orders/student/{student.id}", headers=auth_headers).json()
    assert [o["reference_id"] for o in listed["orders"]] == [ref]


def test_unknown_child_and_unknown_order_are_404(client, parent_user, auth_headers):
    assert _order(client, auth_headers, 99999).status_code == 404
    assert client.get("/cards/orders/CARD-nope", headers=auth_headers).status_code == 404


def test_cards_owed_is_scoped_to_the_admins_school(
    client, db_session, school, parent_user, auth_headers, yo_says,
):
    student, _, _ = _child_without_card(db_session, school, parent_user)
    admin = make_admin(db_session, school, phone="256700999014")
    other_school = models.School(name="Other School", location="Jinja")
    db_session.add(other_school)
    db_session.commit()
    other_admin = make_admin(db_session, other_school, phone="256700999015")
    super_admin = models.User(name="Super", phone="256700999016", role="admin")
    db_session.add(super_admin)
    db_session.commit()

    ref = _order(client, auth_headers, student.id).json()["reference_id"]
    yo_says("SUCCEEDED")

    def owed(user):
        res = client.get("/cards/orders/owed", headers=headers_for(user))
        assert res.status_code == 200, res.text
        return [o["reference_id"] for o in res.json()["orders"]]

    assert owed(admin) == [ref]
    assert owed(other_admin) == []
    assert owed(super_admin) == [ref]
    assert client.get("/cards/orders/owed", headers=auth_headers).status_code == 403

    client.put(
        f"/students/{student.id}/assign-nfc", params={"tag_uid": "04A21B55"},
        headers=headers_for(admin),
    )
    assert owed(admin) == []


def test_issue_page_shows_cards_paid_for(client):
    from app.routes import issue
    assert "/cards/orders/owed" in issue.PAGE
    assert 'id="paidChip"' in issue.PAGE
