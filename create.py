from connections import engine, SessionLocal
from models import Base, Package

# recreate tables
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

db = SessionLocal()

packages = [
    {"name": "1 hour", "price": 10, "duration_hours": 1},
    {"name": "2 hours", "price": 20, "duration_hours": 2},
    {"name": "3 hours", "price": 30, "duration_hours": 3},
    {"name": "5 hours", "price": 50, "duration_hours": 5},
    {"name": "8 hours", "price": 80, "duration_hours": 8},
    {"name": "15 hours", "price": 150, "duration_hours": 15},
]

for pkg in packages:
    exists = db.query(Package).filter_by(name=pkg["name"]).first()
    if not exists:
        db.add(Package(**pkg))

db.commit()
db.close()

print("Tables recreated with latest columns")