# ================================================
# database.py — PostgreSQL version
# ------------------------------------------------
# Now using PostgreSQL instead of SQLite.
#
# WHY PostgreSQL?
# ✅ Handles many users at once
# ✅ Production ready
# ✅ Required for deployment
# ✅ Better performance
# ✅ More reliable for financial data
# ================================================

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is not set in .env\n"
        "Example: postgresql://user:password@localhost/schoolwallet"
    )

# ── Create engine ────────────────────────────────
# PostgreSQL does not need check_same_thread
# pool_size → keep 5 connections ready
# max_overflow → allow 10 extra during busy periods
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    echo=False
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_connection():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("✅ PostgreSQL connected successfully")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        raise


def create_tables():
    from app import models  # noqa
    Base.metadata.create_all(bind=engine)
    print("✅ All tables created in PostgreSQL")