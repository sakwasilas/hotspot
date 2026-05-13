import os
from dotenv import load_dotenv
from connections import engine, SessionLocal
from models import Base, Package, Admin
from werkzeug.security import generate_password_hash

# Load environment variables from .env file
load_dotenv()

# Create tables
Base.metadata.create_all(bind=engine)

db = SessionLocal()

# Seed packages (unchanged)
packages = [
    {"name": "2 Hours", "price": 10, "duration_hours": 2},
    {"name": "5 Hours", "price": 20, "duration_hours": 5},
    {"name": "12 Hours", "price": 40, "duration_hours": 12},
    {"name": "24 Hours", "price": 50, "duration_hours": 24},
    {"name": "3 Days", "price": 100, "duration_hours": 72},
    {"name": "7 Days", "price": 170, "duration_hours": 168},
    {"name": "10 Days", "price": 220, "duration_hours": 240},
    {"name": "15 Days", "price": 350, "duration_hours": 360},
    {"name": "31 Days", "price": 700, "duration_hours": 744},
]

for pkg in packages:
    exists = db.query(Package).filter_by(name=pkg["name"]).first()
    if not exists:
        db.add(Package(**pkg))

# Admin from environment
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Duka.2480")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not ADMIN_PASSWORD:
    raise ValueError("❌ ADMIN_PASSWORD not set in environment. Please set it before running create.py")

admin_exists = db.query(Admin).filter_by(username=ADMIN_USERNAME).first()
if not admin_exists:
    admin = Admin(username=ADMIN_USERNAME, password=generate_password_hash(ADMIN_PASSWORD))
    db.add(admin)
    print(f"✅ Admin user '{ADMIN_USERNAME}' created.")
else:
    print(f"ℹ️ Admin user '{ADMIN_USERNAME}' already exists.")

db.commit()
db.close()

print("✅ Database seeded successfully (packages + admin ready)")