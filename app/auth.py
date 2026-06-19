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
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a signed JWT.

    `data` should contain at least {"sub": user.phone} so we can
    look the user back up on every protected request.
    """
    to_encode = data.copy()
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