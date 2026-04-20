from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()


class Package(Base):
    __tablename__ = "packages"

    id = Column(Integer, primary_key=True)
    name = Column(String(50))
    price = Column(Float)
    duration_hours = Column(Integer)

    def __repr__(self):
        return f"<Package {self.name}>"


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20))
    ip_address = Column(String(50))
    mac_address = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    sessions = relationship("Session", back_populates="customer")

    def __repr__(self):
        return f"<Customer {self.phone}>"


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    checkout_request_id = Column(String(100), unique=True)
    phone = Column(String(20))
    package_id = Column(Integer, ForeignKey("packages.id"))
    amount = Column(Float)
    status = Column(String(20))
    receipt_number = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    package = relationship("Package")

    def __repr__(self):
        return f"<Payment {self.phone} - {self.status}>"


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    package_id = Column(Integer, ForeignKey("packages.id"))
    start_time = Column(DateTime, default=datetime.utcnow)
    end_time = Column(DateTime)
    status = Column(String(20))

    customer = relationship("Customer", back_populates="sessions")
    package = relationship("Package")

  