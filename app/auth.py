# ================================================
# app/auth.py
# ------------------------------------------------
# Authentication utilities — JWT creation/verification
# and PIN hashing (bcrypt via passlib).
#
# This file DEFINES the functions. It does NOT import
# from app.routes.auth — that file imports FROM here.
#
# Used by:
#   app/routes/auth.py   → login, register, /me endpoints
#   app/routes/*.py       → Depends(get_current_user) on protected routes
# ================================================

import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

# ── Config ────────────────────────────────────────
SECRET_KEY              = os.getenv("SECRET_KEY", "")
ALGORITHM                = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

if not SECRET_KEY:
    # Fail loudly rather than silently signing tokens with an empty key
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Set it in Railway → Variables before starting the app."
    )

# ── PIN hashing context (bcrypt) ───────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── OAuth2 scheme — tells FastAPI where to find the login endpoint ──
# tokenUrl is just used for the Swagger UI "Authorize" button.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


# ================================================
# PIN HASHING
# ================================================
def hash_pin(pin: str) -> str:
    """Hash a plaintext PIN for storage in User.pin_hash."""
    return pwd_context.hash(pin)


def verify_pin(plain_pin: str, hashed_pin: str) -> bool:
    """Check a plaintext PIN against the stored bcrypt hash."""
    if not hashed_pin:
        return False
    return pwd_context.verify(plain_pin, hashed_pin)


# ================================================
# JWT TOKEN CREATION
# ================================================
def create_access_token(
    user_id: int,
    role: str,
    phone: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT.

    Called from app/routes/auth.py as:
        create_access_token(user_id=user.id, role=user.role, phone=user.phone)

    "sub" is set to phone so get_current_user() can look the user
    back up on every protected request via User.phone.
    """
    to_encode = {
        "sub": phone,
        "user_id": user_id,
        "role": role,
    }
    expire = datetime.utcnow() + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ================================================
# GET CURRENT USER (dependency for protected routes)
# ================================================
def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Decode the JWT from the Authorization header, look up the user,
    and return it. Raises 401 if the token is invalid/expired or the
    user no longer exists.

    Usage in any route:
        from app.auth import get_current_user
        @router.get("/protected")
        def protected_route(current_user: User = Depends(get_current_user)):
            ...
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        phone: str = payload.get("sub")
        if phone is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.phone == phone).first()
    if user is None:
        raise credentials_exception

    return user


# ================================================
# GET CURRENT ADMIN (dependency for admin-only routes)
# ================================================
def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Builds on get_current_user — requires a valid token AND that the
    user's role is "admin". Raises 403 if the user is logged in but
    not an admin (e.g. parent or merchant).

    Usage in any route:
        from app.auth import get_current_admin
        @router.get("/admin-only")
        def admin_route(current_admin: User = Depends(get_current_admin)):
            ...
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires admin privileges.",
        )
    return current_user


# ================================================
# SCHOOL SCOPING
# ------------------------------------------------
# get_current_admin answers "is this an admin?".
# These answer "is this admin allowed to touch THIS school?".
#
# The rule:
#   role="admin", school_id=3     → manages school 3 only
#   role="admin", school_id=NULL  → super admin, manages everything
#
# No new role value, so nothing that already checks
# role == "admin" needs changing.
#
# ⚠️ Because NULL means "unlimited", every admin who should be
# scoped MUST have school_id set explicitly. Check with:
#   SELECT id, name, phone, school_id FROM users WHERE role='admin';
# Only your own account should have school_id NULL.
# ================================================
def is_super_admin(user: User) -> bool:
    """True for an admin with no school attached — full cross-school access."""
    return user.role == "admin" and user.school_id is None


def assert_school_access(user: User, school_id: Optional[int]) -> None:
    """
    Raise 403 unless `user` may act on a resource belonging to `school_id`.

    Call it AFTER loading the resource, using that resource's school:

        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(404, "Student not found")
        assert_school_access(current_admin, student.school_id)

    Note the school comes from the resource, never from the request — a
    caller-supplied school_id would defeat the whole check.
    """
    if is_super_admin(user):
        return

    # A scoped admin with no school of their own can act on nothing.
    if user.school_id is None or school_id is None or user.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage students and cards in your own school.",
        )


def visible_school_id(user: User) -> Optional[int]:
    """
    The school a list query should be filtered to, or None for "no filter".

        q = db.query(Student)
        school = visible_school_id(current_user)
        if school is not None:
            q = q.filter(Student.school_id == school)

    Returns None only for super admins. Everyone else is pinned to their
    own school, so a merchant or scoped admin cannot enumerate other
    schools by leaving a filter off.
    """
    if is_super_admin(user):
        return None
    return user.school_id