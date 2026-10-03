# ================================================
# tests/test_account_closure.py
# ------------------------------------------------
# A parent deletes their account (app/closures.py, app/routes/account.py).
#
#   GET  /account/closure/preview   what will happen, before confirming
#   POST /account/closure           PIN + "DELETE": start the closure
#   GET  /account/delete            web page for the Play Store URL
#   POST /account/delete/preview    the same, by phone + PIN
#   POST /account/delete
#   GET  /account/closures          operator: open closures
#   POST /account/closures/{id}/cancel   operator, during the hold only
#   POST /account/closures/process       operator: run what is due now
#
# Lifecycle: request → 72-hour hold → refund to the registered number →
# anonymise. Financial rows are never deleted; the person is removed
# from them. Refunds use the payout rules: record before sending, and a
# send whose fate is unknown is polled, never re-sent.
# ================================================

from datetime import datetime, timedelta

import pytest

from app import models
from app.routes.ussd import PendingUssdRegistration
from tests.conftest import headers_for, make_student_with_wallet

PHONE = "256700111222"   # parent_user's phone in conftest


# ── Helpers ───────────────────────────────────────────
@pytest.fixture()
def sms(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.closures.send_sms_sync",
        lambda phone, message: sent.append((phone, message)) or {"success": True},
    )
    return sent


@pytest.fixture()
def yo(monkeypatch):
    """Records refund sends; answers with whatever `yo.next` says."""
    class Yo:
        sends = []
        polls = []
        next = {"_Delivery": "responded", "Status": "OK",
                "TransactionStatus": "SUCCEEDED"}
        poll_answer = {"_Delivery": "responded", "Status": "OK",
                       "TransactionStatus": "SUCCEEDED"}

    async def fake_disburse(**kwargs):
        Yo.sends.append(kwargs)
        return dict(Yo.next)

    async def fake_verify(ref):
        Yo.polls.append(ref)
        return dict(Yo.poll_answer)

    Yo.sends, Yo.polls = [], []
    monkeypatch.setattr("app.closures.disburse_to_merchant", fake_disburse)
    monkeypatch.setattr("app.closures.verify_transaction", fake_verify)
    return Yo


def _family(db_session, school, parent, *, balances=(7000, 3000)):
    kids = []
    for i, bal in enumerate(balances):
        student, wallet, card = make_student_with_wallet(
            db_session, school, parent, balance=bal, tag_uid=f"CAFE{i:04d}",
        )
        student.name = f"Child {i}"
        student.dob = "2015-01-12"
        student.class_name = "P4"
        student.account_number = f"00300000000{i}"
        db_session.commit()
        kids.append((student, wallet, card))
    return kids


def _close(client, headers, pin="1234", confirm="DELETE"):
    return client.post("/account/closure", json={"pin": pin, "confirm": confirm},
                       headers=headers)


def _due_now(db_session):
    for c in db_session.query(models.AccountClosure).all():
        c.process_after = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()


def _process(client, super_admin_headers):
    return client.post("/account/closures/process", headers=super_admin_headers)


def _closure(db_session):
    db_session.expire_all()
    return db_session.query(models.AccountClosure).one()


