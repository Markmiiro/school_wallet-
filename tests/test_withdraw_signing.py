# ================================================
# tests/test_withdraw_signing.py
# ------------------------------------------------
# Money-critical: withdraw request signing.
#   sign_withdraw_request()  (app/momo.py)
#
# app/momo.py is on CLAUDE.md's READ-ONLY list — bugs found here are
# reported, never silently patched.
#
# Per CLAUDE.md: "do not touch the key material in tests; mock the
# signer." We never read the real private_key.pem / YO_PRIVATE_KEY.
# Instead each test generates its own throwaway RSA keypair and
# monkeypatches app.momo.YO_PRIVATE_KEY / YO_USERNAME to point at it —
# this exercises the real signing code path with fake key material.
# ================================================

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import momo


@pytest.fixture(scope="session")
def test_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_key, private_pem


@pytest.fixture()
def signed_env(monkeypatch, test_keypair):
    """Point app.momo at a throwaway test key + known username."""
    private_key, private_pem = test_keypair
    monkeypatch.setattr(momo, "YO_PRIVATE_KEY", private_pem)
    monkeypatch.setattr(momo, "YO_USERNAME", "test-api-username")
    return private_key


def verify(private_key, concat: str, signature_b64: str) -> bool:
    """Verify a signature the same way sign_withdraw_request builds it."""
    public_key = private_key.public_key()
    sha1_hex = hashlib.sha1(concat.encode("utf-8")).hexdigest()
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            sha1_hex.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
        return True
    except Exception:
        return False


# ── Missing key ─────────────────────────────────────
def test_raises_when_private_key_not_configured(monkeypatch):
    monkeypatch.setattr(momo, "YO_PRIVATE_KEY", "")
    with pytest.raises(RuntimeError):
        momo.sign_withdraw_request(
            amount=5000, account="256700000001", narrative="Payout",
            external_reference="ref-1", nonce="nonce-1",
        )


# ── Correct field order (§4.1) ──────────────────────
def test_signature_matches_spec_field_order(signed_env):
    private_key = signed_env
    sig = momo.sign_withdraw_request(
        amount=5000, account="256700000001", narrative="Daily payout to Tuck Shop",
        external_reference="ext-ref-123", nonce="nonce-abc",
    )
    expected_concat = (
        "test-api-username" "5000" "256700000001"
        "Daily payout to Tuck Shop" "ext-ref-123" "nonce-abc"
    )
    assert verify(private_key, expected_concat, sig), (
        "signature does not verify against APIUsername+Amount+Account+"
        "Narrative+ExternalReference+Nonce concatenation order from §4.1"
    )


def test_signature_breaks_if_fields_are_reordered(signed_env):
    """Sanity check that the verify() helper is actually sensitive to order."""
    private_key = signed_env
    sig = momo.sign_withdraw_request(
        amount=5000, account="256700000001", narrative="Narrative-X",
        external_reference="Ref-Y", nonce="Nonce-Z",
    )
    wrong_order_concat = "test-api-username" "5000" "256700000001" "Ref-Y" "Narrative-X" "Nonce-Z"
    assert not verify(private_key, wrong_order_concat, sig)


# ── Determinism / stability ─────────────────────────
def test_signature_is_stable_for_identical_inputs(signed_env):
    kwargs = dict(
        amount=1000, account="256700000002", narrative="Same narrative",
        external_reference="same-ref", nonce="same-nonce",
    )
    sig1 = momo.sign_withdraw_request(**kwargs)
    sig2 = momo.sign_withdraw_request(**kwargs)
    assert sig1 == sig2, "PKCS1v15 signing should be deterministic for identical inputs"


def test_signature_changes_when_amount_changes(signed_env):
    base = dict(account="256700000002", narrative="N", external_reference="R", nonce="Nonce")
    sig_a = momo.sign_withdraw_request(amount=1000, **base)
    sig_b = momo.sign_withdraw_request(amount=2000, **base)
    assert sig_a != sig_b


# ── Truncation (§4.1: narrative/ext_ref/nonce truncated to 255 chars) ──
def test_narrative_over_255_chars_is_truncated_before_signing(signed_env):
    private_key = signed_env
    long_narrative = "N" * 300
    sig = momo.sign_withdraw_request(
        amount=1000, account="256700000003", narrative=long_narrative,
        external_reference="ref", nonce="nonce",
    )
    truncated = long_narrative[:255]
    expected_concat = f"test-api-username1000256700000003{truncated}refnonce"
    assert verify(private_key, expected_concat, sig), (
        "narrative longer than 255 chars must be truncated to 255 before "
        "concatenation, per §4.1"
    )


def test_external_reference_and_nonce_over_255_chars_are_truncated(signed_env):
    private_key = signed_env
    long_ref = "R" * 300
    long_nonce = "N" * 300
    sig = momo.sign_withdraw_request(
        amount=1000, account="256700000004", narrative="short",
        external_reference=long_ref, nonce=long_nonce,
    )
    expected_concat = (
        f"test-api-username1000256700000004short"
        f"{long_ref[:255]}{long_nonce[:255]}"
    )
    assert verify(private_key, expected_concat, sig)


# ── Nonce uniqueness (§4.1: must be unique per call, including failures) ──
def test_generate_nonce_is_unique_per_call():
    nonces = {momo._generate_nonce() for _ in range(500)}
    assert len(nonces) == 500
