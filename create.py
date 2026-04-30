from connections import engine, SessionLocal
from models import Base, Package, Admin
from werkzeug.security import generate_password_hash

# =========================
# CREATE TABLES (SAFE)
# =========================
Base.metadata.create_all(bind=engine)

db = SessionLocal()

# =========================
# SEED PACKAGES
# =========================
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

# =========================
# SEED ADMIN
# =========================
admin_exists = db.query(Admin).filter_by(username="Duka.2480").first()

if not admin_exists:
    admin = Admin(
        username="Duka.2480",
        password=generate_password_hash("silas")
    )
    db.add(admin)

# =========================
# COMMIT CHANGES
# =========================
db.commit()
db.close()

print("✅ Database seeded successfully (packages + admin ready)")