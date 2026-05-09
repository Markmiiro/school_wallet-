from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base

# USERS
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    phone = Column(String, unique=True)
    role = Column(String)

    students = relationship("Student", back_populates="parent")


# SCHOOLS
class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    location = Column(String)

    students = relationship("Student", back_populates="school")


# STUDENTS
class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)

    school_id = Column(Integer, ForeignKey("schools.id"))
    parent_id = Column(Integer, ForeignKey("users.id"))

    school = relationship("School", back_populates="students")
    parent = relationship("User", back_populates="students")

    wallet = relationship("Wallet", back_populates="student", uselist=False)
    nfc_tag = relationship("NFCTag", back_populates="student", uselist=False)


# WALLETS
class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    balance = Column(Float, default=0)
    is_active = Column(Boolean, default=True)

    student_id = Column(Integer, ForeignKey("students.id"))

    student = relationship("Student", back_populates="wallet")
    transactions = relationship("Transaction", back_populates="wallet")
    payments = relationship("Payment", back_populates="wallet")


# TRANSACTIONS
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)

    wallet_id = Column(Integer, ForeignKey("wallets.id"))
    merchant_id = Column(Integer, ForeignKey("merchants.id"))

    amount = Column(Float)
    type = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="transactions")
    merchant = relationship("Merchant", back_populates="transactions")


# NFC TAGS
class NFCTag(Base):
    __tablename__ = "nfc_tags"

    id = Column(Integer, primary_key=True, index=True)

    # Physical NFC unique ID
    tag_uid = Column(String, unique=True, nullable=True)

    # Can this tag make payments?
    is_active = Column(Boolean, default=True)

    # Which student owns this tag
    student_id = Column(Integer, ForeignKey("students.id"))

    student = relationship("Student", back_populates="nfc_tag")

# MERCHANTS
class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)

    school_id = Column(Integer, ForeignKey("schools.id"))

    transactions = relationship("Transaction", back_populates="merchant")


# PAYMENTS
class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)

    wallet_id = Column(Integer, ForeignKey("wallets.id"))

    amount = Column(Float)
    status = Column(String)
    reference = Column(String)

    wallet = relationship("Wallet", back_populates="payments")