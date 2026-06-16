# ================================================
# app/routes/auth.py
# ------------------------------------------------
# Authentication endpoints.
#
# LOGIN:    POST /auth/login    → phone + PIN → JWT
# REGISTER: POST /auth/register → create user with PIN
# ME:       GET  /auth/me       → who am I?
#
# All protected endpoints use Bearer token.
# ================================================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.models import User
from app.auth import (
    verify_pin,
    hash_pin,
    create_access_token,
    get_current_user,
)

router = APIRouter()


# ── Schemas ───────────────────────────────────────
class LoginRequest(BaseModel):
    phone: str
    pin: str


class RegisterRequest(BaseModel):
    name: str
    phone: str
    role: str
    pin: str


# ════════════════════════════════════════════════
# POST /auth/login
# ════════════════════════════════════════════════
@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Login with phone number + PIN.
    Returns a JWT Bearer token for all protected endpoints.

    Example:
    {
        "phone": "256760945424",
        "pin":   "1234"
    }
    """

    # ── Find user ─────────────────────────────────
    user = db.query(User).filter(User.phone == payload.phone).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Phone number not registered"
        )

    # ── Check PIN is set ──────────────────────────
    if not user.pin_hash:
        raise HTTPException(
            status_code=401,
            detail="No PIN set for this account. Contact admin."
        )

    # ── Verify PIN ────────────────────────────────
    if not verify_pin(payload.pin, user.pin_hash):
        raise HTTPException(
            status_code=401,
            detail="Wrong PIN. Please try again."
        )

    # ── Create token ──────────────────────────────
    token = create_access_token(
        user_id=user.id,
        role=user.role,
        phone=user.phone,
    )

    return {
        "access_token": token,
        "token_type":   "bearer",
        "user": {
            "id":    user.id,
            "name":  user.name,
            "phone": user.phone,
            "role":  user.role,
        },
    }


# ════════════════════════════════════════════════
# POST /auth/register
# ════════════════════════════════════════════════
@router.post("/register")
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """
    Register a new user with a PIN.

    Role must be one of: parent | admin | merchant

    Example:
    {
        "name":  "Mark Miiro",
        "phone": "256760945424",
        "role":  "admin",
        "pin":   "1234"
    }
    """

    # ── Validate role ─────────────────────────────
    valid_roles = ["parent", "admin", "merchant"]
    if payload.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Role must be one of: {valid_roles}"
        )

    # ── Check phone not already taken ─────────────
    existing = db.query(User).filter(User.phone == payload.phone).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Phone {payload.phone} is already registered"
        )

    # ── Validate PIN ──────────────────────────────
    if len(payload.pin) < 4:
        raise HTTPException(
            status_code=400,
            detail="PIN must be at least 4 digits"
        )

    # ── Create user ───────────────────────────────
    # pin_hash stores the bcrypt hash — never store plain PIN
    user = User(
        name=payload.name,
        phone=payload.phone,
        role=payload.role,
        pin_hash=hash_pin(payload.pin),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # ── Return token immediately ──────────────────
    token = create_access_token(
        user_id=user.id,
        role=user.role,
        phone=user.phone,
    )

    return {
        "message":      "User registered successfully",
        "access_token": token,
        "token_type":   "bearer",
        "user": {
            "id":    user.id,
            "name":  user.name,
            "phone": user.phone,
            "role":  user.role,
        },
    }


# ════════════════════════════════════════════════
# GET /auth/me
# ════════════════════════════════════════════════
@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """
    Returns the currently logged-in user's profile.
    Requires: Authorization: Bearer <token>
    """
    return {
        "id":    current_user.id,
        "name":  current_user.name,
        "phone": current_user.phone,
        "role":  current_user.role,
    }