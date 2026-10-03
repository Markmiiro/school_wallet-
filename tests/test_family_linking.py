# ================================================
# tests/test_family_linking.py
# ------------------------------------------------
# How a parent gets their children (decision 3 Oct 2026).
#
# The school's roster carries each child's guardian phone number. A
# parent whose account phone matches sees those children appear, after
# proving once, by SMS code, that they hold that phone. A card number is
# never what links a family: it is printed on the card.
#
#   POST /students/?guardian_phone=...        school: roster entry
#   PUT  /students/{id}/guardian-phone        school: set or correct it
#   GET  /family/claimable                    parent: how many, not who
#   POST /family/send-code                    parent: SMS a 6-digit code
#   POST /family/claim  {code}                parent: attach them
# ================================================

from datetime import datetime, timedelta

import pytest

from app import models
from tests.conftest import headers_for, make_admin

PHONE = "256700111222"   # parent_user's phone in conftest


@pytest.fixture()
def sms(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.routes.family.send_sms_sync",
        lambda phone, message: sent.append((phone, message)) or {"success": True},
    )
    return sent


def _code(sms):
    import re
    return re.search(r"\b(\d{6})\b", sms[-1][1]).group(1)


def _roster_child(client, admin_headers, school, name, phone=PHONE):
    res = client.post("/students/", params={
        "name": name, "school_id": school.id, "guardian_phone": phone,
    }, headers=admin_headers)
    assert res.status_code == 200, res.text
    return res.json()["student"]["id"]


def _claim(client, headers, code=None):
    return client.post("/family/claim", json={"code": code} if code else {},
                       headers=headers)


# ── The school's roster ──────────────────────────────
def test_school_creates_a_roster_child_with_a_guardian_phone_and_no_parent(
    client, db_session, school, admin_headers,
):
    res = client.post("/students/", params={
        "name": "Amina N", "school_id": school.id, "guardian_phone": "0700 111 222",
    }, headers=admin_headers)
    assert res.status_code == 200, res.text
    student = db_session.get(models.Student, res.json()["student"]["id"])
    assert student.parent_id is None
    assert student.guardian_phone == PHONE


def test_a_bad_guardian_phone_is_refused(client, school, admin_headers):
    res = client.post("/students/", params={
        "name": "Amina N", "school_id": school.id, "guardian_phone": "12345",
    }, headers=admin_headers)
    assert res.status_code == 422


def test_school_can_correct_the_guardian_phone_only_for_its_own_pupils(
    client, db_session, school, second_school, admin_headers, auth_headers,
):
    sid = _roster_child(client, admin_headers, school, "Amina N", phone="256700000001")
    url = f"/students/{sid}/guardian-phone"
    assert client.put(url, params={"phone": "0700111222"},
                      headers=admin_headers).status_code == 200
    assert db_session.get(models.Student, sid).guardian_phone == PHONE

    other = make_admin(db_session, second_school, phone="256700999050")
    assert client.put(url, params={"phone": "0700000002"},
                      headers=headers_for(other)).status_code == 403
    assert client.put(url, params={"phone": "0700000002"},
                      headers=auth_headers).status_code == 403


def test_school_list_shows_the_guardian_phone(client, school, admin_headers):
    _roster_child(client, admin_headers, school, "Amina N")
    rows = client.get("/students/", headers=admin_headers).json()
    rows = rows["students"] if isinstance(rows, dict) else rows
    assert any(r.get("guardian_phone") == PHONE for r in rows)


