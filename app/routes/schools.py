from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import School

router = APIRouter()


# ================================================
# POST /schools/
# Create a new school
# ================================================
@router.post("/")
def create_school(
    name: str,
    location: str,
    db: Session = Depends(get_db)
):
    """Register a new school in the system."""

    # Check school name not already taken
    existing = db.query(School).filter(School.name == name).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"A school named '{name}' already exists"
        )

    school = School(name=name, location=location)
    db.add(school)
    db.commit()
    db.refresh(school)

    return {
        "message": "School created successfully",
        "school": {
            "id": school.id,
            "name": school.name,
            "location": school.location,
        }
    }


# ================================================
# GET /schools/
# Get ALL schools
# ================================================
@router.get("/")
def get_all_schools(db: Session = Depends(get_db)):
    """Get all schools in the system."""
    schools = db.query(School).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "location": s.location,
        }
        for s in schools
    ]


# ================================================
# GET /schools/{school_id}
# Get ONE school by ID
# ================================================
@router.get("/{school_id}")
def get_school(school_id: int, db: Session = Depends(get_db)):
    """Get a specific school by its ID."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(
            status_code=404,
            detail=f"School with ID {school_id} not found"
        )
    return {
        "id": school.id,
        "name": school.name,
        "location": school.location,
    }


# ================================================
# PUT /schools/{school_id}
# Update a school's name or location
# ================================================
@router.put("/{school_id}")
def update_school(
    school_id: int,
    name: str = None,
    location: str = None,
    db: Session = Depends(get_db)
):
    """Update a school's name or location."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    if name:
        school.name = name
    if location:
        school.location = location

    db.commit()
    db.refresh(school)

    return {
        "message": "School updated successfully",
        "school": {
            "id": school.id,
            "name": school.name,
            "location": school.location,
        }
    }