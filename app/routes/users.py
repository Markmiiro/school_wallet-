from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

router = APIRouter()


# ================================================
# POST /users/
# Create a new user (parent, admin, or merchant)
# ================================================
@router.post("/")
def create_user(
    name: str,
    phone: str,
    role: str,
    db: Session = Depends(get_db)
):
    """
    Register a new user.
    Role must be: parent, admin, or merchant
    """

    # Check role is valid
    valid_roles = ["parent", "admin", "merchant"]
    if role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Must be one of: {valid_roles}"
        )

    # Check phone not already registered
    existing = db.query(User).filter(User.phone == phone).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Phone number {phone} is already registered"
        )

    user = User(name=name, phone=phone, role=role)
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "message": "User created successfully",
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "role": user.role,
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


# ================================================
# DELETE /users/{user_id}
# Soft concept — in real system we never hard delete
# But useful during testing
# ================================================
@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    """
    Delete a user.
    WARNING: Only use during testing.
    In production never delete users — it breaks transaction history.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user)
    db.commit()

    return {"message": f"User {user_id} ({user.name}) deleted"}