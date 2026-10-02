from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import School, Student, User
from app.auth import hash_pin, get_current_admin, is_super_admin, assert_school_access
from app.permissions import clean_name, clean_phone


router = APIRouter()


class CreateUserBody(BaseModel):
    """
    JSON body for POST /users/. Preferred over the query-string form
    because a PIN in the URL ends up in access logs in readable form.
    """
    name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    pin: Optional[str] = None
    school_id: Optional[int] = None


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "role": user.role,
        "school_id": user.school_id,
    }


def assert_can_manage_user(current_admin: User, target: User) -> None:
    """
    Who may EDIT an account. A super admin may edit any. A scoped admin
    may only edit staff accounts of their own school — never parents
    (who have no school_id) and never a super admin.
    """
    if is_super_admin(current_admin):
        return
    assert_school_access(current_admin, target.school_id)


def _parent_ids_at_school(db: Session, school_id: int):
    """Ids of parents with at least one child enrolled at `school_id`."""
    return (
        db.query(Student.parent_id)
        .filter(Student.school_id == school_id, Student.parent_id.isnot(None))
    )


def _visible_users(db: Session, current_admin: User):
    """
    Who an admin may SEE. A super admin sees everyone. A scoped admin
    sees the staff of their own school plus the parents of children
    enrolled there.
    """
    q = db.query(User)
    if is_super_admin(current_admin):
        return q
    school_id = current_admin.school_id
    return q.filter(
        (User.school_id == school_id)
        | ((User.role == "parent") & User.id.in_(_parent_ids_at_school(db, school_id)))
    )


# ================================================
# POST /users/
# Admin-only staff/parent provisioning.
#
# role="parent" is always school_id=None (parents are scoped via
# Student.parent_id, not User.school_id — same invariant POST
# /auth/register enforces for self-signup).
#
# role="admin"/"merchant" requires a school_id, and a scoped admin may
# only target their own school (assert_school_access). Minting another
# school_id=None SUPER admin requires the caller already be one.
#
# `pin` is required here (unlike the admin console's other flows) so
# the created account can actually log in — previously this endpoint
# never set pin_hash at all, which meant every account it created was
# permanently unable to authenticate (verify_pin() rejects a null
# hash). The new staff member can change it via /auth/change-pin.
#
# Fields may be sent as a JSON body (preferred — keeps the PIN out of
# the URL and so out of access logs) or as query parameters (the
# original form, still accepted so existing callers keep working).
# A JSON field wins over the same query parameter.
# ================================================
@router.post("/")
def create_user(
    name: Optional[str] = None,
    phone: Optional[str] = None,
    role: Optional[str] = None,
    pin: Optional[str] = None,
    school_id: Optional[int] = None,
    body: Optional[CreateUserBody] = Body(default=None),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Register a new user (parent, admin, or merchant) on an admin's behalf.
    """
    if body is not None:
        name = body.name if body.name is not None else name
        phone = body.phone if body.phone is not None else phone
        role = body.role if body.role is not None else role
        pin = body.pin if body.pin is not None else pin
        school_id = body.school_id if body.school_id is not None else school_id

    missing = [
        field for field, value in
        (("name", name), ("phone", phone), ("role", role), ("pin", pin))
        if value is None
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required field(s): {', '.join(missing)}"
        )

    name = clean_name(name)
    phone = clean_phone(phone)

    valid_roles = ["parent", "admin", "merchant"]
    if role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Must be one of: {valid_roles}"
        )

    if not pin.isdigit() or len(pin) != 4:
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")

    if role == "parent":
        school_id = None
    elif school_id is None:
        if role == "admin":
            # Minting a school_id=None account is minting a super admin —
            # only an existing super admin may do that.
            if not is_super_admin(current_admin):
                raise HTTPException(
                    status_code=403,
                    detail="Only a super admin can create another super admin",
                )
        else:  # merchant
            raise HTTPException(status_code=400, detail="school_id is required for role 'merchant'")
    else:
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail=f"School {school_id} not found")
        assert_school_access(current_admin, school_id)

    # Check phone not already registered (fast path; the try/except
    # below is what actually protects against a concurrent duplicate).
    existing = db.query(User).filter(User.phone == phone).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Phone number {phone} is already registered"
        )

    user = User(name=name, phone=phone, role=role, school_id=school_id, pin_hash=hash_pin(pin))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Phone number {phone} is already registered"
        )
    db.refresh(user)

    return {
        "message": "User created successfully",
        "user": user_payload(user),
    }


# ================================================
# GET /users/
# List users. Admin only.
#
# Was: every user's name, phone and role, to anyone with the URL.
# A super admin sees everyone; a scoped admin sees the staff of their
# own school and the parents of children enrolled there.
# ================================================
@router.get("/")
def get_all_users(
    phone: Optional[str] = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Users visible to the calling admin.

    With `phone`, looks up one account by its exact number. That is how
    a school admin finds a parent who has registered but has no child at
    the school yet (to enrol the child against the parent's id) without
    being able to browse every parent in the system.
    """
    if phone is not None:
        phone = clean_phone(phone)
        match = db.query(User).filter(User.phone == phone).first()
        if match is None:
            return []
        if (
            is_super_admin(current_admin)
            or match.role == "parent"
            or match.school_id == current_admin.school_id
        ):
            return [user_payload(match)]
        return []

    return [user_payload(u) for u in _visible_users(db, current_admin).all()]


# ================================================
# GET /users/role/{role}
# Users with a specific role. Admin only, same scoping as GET /users/.
#
# Declared before /{user_id} so "role" is never parsed as a user id.
# ================================================
@router.get("/role/{role}")
def get_users_by_role(
    role: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Get all users with a specific role.
    Useful to list all parents, all admins, or all merchants.
    """
    valid_roles = ["parent", "admin", "merchant"]
    if role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {valid_roles}"
        )

    users = _visible_users(db, current_admin).filter(User.role == role).all()
    return {
        "role": role,
        "total": len(users),
        "users": [
            {"id": u.id, "name": u.name, "phone": u.phone}
            for u in users
        ]
    }


# ================================================
# GET /users/{user_id}
# Get ONE user by their ID. Admin only, school-scoped.
# ================================================
@router.get("/{user_id}")
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get a specific user by their ID."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User with ID {user_id} not found"
        )
    visible = _visible_users(db, current_admin).filter(User.id == user_id).first()
    if visible is None:
        raise HTTPException(status_code=403, detail="Not permitted to view this user")
    return user_payload(user)


# ================================================
# PUT /users/{user_id}
# Update a user's name or phone. Admin only, school-scoped.
#
# This endpoint used to take no credentials at all. That mattered more
# than it looks: a login token identifies its user BY PHONE (see
# get_current_user in app/auth.py), so whoever can set a user's phone
# can make their own token resolve to that user. Changing a phone here
# is therefore an account-ownership change and is restricted to an
# admin who is allowed to manage the target account.
# ================================================
@router.put("/{user_id}")
def update_user(
    user_id: int,
    name: str = None,
    phone: str = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Update a user's name or phone number."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    assert_can_manage_user(current_admin, user)

    if name:
        user.name = clean_name(name)
    if phone:
        phone = clean_phone(phone)
        # Check new phone not taken by someone else
        existing = db.query(User).filter(
            User.phone == phone,
            User.id != user_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Phone {phone} is already registered to another user"
            )
        user.phone = phone

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Phone {phone} is already registered to another user"
        )
    db.refresh(user)

    return {
        "message": "User updated successfully",
        "user": user_payload(user),
    }
