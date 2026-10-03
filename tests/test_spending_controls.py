# ================================================
# tests/test_spending_controls.py
# ------------------------------------------------
# A child's spending controls (3 Oct 2026).
#
#   GET  /wallets/{student_id}/controls      limit, today's spend, card
#                                            state, change history
#   PUT  /wallets/{student_id}/limit         {daily_limit, pin}
#   POST /students/{student_id}/card/block   {pin}  pause the card
#   POST /students/{student_id}/card/unblock {pin}  resume it
#
# These are money controls: every change needs the caller's PIN (a wrong
# PIN is 400 and counts toward the login lockout), and every change
# writes an audit row — who, what, from, to, when.
# ================================================

import uuid
from datetime import datetime, timedelta

from app import models
from tests.conftest import headers_for, make_admin, make_student_with_wallet


def _limit(client, student, headers, value, pin="1234"):
    return client.put(f"/wallets/{student.id}/limit",
                      json={"daily_limit": value, "pin": pin}, headers=headers)


def _card(client, student, action, headers, pin="1234"):
    return client.post(f"/students/{student.id}/card/{action}",
                       json={"pin": pin}, headers=headers)


def _controls(client, student, headers):
    return client.get(f"/wallets/{student.id}/controls", headers=headers)


def _pay(client, nfc, merchant, staff_headers, amount):
    return client.post("/payments/nfc", params={
        "tag_uid": nfc.tag_uid, "merchant_id": merchant.id, "amount": amount,
        "request_id": str(uuid.uuid4()),
    }, headers=staff_headers)


def _audit(db_session):
    db_session.expire_all()
    return db_session.query(models.ControlChange).order_by(models.ControlChange.id).all()


