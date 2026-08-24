# ══════════════════════════════════════════════════════
# tests/test_diagnostics_endpoint.py
# ------------------------------------------------------
# GET /diagnostics/yo exists to make "will this deployment actually call
# Yo?" observable. Two things must hold or it is worse than useless:
#
#   1. It must report the SAME answer _is_test_mode() gives, in every
#      combination — a diagnostics endpoint that can disagree with the
#      thing it reports on would license exactly the false confidence it
#      was built to remove.
#   2. It must never leak a credential. The YO_API_URL host is returned;
#      the path, the query, and any embedded user:password@ are not.
# ══════════════════════════════════════════════════════

import pytest

from app import momo
from app.routes.diagnostics import _safe_host


def _set_mode(monkeypatch, *, username="90000125126", live=None, app_env="development"):
    """Drive _is_test_mode()'s inputs — same helper shape as test_yo_live_switch."""
    monkeypatch.setattr(momo, "YO_USERNAME", username)
    monkeypatch.setattr(momo, "APP_ENV", app_env)
    monkeypatch.setattr(momo, "_YO_LIVE_EXPLICIT", live is not None)
    monkeypatch.setattr(momo, "YO_LIVE", bool(live))


# ── agreement with _is_test_mode() ────────────────────
@pytest.mark.parametrize("live", [None, True, False])
@pytest.mark.parametrize("app_env", ["development", "production"])
@pytest.mark.parametrize("username", ["90000125126", ""])
def test_reported_test_mode_always_matches_the_real_function(
    client, monkeypatch, live, app_env, username,
):
    _set_mode(monkeypatch, username=username, live=live, app_env=app_env)
    body = client.get("/diagnostics/yo").json()
    assert body["test_mode"] is momo._is_test_mode(), (
        "the endpoint disagreed with _is_test_mode() — it would report "
        "confidence the process does not have"
    )


def test_reports_the_live_sandbox_configuration(client, monkeypatch):
    """
    The combination this whole change exists to make expressible and, now,
    checkable in one request: real Yo calls with APP_ENV still development.
    """
    _set_mode(monkeypatch, live=True, app_env="development")
    monkeypatch.setattr(
        momo, "YO_API_URL", "https://sandbox.yo.co.ug/services/yopaymentsdev/task.php"
    )
    body = client.get("/diagnostics/yo").json()
    assert body == {
        "test_mode":        False,
        "yo_live":          True,
        "yo_live_explicit": True,
        "yo_api_host":      "https://sandbox.yo.co.ug",
    }


def test_reports_inert_yo_live_as_test_mode(client, monkeypatch):
    """
    The failure that prompted this endpoint: YO_LIVE set in the
    environment but not read by the deployed code. Here that is
    yo_live_explicit=False while test_mode stays True.
    """
    _set_mode(monkeypatch, live=None, app_env="development")
    body = client.get("/diagnostics/yo").json()
    assert body["test_mode"] is True
    assert body["yo_live_explicit"] is False


# ── no credential ever leaves ─────────────────────────
def test_host_strips_path_query_and_embedded_credentials():
    assert _safe_host(
        "https://user:sekrit@paymentsapi1.yo.co.ug:443/ybs/task.php?a=1"
    ) == "https://paymentsapi1.yo.co.ug"


@pytest.mark.parametrize("raw", ["", None])
def test_host_of_unset_url_is_empty_not_raw(raw):
    assert _safe_host(raw or "") == ""


@pytest.mark.parametrize("raw", ["not a url", "task.php", "://broken"])
def test_unparseable_url_is_masked_never_echoed(raw):
    assert _safe_host(raw) == "<unparseable>"


def test_response_body_contains_no_credentials(client, monkeypatch):
    _set_mode(monkeypatch, live=True)
    monkeypatch.setattr(momo, "YO_USERNAME", "90000125126")
    monkeypatch.setattr(momo, "YO_PASSWORD", "super-secret-password")
    monkeypatch.setattr(
        momo, "YO_API_URL", "https://user:sekrit@paymentsapi1.yo.co.ug/ybs/task.php"
    )
    raw = client.get("/diagnostics/yo").text
    for secret in ("90000125126", "super-secret-password", "sekrit", "task.php", "ybs"):
        assert secret not in raw, f"{secret!r} leaked into the diagnostics response"


def test_returns_exactly_the_four_agreed_keys(client):
    """
    Pinned so a future field cannot be added casually — this endpoint is
    unauthenticated, so every key is public.
    """
    assert set(client.get("/diagnostics/yo").json()) == {
        "test_mode", "yo_live", "yo_live_explicit", "yo_api_host",
    }


def test_main_app_actually_mounts_the_endpoint():
    """
    tests/conftest.py builds its own minimal app, so every test above would
    still pass if main.py never mounted this router — the endpoint would be
    green in CI and 404 in production, which is the failure mode this whole
    endpoint exists to prevent. Assert against the real app.
    """
    from main import app

    paths = {r.path for r in app.routes}
    assert "/diagnostics/yo" in paths, (
        "main.py does not mount the diagnostics router — the endpoint would "
        "404 on the deployed service"
    )
