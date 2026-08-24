# ══════════════════════════════════════════════════════
# tests/test_yo_live_switch.py
# ------------------------------------------------------
# Pins _is_test_mode()'s full truth table.
#
# _is_test_mode() is the single gate between "fake a success" and "put a
# real request on the wire". Every other safety property in the payout
# path assumes it answers correctly, and it answers from three inputs:
# YO_USERNAME, YO_LIVE, and — only when YO_LIVE is unset — APP_ENV.
#
# That last clause is a deliberate migration shim (see the comment on
# _YO_LIVE_EXPLICIT in app/momo.py). It is pinned here NOT because it is
# the intended end state but because it is temporary: a shim nothing
# tests is one that gets removed by accident, or kept forever because
# nothing complains. When it is removed on purpose, the four fallback
# cases below should be deleted in the same commit.
# ══════════════════════════════════════════════════════

import pytest

from app import momo


def _set_mode(monkeypatch, *, username="90000125126", live=None, app_env="development"):
    """
    Drive _is_test_mode()'s three inputs directly.

    `live=None` means YO_LIVE was never set, which is the fallback case —
    _YO_LIVE_EXPLICIT False, and the value of YO_LIVE itself irrelevant.
    """
    monkeypatch.setattr(momo, "YO_USERNAME", username)
    monkeypatch.setattr(momo, "APP_ENV", app_env)
    monkeypatch.setattr(momo, "_YO_LIVE_EXPLICIT", live is not None)
    monkeypatch.setattr(momo, "YO_LIVE", bool(live))


# ── YO_USERNAME short-circuit ─────────────────────────
@pytest.mark.parametrize("live", [None, True, False])
@pytest.mark.parametrize("app_env", ["development", "production"])
def test_no_username_is_always_test_mode(monkeypatch, live, app_env):
    """
    No credentials means nothing can be sent, whatever the switches say.
    Checked first so a stray YO_LIVE=true on a box with no YO_USERNAME
    cannot talk itself into thinking it is live.
    """
    _set_mode(monkeypatch, username="", live=live, app_env=app_env)
    assert momo._is_test_mode() is True


# ── YO_LIVE set explicitly: sole authority ────────────
@pytest.mark.parametrize("app_env", ["development", "production"])
def test_explicit_yo_live_true_leaves_test_mode(monkeypatch, app_env):
    """
    The configuration this change exists to make expressible: real calls
    while APP_ENV stays "development" so webhook.py and ussd.py keep
    verifying against the SANDBOX certs.
    """
    _set_mode(monkeypatch, live=True, app_env=app_env)
    assert momo._is_test_mode() is False


@pytest.mark.parametrize("app_env", ["development", "production"])
def test_explicit_yo_live_false_forces_test_mode(monkeypatch, app_env):
    """
    YO_LIVE=false must win even under APP_ENV=production — that is what
    makes it a kill switch rather than a suggestion.
    """
    _set_mode(monkeypatch, live=False, app_env=app_env)
    assert momo._is_test_mode() is True


# ── YO_LIVE unset: the migration shim ─────────────────
# DELETE THESE FOUR when the fallback is removed from app/momo.py.
def test_unset_yo_live_falls_back_to_app_env_production(monkeypatch):
    _set_mode(monkeypatch, live=None, app_env="production")
    assert momo._is_test_mode() is False, (
        "with YO_LIVE unset, APP_ENV=production must still leave test mode — "
        "otherwise an existing production deploy silently starts faking success"
    )


def test_unset_yo_live_falls_back_to_app_env_development(monkeypatch):
    _set_mode(monkeypatch, live=None, app_env="development")
    assert momo._is_test_mode() is True


def test_unset_yo_live_treats_unknown_app_env_as_test_mode(monkeypatch):
    """
    Only the exact string "production" is live. A typo or a third value
    like "staging" must fail closed, not open.
    """
    _set_mode(monkeypatch, live=None, app_env="staging")
    assert momo._is_test_mode() is True


def test_app_env_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    """
    Three existing test files leave test mode by patching momo.APP_ENV
    after import. That only works because the fallback re-reads it per
    call. Pinned so the mechanism they rely on cannot vanish silently.
    """
    _set_mode(monkeypatch, live=None, app_env="development")
    assert momo._is_test_mode() is True
    monkeypatch.setattr(momo, "APP_ENV", "production")
    assert momo._is_test_mode() is False


# ── _env_flag parsing ─────────────────────────────────
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " true "])
def test_env_flag_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv("YO_LIVE_PROBE", raw)
    assert momo._env_flag("YO_LIVE_PROBE", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "nonsense"])
def test_env_flag_falsy_spellings(monkeypatch, raw):
    monkeypatch.setenv("YO_LIVE_PROBE", raw)
    assert momo._env_flag("YO_LIVE_PROBE", True) is False


@pytest.mark.parametrize("raw", ["", "   "])
def test_env_flag_blank_uses_default(monkeypatch, raw):
    """
    Blank must be indistinguishable from unset, or a Railway variable
    cleared to an empty string would read as an explicit "false".
    """
    monkeypatch.setenv("YO_LIVE_PROBE", raw)
    assert momo._env_flag("YO_LIVE_PROBE", True) is True
    assert momo._env_flag("YO_LIVE_PROBE", False) is False


def test_env_flag_unset_uses_default(monkeypatch):
    monkeypatch.delenv("YO_LIVE_PROBE", raising=False)
    assert momo._env_flag("YO_LIVE_PROBE", True) is True
    assert momo._env_flag("YO_LIVE_PROBE", False) is False
