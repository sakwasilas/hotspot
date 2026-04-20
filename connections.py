from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

DATABASE_URL = "mysql+pymysql://root:2480@localhost/hotspot_db"

engine = create_engine(DATABASE_URL, echo=True)

Session = scoped_session(sessionmaker(bind=engine))
SessionLocal = Session