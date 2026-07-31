from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import School, User
from app.auth import hash_pin, get_current_admin, is_super_admin, assert_school_access


router = APIRouter()


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
# ================================================
@router.post("/")
def create_user(
    name: str,
    phone: str,
    role: str,
    pin: str,
    school_id: int = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """
    Register a new user (parent, admin, or merchant) on an admin's behalf.
    """

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
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "role": user.role,
            "school_id": user.school_id,
        }
    }


# ================================================
# GET /users/
# Get ALL users
# ================================================
@router.get("/")
def get_all_users(db: Session = Depends(get_db)):
    """Get all users in the system."""
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "role": u.role,
        }
        for u in users
    ]


# ================================================
# GET /users/{user_id}
# Get ONE user by their ID
# ================================================
@router.get("/{user_id}")
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Get a specific user by their ID."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User with ID {user_id} not found"
        )
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "role": user.role,
    }


# ================================================
# GET /users/role/{role}
# Get all users with a specific role
# ================================================
@router.get("/role/{role}")
def get_users_by_role(role: str, db: Session = Depends(get_db)):
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

    users = db.query(User).filter(User.role == role).all()
    return {
        "role": role,
        "total": len(users),
        "users": [
            {"id": u.id, "name": u.name, "phone": u.phone}
            for u in users
        ]
    }


# ================================================
# PUT /users/{user_id}
# Update a user's name or phone
# ================================================
@router.put("/{user_id}")
def update_user(
    user_id: int,
    name: str = None,
    phone: str = None,
    db: Session = Depends(get_db)
):
    """Update a user's name or phone number."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if name:
        user.name = name
    if phone:
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

    db.commit()
    db.refresh(user)

    return {
        "message": "User updated successfully",
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "role": user.role,
        }
    }
