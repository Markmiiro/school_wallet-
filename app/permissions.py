# ================================================
# app/permissions.py
# ------------------------------------------------
# Authorization helpers shared by the route files.
#
# app/auth.py answers "who is this?" (get_current_user) and "is this
# admin allowed to touch this school?" (assert_school_access). These
# build on it for the two questions the routes kept leaving unasked:
#
#   - may this user run a till for this school?   (assert_till_staff)
#   - may this user see / fund this wallet?       (assert_wallet_access)
#
# Kept out of app/auth.py on purpose — that file is money code and is
# not edited without explicit sign-off.
# ================================================

import re
from typing import Optional

from fastapi import HTTPException

from app.auth import is_super_admin
from app.models import Student, User

STAFF_ROLES = ("admin", "merchant")

_PHONE_RE = re.compile(r"\A256\d{9}\Z")


def assert_staff(user: User) -> None:
    """403 unless the caller is an admin or a merchant (tuck-shop staff)."""
    if user.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=403,
            detail="This action is for school staff only.",
        )


def assert_till_staff(user: User, school_id: Optional[int]) -> None:
    """
    403 unless `user` is staff of the school `school_id`.

    Pass the school of the RESOURCE being acted on (the merchant, the
    student), never a school id taken from the request.
    """
    assert_staff(user)
    if is_super_admin(user):
        return
    if user.school_id is None or school_id is None or user.school_id != school_id:
        raise HTTPException(
            status_code=403,
            detail="You can only act for your own school.",
        )


def assert_wallet_access(user: User, student: Optional[Student]) -> None:
    """
    403 unless `user` may see or fund the wallet belonging to `student`.

        super admin → any wallet
        admin       → wallets of students in their own school
        merchant    → wallets of students in their own school
        parent      → their own children's wallets only
    """
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    if is_super_admin(user):
        return

    if user.role in STAFF_ROLES:
        if user.school_id is not None and user.school_id == student.school_id:
            return

    if user.role == "parent" and student.parent_id == user.id:
        return

    raise HTTPException(status_code=403, detail="Not permitted for this wallet")


def clean_phone(raw: Optional[str]) -> str:
    """Normalise to 256XXXXXXXXX, or raise 422."""
    cleaned = (raw or "").replace(" ", "").replace("+", "")
    if not _PHONE_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=422,
            detail="Phone must be in 256XXXXXXXXX format (12 digits).",
        )
    return cleaned


def clean_local_phone(raw: Optional[str]) -> str:
    """
    clean_phone(), also accepting the local forms a school types into a
    roster: 0700 111 222, 700111222, +256 700 111 222.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "256" + digits[1:]
    elif len(digits) == 9:
        digits = "256" + digits
    return clean_phone(digits)


def clean_name(raw: Optional[str], *, what: str = "Name") -> str:
    """Trim, and require 1–100 characters, or raise 422."""
    cleaned = (raw or "").strip()
    if not cleaned or len(cleaned) > 100:
        raise HTTPException(
            status_code=422,
            detail=f"{what} must be between 1 and 100 characters.",
        )
    return cleaned
