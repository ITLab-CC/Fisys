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
            if is_sqlite:
                if "discord_id" not in user_columns:
                    conn.execute(text("ALTER TABLE users ADD COLUMN discord_id VARCHAR"))
            else:
                conn.execute(text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'users' AND column_name = 'discord_id'
                        ) THEN
                            ALTER TABLE users ADD COLUMN discord_id VARCHAR;
                        END IF;
                    END
                    $$;
                    """
                ))
            if "created_at" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN created_at {timestamp_type} {default_clause}"))
            if "last_seen" not in user_columns:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN last_seen {timestamp_type} {default_clause}"))
            updated_user_columns = {col["name"] for col in inspect(conn).get_columns("users")}
            if {"created_at", "last_seen"}.issubset(updated_user_columns):
                conn.execute(text("UPDATE users SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), last_seen = COALESCE(last_seen, CURRENT_TIMESTAMP)"))

            tables = set(inspector.get_table_names())
            if "discord_bot_config" in tables:
                bot_columns = [col["name"] for col in inspector.get_columns("discord_bot_config")]
                if "use_dm" not in bot_columns:
                    if is_sqlite:
                        conn.execute(text("ALTER TABLE discord_bot_config ADD COLUMN use_dm BOOLEAN DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE discord_bot_config ADD COLUMN IF NOT EXISTS use_dm BOOLEAN DEFAULT FALSE"))
                if "failure_message_template" not in bot_columns:
                    if is_sqlite:
                        conn.execute(text("ALTER TABLE discord_bot_config ADD COLUMN failure_message_template TEXT"))
                    else:
                        conn.execute(text("ALTER TABLE discord_bot_config ADD COLUMN IF NOT EXISTS failure_message_template TEXT"))
                failure_default = "Hey {username}, dein Druckauftrag {job_name} auf {printer_name} ist fehlgeschlagen: {failure_reason}"
                conn.execute(
                    text("UPDATE discord_bot_config SET failure_message_template = :default WHERE failure_message_template IS NULL"),
                    {"default": failure_default}
                )
                result = conn.execute(text("SELECT COUNT(*) FROM discord_bot_config"))
                count = result.scalar() if result is not None else 0
                if not count:
                    default_template = "Hey {username}, dein Druckauftrag {job_name} auf {printer_name} ist fertig!"
                    conn.execute(
                        text("INSERT INTO discord_bot_config (id, enabled, use_dm, message_template, failure_message_template) VALUES (1, :enabled, :use_dm, :template, :failure_template)"),
                        {"enabled": False, "use_dm": False, "template": default_template, "failure_template": failure_default}
                    )
    except Exception as exc:
        print(f"[DB] Konnte Datenbank-Anpassungen nicht durchführen: {exc}")


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
