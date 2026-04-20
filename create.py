from connections import engine, SessionLocal
from models import Base, Package

# create tables
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

db = SessionLocal()

packages = [
    {"name": "1 hour", "price": 20, "duration_hours": 1},
    {"name": "3 hours", "price": 25, "duration_hours": 3},
    {"name": "8 hours", "price": 40, "duration_hours": 8},
    {"name": "15 hours", "price": 100, "duration_hours": 15},
]

for pkg in packages:
    exists = db.query(Package).filter_by(name=pkg["name"]).first()
    if not exists:
        db.add(Package(**pkg))

db.commit()
db.close()

print("Tables recreated with latest columns")