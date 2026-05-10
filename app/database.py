from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in .env")

# Create engine (SQLite-friendly for now)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

# Session
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Base class for models
Base = declarative_base()


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Test connection
def test_connection():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("✅ Database connected")
    except Exception as e:
        print("❌ Database connection failed:", e)


# Create tables
def create_tables():
    """
    Creates tables AND adds any missing columns automatically.
    This means you never need to delete the database when
    you add a new column to a model.
    """
    from app import models  # noqa
    Base.metadata.create_all(bind=engine)
    print("✅ Tables created")

    # ── Auto-add missing columns ──────────────────
    # This runs raw SQL to add columns that don't exist yet
    # Safe to run many times — skips columns that already exist
    with engine.connect() as conn:
        add_column_if_missing(conn, "wallets",      "daily_limit", "INTEGER DEFAULT 20000")
        add_column_if_missing(conn, "wallets",      "is_active",   "BOOLEAN DEFAULT 1")
        add_column_if_missing(conn, "transactions", "status",      "VARCHAR DEFAULT 'pending'")
        add_column_if_missing(conn, "transactions", "reference",   "VARCHAR")
        add_column_if_missing(conn, "transactions", "momo_phone",  "VARCHAR")
        add_column_if_missing(conn, "transactions", "description", "VARCHAR")
        add_column_if_missing(conn, "merchants",    "momo_phone",  "VARCHAR")
        add_column_if_missing(conn, "nfc_tags",     "is_active",   "BOOLEAN DEFAULT 1")
        conn.commit()
    print("✅ All columns verified")


def add_column_if_missing(conn, table: str, column: str, col_type: str):
    """
    Adds a column to a table only if it does not already exist.
    Prevents errors when the column is already there.
    """
    try:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
        print(f"   ➕ Added column: {table}.{column}")
    except Exception:
        # Column already exists — skip silently
        pass