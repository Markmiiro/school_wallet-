from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Student, Wallet, NFCTag, School, User

router = APIRouter()


# ================================================
# POST /students/
# Create a new student
# Auto creates wallet + NFC slot
# ================================================
@router.post("/")
def create_student(
    name: str,
    school_id: int,
    parent_id: int,
    db: Session = Depends(get_db)
):
    """Register a new student. Automatically creates their wallet and NFC tag slot."""

    # Check school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    # Check parent exists
    parent = db.query(User).filter(User.id == parent_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail=f"Parent {parent_id} not found")

    # Create student
    student = Student(
        name=name,
        school_id=school_id,
        parent_id=parent_id
    )
    db.add(student)
    db.flush()  # get student.id before committing

    # Auto create wallet — starts at zero balance
    wallet = Wallet(
        student_id=student.id,
        balance=0,
        is_active=True
    )
    db.add(wallet)

    # Auto create NFC slot — no bracelet assigned yet
    # NOTE: no is_active on NFCTag — your model does not have it
    nfc_tag = NFCTag(
        student_id=student.id,
        tag_uid=None,
    )
    db.add(nfc_tag)

    # Save everything at once
    db.commit()
    db.refresh(student)

    return {
        "message": "Student created successfully",
        "student": {
            "id": student.id,
            "name": student.name,
            "school_id": student.school_id,
            "parent_id": student.parent_id,
        },
        "wallet": {
            "id": wallet.id,
            "balance": wallet.balance,
            "is_active": wallet.is_active,
        },
        "nfc_tag": {
            "tag_uid": nfc_tag.tag_uid,
            "status": "no bracelet assigned yet"
        }
    }


# ================================================
# GET /students/
# Get ALL students
# ================================================
@router.get("/")
def get_all_students(db: Session = Depends(get_db)):
    """Get all students in the system."""
    students = db.query(Student).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "school_id": s.school_id,
            "parent_id": s.parent_id,
        }
        for s in students
    ]


# ================================================
# GET /students/{student_id}
# Get ONE student by their ID
# ================================================
@router.get("/{student_id}")
def get_student(student_id: int, db: Session = Depends(get_db)):
    """Get a specific student by their ID."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(
            status_code=404,
            detail=f"Student with ID {student_id} not found"
        )
    return {
        "id": student.id,
        "name": student.name,
        "school_id": student.school_id,
        "parent_id": student.parent_id,
    }


# ================================================
# GET /students/school/{school_id}
# Get all students in a specific school
# ================================================
@router.get("/school/{school_id}")
def get_students_by_school(school_id: int, db: Session = Depends(get_db)):
    """Get all students enrolled in a specific school."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail=f"School {school_id} not found")

    students = db.query(Student).filter(Student.school_id == school_id).all()
    return {
        "school": school.name,
        "total_students": len(students),
        "students": [
            {"id": s.id, "name": s.name, "parent_id": s.parent_id}
            for s in students
        ]
    }


# ================================================
# GET /students/parent/{parent_id}
# Get all students under one parent
# ================================================
@router.get("/parent/{parent_id}")
def get_students_by_parent(parent_id: int, db: Session = Depends(get_db)):
    """Get all students belonging to a specific parent."""
    parent = db.query(User).filter(User.id == parent_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail=f"Parent {parent_id} not found")

    students = db.query(Student).filter(Student.parent_id == parent_id).all()
    return {
        "parent": parent.name,
        "total_children": len(students),
        "students": [
            {"id": s.id, "name": s.name, "school_id": s.school_id}
            for s in students
        ]
    }


# ================================================
# PUT /students/{student_id}/assign-nfc
# Assign a physical NFC bracelet to a student
# ================================================
@router.put("/{student_id}/assign-nfc")
def assign_nfc_tag(
    student_id: int,
    tag_uid: str,
    db: Session = Depends(get_db)
):
    """
    Assign a physical NFC bracelet to a student.
    Once assigned, the student can tap to pay at the tuck shop.
    """
    # Check student exists
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Check this tag is not already used by someone else
    already_used = db.query(NFCTag).filter(NFCTag.tag_uid == tag_uid).first()
    if already_used:
        raise HTTPException(
            status_code=400,
            detail=f"NFC tag {tag_uid} is already assigned to another student"
        )

    # Find this student's NFC slot and assign the tag
    nfc = db.query(NFCTag).filter(NFCTag.student_id == student_id).first()
    if not nfc:
        raise HTTPException(status_code=404, detail="NFC slot not found for this student")

    nfc.tag_uid = tag_uid
    db.commit()

    return {
        "message": "NFC bracelet assigned successfully",
        "student_id": student_id,
        "student_name": student.name,
        "tag_uid": tag_uid,
        "status": "ready to tap and pay ✅"
    }


# ================================================
# PUT /students/{student_id}/deactivate
# Deactivate a student who left the school
# ================================================
@router.put("/{student_id}/deactivate")
def deactivate_student(student_id: int, db: Session = Depends(get_db)):
    """
    Deactivate a student.
    Never deletes — keeps full transaction history intact.
    Also deactivates their wallet so no payments can be made.
    """
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Deactivate wallet too
    wallet = db.query(Wallet).filter(Wallet.student_id == student_id).first()
    if wallet:
        wallet.is_active = False

    db.commit()

    return {
        "message": f"{student.name} has been deactivated",
        "student_id": student_id,
        "wallet_deactivated": True,
        "note": "Transaction history is preserved"
    }