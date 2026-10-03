# ================================================
# app/routes/auth.py
# ------------------------------------------------
# Authentication endpoints:
# GET  /auth/terms    → current terms and privacy text + version
# POST /auth/login    → get a JWT token
# POST /auth/register → create a new user
# GET  /auth/me       → get current user info
# POST /auth/change-pin → change PIN
# ================================================

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator

from app.database import get_db
from app.models import User
from app.auth import hash_pin, verify_pin, create_access_token, get_current_user
from app import terms

router = APIRouter()

# ── Rate-limiting settings ─────────────────────────
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


# ================================================
# SCHEMAS
# ================================================

class LoginRequest(BaseModel):
    phone: str
    pin: str
    # Sent by the app after the parent has read and accepted the terms
    # it was shown. Must be the current version to count.
    accept_terms_version: Optional[str] = None

class RegisterRequest(BaseModel):
    """
    Public self-signup — parent accounts only. `role` and `school_id`
    are deliberately not fields here: admin/merchant accounts are
    created exclusively through an authenticated-admin action, never by
    an unauthenticated caller choosing their own role. Any `role` or
    `school_id` a client sends is silently ignored (extra="ignore"),
    not validated-and-rejected, so this can't break existing clients
    that still send one.
    """
    model_config = {"extra": "ignore"}

    name: str
    phone: str
    pin: str
    # The version of the terms the parent accepted on the screen before
    # this form. No account is created unless it is the current one.
    terms_version: Optional[str] = None

    @field_validator("pin")
    def pin_must_be_4_digits(cls, v):
        if not v.isdigit() or len(v) != 4:
            raise ValueError("PIN must be exactly 4 digits")
        return v

    @field_validator("phone")
    def phone_must_be_valid(cls, v):
        v = v.replace(" ", "").replace("+", "")
        if not v.startswith("256") or len(v) != 12:
            raise ValueError("Phone must be 256XXXXXXXXX format")
        return v

class ChangePinRequest(BaseModel):
    current_pin: str
    new_pin: str

    @field_validator("new_pin")
    def pin_must_be_4_digits(cls, v):
        if not v.isdigit() or len(v) != 4:
            raise ValueError("New PIN must be exactly 4 digits")
        return v


# ================================================
# ENDPOINT 0 — The terms a parent is asked to accept
# ================================================
@router.get("/terms")
def get_terms():
    """
    Public. The current version, the summary shown on the acceptance
    screen, and the full Terms of Use and Privacy Policy.
    """
    return terms.terms_payload()


# ── Who must have accepted the current terms to log in ──
# Parents only. School staff sign in through the till and card pages,
# which have no acceptance screen; gating them here would lock every
# till out the moment the version changes.
def _must_accept_terms(user: User) -> bool:
    return user.role == "parent" and user.terms_version != terms.CURRENT_TERMS_VERSION


