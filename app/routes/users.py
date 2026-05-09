from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

router = APIRouter()


# CREATE USER
@router.post("/users")
def create_user(
    name: str,
    phone: str,
    role: str,
    db: Session = Depends(get_db)
):
    user = User(
        name=name,
        phone=phone,
        role=role
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "message": "User created successfully",
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "role": user.role
        }
    }


# GET ALL USERS
@router.get("/users")
def get_users(db: Session = Depends(get_db)):
    users = db.query(User).all()

    return users