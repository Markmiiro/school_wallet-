from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import os

from app.database import get_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY   = os.getenv("SECRET_KEY", "school_wallet_secret_change_this")
ALGORITHM    = "HS256"
EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "24"))

bearer_scheme = HTTPBearer()


def hash_pin(pin: str) -> str:
    return pwd_context.hash(pin)


def verify_pin(plain_pin: str, hashed_pin: str) -> bool:
    return pwd_context.verify(plain_pin, hashed_pin)


def create_access_token(user_id: int, role: str, phone: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=EXPIRE_HOURS)
    payload = {
        "sub":   str(user_id),
        "role":  role,
        "phone": phone,
        "exp":   expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please login again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db)
):
    from app.models import User
    token   = credentials.credentials
    payload = decode_token(token)
    user_id = payload.get("sub")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    user = db.query(User).filter(User.id == int(user_id)).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    return user


def get_current_parent(current_user=Depends(get_current_user)):
    if current_user.role != "parent":
        raise HTTPException(status_code=403, detail="Parents only")
    return current_user


def get_current_admin(current_user=Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    return current_user


def get_current_merchant(current_user=Depends(get_current_user)):
    if current_user.role != "merchant":
        raise HTTPException(status_code=403, detail="Merchants only")
    return current_user


def get_admin_or_parent(current_user=Depends(get_current_user)):
    if current_user.role not in ["admin", "parent"]:
        raise HTTPException(status_code=403, detail="Admins and parents only")
    return current_user