# ================================================
# tests/test_authorization_gaps.py
# ------------------------------------------------
# Findings from NUVORA_CODE_MAP.md (1 Oct 2026): endpoints that took no
# credentials at all, or accepted any valid token without asking whose
# wallet / which school it was for.
#
#   1. PUT /users/{id} — unauthenticated phone change. Tokens resolve
#      their user BY PHONE, so this was an account takeover.
#   2. /payments/, /payments/nfc, /payments/sync — any logged-in user,
#      including a self-registered parent, could debit any wallet.
#   3. /wallets/wallets/{id}, /wallets/{id}/history, /tuckshop/check,
#      GET /users/*, GET /merchants/* — readable with no token.
#   4. /topup/* — no check that the wallet was the caller's child.
#   5. /reports/* reads and /analytics/* — admin, but not school-scoped.
#   6. Daily limit — nothing mounted could change it.
# ================================================

import uuid

import pytest

from app import models
from app.auth import hash_pin
from tests.conftest import (
    headers_for,
    make_admin,
    make_merchant_user,
    make_student_with_wallet,
)
from app.terms import CURRENT_TERMS_VERSION


def _make_parent(db_session, *, phone, name="Other Parent"):
    u = models.User(name=name, phone=phone, role="parent", pin_hash=hash_pin("1234"))
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_merchant(db_session, school, *, name="Other Tuck Shop"):
    m = models.Merchant(
        name=name, school_id=school.id, momo_phone="256700333555", is_active=True,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


# ══════════════════════════════════════════════════════
# 1. users.py — PUT /users/{id} and the GET listings
# ══════════════════════════════════════════════════════

def test_update_user_requires_auth(client, admin_user):
    res = client.put(f"/users/{admin_user.id}", params={"phone": "256700000001"})
    assert res.status_code == 401


def test_parent_cannot_update_any_user(client, admin_user, parent_user, auth_headers):
    for target in (admin_user, parent_user):
        res = client.put(
            f"/users/{target.id}", params={"name": "Hijacked"}, headers=auth_headers,
        )
        assert res.status_code == 403


def test_phone_swap_takeover_is_blocked(client, db_session, admin_user, parent_user, auth_headers):
    """
    The attack: hold a parent token (sub = the parent's phone), move the
    parent account off that number, then put the number on an admin.
    get_current_user looks users up by phone, so the parent's token
    would start resolving to the admin.
    """
    parents_phone = parent_user.phone

    step1 = client.put(f"/users/{parent_user.id}", params={"phone": "256700000002"})
    step2 = client.put(f"/users/{admin_user.id}", params={"phone": parents_phone})
    assert step1.status_code == 401
    assert step2.status_code == 401

    db_session.refresh(admin_user)
    db_session.refresh(parent_user)
    assert parent_user.phone == parents_phone
    assert admin_user.phone != parents_phone

    # The parent's token still resolves to the parent.
    me = client.get("/auth/me", headers=auth_headers)
    assert me.status_code == 200
    assert me.json()["role"] == "parent"


def test_scoped_admin_cannot_update_parent_or_other_school_staff(
    client, db_session, school, second_school, parent_user, super_admin_user,
):
    admin = make_admin(db_session, school, phone="256700999020")
    other_admin = make_admin(db_session, second_school, phone="256700999021")

    for target in (parent_user, other_admin, super_admin_user):
        res = client.put(
            f"/users/{target.id}", params={"phone": "256700000003"}, headers=headers_for(admin),
        )
        assert res.status_code == 403


def test_scoped_admin_can_update_own_school_staff(client, db_session, school, staff_user):
    admin = make_admin(db_session, school, phone="256700999022")
    res = client.put(
        f"/users/{staff_user.id}", params={"name": "Renamed Staff"}, headers=headers_for(admin),
    )
    assert res.status_code == 200
    assert res.json()["user"]["name"] == "Renamed Staff"


def test_update_user_rejects_malformed_phone(client, parent_user, super_admin_headers):
    res = client.put(
        f"/users/{parent_user.id}", params={"phone": "0771234567"}, headers=super_admin_headers,
    )
    assert res.status_code == 422


@pytest.mark.parametrize("path", ["/users/", "/users/1", "/users/role/admin"])
def test_user_listings_require_auth(client, admin_user, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/users/", "/users/role/admin"])
def test_parent_cannot_list_users(client, admin_user, auth_headers, path):
    assert client.get(path, headers=auth_headers).status_code == 403


def test_scoped_admin_sees_own_staff_and_own_schools_parents_only(
    client, db_session, school, second_school, parent_user, student_with_wallet,
):
    admin = make_admin(db_session, school, phone="256700999023")
    other_admin = make_admin(db_session, second_school, phone="256700999024")
    stranger = _make_parent(db_session, phone="256700555901")  # no child at `school`

    ids = {u["id"] for u in client.get("/users/", headers=headers_for(admin)).json()}
    assert admin.id in ids
    assert parent_user.id in ids        # has a child at this school
    assert other_admin.id not in ids
    assert stranger.id not in ids

    assert client.get(f"/users/{other_admin.id}", headers=headers_for(admin)).status_code == 403


def test_scoped_admin_can_find_a_parent_by_exact_phone(client, db_session, school):
    admin = make_admin(db_session, school, phone="256700999025")
    newcomer = _make_parent(db_session, phone="256700555902", name="New Parent")

    res = client.get("/users/", params={"phone": "256700555902"}, headers=headers_for(admin))
    assert res.status_code == 200
    assert [u["id"] for u in res.json()] == [newcomer.id]


def test_create_user_accepts_json_body_so_the_pin_stays_out_of_the_url(client, super_admin_headers):
    res = client.post(
        "/users/",
        json={"name": "Body Parent", "phone": "256700555903", "role": "parent", "pin": "4321"},
        headers=super_admin_headers,
    )
    assert res.status_code == 200
    login = client.post("/auth/login", json={
        "phone": "256700555903", "pin": "4321",
        "accept_terms_version": CURRENT_TERMS_VERSION,
    })
    assert login.status_code == 200


def test_create_user_reports_missing_fields(client, super_admin_headers):
    res = client.post(
        "/users/", params={"name": "No Pin", "phone": "256700555904", "role": "parent"},
        headers=super_admin_headers,
    )
    assert res.status_code == 422


# ══════════════════════════════════════════════════════
# 2. payments.py — who may charge a wallet
# ══════════════════════════════════════════════════════

def test_parent_token_cannot_debit_a_wallet_directly(
    client, db_session, merchant, student_with_wallet, auth_headers,
):
    _, wallet, _ = student_with_wallet
    res = client.post(
        "/payments/",
        params={"wallet_id": wallet.id, "merchant_id": merchant.id, "amount": 5000},
        headers=auth_headers,
    )
    assert res.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_parent_token_cannot_charge_by_nfc(
    client, db_session, merchant, student_with_wallet, auth_headers,
):
    _, wallet, nfc = student_with_wallet
    res = client.post(
        "/payments/nfc",
        params={
            "tag_uid": nfc.tag_uid, "merchant_id": merchant.id,
            "amount": 5000, "request_id": str(uuid.uuid4()),
        },
        headers=auth_headers,
    )
    assert res.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_parent_token_cannot_sync_offline_payments(
    client, db_session, merchant, student_with_wallet, auth_headers,
):
    _, wallet, nfc = student_with_wallet
    res = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "dev-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 5000, "request_id": str(uuid.uuid4())}],
        headers=auth_headers,
    )
    assert res.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_staff_cannot_credit_another_schools_merchant(
    client, db_session, second_school, student_with_wallet, staff_headers,
):
    _, wallet, nfc = student_with_wallet
    foreign_merchant = _make_merchant(db_session, second_school)
    res = client.post(
        "/payments/nfc",
        params={
            "tag_uid": nfc.tag_uid, "merchant_id": foreign_merchant.id,
            "amount": 1000, "request_id": str(uuid.uuid4()),
        },
        headers=staff_headers,
    )
    assert res.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_staff_cannot_charge_a_card_from_another_school(
    client, db_session, second_school, parent_user, merchant, staff_headers,
):
    _, wallet, nfc = make_student_with_wallet(
        db_session, second_school, parent_user, tag_uid="FFEEDDCC",
    )
    res = client.post(
        "/payments/nfc",
        params={
            "tag_uid": nfc.tag_uid, "merchant_id": merchant.id,
            "amount": 1000, "request_id": str(uuid.uuid4()),
        },
        headers=staff_headers,
    )
    assert res.status_code == 403
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_sync_refuses_a_card_from_another_school_for_that_item_only(
    client, db_session, school, second_school, parent_user, merchant, staff_headers,
):
    _, own_wallet, own_nfc = make_student_with_wallet(db_session, school, parent_user)
    _, foreign_wallet, foreign_nfc = make_student_with_wallet(
        db_session, second_school, parent_user, tag_uid="FFEEDDCC",
    )
    res = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "dev-1"},
        json=[
            {"tag_uid": own_nfc.tag_uid, "amount": 1000, "request_id": str(uuid.uuid4())},
            {"tag_uid": foreign_nfc.tag_uid, "amount": 1000, "request_id": str(uuid.uuid4())},
        ],
        headers=staff_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["processed"] == 1
    assert body["failed"] == 1
    db_session.refresh(own_wallet)
    db_session.refresh(foreign_wallet)
    assert own_wallet.balance == 9000
    assert foreign_wallet.balance == 10000


