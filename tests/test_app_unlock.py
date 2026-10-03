# ================================================
# tests/test_app_unlock.py
# ------------------------------------------------
# POST /auth/unlock {pin} — the app's lock screen. The parent is still
# signed in (the token is valid); this only checks the PIN, on the
# server, so no PIN or PIN hash ever has to live on the phone. Wrong
# PINs count toward the same lockout as login, and answer 400, never
# 401, which the app reads as "signed out".
# ================================================

from app import models


def _unlock(client, headers, pin):
    return client.post("/auth/unlock", json={"pin": pin}, headers=headers)


def test_right_pin_unlocks(client, parent_user, auth_headers):
    res = _unlock(client, auth_headers, "1234")
    assert res.status_code == 200
    assert res.json() == {"unlocked": True}


def test_wrong_pin_is_refused_and_counts_toward_lockout(
    client, db_session, parent_user, auth_headers,
):
    # 400, not 401: the app reads 401 as "session gone" and signs out.
    res = _unlock(client, auth_headers, "9999")
    assert res.status_code == 400
    assert "Incorrect PIN" in res.json()["detail"]
    db_session.expire_all()
    assert db_session.get(models.User, parent_user.id).failed_login_attempts == 1


def test_five_wrong_pins_lock_unlock_and_login_alike(
    client, parent_user, auth_headers,
):
    for _ in range(4):
        assert _unlock(client, auth_headers, "9999").status_code == 400
    assert _unlock(client, auth_headers, "9999").status_code == 429
    assert _unlock(client, auth_headers, "1234").status_code == 429
    login = client.post("/auth/login", json={"phone": parent_user.phone, "pin": "1234"})
    assert login.status_code == 429


def test_right_pin_resets_the_wrong_pin_count(
    client, db_session, parent_user, auth_headers,
):
    _unlock(client, auth_headers, "9999")
    _unlock(client, auth_headers, "1234")
    db_session.expire_all()
    assert db_session.get(models.User, parent_user.id).failed_login_attempts == 0


def test_unlock_needs_a_valid_session(client):
    assert client.post("/auth/unlock", json={"pin": "1234"}).status_code == 401