# ── The controls view ────────────────────────────────
def test_controls_show_limit_todays_spend_and_card(
    client, db_session, student_with_wallet, merchant, auth_headers, staff_headers,
):
    student, wallet, nfc = student_with_wallet
    assert _pay(client, nfc, merchant, staff_headers, 1500).status_code == 200
    # Yesterday (Kampala) does not count toward today.
    db_session.add(models.Transaction(
        wallet_id=wallet.id, merchant_id=merchant.id, amount=4000, type="payment",
        status="completed", timestamp=datetime.utcnow() - timedelta(days=1, hours=1),
    ))
    db_session.commit()

    res = _controls(client, student, auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["daily_limit"] == 20000
    assert body["spent_today"] == 1500
    assert body["remaining_today"] == 18500
    assert body["limit_min"] == 500 and body["limit_max"] == 5_000_000
    assert body["card"]["state"] == "active"
    assert body["card"]["can_block"] is True and body["card"]["can_unblock"] is False
    assert body["history"] == []


def test_controls_are_for_the_childs_parent_and_school_only(
    client, db_session, second_school, student_with_wallet, admin_headers, staff_headers,
):
    student, _, _ = student_with_wallet
    stranger = models.User(name="S", phone="256700555930", role="parent")
    db_session.add(stranger)
    db_session.commit()
    other_admin = make_admin(db_session, second_school, phone="256700999060")
    assert _controls(client, student, admin_headers).status_code == 200
    for h in (headers_for(stranger), headers_for(other_admin), staff_headers):
        assert _controls(client, student, h).status_code == 403


# ── Limit changes need the PIN and are recorded ──────
def test_limit_change_needs_the_pin(client, db_session, student_with_wallet, auth_headers):
    student, wallet, _ = student_with_wallet
    no_pin = client.put(f"/wallets/{student.id}/limit", json={"daily_limit": 3000},
                        headers=auth_headers)
    assert no_pin.status_code == 422

    wrong = _limit(client, student, auth_headers, 3000, pin="9999")
    assert wrong.status_code == 400            # 400, never 401: no sign-out
    assert "Incorrect PIN" in wrong.json()["detail"]

    db_session.expire_all()
    assert db_session.get(models.Wallet, wallet.id).daily_limit == 20000
    assert db_session.get(models.User, student.parent_id).failed_login_attempts == 1
    assert _audit(db_session) == []


def test_limit_change_is_audited(
    client, db_session, student_with_wallet, parent_user, auth_headers,
):
    student, _, _ = student_with_wallet
    before = datetime.utcnow()
    assert _limit(client, student, auth_headers, 3000).status_code == 200

    [row] = _audit(db_session)
    assert row.student_id == student.id
    assert row.actor_user_id == parent_user.id and row.actor_role == "parent"
    assert row.control == "daily_limit"
    assert (row.old_value, row.new_value) == ("20000", "3000")
    assert before - timedelta(seconds=1) <= row.created_at <= datetime.utcnow()


def test_an_unchanged_limit_writes_no_audit_row(
    client, db_session, student_with_wallet, auth_headers,
):
    student, _, _ = student_with_wallet
    assert _limit(client, student, auth_headers, 20000).status_code == 200
    assert _audit(db_session) == []


def test_parent_sees_the_schools_change_in_the_history(
    client, db_session, student_with_wallet, auth_headers, admin_headers,
):
    student, _, _ = student_with_wallet
    _limit(client, student, admin_headers, 15000)
    _limit(client, student, auth_headers, 8000)
    history = _controls(client, student, auth_headers).json()["history"]
    assert [(h["by"], h["control"], h["from"], h["to"]) for h in history] == [
        ("you", "daily_limit", "15000", "8000"),
        ("school", "daily_limit", "20000", "15000"),
    ]
    assert history[0]["at"].endswith("Z")


# ── Blocking and unblocking the card ─────────────────
def test_block_pauses_the_card_and_unblock_resumes_it(
    client, db_session, student_with_wallet, merchant, auth_headers, staff_headers,
):
    student, wallet, nfc = student_with_wallet

    assert _card(client, student, "block", auth_headers, pin="9999").status_code == 400
    assert _pay(client, nfc, merchant, staff_headers, 500).status_code == 200

    res = _card(client, student, "block", auth_headers)
    assert res.status_code == 200, res.text
    card = _controls(client, student, auth_headers).json()["card"]
    assert card["state"] == "blocked"
    assert card["can_block"] is False and card["can_unblock"] is True

    paused = _pay(client, nfc, merchant, staff_headers, 500)
    assert paused.status_code == 403
    assert "paused" in paused.json()["detail"].lower()

    assert _card(client, student, "unblock", auth_headers).status_code == 200
    assert _pay(client, nfc, merchant, staff_headers, 500).status_code == 200
    db_session.refresh(wallet)
    assert wallet.balance == 9000

    rows = _audit(db_session)
    assert [(r.control, r.old_value, r.new_value) for r in rows] == [
        ("card", "active", "blocked"),
        ("card", "blocked", "active"),
    ]


def test_cannot_block_twice_or_unblock_a_working_card(
    client, student_with_wallet, auth_headers,
):
    student, _, _ = student_with_wallet
    assert _card(client, student, "unblock", auth_headers).status_code == 409
    _card(client, student, "block", auth_headers)
    assert _card(client, student, "block", auth_headers).status_code == 409


def test_unblock_refused_once_the_school_linked_a_replacement(
    client, db_session, student_with_wallet, auth_headers, admin_headers,
):
    student, _, nfc = student_with_wallet
    _card(client, student, "block", auth_headers)
    assert client.put(f"/students/{student.id}/assign-nfc",
                      params={"tag_uid": "04A21B55"},
                      headers=admin_headers).status_code == 200
    assert _card(client, student, "unblock", auth_headers).status_code == 409
    db_session.expire_all()
    assert db_session.get(models.NFCTag, nfc.id).is_active is False


def test_a_blocked_card_can_still_be_reported_lost_and_that_is_audited(
    client, db_session, student_with_wallet, auth_headers,
):
    student, _, nfc = student_with_wallet
    _card(client, student, "block", auth_headers)
    res = client.post(f"/students/{student.id}/report-stolen",
                      params={"reason": "lost"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert db_session.get(models.NFCTag, nfc.id).status == "lost"
    assert _card(client, student, "unblock", auth_headers).status_code == 409
    assert [(r.old_value, r.new_value) for r in _audit(db_session)][-1] == ("blocked", "lost")


def test_school_can_block_its_pupils_card_others_cannot(
    client, db_session, second_school, student_with_wallet, admin_headers, staff_headers,
):
    student, _, _ = student_with_wallet
    other_admin = make_admin(db_session, second_school, phone="256700999061")
    assert _card(client, student, "block", headers_for(other_admin)).status_code == 403
    assert _card(client, student, "block", staff_headers).status_code == 403
    assert _card(client, student, "block", admin_headers).status_code == 200
    assert _audit(db_session)[0].actor_role == "admin"


def test_no_card_means_nothing_to_block(client, db_session, school, parent_user, auth_headers):
    student, _, _ = make_student_with_wallet(db_session, school, parent_user, tag_uid=None)
    assert _card(client, student, "block", auth_headers).status_code == 404
    card = _controls(client, student, auth_headers).json()["card"]
    assert card["state"] == "none" and card["can_block"] is False


# ── Account deletion and a blocked card ──────────────
def test_closing_an_account_closes_a_blocked_card_and_cancel_keeps_it_blocked(
    client, db_session, student_with_wallet, auth_headers, super_admin_headers, monkeypatch,
):
    student, _, nfc = student_with_wallet
    _card(client, student, "block", auth_headers)
    monkeypatch.setattr("app.closures.send_sms_sync", lambda *a, **k: None)
    assert client.post("/account/closure", json={"pin": "1234", "confirm": "DELETE"},
                       headers=auth_headers).status_code == 202
    db_session.expire_all()
    assert db_session.get(models.NFCTag, nfc.id).status == "closed"

    closure = db_session.query(models.AccountClosure).one()
    client.post(f"/account/closures/{closure.id}/cancel", headers=super_admin_headers)
    db_session.expire_all()
    tag = db_session.get(models.NFCTag, nfc.id)
    assert tag.status == "blocked" and tag.is_active is False


# ── Buying a replacement while the card is blocked ───
# Allowed (decision 3 Oct 2026): the parent need not report the card lost
# first. If the card turns up and is unblocked before the new one is
# handed over, it works again, and linking the new card retires it.
def test_blocked_card_allows_buying_a_replacement_and_unblock_still_works(
    client, db_session, student_with_wallet, auth_headers, admin_headers, monkeypatch,
):
    from tests.test_card_orders import _order

    student, _, nfc = student_with_wallet
    _card(client, student, "block", auth_headers)

    res = _order(client, auth_headers, student.id)
    assert res.status_code == 200, res.text
    ref = res.json()["reference_id"]
    monkeypatch.setenv("TEST_YO_TX_STATUS", "SUCCEEDED")
    assert client.get(f"/cards/orders/{ref}", headers=auth_headers).json()["status"] == "paid"

    # The old card is found: unblocking brings it back as the one usable card.
    assert _card(client, student, "unblock", auth_headers).status_code == 200
    db_session.expire_all()
    usable = [t for t in db_session.get(models.Student, student.id).nfc_tags if t.is_active]
    assert [t.id for t in usable] == [nfc.id]
    card = _controls(client, student, auth_headers).json()["card"]
    assert card["state"] == "active" and card["can_block"] is True

    # The paid card is still owed, and linking it retires the old one.
    assert db_session.query(models.CardOrder).one().status == "paid"
    assert client.put(f"/students/{student.id}/assign-nfc",
                      params={"tag_uid": "04A21B55"},
                      headers=admin_headers).status_code == 200
    db_session.expire_all()
    usable = [t for t in db_session.get(models.Student, student.id).nfc_tags if t.is_active]
    assert [t.tag_uid for t in usable] == ["04A21B55"]
    assert db_session.get(models.NFCTag, nfc.id).status == "replaced"
    assert db_session.query(models.CardOrder).one().status == "fulfilled"
