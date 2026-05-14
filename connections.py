from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://hotspot_1xun_user:I3uqYdRFlWePIJ7HdKlId9TvTKcv42Td@dpg-d82sc33bc2fs73bglrr0-a.oregon-postgres.render.com/hotspot_1xun"
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=10,
    max_overflow=20
)

Session = scoped_session(sessionmaker(bind=engine))
SessionLocal = Session