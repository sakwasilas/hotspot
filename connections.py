from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://hotspot_1xun_user:I3uqYdRFlWePIJ7HdKlId9TvTKcv42Td@dpg-d82sc33bc2fs73bglrr0-a.oregon-postgres.render.com/hotspot_1xun"
)

# Fix old postgres:// URLs if Render provides them
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=10,
    max_overflow=20,
    echo=False
)

SessionLocal = scoped_session(
    sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine
    )
)