# ================================================
# app/routes/family.py
# ------------------------------------------------
# A parent gets their children by phone number (decision 3 Oct 2026).
#
# The school's roster names each child's guardian phone
# (Student.guardian_phone). A parent whose account phone matches gets
# those children, once they have proved by SMS code that they hold that
# phone. Signup does not verify the number, so the code is what stops
# someone registering a guardian's number first and taking the family.
#
#   GET  /family/claimable   how many children wait on this number; no
#                            names until the phone is proved
#   POST /family/send-code   SMS a 6-digit code to the account phone
#   POST /family/claim       {code} the first time; nothing after that
#
# Only children with no parent yet are attached. A child already on
# someone's account is never moved by a phone match: the school decides.
# ================================================

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import PhoneVerification, Student, User
from app.sms import send_sms_sync

router = APIRouter()

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
MAX_SENDS_PER_DAY = 5


class ClaimRequest(BaseModel):
    code: Optional[str] = None


def _hash(user: User, code: str) -> str:
    # Keyed with the app secret and bound to the user, so a leaked table
    # does not hand out codes, and one parent's code fails for another.
    key = os.environ["SECRET_KEY"].encode()
    return hmac.new(key, f"{user.id}:{code}".encode(), hashlib.sha256).hexdigest()


def _assert_parent(user: User) -> None:
    if user.role != "parent":
        raise HTTPException(status_code=403, detail="For parent accounts only.")


def _waiting(db: Session, user: User) -> list:
    return (
        db.query(Student)
        .filter(Student.guardian_phone == user.phone, Student.parent_id.is_(None))
        .order_by(Student.id).all()
    )


def _verified(db: Session, user: User) -> bool:
    return db.query(PhoneVerification).filter(
        PhoneVerification.user_id == user.id,
        PhoneVerification.phone == user.phone,
        PhoneVerification.verified_at.isnot(None),
    ).first() is not None


@router.get("/claimable")
def claimable(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_parent(current_user)
    return {"count": len(_waiting(db, current_user)),
            "verified": _verified(db, current_user)}


@router.post("/send-code")
def send_code(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_parent(current_user)
    # No children waiting, no SMS: the endpoint cannot be used to send
    # texts at our cost for nothing.
    if not _waiting(db, current_user):
        raise HTTPException(
            status_code=404,
            detail="No children are registered under your number. Ask the "
                   "school office to add your number to their records.",
        )

    now = datetime.utcnow()
    recent = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.user_id == current_user.id,
                PhoneVerification.sent_at > now - timedelta(days=1))
        .order_by(PhoneVerification.sent_at.desc()).all()
    )
    if recent and recent[0].sent_at > now - RESEND_AFTER:
        raise HTTPException(status_code=429,
                            detail="A code was just sent. Wait a minute before asking again.")
    if len(recent) >= MAX_SENDS_PER_DAY:
        raise HTTPException(status_code=429,
                            detail="Too many codes today. Try again tomorrow.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(PhoneVerification(
        user_id=current_user.id, phone=current_user.phone,
        code_hash=_hash(current_user, code), sent_at=now,
        expires_at=now + CODE_TTL,
    ))
    db.commit()
    send_sms_sync(current_user.phone, (
        f"Your Nuvora code is {code}. It adds your children to your account. "
        f"Never share it. It expires in 10 minutes."
    ))
    return {"message": "Code sent by SMS.", "expires_in_minutes": 10}


def _check_code(db: Session, user: User, code: Optional[str]) -> None:
    """Raise unless `code` matches this user's latest live code."""
    if not code:
        raise HTTPException(status_code=400,
                            detail="Enter the code we sent to your phone.")
    row = (
        db.query(PhoneVerification)
        .filter(PhoneVerification.user_id == user.id,
                PhoneVerification.phone == user.phone,
                PhoneVerification.verified_at.is_(None))
        .order_by(PhoneVerification.sent_at.desc())
        .with_for_update().first()
    )
    if row is None or row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400,
                            detail="That code has expired. Ask for a new one.")
    if row.attempts >= MAX_ATTEMPTS:
        raise HTTPException(status_code=429,
                            detail="Too many wrong codes. Ask for a new one.")
    if not hmac.compare_digest(row.code_hash, _hash(user, code.strip())):
        row.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="That code is not right.")
    row.verified_at = datetime.utcnow()


@router.post("/claim")
def claim(
    data: ClaimRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_parent(current_user)
    if not _verified(db, current_user):
        _check_code(db, current_user, data.code)

    children = _waiting(db, current_user)
    for child in children:
        child.parent_id = current_user.id
    db.commit()
    return {"added": [c.name for c in children]}
