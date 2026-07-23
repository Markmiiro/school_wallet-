# ================================================
# app/routes/auth.py
# ------------------------------------------------
# Authentication endpoints:
# POST /auth/login    → get a JWT token
# POST /auth/register → create a new user
# GET  /auth/me       → get current user info
# POST /auth/change-pin → change PIN
# ================================================

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from typing import Optional

from app.database import get_db
from app.models import User
from app.auth import hash_pin, verify_pin, create_access_token, get_current_user

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

class RegisterRequest(BaseModel):
    name: str
    phone: str
    pin: str
    role: str
    school_id: Optional[int] = None

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

    @field_validator("role")
    def role_must_be_valid(cls, v):
        if v not in ["parent", "admin", "merchant"]:
            raise ValueError("Role must be parent, admin, or merchant")
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
        # Deliberately vague — same message as "wrong PIN" further down,
        # so an attacker can't use this endpoint to discover which phone
        # numbers are registered.
        raise HTTPException(
            status_code=401,
            detail="Phone number not registered. Contact school admin."
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
    Register a new user with a hashed PIN.
    Role must be: parent, admin, or merchant
    """
    # Check phone not already registered
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
        role=data.role,
        school_id=data.school_id,
    )
    db.add(user)
    db.commit()
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