# ── Preview ───────────────────────────────────────────
def test_preview_shows_children_balances_card_fee_and_refund_number(
    client, db_session, school, parent_user, auth_headers,
):
    kids = _family(db_session, school, parent_user)
    db_session.add(models.CardOrder(
        student_id=kids[0][0].id, ordered_by=parent_user.id, card_color="Blue",
        amount=25000, status="paid", reference="CARD-x1",
        momo_phone="256771234567", network="MTN",
    ))
    db_session.commit()

    res = client.get("/account/closure/preview", headers=auth_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [c["name"] for c in body["children"]] == ["Child 0", "Child 1"]
    assert [c["balance"] for c in body["children"]] == [7000, 3000]
    assert body["unissued_card_fees"] == 25000
    assert body["refund_total"] == 35000
    assert body["refund_phone"].endswith("222") and "700111" not in body["refund_phone"]
    assert body["hold_hours"] == 72
    assert body["consequences"], "the app shows these before the PIN step"


def test_staff_cannot_close_an_account_this_way(client, staff_headers):
    assert client.get("/account/closure/preview", headers=staff_headers).status_code == 403
    assert _close(client, staff_headers).status_code == 403


# ── Request ───────────────────────────────────────────
def test_wrong_pin_changes_nothing_and_counts_toward_lockout(
    client, db_session, school, parent_user, auth_headers,
):
    _family(db_session, school, parent_user)
    res = _close(client, auth_headers, pin="9999")
    assert res.status_code == 401
    db_session.expire_all()
    assert db_session.query(models.AccountClosure).count() == 0
    user = db_session.get(models.User, parent_user.id)
    assert user.phone == PHONE
    assert user.failed_login_attempts == 1
    assert all(w.is_active for w in db_session.query(models.Wallet))


def test_confirmation_word_is_required(
    client, db_session, school, parent_user, auth_headers,
):
    _family(db_session, school, parent_user)
    for word in ("", "delete", "yes", "DELETE "):
        assert _close(client, auth_headers, confirm=word).status_code == 400
    assert db_session.query(models.AccountClosure).count() == 0


def test_request_freezes_everything_signs_out_and_holds_for_72_hours(
    client, db_session, school, parent_user, auth_headers, sms,
):
    kids = _family(db_session, school, parent_user)
    before = datetime.utcnow()

    res = _close(client, auth_headers)
    assert res.status_code == 202, res.text

    c = _closure(db_session)
    assert c.status == "held"
    assert c.refund_phone == PHONE
    assert timedelta(hours=71, minutes=59) < c.process_after - before < timedelta(hours=72, minutes=1)

    for _, wallet, card in kids:
        assert db_session.get(models.Wallet, wallet.id).is_active is False
        tag = db_session.get(models.NFCTag, card.id)
        assert tag.is_active is False and tag.status == "closed"

    user = db_session.get(models.User, parent_user.id)
    assert user.phone != PHONE

    # Every token issued before is dead: tokens name the user by phone.
    assert client.get("/auth/me", headers=auth_headers).status_code == 401
    login = client.post("/auth/login", json={"phone": PHONE, "pin": "1234"})
    assert login.status_code == 401

    assert len(sms) == 1 and sms[0][0] == PHONE
    assert "0760 945 424" in sms[0][1] and "72 hours" in sms[0][1]


def test_closed_card_cannot_pay(
    client, db_session, school, parent_user, auth_headers, staff_headers, merchant,
):
    kids = _family(db_session, school, parent_user)
    _close(client, auth_headers)
    res = client.post("/payments/nfc", params={
        "tag_uid": kids[0][2].tag_uid, "merchant_id": merchant.id, "amount": 500,
        "request_id": "tap-after-closure",
    }, headers=staff_headers)
    assert res.status_code in (400, 403, 404), res.text
    assert db_session.get(models.Wallet, kids[0][1].id).balance == 7000


# ── Processing ────────────────────────────────────────
def test_nothing_happens_during_the_hold(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    assert _process(client, super_admin_headers).status_code == 200
    assert yo.sends == []
    assert _closure(db_session).status == "held"


def test_after_the_hold_balance_and_card_fee_are_refunded_then_person_removed(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    merchant, yo,
):
    kids = _family(db_session, school, parent_user)
    w0 = kids[0][1]
    db_session.add_all([
        models.Transaction(wallet_id=w0.id, amount=10000, type="topup",
                           status="completed", reference="TOP-1",
                           momo_phone="256771234567"),
        models.Transaction(wallet_id=w0.id, merchant_id=merchant.id, amount=3000,
                           type="payment", status="completed"),
        models.CardOrder(student_id=kids[1][0].id, ordered_by=parent_user.id,
                         card_color="Red", amount=25000, status="paid",
                         reference="CARD-x2", momo_phone="256771234567",
                         network="MTN"),
        PendingUssdRegistration(reference="USSD-1", phone=PHONE,
                                student_name="Someone", dob="2016", class_name="P1",
                                school_name="Test School", card_color="Blue"),
    ])
    db_session.commit()

    _close(client, auth_headers)
    _due_now(db_session)
    res = _process(client, super_admin_headers)
    assert res.status_code == 200, res.text

    assert len(yo.sends) == 1
    send = yo.sends[0]
    assert send["phone"] == PHONE                      # registered number only
    assert send["amount"] == 7000 + 3000 + 25000
    assert send["external_reference"].startswith("NUV-CLOSE-")

    c = _closure(db_session)
    assert c.status == "completed"
    assert c.refund_amount == 35000
    assert c.refund_phone == PHONE                     # kept as proof of refund

    # Money: wallets emptied by a recorded refund, card order refunded.
    for _, wallet, _card in kids:
        assert db_session.get(models.Wallet, wallet.id).balance == 0
    refunds = db_session.query(models.Transaction).filter_by(type="refund").all()
    assert sorted(t.amount for t in refunds) == [3000, 7000]
    assert all(t.status == "completed" for t in refunds)
    order = db_session.query(models.CardOrder).filter_by(reference="CARD-x2").one()
    assert order.status == "refunded"

    # Person removed.
    user = db_session.get(models.User, parent_user.id)
    assert user.name == "Deleted user" and user.pin_hash is None
    assert user.terms_version is not None              # proof of consent kept
    for student, _w, _c in kids:
        s = db_session.get(models.Student, student.id)
        assert s.name == f"Closed account · {student.account_number}"
        assert s.dob is None and s.class_name is None
        assert s.account_number == student.account_number
    assert all(t.momo_phone is None for t in db_session.query(models.Transaction))
    assert order.momo_phone == ""
    assert db_session.query(PendingUssdRegistration).count() == 0

    # Records kept: the purchase still belongs to the same merchant.
    sale = db_session.query(models.Transaction).filter_by(type="payment").one()
    assert sale.amount == 3000 and sale.merchant_id == merchant.id


def test_zero_balance_is_anonymised_without_calling_yo(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user, balances=(0,))
    _close(client, auth_headers)
    _due_now(db_session)
    _process(client, super_admin_headers)
    assert yo.sends == []
    c = _closure(db_session)
    assert c.status == "completed" and c.refund_amount == 0


def test_a_recent_pending_top_up_defers_the_refund(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    kids = _family(db_session, school, parent_user)
    db_session.add(models.Transaction(
        wallet_id=kids[0][1].id, amount=5000, type="topup", status="pending",
        reference="TOP-PENDING", timestamp=datetime.utcnow(),
    ))
    db_session.commit()
    _close(client, auth_headers)
    _due_now(db_session)
    _process(client, super_admin_headers)
    assert yo.sends == []
    assert _closure(db_session).status == "held"


def test_failed_refund_keeps_the_person_and_is_retried_with_a_new_reference(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    _due_now(db_session)

    yo.next = {"_Delivery": "responded", "Status": "OK", "TransactionStatus": "FAILED"}
    _process(client, super_admin_headers)
    c = _closure(db_session)
    assert c.status == "failed"
    assert db_session.get(models.User, parent_user.id).name == "Test Parent"

    yo.next = {"_Delivery": "responded", "Status": "OK", "TransactionStatus": "SUCCEEDED"}
    _process(client, super_admin_headers)
    assert len(yo.sends) == 2
    assert yo.sends[0]["external_reference"] != yo.sends[1]["external_reference"]
    assert yo.sends[1]["amount"] == 10000              # same amount, not re-read
    assert _closure(db_session).status == "completed"


def test_unknown_refund_is_polled_never_resent(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    _due_now(db_session)

    yo.next = {"_Delivery": "unknown", "StatusMessage": "timeout"}
    _process(client, super_admin_headers)
    assert _closure(db_session).status == "indeterminate"

    yo.poll_answer = {"_Delivery": "responded", "Status": "OK",
                      "TransactionStatus": "PENDING"}
    _process(client, super_admin_headers)
    assert len(yo.sends) == 1 and len(yo.polls) == 1
    assert _closure(db_session).status == "indeterminate"

    yo.poll_answer = {"_Delivery": "responded", "Status": "OK",
                      "TransactionStatus": "SUCCEEDED"}
    _process(client, super_admin_headers)
    assert len(yo.sends) == 1
    assert _closure(db_session).status == "completed"


def test_daily_cron_also_processes_due_closures(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    yo, monkeypatch,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    _due_now(db_session)
    monkeypatch.setenv("SETTLEMENT_SECRET", "s3cret")
    res = client.post("/reports/settlements/auto?secret=s3cret",
                      headers=super_admin_headers)
    assert res.status_code == 200, res.text
    assert res.json()["account_closures"][0]["outcome"] == "completed"
    assert _closure(db_session).status == "completed"


# ── After it is done ──────────────────────────────────
def test_school_settlement_totals_are_unchanged_and_show_the_account_number(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    admin_headers, merchant, yo,
):
    kids = _family(db_session, school, parent_user)
    db_session.add(models.Transaction(
        wallet_id=kids[0][1].id, merchant_id=merchant.id, amount=1500,
        type="payment", status="completed",
    ))
    db_session.commit()
    today = datetime.utcnow().date()
    url = f"/reports/school/{school.id}/settlement?report_date={today}"
    before = client.get(url, headers=admin_headers).json()

    _close(client, auth_headers)
    _due_now(db_session)
    _process(client, super_admin_headers)

    after = client.get(url, headers=admin_headers).json()
    assert str(before).count("1500") == str(after).count("1500")
    assert "Child 0" not in str(after)
    assert "Closed account · 003000000000" in str(after)


def test_refund_phone_is_dropped_after_the_retention_period(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    _due_now(db_session)
    _process(client, super_admin_headers)

    c = _closure(db_session)
    c.completed_at = datetime.utcnow() - timedelta(days=5 * 366)
    db_session.commit()
    _process(client, super_admin_headers)
    c = _closure(db_session)
    assert c.refund_phone is None and c.refund_amount == 10000


# ── Cancelling ────────────────────────────────────────
def test_operator_can_cancel_during_the_hold_and_everything_comes_back(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
):
    kids = _family(db_session, school, parent_user)
    kids[1][1].is_active = False          # frozen by the parent before; stays so
    db_session.commit()
    _close(client, auth_headers)
    c = _closure(db_session)

    res = client.post(f"/account/closures/{c.id}/cancel", headers=super_admin_headers)
    assert res.status_code == 200, res.text
    db_session.expire_all()
    assert _closure(db_session).status == "cancelled"
    assert db_session.get(models.User, parent_user.id).phone == PHONE
    assert db_session.get(models.Wallet, kids[0][1].id).is_active is True
    assert db_session.get(models.Wallet, kids[1][1].id).is_active is False
    tag = db_session.get(models.NFCTag, kids[0][2].id)
    assert tag.is_active is True and tag.status == "active"
    assert client.post("/auth/login", json={"phone": PHONE, "pin": "1234"}).status_code == 200


def test_cannot_cancel_once_processed_and_school_admins_cannot_cancel(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    c = _closure(db_session)
    assert client.post(f"/account/closures/{c.id}/cancel",
                       headers=admin_headers).status_code == 403
    _due_now(db_session)
    _process(client, super_admin_headers)
    assert client.post(f"/account/closures/{c.id}/cancel",
                       headers=super_admin_headers).status_code == 409


def test_operator_list_shows_open_closures(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    admin_headers,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    assert client.get("/account/closures", headers=admin_headers).status_code == 403
    rows = client.get("/account/closures", headers=super_admin_headers).json()
    assert rows[0]["status"] == "held" and rows[0]["refund_phone"] == PHONE


# ── Web route (Play Store deletion URL) ──────────────
def test_web_page_is_public_html(client):
    res = client.get("/account/delete")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Delete your Nuvora account" in res.text
    assert "0760 945 424" in res.text


def test_web_request_needs_phone_pin_and_confirmation(
    client, db_session, school, parent_user, sms,
):
    _family(db_session, school, parent_user)

    wrong = client.post("/account/delete/preview", json={"phone": "0700111222", "pin": "0000"})
    assert wrong.status_code == 401

    prev = client.post("/account/delete/preview", json={"phone": "0700111222", "pin": "1234"})
    assert prev.status_code == 200 and prev.json()["refund_total"] == 10000

    no = client.post("/account/delete", json={"phone": "0700111222", "pin": "1234",
                                              "confirm": "ok"})
    assert no.status_code == 400

    yes = client.post("/account/delete", json={"phone": "0700111222", "pin": "1234",
                                               "confirm": "DELETE"})
    assert yes.status_code == 202, yes.text
    c = _closure(db_session)
    assert c.status == "held" and c.requested_via == "web"


def test_unknown_phone_on_the_web_gets_the_same_answer_as_a_wrong_pin(client):
    a = client.post("/account/delete/preview", json={"phone": "0799999999", "pin": "1234"})
    assert a.status_code == 401


def test_a_second_request_is_refused(
    client, db_session, school, parent_user, auth_headers, sms,
):
    _family(db_session, school, parent_user)
    assert _close(client, auth_headers).status_code == 202
    # The token is dead now; the web route finds no account by that phone.
    again = client.post("/account/delete", json={"phone": PHONE, "pin": "1234",
                                                  "confirm": "DELETE"})
    assert again.status_code == 401
    assert db_session.query(models.AccountClosure).count() == 1


def test_a_refund_being_sent_right_now_is_left_alone(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    c = _closure(db_session)
    c.status, c.refund_amount, c.attempts = "pending", 10000, 1
    c.yo_reference, c.last_sent_at = f"NUV-CLOSE-{c.id}-1", datetime.utcnow()
    db_session.commit()
    _process(client, super_admin_headers)
    assert yo.sends == [] and yo.polls == []
    assert _closure(db_session).status == "pending"


def test_cron_stops_after_three_failed_sends_and_an_operator_can_retry(
    client, db_session, school, parent_user, auth_headers, super_admin_headers,
    yo, monkeypatch,
):
    _family(db_session, school, parent_user)
    _close(client, auth_headers)
    _due_now(db_session)
    monkeypatch.setenv("SETTLEMENT_SECRET", "s3cret")
    yo.next = {"_Delivery": "responded", "Status": "OK", "TransactionStatus": "FAILED"}
    for _ in range(5):
        client.post("/reports/settlements/auto?secret=s3cret", headers=super_admin_headers)
    assert len(yo.sends) == 3
    assert _closure(db_session).status == "needs_human"

    yo.next = {"_Delivery": "responded", "Status": "OK", "TransactionStatus": "SUCCEEDED"}
    _process(client, super_admin_headers)          # operator: uncapped
    assert len(yo.sends) == 4
    assert _closure(db_session).status == "completed"


def test_money_arriving_after_closure_is_flagged_every_run(
    client, db_session, school, parent_user, auth_headers, super_admin_headers, yo,
):
    kids = _family(db_session, school, parent_user, balances=(0,))
    _close(client, auth_headers)
    _due_now(db_session)
    _process(client, super_admin_headers)
    # A USSD top-up by account number lands after the closure finished.
    wallet = db_session.get(models.Wallet, kids[0][1].id)
    wallet.balance = 2000
    db_session.commit()
    results = _process(client, super_admin_headers).json()["results"]
    assert {"closure_id": _closure(db_session).id, "outcome": "money_after_closure",
            "wallet_id": wallet.id, "balance_ugx": 2000} in results
