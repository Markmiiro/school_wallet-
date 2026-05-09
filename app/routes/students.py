from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Student, Wallet, NFCTag

router = APIRouter()


# CREATE STUDENT
@router.post("/students")
def create_student(
    name: str,
    school_id: int,
    parent_id: int,
    db: Session = Depends(get_db)
):

    # Create student
    student = Student(
        name=name,
        school_id=school_id,
        parent_id=parent_id
    )

    # Save student first
    db.add(student)

    # Generate student.id
    db.flush()

    # Create wallet automatically
    wallet = Wallet(
        student_id=student.id,
        balance=0,
        is_active=True
    )

    db.add(wallet)

    # Create NFC slot automatically
    nfc_tag = NFCTag(
        student_id=student.id,
        tag_uid=None,
        is_active=True
    )

    db.add(nfc_tag)

    # Save everything
    db.commit()

    # Reload student data
    db.refresh(student)

    # Return response
    return {
        "message": "Student created successfully",

        "student": {
            "id": student.id,
            "name": student.name,
            "school_id": student.school_id,
            "parent_id": student.parent_id
        },

        "wallet": {
            "balance": wallet.balance,
            "active": wallet.is_active
        },

        "nfc_tag": {
            "tag_uid": nfc_tag.tag_uid,
            "active": nfc_tag.is_active
        }
    }
# GET ALL STUDENTS
@router.get("/students")
def get_students(db: Session = Depends(get_db)):
    students = db.query(Student).all()

    return students