# ================================================
# ENDPOINT 1 — Login
# ================================================
@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    """
    Login with phone number and PIN.
    Returns a JWT token valid for 24 hours.
    Include this token in all future requests:
    Headers: Authorization: Bearer YOUR_TOKEN_HERE

    Locks the account for LOCKOUT_MINUTES after MAX_FAILED_ATTEMPTS
    consecutive wrong PINs, to prevent brute-forcing a 4-digit PIN.
    """
    # Clean phone number
    phone = data.phone.replace(" ", "").replace("+", "")

    # Find user by phone
    user = db.query(User).filter(User.phone == phone).first()

    if not user:
        # Deliberately worded to match a fresh account's first wrong-PIN
        # message below, so a single probe can't distinguish "this phone
        # isn't registered" from "this phone is registered but the PIN was
        # wrong." Not a perfect defense — if an attacker first builds up
        # failed attempts against a real number, its count will start to
        # diverge from this fixed one — but it closes the trivial
        # single-request enumeration this endpoint's comment always
        # claimed to prevent.
        raise HTTPException(
            status_code=401,
            detail=f"Incorrect PIN. {MAX_FAILED_ATTEMPTS - 1} attempt(s) remaining before lockout."
        )

    # ── Check if account is currently locked ───────
    if user.locked_until and user.locked_until > datetime.utcnow():
        minutes_left = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {minutes_left} minute(s)."
        )

    # ── Verify PIN ──────────────────────────────────
    if not verify_pin(data.pin, user.pin_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1

        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            db.commit()
            print(f"🔒 Account locked: {user.name} ({user.phone}) — too many failed attempts")
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
            )

        db.commit()
        attempts_left = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
        raise HTTPException(
            status_code=401,
            detail=f"Incorrect PIN. {attempts_left} attempt(s) remaining before lockout."
        )

    # ── Success: reset the counters ─────────────────
    user.failed_login_attempts = 0
    user.locked_until = None

    # ── Terms: the PIN is right, but no token until the current terms
    #    are accepted. Checked only after the PIN, so this answer is
    #    never given to someone who does not know it. ──
    if _must_accept_terms(user):
        if not terms.is_current(data.accept_terms_version):
            db.commit()
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        "Please read and accept the Nuvora terms and privacy "
                        "policy to continue. If you do not see them, refresh "
                        "the app."
                    ),
                    "code": "terms_required",
                    "terms_version": terms.CURRENT_TERMS_VERSION,
                },
            )
        user.terms_version = terms.CURRENT_TERMS_VERSION
        user.terms_accepted_at = datetime.utcnow()

    db.commit()

    # Create token
    token = create_access_token(
        user_id=user.id,
        role=user.role,
        phone=user.phone,
    )

    print(f"✅ Login: {user.name} ({user.role})")

    return {
        "message":    f"Welcome back {user.name}! 👋",
        "token":      token,
        "token_type": "bearer",
        "user": {
            "id":        user.id,
            "name":      user.name,
            "phone":     user.phone,
            "role":      user.role,
            "school_id": user.school_id,
        },
        "expires_in": "24 hours",
        "note": "Include token in all requests: Authorization: Bearer YOUR_TOKEN"
    }


# ================================================
# ENDPOINT 2 — Register
# ================================================
@router.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    """
    Public self-signup. Always creates a parent account — see
    RegisterRequest's docstring for why role/school_id aren't inputs.
    """
    # Acceptance comes BEFORE the account exists, and it must be of the
    # version in force now — not a stale one, and not a bare "yes".
    if not terms.is_current(data.terms_version):
        raise HTTPException(
            status_code=400,
            detail=(
                "Accept the current terms and privacy policy to create an "
                "account."
            ),
        )

    # Check phone not already registered (fast path; the try/except
    # below is what actually protects against a concurrent duplicate,
    # since this check-then-act has a race window of its own).
    existing = db.query(User).filter(
        User.phone == data.phone
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="This phone number is already registered."
        )

    # Create user with hashed PIN
    user = User(
        name=data.name,
        phone=data.phone,
        pin_hash=hash_pin(data.pin),
        role="parent",
        school_id=None,
        terms_version=terms.CURRENT_TERMS_VERSION,
        terms_accepted_at=datetime.utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Another request registered this same phone number in the
        # window between the check above and this commit.
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="This phone number is already registered."
        )
    db.refresh(user)

    # Create token immediately so they are logged in
    token = create_access_token(
        user_id=user.id,
        role=user.role,
        phone=user.phone,
    )

    print(f"✅ New user registered: {user.name} ({user.role})")

    return {
        "message":  f"Account created successfully! Welcome {user.name} 🎉",
        "token":    token,
        "token_type": "bearer",
        "user": {
            "id":    user.id,
            "name":  user.name,
            "phone": user.phone,
            "role":  user.role,
        }
    }


# ================================================
# ENDPOINT 3 — Get current user info
# ================================================
@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """
    Returns the currently logged-in user's details.
    Use this to verify your token is working.
    """
    return {
        "id":        current_user.id,
        "name":      current_user.name,
        "phone":     current_user.phone,
        "role":      current_user.role,
        "school_id": current_user.school_id,
    }


# ================================================
# ENDPOINT 4 — Change PIN
# ================================================
@router.post("/change-pin")
def change_pin(
    data: ChangePinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Change the logged-in user's PIN.
    Requires current PIN for verification.
    """
    # Verify current PIN
    if not verify_pin(data.current_pin, current_user.pin_hash):
        raise HTTPException(
            status_code=401,
            detail="Current PIN is incorrect."
        )

    # Update to new hashed PIN
    current_user.pin_hash = hash_pin(data.new_pin)
    db.commit()

    return {
        "message": "PIN changed successfully ✅",
        "note":    "Please login again with your new PIN"
    }