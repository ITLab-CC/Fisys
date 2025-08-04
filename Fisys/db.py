from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base

# SQLite-Datei
DATABASE_URL = "sqlite:///filamente.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)

def init_db():
    Base.metadata.create_all(bind=engine)