def test_sync_rejects_unknown_merchant(client, student_with_wallet, staff_headers):
    _, _, nfc = student_with_wallet
    res = client.post(
        "/payments/sync",
        params={"merchant_id": 999999, "device_id": "dev-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 1000, "request_id": str(uuid.uuid4())}],
        headers=staff_headers,
    )
    assert res.status_code == 404


def test_sync_rejects_fractional_amount(
    client, db_session, merchant, student_with_wallet, staff_headers,
):
    _, wallet, nfc = student_with_wallet
    res = client.post(
        "/payments/sync",
        params={"merchant_id": merchant.id, "device_id": "dev-1"},
        json=[{"tag_uid": nfc.tag_uid, "amount": 1000.75, "request_id": str(uuid.uuid4())}],
        headers=staff_headers,
    )
    assert res.status_code == 200
    assert res.json()["failed"] == 1
    db_session.refresh(wallet)
    assert wallet.balance == 10000


def test_merchant_sales_are_not_readable_by_parents_or_other_schools(
    client, db_session, second_school, merchant, auth_headers, staff_headers,
):
    other_staff = make_merchant_user(db_session, second_school, phone="256700999030")
    assert client.get(f"/payments/merchant/{merchant.id}", headers=auth_headers).status_code == 403
    assert client.get(
        f"/payments/merchant/{merchant.id}", headers=headers_for(other_staff),
    ).status_code == 403
    assert client.get(f"/payments/merchant/{merchant.id}", headers=staff_headers).status_code == 200


def test_sync_status_requires_auth(client):
    assert client.get("/payments/sync/status/dev-1").status_code == 401


# ══════════════════════════════════════════════════════
# 3. Reads that needed no token
# ══════════════════════════════════════════════════════

def test_wallet_reads_require_auth(client, student_with_wallet):
    student, _, _ = student_with_wallet
    assert client.get(f"/wallets/wallets/{student.id}").status_code == 401
    assert client.get(f"/wallets/{student.id}/history").status_code == 401


def test_parent_reads_own_childs_wallet_with_its_daily_limit(
    client, student_with_wallet, auth_headers,
):
    student, wallet, _ = student_with_wallet
    res = client.get(f"/wallets/wallets/{student.id}", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["balance"] == 10000
    assert res.json()["daily_limit"] == 20000

    history = client.get(f"/wallets/{student.id}/history", headers=auth_headers)
    assert history.status_code == 200
    assert history.json()["wallet_id"] == wallet.id
    assert history.json()["daily_limit"] == 20000


def test_another_parent_cannot_read_the_wallet(client, db_session, student_with_wallet):
    student, _, _ = student_with_wallet
    intruder = _make_parent(db_session, phone="256700555910")
    assert client.get(
        f"/wallets/wallets/{student.id}", headers=headers_for(intruder),
    ).status_code == 403
    assert client.get(
        f"/wallets/{student.id}/history", headers=headers_for(intruder),
    ).status_code == 403


def test_other_schools_staff_cannot_read_the_wallet(
    client, db_session, second_school, student_with_wallet,
):
    student, _, _ = student_with_wallet
    other_admin = make_admin(db_session, second_school, phone="256700999031")
    assert client.get(
        f"/wallets/wallets/{student.id}", headers=headers_for(other_admin),
    ).status_code == 403


def test_history_limit_is_bounded(client, student_with_wallet, auth_headers):
    student, _, _ = student_with_wallet
    res = client.get(
        f"/wallets/{student.id}/history", params={"limit": 100000}, headers=auth_headers,
    )
    assert res.status_code == 422


def test_card_check_requires_staff_of_the_cards_school(
    client, db_session, second_school, student_with_wallet, auth_headers, staff_headers,
):
    _, _, nfc = student_with_wallet
    url = f"/tuckshop/check?tag_uid={nfc.tag_uid}"
    other_staff = make_merchant_user(db_session, second_school, phone="256700999032")

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth_headers).status_code == 403
    assert client.get(url, headers=headers_for(other_staff)).status_code == 403
    assert client.get(url, headers=staff_headers).status_code == 200


def test_merchant_listings_require_auth(client, merchant, school):
    assert client.get("/merchants/").status_code == 401
    assert client.get(f"/merchants/{merchant.id}").status_code == 401
    assert client.get(f"/merchants/school/{school.id}").status_code == 401


def test_parent_cannot_read_merchants(client, merchant, school, auth_headers):
    assert client.get("/merchants/", headers=auth_headers).status_code == 403
    assert client.get(f"/merchants/{merchant.id}", headers=auth_headers).status_code == 403
    assert client.get(f"/merchants/school/{school.id}", headers=auth_headers).status_code == 403


def test_scoped_admin_lists_only_own_schools_merchants(
    client, db_session, school, second_school, merchant,
):
    admin = make_admin(db_session, school, phone="256700999033")
    foreign = _make_merchant(db_session, second_school)

    listed = client.get("/merchants/", headers=headers_for(admin)).json()
    assert [m["id"] for m in listed] == [merchant.id]
    assert listed[0]["momo_phone"] == merchant.momo_phone   # admins still see it
    assert client.get(f"/merchants/{foreign.id}", headers=headers_for(admin)).status_code == 403


def test_create_merchant_rejects_malformed_payout_phone(client, school, super_admin_headers):
    res = client.post(
        "/merchants/",
        params={"name": "Bad Phone Shop", "school_id": school.id, "momo_phone": "12345"},
        headers=super_admin_headers,
    )
    assert res.status_code == 422


# ══════════════════════════════════════════════════════
# 4. topup.py — whose wallet
# ══════════════════════════════════════════════════════

def _topup_body(wallet):
    return {
        "wallet_id": wallet.id, "amount": 5000,
        "phone_number": "256771234567", "network": "MTN",
    }


def test_parent_can_start_a_topup_for_own_child(client, student_with_wallet, auth_headers):
    _, wallet, _ = student_with_wallet
    res = client.post("/topup/", json=_topup_body(wallet), headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["status"] == "pending"


def test_another_parent_cannot_start_a_topup_or_read_it(
    client, db_session, student_with_wallet, auth_headers,
):
    _, wallet, _ = student_with_wallet
    intruder = _make_parent(db_session, phone="256700555911")

    res = client.post("/topup/", json=_topup_body(wallet), headers=headers_for(intruder))
    assert res.status_code == 403
    assert db_session.query(models.Transaction).filter_by(wallet_id=wallet.id).count() == 0

    started = client.post("/topup/", json=_topup_body(wallet), headers=auth_headers).json()
    assert client.get(
        f"/topup/{started['reference_id']}", headers=headers_for(intruder),
    ).status_code == 403
    assert client.get(
        f"/topup/history/{wallet.id}", headers=headers_for(intruder),
    ).status_code == 403
    assert client.get(f"/topup/history/{wallet.id}", headers=auth_headers).status_code == 200


# ══════════════════════════════════════════════════════
# 5. reports.py reads and analytics.py — school scoping
# ══════════════════════════════════════════════════════

def test_reports_are_scoped_to_the_admins_school(
    client, db_session, school, second_school, merchant, admin_headers,
):
    other_admin = make_admin(db_session, second_school, phone="256700999034")
    theirs = headers_for(other_admin)

    for path in (
        f"/reports/merchant/{merchant.id}/daily",
        f"/reports/merchant/{merchant.id}/dashboard",
        f"/reports/school/{school.id}/settlement",
    ):
        assert client.get(path, headers=theirs).status_code == 403, path
        assert client.get(path, headers=admin_headers).status_code == 200, path


def test_analytics_are_scoped_to_the_admins_school(
    client, db_session, school, second_school, student_with_wallet, admin_headers,
):
    student, _, _ = student_with_wallet
    other_admin = make_admin(db_session, second_school, phone="256700999035")
    theirs = headers_for(other_admin)

    for path in (
        f"/analytics/school/{school.id}/overview",
        f"/analytics/school/{school.id}/daily",
        f"/analytics/school/{school.id}/weekly",
        f"/analytics/student/{student.id}/summary",
    ):
        assert client.get(path, headers=theirs).status_code == 403, path
        assert client.get(path, headers=admin_headers).status_code == 200, path


# ══════════════════════════════════════════════════════
# 6. Daily limit — settable, and still enforced
# ══════════════════════════════════════════════════════

def test_parent_sets_own_childs_daily_limit_and_it_is_enforced(
    client, db_session, merchant, student_with_wallet, auth_headers, staff_headers,
):
    student, wallet, nfc = student_with_wallet

    res = client.put(
        f"/wallets/{student.id}/limit", params={"daily_limit": 3000}, headers=auth_headers,
    )
    assert res.status_code == 200
    assert res.json()["old_limit"] == 20000
    assert res.json()["daily_limit"] == 3000

    def charge(amount):
        return client.post(
            "/payments/nfc",
            params={
                "tag_uid": nfc.tag_uid, "merchant_id": merchant.id,
                "amount": amount, "request_id": str(uuid.uuid4()),
            },
            headers=staff_headers,
        )

    assert charge(2000).status_code == 200
    assert charge(2000).status_code == 400       # 4,000 > 3,000
    db_session.refresh(wallet)
    assert wallet.balance == 8000


def test_daily_limit_cannot_be_set_by_strangers_or_till_staff(
    client, db_session, second_school, student_with_wallet, staff_headers,
):
    student, wallet, _ = student_with_wallet
    intruder = _make_parent(db_session, phone="256700555912")
    other_admin = make_admin(db_session, second_school, phone="256700999036")

    url = f"/wallets/{student.id}/limit"
    assert client.put(url, params={"daily_limit": 3000}).status_code == 401
    for headers in (headers_for(intruder), headers_for(other_admin), staff_headers):
        assert client.put(url, params={"daily_limit": 3000}, headers=headers).status_code == 403

    db_session.refresh(wallet)
    assert wallet.daily_limit == 20000


def test_school_admin_can_set_daily_limit(client, student_with_wallet, admin_headers):
    student, _, _ = student_with_wallet
    res = client.put(
        f"/wallets/{student.id}/limit", params={"daily_limit": 15000}, headers=admin_headers,
    )
    assert res.status_code == 200


@pytest.mark.parametrize("bad", [0, -1, 499, 5_000_001])
def test_daily_limit_bounds(client, student_with_wallet, auth_headers, bad):
    student, _, _ = student_with_wallet
    res = client.put(
        f"/wallets/{student.id}/limit", params={"daily_limit": bad}, headers=auth_headers,
    )
    assert res.status_code == 422
