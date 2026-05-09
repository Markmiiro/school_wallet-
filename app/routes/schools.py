from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import School

# Create router object
router = APIRouter()


# =========================================
# CREATE SCHOOL
# =========================================
@router.post("/schools")
def create_school(
    name: str,
    location: str,
    db: Session = Depends(get_db)
):
    # Create school object
    school = School(
        name=name,
        location=location
    )

    # Save to database
    db.add(school)
    db.commit()
    db.refresh(school)

    return {
        "message": "School created successfully",
        "school": {
            "id": school.id,
            "name": school.name,
            "location": school.location
        }
    }


# =========================================
# GET ALL SCHOOLS
# =========================================
@router.get("/schools")
def get_schools(db: Session = Depends(get_db)):
    schools = db.query(School).all()

    return schools