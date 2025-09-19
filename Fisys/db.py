import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from models import Base
from sqlalchemy.orm import Session


load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///fallback.db")

is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if is_sqlite else {},
    echo=False
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)

def init_db():
    Base.metadata.create_all(bind=engine)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            columns = [col["name"] for col in inspector.get_columns("filament_spule")]
            if "printer_serial" not in columns:
                conn.execute(text("ALTER TABLE filament_spule ADD COLUMN printer_serial VARCHAR"))
    except Exception as exc:
        print(f"[DB] Konnte printer_serial nicht ergänzen: {exc}")


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