# ── What the parent sees before proving the phone ────
def test_claimable_gives_a_count_but_no_names(
    client, school, admin_headers, auth_headers,
):
    _roster_child(client, admin_headers, school, "Amina N")
    _roster_child(client, admin_headers, school, "Brian N")
    _roster_child(client, admin_headers, school, "Someone Else", phone="256700000009")
    res = client.get("/family/claimable", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {"count": 2, "verified": False}
    assert "Amina" not in res.text


def test_claim_needs_a_code_first(client, db_session, school, admin_headers, auth_headers):
    sid = _roster_child(client, admin_headers, school, "Amina N")
    res = _claim(client, auth_headers)
    assert res.status_code == 400
    assert db_session.get(models.Student, sid).parent_id is None


def test_staff_cannot_use_the_family_routes(client, staff_headers):
    assert client.get("/family/claimable", headers=staff_headers).status_code == 403
    assert client.post("/family/send-code", headers=staff_headers).status_code == 403


# ── The code ─────────────────────────────────────────
def test_code_is_sent_to_the_account_phone_and_stored_hashed(
    client, db_session, school, admin_headers, auth_headers, sms,
):
    _roster_child(client, admin_headers, school, "Amina N")
    res = client.post("/family/send-code", headers=auth_headers)
    assert res.status_code == 200, res.text
    assert sms[0][0] == PHONE
    code = _code(sms)
    row = db_session.query(models.PhoneVerification).one()
    assert code not in (row.code_hash or "")


def test_no_code_is_sent_when_nothing_is_waiting(client, auth_headers, sms):
    assert client.post("/family/send-code", headers=auth_headers).status_code == 404
    assert sms == []


def test_codes_cannot_be_requested_back_to_back(
    client, school, admin_headers, auth_headers, sms,
):
    _roster_child(client, admin_headers, school, "Amina N")
    assert client.post("/family/send-code", headers=auth_headers).status_code == 200
    assert client.post("/family/send-code", headers=auth_headers).status_code == 429
    assert len(sms) == 1


def test_wrong_code_is_refused_and_five_wrong_codes_burn_it(
    client, db_session, school, admin_headers, auth_headers, sms,
):
    sid = _roster_child(client, admin_headers, school, "Amina N")
    client.post("/family/send-code", headers=auth_headers)
    code = _code(sms)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert _claim(client, auth_headers, wrong).status_code == 400
    assert _claim(client, auth_headers, code).status_code == 429
    assert db_session.get(models.Student, sid).parent_id is None


def test_an_expired_code_is_refused(
    client, db_session, school, admin_headers, auth_headers, sms,
):
    _roster_child(client, admin_headers, school, "Amina N")
    client.post("/family/send-code", headers=auth_headers)
    row = db_session.query(models.PhoneVerification).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    assert _claim(client, auth_headers, _code(sms)).status_code == 400


# ── Claiming ─────────────────────────────────────────
def test_right_code_attaches_only_unclaimed_children_on_that_number(
    client, db_session, school, admin_headers, auth_headers, parent_user, sms,
):
    a = _roster_child(client, admin_headers, school, "Amina N")
    b = _roster_child(client, admin_headers, school, "Brian N")
    other = _roster_child(client, admin_headers, school, "Someone Else", phone="256700000009")
    # Already someone's: the school decides, not a phone match.
    taken = _roster_child(client, admin_headers, school, "Taken Child")
    someone = models.User(name="X", phone="256700000123", role="parent")
    db_session.add(someone)
    db_session.commit()
    db_session.get(models.Student, taken).parent_id = someone.id
    db_session.commit()

    client.post("/family/send-code", headers=auth_headers)
    res = _claim(client, auth_headers, _code(sms))
    assert res.status_code == 200, res.text
    assert sorted(res.json()["added"]) == ["Amina N", "Brian N"]

    db_session.expire_all()
    assert db_session.get(models.Student, a).parent_id == parent_user.id
    assert db_session.get(models.Student, b).parent_id == parent_user.id
    assert db_session.get(models.Student, other).parent_id is None
    assert db_session.get(models.Student, taken).parent_id == someone.id

    kids = client.get(f"/students/parent/{parent_user.id}", headers=auth_headers).json()
    assert {s["name"] for s in kids["students"]} == {"Amina N", "Brian N"}


def test_once_verified_a_new_roster_child_is_claimed_without_another_code(
    client, school, admin_headers, auth_headers, sms,
):
    _roster_child(client, admin_headers, school, "Amina N")
    client.post("/family/send-code", headers=auth_headers)
    _claim(client, auth_headers, _code(sms))

    _roster_child(client, admin_headers, school, "Baby N")
    assert client.get("/family/claimable", headers=auth_headers).json() == \
        {"count": 1, "verified": True}
    res = _claim(client, auth_headers)
    assert res.status_code == 200 and res.json()["added"] == ["Baby N"]
    assert len(sms) == 1


def test_a_code_sent_to_one_parent_does_not_work_for_another(
    client, db_session, school, admin_headers, auth_headers, sms,
):
    _roster_child(client, admin_headers, school, "Amina N")
    client.post("/family/send-code", headers=auth_headers)
    code = _code(sms)
    from app.auth import hash_pin
    from app.terms import CURRENT_TERMS_VERSION
    intruder = models.User(name="I", phone="256700555920", role="parent",
                           pin_hash=hash_pin("1234"), terms_version=CURRENT_TERMS_VERSION)
    db_session.add(intruder)
    db_session.commit()
    assert _claim(client, headers_for(intruder), code).status_code == 400


# ── Account deletion removes the roster phone too ────
def test_account_closure_clears_the_guardian_phone(
    client, db_session, school, admin_headers, auth_headers, super_admin_headers,
    sms, monkeypatch,
):
    sid = _roster_child(client, admin_headers, school, "Amina N")
    client.post("/family/send-code", headers=auth_headers)
    _claim(client, auth_headers, _code(sms))

    monkeypatch.setattr("app.closures.send_sms_sync", lambda *a, **k: None)
    assert client.post("/account/closure", json={"pin": "1234", "confirm": "DELETE"},
                       headers=auth_headers).status_code == 202
    for c in db_session.query(models.AccountClosure).all():
        c.process_after = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()
    client.post("/account/closures/process", headers=super_admin_headers)
    db_session.expire_all()
    assert db_session.get(models.Student, sid).guardian_phone is None
