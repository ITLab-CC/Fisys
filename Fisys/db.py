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
            if "letzte_aktion" not in columns:
                conn.execute(text("ALTER TABLE filament_spule ADD COLUMN letzte_aktion VARCHAR"))
            user_columns = [col["name"] for col in inspector.get_columns("users")]
            timestamp_type = "TIMESTAMP" if is_sqlite else "TIMESTAMP WITH TIME ZONE"
            default_clause = "DEFAULT CURRENT_TIMESTAMP"
            if "created_at" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN created_at {timestamp_type} {default_clause}"))
            if "last_seen" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN last_seen {timestamp_type} {default_clause}"))
            updated_user_columns = {col["name"] for col in inspect(conn).get_columns("users")}
            if {"created_at", "last_seen"}.issubset(updated_user_columns):
                conn.execute(text("UPDATE users SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), last_seen = COALESCE(last_seen, CURRENT_TIMESTAMP)"))
    except Exception as exc:
        print(f"[DB] Konnte Datenbank-Anpassungen nicht durchführen: {exc}")


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
