from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Numeric, Enum
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True)
    username =Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, index=True, nullable=False)
    ip_address = Column(String(50))
    mac_address = Column(String(50), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sessions = relationship("Session", back_populates="customer", cascade="all, delete")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    checkout_request_id = Column(String(100), unique=True, index=True)
    phone = Column(String(20), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    package_id = Column(Integer, ForeignKey("packages.id"))
    amount = Column(Numeric(10, 2), nullable=False)
    status = Column(Enum("pending", "paid", "failed", name="payment_status"), default="pending", nullable=False)
    receipt_number = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    package = relationship("Package")
    customer = relationship("Customer")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    package_id = Column(Integer, ForeignKey("packages.id"))
    start_time = Column(DateTime, default=datetime.utcnow)
    end_time = Column(DateTime)
    status = Column(Enum("active", "expired", name="session_status"), default="active", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="sessions")
    package = relationship("Package")


class Package(Base):
    __tablename__ = "packages"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    duration_hours = Column(Integer, nullable=False)