# ================================================
# tests/test_open_write_endpoints_lockdown.py
# ------------------------------------------------
# Findings from the app/ audit: several write endpoints had no auth at
# all.
#
#   - POST /merchants/                              (app/routes/merchants.py)
#   - POST /schools/, PUT /schools/{id},
#     POST /schools/{id}/badge, PUT /schools/{id}/badge-url
#                                                     (app/routes/schools.py)
#   - POST /users/, DELETE /users/{id}               (app/routes/users.py)
#
# Fix: gate every one of these with get_current_admin, then scope with
# assert_school_access / is_super_admin from app/auth.py — the same
# helpers app/routes/students.py's create_student already uses.
#
# users.py's create endpoint also gains a required `pin` field, so
# admin-created accounts are actually usable (before this fix,
# pin_hash was never set — the account could never log in, since
# verify_pin() returns False for a null hash), and no longer lets a
# scoped admin mint a school_id=None super-admin.
#
# DELETE /users/{id} is removed outright rather than just admin-gated:
# nothing in this codebase calls it, every other "remove" concept here
# is soft (is_active/status on Student/Wallet/NFCTag), and a hard
# delete can orphan Student.parent_id.
# ================================================

from app.models import User
from tests.conftest import make_admin, headers_for
from app.terms import CURRENT_TERMS_VERSION


# ══════════════════════════════════════════════════════
# merchants.py — POST /merchants/
# ══════════════════════════════════════════════════════

def test_create_merchant_requires_auth(client, school):
    res = client.post("/merchants/", params={
        "name": "Rogue Tuck Shop", "school_id": school.id, "momo_phone": "256700000099",
    })
    assert res.status_code == 401


def test_scoped_admin_cannot_create_merchant_for_other_school(client, db_session, school, second_school):
    admin = make_admin(db_session, school, phone="256700999010")
    res = client.post(
        "/merchants/",
        params={"name": "Cross-School Shop", "school_id": second_school.id, "momo_phone": "256700000099"},
        headers=headers_for(admin),
    )
    assert res.status_code == 403


def test_scoped_admin_can_create_merchant_for_own_school(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999011")
    res = client.post(
        "/merchants/",
        params={"name": "Legit Tuck Shop", "school_id": school.id, "momo_phone": "256700000099"},
        headers=headers_for(admin),
    )
    assert res.status_code == 200


def test_super_admin_can_create_merchant_for_any_school(client, school, super_admin_headers):
    res = client.post(
        "/merchants/",
        params={"name": "Any School Shop", "school_id": school.id, "momo_phone": "256700000099"},
        headers=super_admin_headers,
    )
    assert res.status_code == 200


# ══════════════════════════════════════════════════════
# schools.py — POST /schools/, PUT /schools/{id}, badge endpoints
# ══════════════════════════════════════════════════════

def test_create_school_requires_auth(client):
    res = client.post("/schools/", params={"name": "New School", "location": "Mbale"})
    assert res.status_code == 401


def test_scoped_admin_cannot_create_school(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999012")
    res = client.post(
        "/schools/", params={"name": "New School", "location": "Mbale"}, headers=headers_for(admin),
    )
    assert res.status_code == 403


def test_super_admin_can_create_school(client, super_admin_headers):
    res = client.post(
        "/schools/", params={"name": "New School", "location": "Mbale"}, headers=super_admin_headers,
    )
    assert res.status_code == 200


def test_update_school_requires_auth(client, school):
    res = client.put(f"/schools/{school.id}", params={"name": "Renamed"})
    assert res.status_code == 401


def test_scoped_admin_cannot_update_other_school(client, db_session, school, second_school):
    admin = make_admin(db_session, school, phone="256700999013")
    res = client.put(
        f"/schools/{second_school.id}", params={"name": "Hijacked Name"}, headers=headers_for(admin),
    )
    assert res.status_code == 403


def test_scoped_admin_can_update_own_school(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999014")
    res = client.put(
        f"/schools/{school.id}", params={"name": "Renamed Own School"}, headers=headers_for(admin),
    )
    assert res.status_code == 200


def test_scoped_admin_cannot_set_other_schools_badge_url(client, db_session, school, second_school):
    admin = make_admin(db_session, school, phone="256700999015")
    res = client.put(
        f"/schools/{second_school.id}/badge-url",
        params={"badge_url": "https://example.com/badge.png"},
        headers=headers_for(admin),
    )
    assert res.status_code == 403


# ══════════════════════════════════════════════════════
# users.py — POST /users/, DELETE /users/{id}
# ══════════════════════════════════════════════════════

def test_create_user_requires_auth(client):
    res = client.post("/users/", params={
        "name": "Attacker", "phone": "256700555010", "role": "admin", "pin": "1234",
    })
    assert res.status_code == 401


def test_create_user_parent_role_forces_school_id_none(client, db_session, school, super_admin_headers):
    res = client.post(
        "/users/",
        params={
            "name": "New Parent", "phone": "256700555011", "role": "parent",
            "pin": "1234", "school_id": school.id,
        },
        headers=super_admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["user"]["school_id"] is None
    user = db_session.query(User).filter_by(phone="256700555011").first()
    assert user.school_id is None


def test_scoped_admin_cannot_create_merchant_user_for_other_school(client, db_session, school, second_school):
    admin = make_admin(db_session, school, phone="256700999016")
    res = client.post(
        "/users/",
        params={
            "name": "Cross-School Merchant", "phone": "256700555012", "role": "merchant",
            "pin": "1234", "school_id": second_school.id,
        },
        headers=headers_for(admin),
    )
    assert res.status_code == 403


def test_scoped_admin_cannot_create_super_admin(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999017")
    res = client.post(
        "/users/",
        params={"name": "Escalated Admin", "phone": "256700555013", "role": "admin", "pin": "1234"},
        headers=headers_for(admin),
    )
    assert res.status_code == 403


def test_super_admin_can_create_super_admin(client, super_admin_headers):
    res = client.post(
        "/users/",
        params={"name": "New Super Admin", "phone": "256700555014", "role": "admin", "pin": "1234"},
        headers=super_admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["user"]["school_id"] is None


def test_scoped_admin_can_create_admin_for_own_school(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999018")
    res = client.post(
        "/users/",
        params={
            "name": "New School Admin", "phone": "256700555015", "role": "admin",
            "pin": "1234", "school_id": school.id,
        },
        headers=headers_for(admin),
    )
    assert res.status_code == 200
    assert res.json()["user"]["school_id"] == school.id


def test_created_user_can_actually_log_in_with_the_set_pin(client, super_admin_headers):
    res = client.post(
        "/users/",
        params={"name": "Usable Parent", "phone": "256700555016", "role": "parent", "pin": "4321"},
        headers=super_admin_headers,
    )
    assert res.status_code == 200

    login_res = client.post("/auth/login", json={
        "phone": "256700555016", "pin": "4321",
        "accept_terms_version": CURRENT_TERMS_VERSION,
    })
    assert login_res.status_code == 200
    assert login_res.json()["token"]


def test_delete_user_endpoint_no_longer_exists(client, db_session, parent_user, super_admin_headers):
    res = client.delete(f"/users/{parent_user.id}", headers=super_admin_headers)
    assert res.status_code in (404, 405)
    # The user must still exist — the endpoint shouldn't have deleted it
    # even if the route somehow still responded.
    assert db_session.query(User).filter_by(id=parent_user.id).first() is not None
