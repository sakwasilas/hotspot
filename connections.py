from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

#DATABASE_URL = "mysql+pymysql://root:2480@localhost/hotspot_2_kevin"
DATABASE_URL = "sqlite:///hotspot_2_kevin"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


Session = scoped_session(sessionmaker(bind=engine))
SessionLocal = Session