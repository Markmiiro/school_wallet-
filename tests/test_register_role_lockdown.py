# ================================================
# tests/test_register_role_lockdown.py
# ------------------------------------------------
# CRITICAL bug found in the app/ audit: POST /auth/register let the
# caller freely choose `role` (including "admin") with `school_id`
# defaulting to null — a full unauthenticated super-admin takeover path
# (role="admin" + school_id=None passes is_super_admin() in app/auth.py).
#
# Fix under test: /register becomes parent-only. `role` and `school_id`
# are removed from the request schema entirely — not validated-and-
# restricted, removed from the interface — so no caller input can ever
# produce anything but role="parent", school_id=None. Creating
# admin/merchant accounts becomes exclusively an authenticated-admin
# action elsewhere (tracked separately).
#
# Also covers the unhandled IntegrityError on a duplicate-phone race
# (the ordinary sequential duplicate was already caught by the existing
# pre-check; only concurrent duplicates hit the unhandled path).
# ================================================

import concurrent.futures
import os

import pytest

from app.models import User
from app.terms import CURRENT_TERMS_VERSION


def test_register_with_role_admin_is_ignored_creates_parent(client, db_session):
    """
    Before the fix: this creates a role="admin", school_id=None user —
    i.e. a super-admin (see is_super_admin() in app/auth.py) — from a
    single unauthenticated request. After the fix: role is not read
    from input at all, so this must create an ordinary parent.
    """
    res = client.post("/auth/register", json={
        "name": "Attacker",
        "phone": "256700555001",
        "pin": "1234", "terms_version": CURRENT_TERMS_VERSION,
        "role": "admin",
    })
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "parent"

    user = db_session.query(User).filter_by(phone="256700555001").first()
    assert user.role == "parent"
    assert user.school_id is None


def test_register_with_role_merchant_and_school_id_is_ignored_creates_parent(client, db_session, school):
    """Same escalation attempt, this time via role="merchant" + a real school_id."""
    res = client.post("/auth/register", json={
        "name": "Attacker Two",
        "phone": "256700555002",
        "pin": "1234", "terms_version": CURRENT_TERMS_VERSION,
        "role": "merchant",
        "school_id": school.id,
    })
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "parent"

    user = db_session.query(User).filter_by(phone="256700555002").first()
    assert user.role == "parent"
    assert user.school_id is None


def test_register_with_no_role_field_still_works(client):
    """
    New contract: `role` is no longer a field on the request at all, so
    a request that never mentions it (the normal client shape once the
    Flutter app is updated to match) must still succeed as a parent
    signup — this is the regression check that the lockdown didn't
    break ordinary registration.
    """
    res = client.post("/auth/register", json={
        "name": "Ordinary Parent",
        "phone": "256700555004",
        "pin": "1234", "terms_version": CURRENT_TERMS_VERSION,
    })
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "parent"
    assert res.json()["token"]


def test_duplicate_phone_registration_returns_clean_400_sequential(client):
    """
    The ordinary (non-racy) duplicate case: this was already handled by
    the existing pre-check before this fix. Kept as a regression check
    that wrapping the insert in try/except IntegrityError didn't change
    this path's behavior.
    """
    payload = {"name": "First", "phone": "256700555003", "pin": "1234", "terms_version": CURRENT_TERMS_VERSION}
    res1 = client.post("/auth/register", json=payload)
    assert res1.status_code == 200

    payload2 = {"name": "Second", "phone": "256700555003", "pin": "5678", "terms_version": CURRENT_TERMS_VERSION}
    res2 = client.post("/auth/register", json=payload2)
    assert res2.status_code == 400
    assert "already registered" in res2.json()["detail"].lower()


def test_concurrent_duplicate_phone_registration_returns_clean_400_not_500(client):
    """
    The actual race the unhandled IntegrityError bug lives in: two
    concurrent registrations for the same phone both pass the
    check-then-act pre-check before either commits. One must win (200),
    the other must get a clean 400 — not an unhandled 500 from the
    database's own unique constraint on User.phone.

    Skipped on SQLite for the same reason as the equivalent webhook.py
    concurrency tests (see test_webhook_credit.py): StaticPool shares
    one connection across sessions and serializes writes at the driver
    level, which would misrepresent whether the IntegrityError path is
    actually exercised. Run with TEST_DATABASE_URL set to a throwaway
    local Postgres db for real proof.
    """
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip(
            "Needs TEST_DATABASE_URL set to a throwaway Postgres db — SQLite "
            "(StaticPool) shares one connection and can't demonstrate this "
            "race either way."
        )

    def fire(name):
        return client.post("/auth/register", json={
            "name": name, "phone": "256700555005", "pin": "1234", "terms_version": CURRENT_TERMS_VERSION,
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fire, ["Racer A", "Racer B"]))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 400], (
        f"expected exactly one winner (200) and one clean duplicate rejection "
        f"(400), got {statuses} — a 500 means the race's IntegrityError isn't "
        f"handled"
    )
