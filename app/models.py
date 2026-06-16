from sqlalchemy import (
    Column, Integer, String, Float,
    ForeignKey, DateTime, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


# ════════════════════════════════════════════════
# USERS
# Parents, admins, and merchants all live here.
# Role determines what they can do.
# ════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    id        = Column(Integer, primary_key=True, index=True)
    name      = Column(String, nullable=False)
    phone     = Column(String, unique=True, nullable=False)
    role      = Column(String, nullable=False)   # parent | admin | merchant
    pin_hash  = Column(String, nullable=True)    # hashed PIN for login
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)

    # Relationships
    students = relationship("Student", back_populates="parent")


# ════════════════════════════════════════════════
# SCHOOLS
# ════════════════════════════════════════════════
class School(Base):
    __tablename__ = "schools"

    id       = Column(Integer, primary_key=True, index=True)
    name     = Column(String, nullable=False)
    location = Column(String, nullable=True)

    # Relationships
    students  = relationship("Student", back_populates="school")
    merchants = relationship("Merchant", back_populates="school")


# ════════════════════════════════════════════════
# STUDENTS
# ════════════════════════════════════════════════
class Student(Base):
    __tablename__ = "students"

    id        = Column(Integer, primary_key=True, index=True)
    name      = Column(String, nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Relationships
    school  = relationship("School", back_populates="students")
    parent  = relationship("User", back_populates="students")
    wallet  = relationship("Wallet", back_populates="student", uselist=False)
    nfc_tag = relationship("NFCTag", back_populates="student", uselist=False)


# ════════════════════════════════════════════════
# WALLETS
# One wallet per student.
# Balance is in UGX (stored as Float).
# ════════════════════════════════════════════════
class Wallet(Base):
    __tablename__ = "wallets"

    id          = Column(Integer, primary_key=True, index=True)
    balance     = Column(Float, default=0.0)
    is_active   = Column(Boolean, default=True)
    daily_limit = Column(Integer, default=20000)   # UGX per day
    student_id  = Column(Integer, ForeignKey("students.id"), nullable=False)

    # Relationships
    student      = relationship("Student", back_populates="wallet")
    transactions = relationship("Transaction", back_populates="wallet")
    payments     = relationship("Payment", back_populates="wallet")


# ════════════════════════════════════════════════
# TRANSACTIONS
# Every money movement — top-ups and payments.
# NEVER delete rows from this table.
# NEVER update the amount after creation.
# ════════════════════════════════════════════════
class Transaction(Base):
    __tablename__ = "transactions"

    id          = Column(Integer, primary_key=True, index=True)
    wallet_id   = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True)
    amount      = Column(Float, nullable=False)
    type        = Column(String, nullable=False)          # topup | payment
    status      = Column(String, default="pending")       # pending | completed | failed
    reference   = Column(String, nullable=True)           # Yo Uganda ExternalReference
    momo_phone  = Column(String, nullable=True)           # phone used for top-up
    description = Column(String, nullable=True)           # e.g. "Lunch money"
    timestamp   = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet   = relationship("Wallet", back_populates="transactions")
    merchant = relationship("Merchant", back_populates="transactions")


# ════════════════════════════════════════════════
# NFC TAGS
# One NFC card per student.
# tag_uid is the physical card's unique ID.
# ════════════════════════════════════════════════
class NFCTag(Base):
    __tablename__ = "nfc_tags"

    id         = Column(Integer, primary_key=True, index=True)
    tag_uid    = Column(String, unique=True, nullable=True)
    is_active  = Column(Boolean, default=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)

    # Relationships
    student = relationship("Student", back_populates="nfc_tag")


# ════════════════════════════════════════════════
# MERCHANTS
# Tuck shop vendors inside a school.
# momo_phone receives end-of-day payout.
# ════════════════════════════════════════════════
class Merchant(Base):
    __tablename__ = "merchants"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String, nullable=False)
    school_id  = Column(Integer, ForeignKey("schools.id"), nullable=False)
    momo_phone = Column(String, nullable=True)
    is_active  = Column(Boolean, default=True)

    # Relationships
    school       = relationship("School", back_populates="merchants")
    transactions = relationship("Transaction", back_populates="merchant")


# ════════════════════════════════════════════════
# PAYMENTS
# Records of NFC payment attempts at tuck shop.
# Separate from Transactions to track
# payment-specific details (NFC, offline sync).
# ════════════════════════════════════════════════
class Payment(Base):
    __tablename__ = "payments"

    id        = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id"), nullable=False)
    amount    = Column(Float, nullable=False)
    status    = Column(String, nullable=False)     # completed | failed
    reference = Column(String, nullable=True)      # idempotency key
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Relationships
    wallet = relationship("Wallet", back_populates="payments")