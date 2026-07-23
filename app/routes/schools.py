import os

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import School, User
from app.auth import get_current_admin

router = APIRouter()


# ================================================
# CLOUDINARY CONFIG
# ------------------------------------------------
# Credentials come from environment variables set in the
# Railway dashboard (Variables tab) — NEVER from a committed
# .env file. Required vars:
#
#   CLOUDINARY_CLOUD_NAME
#   CLOUDINARY_API_KEY
#   CLOUDINARY_API_SECRET
#
# If they're missing, the upload endpoint returns a clear 503
# rather than failing with a confusing SDK error.
# ================================================

MAX_BADGE_BYTES = 2 * 1024 * 1024   # 2 MB — crests should be small
ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/svg+xml",
}


def _cloudinary_ready() -> bool:
    return all([
        os.getenv("CLOUDINARY_CLOUD_NAME"),
        os.getenv("CLOUDINARY_API_KEY"),
        os.getenv("CLOUDINARY_API_SECRET"),
    ])


def _configure_cloudinary():
    """Configure the SDK lazily, so a missing package or missing
    credentials only affects the badge endpoint — not app startup."""
    import cloudinary  # imported here on purpose, see docstring

    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )
    return cloudinary


def school_payload(school: School) -> dict:
    return {
        "id": school.id,
        "name": school.name,
        "location": school.location,
        "badge_url": school.badge_url,
    }


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
        "school": school_payload(school),
    }


# ================================================
# GET /schools/
# Get ALL schools
# ================================================
@router.get("/")
def get_all_schools(db: Session = Depends(get_db)):
    """Get all schools in the system."""
    schools = db.query(School).all()
    return [school_payload(s) for s in schools]


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
    return school_payload(school)


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
        "school": school_payload(school),
    }


# ================================================
# POST /schools/{school_id}/badge
# Upload a school crest/badge image.
#
# Admin only. Accepts a real image file, uploads it to
# Cloudinary, and stores the resulting HTTPS URL on the school.
#
# The image is stored under a per-school public_id, so
# re-uploading replaces the previous crest rather than
# accumulating orphaned files.
# ================================================
@router.post("/{school_id}/badge")
async def upload_school_badge(
    school_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Upload a school badge image. Admins only."""

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    if not _cloudinary_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "Image hosting is not configured. Set CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in the Railway "
                "environment variables."
            ),
        )

    # ── Validate type ────────────────────────────
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{file.content_type}'. "
                f"Allowed: PNG, JPEG, WEBP, SVG."
            ),
        )

    # ── Validate size ────────────────────────────
    contents = await file.read()
    if len(contents) > MAX_BADGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Image is too large. Maximum size is "
                   f"{MAX_BADGE_BYTES // (1024 * 1024)} MB.",
        )
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # ── Upload ───────────────────────────────────
    try:
        cloudinary = _configure_cloudinary()
        import cloudinary.uploader  # noqa: E402

        result = cloudinary.uploader.upload(
            contents,
            folder="school_wallet/badges",
            public_id=f"school_{school_id}",
            overwrite=True,
            resource_type="image",
        )
        badge_url = result.get("secure_url")
        if not badge_url:
            raise RuntimeError("Cloudinary did not return a secure_url")

    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="The 'cloudinary' package is not installed on the server.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Image upload failed: {e}",
        )

    school.badge_url = badge_url
    db.commit()
    db.refresh(school)

    return {
        "message": "Badge uploaded successfully",
        "school": school_payload(school),
    }


# ================================================
# PUT /schools/{school_id}/badge-url
# Set the badge URL directly, without uploading a file.
#
# Useful when the crest is already hosted somewhere (or for
# clearing a badge by passing an empty value). Admin only.
# ================================================
@router.put("/{school_id}/badge-url")
def set_school_badge_url(
    school_id: int,
    badge_url: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """Set or clear a school's badge URL directly. Admins only."""

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    if badge_url:
        cleaned = badge_url.strip()
        if not cleaned.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="badge_url must be an https:// link.",
            )
        school.badge_url = cleaned
    else:
        # No value supplied — clear the badge.
        school.badge_url = None

    db.commit()
    db.refresh(school)

    return {
        "message": "Badge URL updated",
        "school": school_payload(school),
    }