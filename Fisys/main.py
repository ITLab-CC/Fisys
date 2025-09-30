from contextlib import asynccontextmanager
import os
import string
from datetime import datetime, timezone
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from db import SessionLocal, init_db
from models import (
    FilamentTyp,
    FilamentSpule,
    FilamentVerbrauch,
    FilamentSpuleHistorie,
    PrinterJobHistory,
    User,
    DashboardNote,
    DiscordNotificationSubscription,
    DiscordBotConfig,
)
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Response, BackgroundTasks, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session, joinedload
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from qrcode_utils import generate_qrcode_for_spule, delete_qrcode_for_spule
import shutil
import requests
from auth import router as auth_router, serializer, COOKIE_MAX_AGE
from printer_service import start_printer_service, stop_printer_service
from models import Printer, PrinterCreate, PrinterUpdate, PrinterRead

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Event Loop für Thread->Async Bridge merken
    import asyncio as _asyncio
    global APP_EVENT_LOOP
    APP_EVENT_LOOP = _asyncio.get_running_loop()
    
    # Aus der Datenbank konfigurierte Drucker laden und Services starten
    def _start_selected_printers():
        db = SessionLocal()
        try:
            for p in db.query(Printer).filter(Printer.show_on_dashboard == True).all():
                start_printer_service(
                    ip=p.ip,
                    serial=p.serial,
                    access_code=p.access_token,
                    on_push=push_to_dashboard,
                    interval_seconds=15,
                    name=p.name,
                )
        finally:
            db.close()

    _start_selected_printers()

    # --- Initialen Admin-Token generieren, falls keine Benutzer vorhanden ---
    from models import User, AuthToken
    import secrets

    def generate_initial_admin_token():
        db = SessionLocal()
        try:
            existing_users = db.query(User).count()
            if existing_users == 0:
                token_str = secrets.token_urlsafe(6)[:8].upper()
                token = AuthToken(token=token_str, rolle="admin", verwendet=False)
                db.add(token)
                db.commit()
                print(f"\n🌟 Initialer Admin-Token (einmalig nutzbar): {token_str}\n")
        finally:
            db.close()
    generate_initial_admin_token()
    try:
        yield
    finally:
        # Graceful shutdown: keine neuen Pushes, WS-Verbindungen schließen, Services stoppen
        global SHUTTING_DOWN
        SHUTTING_DOWN = True
        try:
            for ws in list(dashboard_connections):
                try:
                    await ws.close()
                except Exception:
                    pass
                try:
                    dashboard_connections.remove(ws)
                except ValueError:
                    pass
        except Exception:
            pass
        stop_printer_service()


app = FastAPI(lifespan=lifespan)

# Auth-Router einbinden
app.include_router(auth_router)

# Event Loop global für Thread->Async Bridge
APP_EVENT_LOOP = None  # wird im lifespan gesetzt
SHUTTING_DOWN = False  # verhindert neue Pushes beim Shutdown

# --- WebSocket Dashboard Support ---
from fastapi import WebSocket, WebSocketDisconnect
import asyncio

dashboard_connections: list[WebSocket] = []
LATEST_PRINTER_STATUSES: dict[str, dict] = {}
CURRENT_PRINTER_JOBS: dict[str, dict] = {}
PRINTER_NAME_CACHE: dict[str, Optional[str]] = {}

DEFAULT_DISCORD_MESSAGE_TEMPLATE = "Hey {username}, dein Druckauftrag {job_name} auf {printer_name} ist fertig!"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_printer_name(serial: str, session: Optional[Session] = None) -> Optional[str]:
    if not serial:
        return None
    cached = PRINTER_NAME_CACHE.get(serial)
    if cached is not None:
        return cached
    own_session = False
    if session is None:
        own_session = True
        session = SessionLocal()
    try:
        printer = session.query(Printer).filter(Printer.serial == serial).first()
        if printer:
            PRINTER_NAME_CACHE[serial] = printer.name
            return printer.name
    except Exception:
        pass
    finally:
        if own_session and session is not None:
            session.close()
    return None


def _finalize_printer_job(serial: str, entry: dict, status: str, finished_at: datetime) -> None:
    if not entry:
        return
    start_time = entry.get('start_time') or finished_at
    duration = finished_at - start_time
    duration_seconds = int(duration.total_seconds()) if duration else None
    if duration_seconds is not None and duration_seconds < 0:
        duration_seconds = 0
    job_name_value = entry.get('job_name')
    printer_name = None
    session = SessionLocal()
    try:
        printer_name = _resolve_printer_name(serial, session=session)
        job = PrinterJobHistory(
            printer_serial=serial,
            printer_name=printer_name,
            job_name=job_name_value,
            status=status,
            started_at=start_time,
            finished_at=finished_at,
            duration_seconds=duration_seconds
        )
        session.add(job)
        session.commit()
        job_name_value = job.job_name
    except Exception as exc:
        session.rollback()
        print(f"[PrinterJob] Persistenzfehler: {exc}")
    finally:
        session.close()

    _process_discord_notifications(serial, printer_name, job_name_value, status)


def _track_printer_job(payload: dict) -> None:
    serial = payload.get('serial') or payload.get('printer_serial')
    if not serial:
        return
    try:
        state_raw = payload.get('state') or ''
        state = str(state_raw).lower()
        job_name = (payload.get('job_name') or '').strip() or None
        percent_raw = payload.get('percent')
        try:
            percent_value = float(percent_raw) if percent_raw is not None else None
        except (TypeError, ValueError):
            percent_value = None
        now = _utcnow()
        entry = CURRENT_PRINTER_JOBS.get(serial)
        printing_tokens = ('print', 'run', 'busy', 'working')
        success_tokens = ('finish', 'done', 'complete', 'success')
        failure_tokens = ('fail', 'error', 'cancel', 'abort', 'stopp', 'stopped')
        is_printing = any(token in state for token in printing_tokens) or (percent_value is not None and 0 < percent_value < 100)

        if is_printing:
            if entry:
                if job_name and entry.get('job_name') != job_name:
                    _finalize_printer_job(serial, entry, 'abgebrochen', now)
                    CURRENT_PRINTER_JOBS.pop(serial, None)
                    entry = None
            if not entry:
                CURRENT_PRINTER_JOBS[serial] = {
                    'job_name': job_name or 'Unbekannter Job',
                    'start_time': now,
                    'last_state': state,
                    'last_update': now
                }
            else:
                entry['last_state'] = state
                entry['last_update'] = now
                if job_name and not entry.get('job_name'):
                    entry['job_name'] = job_name
            return

        entry = CURRENT_PRINTER_JOBS.get(serial)
        if not entry:
            return

        status = None
        if any(token in state for token in success_tokens) or (percent_value is not None and percent_value >= 100):
            status = 'erfolgreich'
        elif any(token in state for token in failure_tokens):
            status = 'fehlgeschlagen'
        elif state in ('idle', 'ready', 'standby') and entry.get('last_state') and any(token in entry['last_state'] for token in printing_tokens):
            status = 'erfolgreich'

        entry['last_state'] = state
        entry['last_update'] = now

        if status:
            _finalize_printer_job(serial, entry, status, now)
            CURRENT_PRINTER_JOBS.pop(serial, None)
    except Exception as exc:
        print(f"[PrinterJob] Tracking-Fehler: {exc}")


def push_to_dashboard(payload: dict):
    """Thread-sicher: aus Worker-Threads ins FastAPI-Event-Loop pushen."""
    try:
        if SHUTTING_DOWN:
            return
        global LATEST_PRINTER_STATUSES
        serial = payload.get("serial") or payload.get("printer_serial")
        if serial:
            _track_printer_job(payload)
            LATEST_PRINTER_STATUSES[serial] = payload
        else:
            _track_printer_job(payload)
        # Wenn wir im Event-Loop-Thread sind, direkt Task erstellen
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(notify_dashboard(payload))
                return
        except RuntimeError:
            # kein laufender Loop in diesem Thread -> unten fallback
            pass

        # Aus Fremd-Thread: run_coroutine_threadsafe auf den gemerkten Loop
        global APP_EVENT_LOOP
        if APP_EVENT_LOOP:
            _asyncio.run_coroutine_threadsafe(notify_dashboard(payload), APP_EVENT_LOOP)
        else:
            print("[PrinterService] Kein Event-Loop gesetzt – konnte nicht pushen")
    except Exception as e:
        print(f"[PrinterService] Dashboard push error: {e}")

@app.websocket("/ws/dashboard")
async def websocket_dashboard(ws: WebSocket):
    await ws.accept()
    print("[WS] Client connected")
    dashboard_connections.append(ws)
    try:
        while True:
            await asyncio.sleep(10)  # Verbindung offen halten
    except (WebSocketDisconnect, asyncio.CancelledError):
        print("[WS] Client disconnected")
        if ws in dashboard_connections:
            dashboard_connections.remove(ws)

async def notify_dashboard(data: dict):
    if not dashboard_connections:
        return
    for conn in list(dashboard_connections):
        try:
            await conn.send_json(data)
        except Exception:
            pass


# ---- Printer Verwaltung (Server-seitig) ----
def reload_dashboard_printers():
    """Stoppt alle laufenden Drucker-Services und startet sie entsprechend der DB neu."""
    stop_printer_service()
    db = SessionLocal()
    try:
        selected = db.query(Printer).filter(Printer.show_on_dashboard == True).all()
        selected_serials = {p.serial for p in selected}
        for p in selected:
            start_printer_service(
                ip=p.ip,
                serial=p.serial,
                access_code=p.access_token,
                on_push=push_to_dashboard,
                interval_seconds=15,
                name=p.name,
            )
        # Nicht mehr ausgewählte Stati verwerfen, damit Initial-Fetch sie nicht zurückbringt
        try:
            global LATEST_PRINTER_STATUSES
            for serial in list(LATEST_PRINTER_STATUSES.keys()):
                if serial not in selected_serials:
                    del LATEST_PRINTER_STATUSES[serial]
        except Exception:
            pass
        # An Dashboard broadcasten, welche Drucker ausgewählt sind
        try:
            selected_list = [{"serial": p.serial, "name": p.name} for p in selected]
            push_to_dashboard({"event": "printers_selected", "printers": selected_list})
        except Exception:
            pass
    finally:
        db.close()

# --- DEBUG: Manuell eine WS-Nachricht schicken ---
@app.get("/_debug/ws-ping")
async def debug_ws_ping():
    sample = {
        "serial": "TEST123",
        "state": "printing",
        "percent": 42,
        "eta_min": 13,
        "job_name": "Debug-Job"
    }
    await notify_dashboard(sample)
    return {"sent": sample}

@app.get("/_debug/ws-text")
async def debug_ws_text():
    await notify_dashboard({"event": "debug", "message": "Hello from server"})
    return {"ok": True}

# API endpoint: Get the latest printer status snapshots (all)
@app.get("/api/printer_status_all", response_class=JSONResponse)
def get_printer_status_all():
    # Liefert den letzten bekannten Status aller Drucker (Map serial -> payload)
    return LATEST_PRINTER_STATUSES

# (Kompatibilität) Einzelsnapshot (falls legacy-Frontend): nimm beliebigen
@app.get("/api/printer_status", response_class=JSONResponse)
def get_printer_status_single():
    if not LATEST_PRINTER_STATUSES:
        return {"state": "unknown"}
    # gebe den zuletzt aktualisierten (irgendeinen) zurück
    # Reihenfolge ist in dict nicht garantiert – Ziel: einfache Abwärtskompatibilität
    return next(iter(LATEST_PRINTER_STATUSES.values()))

# Statische Dateien (HTML, CSS, JS)
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "html"))
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=FileResponse)
def serve_index():
    return os.path.join(static_dir, "index.html")

# Filamentseite HTML-Endpoint
@app.get("/filamentseite", response_class=FileResponse)
def serve_filament_page():
    return os.path.join(static_dir, "filamentseite.html")

# Serve filamentseite.html directly at /filamentseite.html
@app.get("/filamentseite.html", response_class=FileResponse)
def serve_filamentseite_html():
    return os.path.join(static_dir, "filamentseite.html")

# Serve spulen.html directly at /spulen.html
@app.get("/spulen.html", response_class=FileResponse)
def serve_spulen_page():
    return os.path.join(static_dir, "spulen.html")

# Helper functions


# Authentication helpers

def get_current_user(request: Request, db: Session) -> User:
    raw_cookie = request.cookies.get("benutzer")
    if not raw_cookie:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    try:
        data = serializer.loads(raw_cookie, max_age=COOKIE_MAX_AGE)
    except Exception:
        raise HTTPException(status_code=401, detail="Session ungültig oder abgelaufen")
    username = data.get('username') if isinstance(data, dict) else None
    if not username:
        raise HTTPException(status_code=401, detail="Session ungültig")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="Benutzer nicht gefunden")
    user.last_seen = datetime.now(timezone.utc)
    db.commit()
    return user


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return ""


def _ensure_discord_config(db: Session) -> DiscordBotConfig:
    config = db.query(DiscordBotConfig).filter(DiscordBotConfig.id == 1).first()
    if not config:
        config = DiscordBotConfig(id=1, message_template=DEFAULT_DISCORD_MESSAGE_TEMPLATE)
        db.add(config)
        db.commit()
        db.refresh(config)
    if getattr(config, 'use_dm', None) is None:
        config.use_dm = False
        db.commit()
        db.refresh(config)
    return config


def _format_discord_message(template: str, context: dict) -> str:
    safe_context = {k: "" if v is None else str(v) for k, v in context.items()}
    try:
        return template.format_map(_SafeFormatDict(safe_context))
    except Exception:
        return template



def _dispatch_discord_message(config: DiscordBotConfig, message: str, user: Optional[User] = None) -> tuple[bool, Optional[str]]:
    if not config.enabled:
        return False, "Bot deaktiviert"

    if config.use_dm:
        if not config.bot_token:
            return False, "Bot-Token erforderlich, um Direktnachrichten zu senden"
        if not user or not user.discord_id or not str(user.discord_id).isdigit():
            return False, "Discord-ID fehlt oder ist ungültig"
        headers = {"Authorization": f"Bot {config.bot_token}", "Content-Type": "application/json"}
        try:
            dm_response = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers,
                json={"recipient_id": str(user.discord_id)},
                timeout=10,
            )
        except Exception as exc:
            return False, str(exc)
        if not (200 <= dm_response.status_code < 300):
            return False, f"DM-Channel-Fehler: HTTP {dm_response.status_code}: {dm_response.text[:200]}"
        channel_payload = dm_response.json() if dm_response.content else {}
        channel_id = channel_payload.get("id")
        if not channel_id:
            return False, "DM-Channel konnte nicht ermittelt werden"
        try:
            message_response = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": message},
                timeout=10,
            )
        except Exception as exc:
            return False, str(exc)
        if 200 <= message_response.status_code < 300:
            return True, None
        return False, f"HTTP {message_response.status_code}: {message_response.text[:200]}"

    if config.webhook_url:
        try:
            response = requests.post(
                config.webhook_url,
                json={"content": message},
                timeout=10,
            )
        except Exception as exc:
            return False, str(exc)
        if 200 <= response.status_code < 300:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    if config.channel_id and config.bot_token:
        url = f"https://discord.com/api/v10/channels/{config.channel_id}/messages"
        headers = {"Authorization": f"Bot {config.bot_token}", "Content-Type": "application/json"}
        try:
            response = requests.post(url, json={"content": message}, headers=headers, timeout=10)
        except Exception as exc:
            return False, str(exc)
        if 200 <= response.status_code < 300:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    return False, "Keine gültige Discord-Konfiguration gefunden"



def _process_discord_notifications(serial: str, printer_name: Optional[str], job_name: Optional[str], status: str) -> None:
    normalized = (status or '').lower()
    is_success = normalized == 'erfolgreich'
    is_failed = normalized in {'fehlgeschlagen', 'abgebrochen'}
    if not is_success and not is_failed:
        return

    session = SessionLocal()
    try:
        pending = (
            session.query(DiscordNotificationSubscription)
            .options(joinedload(DiscordNotificationSubscription.user))
            .filter(
                DiscordNotificationSubscription.printer_serial == serial,
                DiscordNotificationSubscription.status == 'pending'
            )
            .all()
        )
        if not pending:
            return

        config = _ensure_discord_config(session)
        if not config.enabled:
            for sub in pending:
                sub.status = 'skipped'
                sub.last_error = 'Discord-Bot deaktiviert'
            session.commit()
            return

        if is_failed:
            reason = 'Druck abgebrochen.' if normalized == 'abgebrochen' else 'Druck fehlgeschlagen.'
            for sub in pending:
                sub.status = 'failed'
                sub.last_error = reason
            session.commit()
            return

        template = config.message_template or DEFAULT_DISCORD_MESSAGE_TEMPLATE
        now = _utcnow()
        for sub in pending:
            user = sub.user
            if not user or not user.discord_id:
                sub.status = 'failed'
                sub.last_error = 'Discord-ID fehlt'
                continue

            mention = f"<@{user.discord_id}> " if user.discord_id and user.discord_id.isdigit() else ""
            context = {
                'username': user.username,
                'printer_name': printer_name or serial,
                'printer_serial': serial,
                'job_name': job_name or 'Unbekannter Job',
                'status': status,
                'discord_id': user.discord_id,
                'discord_mention': mention,
            }
            message = _format_discord_message(template, context)
            success, error = _dispatch_discord_message(config, message, user=user)
            if success:
                sub.status = 'sent'
                sub.notified_at = now
                sub.last_error = None
            else:
                sub.status = 'failed'
                sub.last_error = error or 'Unbekannter Fehler'
        session.commit()
    finally:
        session.close()


def log_spool_history(db: Session, spule: FilamentSpule, aktion: str, alt: Optional[float] = None, neu: Optional[float] = None) -> None:
    """Persist a history entry for dashboard timeline tracking."""
    try:
        typ = spule.typ
    except Exception:
        typ = None
    entry = FilamentSpuleHistorie(
        spulen_id=spule.spulen_id,
        typ_name=getattr(typ, "name", None),
        material=getattr(typ, "material", None),
        farbe=getattr(typ, "farbe", None),
        durchmesser=getattr(typ, "durchmesser", None),
        aktion=aktion,
        alt_gewicht=alt,
        neu_gewicht=neu if neu is not None else spule.restmenge,
        verpackt=spule.verpackt,
        in_printer=spule.in_printer,
    )
    db.add(entry)

def get_or_create_filament_typ(session, name, material, farbe, durchmesser, hersteller=None, leergewicht: int = 0):
    typ = session.query(FilamentTyp).filter_by(
        name=name,
        material=material,
        farbe=farbe,
        durchmesser=durchmesser
    ).first()

    if typ:
        print("🎯 Bestehender Typ gefunden – wird verwendet.")
        return typ

    print("➕ Neuer Typ wird erstellt.")
    typ = FilamentTyp(
        name=name,
        material=material,
        farbe=farbe,
        durchmesser=durchmesser,
        hersteller=hersteller,
        bildname="platzhalter.jpg",
        leergewicht=leergewicht
    )
    session.add(typ)
    session.commit()
    return typ

def add_filament_spule(session, typ_name, material, farbe, durchmesser, hersteller, gesamtmenge, restmenge):
    # Get or create the filament type
    typ = get_or_create_filament_typ(session, typ_name, material, farbe, durchmesser, hersteller)
    # Create and add a new spool
    spule = FilamentSpule(typ=typ, gesamtmenge=gesamtmenge, restmenge=restmenge)
    session.add(spule)
    session.commit()
    print(f"✅ Neue Spule hinzugefügt: ID {spule.spulen_id}, {restmenge}/{gesamtmenge}g")

def delete_filament_spule(session, spule_id):
    spule = session.query(FilamentSpule).filter_by(spulen_id=spule_id).first()
    if not spule:
        print(f"❌ Keine Spule mit ID {spule_id} gefunden.")
        return
    session.delete(spule)
    session.commit()
    print(f"🗑️ Spule ID {spule_id} wurde gelöscht.")

def list_inventory(session):
    print("\n📦 Aktuelles Lager:")
    for typ in session.query(FilamentTyp).all():
        print(f"{typ.name} – {len(typ.spulen)} Spulen:")
        for spule in typ.spulen:
            print(f"  ID {spule.spulen_id}: {spule.get_prozent_voll():.1f}% voll ({spule.restmenge}/{spule.gesamtmenge}g)")
    print()

# Pydantic Schemas
class FilamentTypBase(BaseModel):
    name: str
    material: str
    farbe: str
    durchmesser: float
    leergewicht: int
    hersteller: Optional[str] = None
    bildname: Optional[str] = None

class FilamentTypCreate(FilamentTypBase):
    pass

class FilamentTypRead(FilamentTypBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class FilamentSpuleRead(BaseModel):
    spulen_id: int
    typ_id: int
    gesamtmenge: float
    restmenge: float
    in_printer: bool
    verpackt: bool
    alt_gewicht: float  # neu hinzugefügt
    printer_serial: Optional[str] = None
    letzte_aktion: Optional[str] = None
    typ: Optional[FilamentTypRead] = None
    model_config = ConfigDict(from_attributes=True)

class FilamentTypWithSpulen(FilamentTypRead):
    spulen: List[FilamentSpuleRead] = []

# Neue Spule bekommt alle Typdaten direkt, nicht typ_id!
class FilamentSpuleCreate(BaseModel):
    name: str
    material: str
    farbe: str
    durchmesser: float
    leergewicht: int
    hersteller: Optional[str] = None
    gesamtmenge: Optional[float] = None
    restmenge: Optional[float] = None
    in_printer: Optional[bool] = False
    verpackt: Optional[bool] = None
    printer_serial: Optional[str] = None

# PATCH-Modell für Spule
class SpuleUpdate(BaseModel):
    restmenge: float
    in_printer: Optional[bool] = None
    gesamtmenge: Optional[float] = None
    printer_serial: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DashboardNoteCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)
    message: str = Field(..., min_length=1, max_length=2000)


class VerbrauchUpdate(BaseModel):
    verbrauch_in_g: float


class UserSettingsUpdate(BaseModel):
    discord_id: Optional[str] = Field(default=None, max_length=64)


class DiscordBotConfigPayload(BaseModel):
    enabled: Optional[bool] = None
    use_dm: Optional[bool] = None
    webhook_url: Optional[str] = Field(default=None, max_length=400)
    bot_token: Optional[str] = Field(default=None, max_length=400)
    channel_id: Optional[str] = Field(default=None, max_length=120)
    message_template: Optional[str] = Field(default=None, max_length=2000)

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



def require_roles(request: Request, db: Session, allowed_roles: set[str]) -> User:
    """Stellt sicher, dass der aktuelle Nutzer eine erlaubte Rolle besitzt."""
    raw_cookie = request.cookies.get("benutzer")
    username = None
    if raw_cookie:
        try:
            data = serializer.loads(raw_cookie, max_age=COOKIE_MAX_AGE)
            username = data.get("username")
        except Exception:
            username = None
    if not username:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == username).first()
    if not user or user.rolle not in allowed_roles:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    return user

def _normalize_printer_serial(db: Session, serial: Optional[str]) -> Optional[str]:
    """Return a valid printer serial (or None) ensuring it exists in the DB."""
    if not serial:
        return None
    printer = db.query(Printer).filter(Printer.serial == serial).first()
    if not printer:
        raise HTTPException(status_code=404, detail=f"Drucker mit Seriennummer '{serial}' nicht gefunden")
    return printer.serial

# API Endpoints

@app.patch("/api/me/settings")
def update_user_settings(payload: UserSettingsUpdate, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    raw = (payload.discord_id or "").strip()
    cleaned = None
    if raw:
        if not raw.isdigit():
            raise HTTPException(status_code=400, detail='Discord-ID darf nur Zahlen enthalten.')
        if len(raw) > 64:
            raise HTTPException(status_code=400, detail='Discord-ID ist zu lang.')
        cleaned = raw
    user.discord_id = cleaned
    db.commit()
    return {"username": user.username, "discord_id": user.discord_id}


@app.get("/api/printers/notifications")
def list_printer_notifications(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    subs = (
        db.query(DiscordNotificationSubscription)
        .filter(
            DiscordNotificationSubscription.user_id == user.id,
            DiscordNotificationSubscription.status == 'pending'
        )
        .all()
    )
    return {
        "items": [
            {
                "id": sub.id,
                "printer_serial": sub.printer_serial,
                "job_name": sub.job_name,
                "created_at": sub.created_at,
            }
            for sub in subs
        ]
    }


@app.post("/api/printers/{serial}/notify", status_code=201)
def register_printer_notification(serial: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user.discord_id:
        raise HTTPException(status_code=400, detail="Bitte hinterlege zuerst deine Discord-ID in den Einstellungen.")

    normalized_serial = _normalize_printer_serial(db, serial.strip())
    config = _ensure_discord_config(db)
    if not config.enabled:
        raise HTTPException(status_code=503, detail="Discord-Bot ist derzeit deaktiviert. Bitte wende dich an einen Admin.")
    if config.use_dm and not config.bot_token:
        raise HTTPException(status_code=503, detail="Für Direktnachrichten muss ein Bot-Token hinterlegt werden.")

    existing = (
        db.query(DiscordNotificationSubscription)
        .filter(
            DiscordNotificationSubscription.user_id == user.id,
            DiscordNotificationSubscription.printer_serial == normalized_serial,
            DiscordNotificationSubscription.status == 'pending'
        )
        .first()
    )

    snapshot = LATEST_PRINTER_STATUSES.get(normalized_serial) or {}
    job_name = (snapshot.get('job_name') or '').strip() or (existing.job_name if existing else None)
    if existing:
        existing.job_name = job_name
        existing.created_at = _utcnow()
        existing.last_error = None
        db.commit()
        return {"detail": "Benachrichtigung aktualisiert", "status": existing.status, "printer_serial": normalized_serial}

    subscription = DiscordNotificationSubscription(
        user_id=user.id,
        printer_serial=normalized_serial,
        job_name=job_name,
        status='pending'
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return {"detail": "Benachrichtigung gespeichert", "printer_serial": subscription.printer_serial, "status": subscription.status}


@app.get("/api/discord/config")
def get_discord_config(request: Request, db: Session = Depends(get_db)):
    require_roles(request, db, {"admin"})
    config = _ensure_discord_config(db)
    return {
        "enabled": config.enabled,
        "use_dm": config.use_dm,
        "webhook_url": config.webhook_url,
        "bot_token": config.bot_token,
        "channel_id": config.channel_id,
        "message_template": config.message_template,
        "updated_at": config.updated_at,
    }


@app.patch("/api/discord/config")
def update_discord_config(payload: DiscordBotConfigPayload, request: Request, db: Session = Depends(get_db)):
    require_roles(request, db, {"admin"})
    config = _ensure_discord_config(db)
    data = payload.model_dump(exclude_unset=True)
    if "enabled" in data:
        config.enabled = bool(data["enabled"])
    if "use_dm" in data:
        config.use_dm = bool(data["use_dm"])
    if "webhook_url" in data:
        value = (data["webhook_url"] or "").strip() or None
        config.webhook_url = value
    if "bot_token" in data:
        value = (data["bot_token"] or "").strip() or None
        config.bot_token = value
    if "channel_id" in data:
        value = (data["channel_id"] or "").strip() or None
        config.channel_id = value
    if "message_template" in data:
        template = (data["message_template"] or "").strip()
        config.message_template = template or DEFAULT_DISCORD_MESSAGE_TEMPLATE
    db.commit()
    db.refresh(config)
    return {
        "enabled": config.enabled,
        "use_dm": config.use_dm,
        "webhook_url": config.webhook_url,
        "bot_token": config.bot_token,
        "channel_id": config.channel_id,
        "message_template": config.message_template,
        "updated_at": config.updated_at,
    }


@app.get("/api/discord/subscriptions")
def list_discord_subscriptions(request: Request, status: Optional[str] = None, db: Session = Depends(get_db)):
    require_roles(request, db, {"mod", "admin"})
    query = (
        db.query(DiscordNotificationSubscription)
        .options(joinedload(DiscordNotificationSubscription.user))
        .order_by(DiscordNotificationSubscription.created_at.desc())
    )
    if status:
        query = query.filter(DiscordNotificationSubscription.status == status)
    items = []
    for sub in query.all():
        user = sub.user
        items.append({
            "id": sub.id,
            "printer_serial": sub.printer_serial,
            "job_name": sub.job_name,
            "status": sub.status,
            "created_at": sub.created_at,
            "notified_at": sub.notified_at,
            "last_error": sub.last_error,
            "user": {
                "username": user.username if user else None,
                "discord_id": user.discord_id if user else None,
            },
        })
    return {"items": items}


@app.delete("/api/discord/subscriptions/{subscription_id}", status_code=204)
def delete_discord_subscription(subscription_id: int, request: Request, db: Session = Depends(get_db)):
    require_roles(request, db, {"mod", "admin"})
    sub = db.get(DiscordNotificationSubscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Abonnement nicht gefunden")
    db.delete(sub)
    db.commit()
    return Response(status_code=204)




@app.get("/typs/", response_model=List[FilamentTypWithSpulen])
def read_typs(db: Session = Depends(get_db)):
    return db.query(FilamentTyp).all()

# --- POST-Endpunkt zum Erstellen eines neuen Typs ---
@app.post("/typs/")
def create_typ(typ: FilamentTypBase, db: Session = Depends(get_db)):
    neuer_typ = FilamentTyp(
        name=typ.name,
        material=typ.material,
        farbe=typ.farbe,
        durchmesser=typ.durchmesser,
        hersteller=typ.hersteller,
        hinweise=getattr(typ, 'hinweise', None),
        bildname=getattr(typ, 'bildname', 'platzhalter.jpg'),
        leergewicht=typ.leergewicht
    )
    db.add(neuer_typ)
    db.commit()
    db.refresh(neuer_typ)
    return neuer_typ

@app.get("/typs/{typ_id}", response_model=FilamentTypWithSpulen)
def read_typ(typ_id: int, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ not found")
    return typ


@app.put("/typs/{typ_id}", response_model=FilamentTypRead)
def update_typ(typ_id: int, typ_update: FilamentTypCreate, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ not found")
    for field, value in typ_update.dict().items():
        setattr(typ, field, value)
    db.commit()
    db.refresh(typ)
    return typ

# PATCH-Endpoint für Typ
@app.patch("/typs/{typ_id}", response_model=FilamentTypRead)
def patch_typ(typ_id: int, update: FilamentTypCreate, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ nicht gefunden")
    for field, value in update.dict().items():
        if field != "bildname":
            setattr(typ, field, value)
    db.commit()
    db.refresh(typ)
    return typ

@app.delete("/typs/{typ_id}")
def delete_typ(typ_id: int, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ not found")
    db.delete(typ)
    db.commit()
    return {"detail": f"Typ {typ_id} wurde gelöscht"}


@app.post("/spulen/", response_model=FilamentSpuleRead)
async def create_spule(spule: FilamentSpuleCreate, db: Session = Depends(get_db)):
    typ = get_or_create_filament_typ(
        db,
        name=spule.name,
        material=spule.material,
        farbe=spule.farbe,
        durchmesser=spule.durchmesser,
        hersteller=spule.hersteller,
        leergewicht=spule.leergewicht
    )

    if spule.gesamtmenge is None or spule.restmenge is None:
        # Nur Typ wurde erstellt/zurückgegeben
        return JSONResponse({"detail": f"Nur Typ '{typ.name}' wurde erstellt oder verwendet"}, status_code=201)

    is_verpackt = getattr(spule, "verpackt", None)
    assigned_serial: Optional[str] = None
    if spule.in_printer:
        if not spule.printer_serial:
            raise HTTPException(status_code=400, detail="Bitte Seriennummer des Druckers angeben, wenn die Spule im Drucker ist.")
        assigned_serial = _normalize_printer_serial(db, spule.printer_serial)
    else:
        assigned_serial = None

    if not is_verpackt:
        match = db.query(FilamentSpule).join(FilamentTyp).filter(
            FilamentSpule.verpackt == True,
            FilamentTyp.name == spule.name,
            FilamentTyp.material == spule.material,
            FilamentTyp.farbe == spule.farbe,
            FilamentTyp.durchmesser == spule.durchmesser
        ).first()
        if match:
            previous_rest = match.restmenge
            match.verpackt = False
            match.gesamtmenge = spule.gesamtmenge
            match.restmenge = spule.restmenge
            match.alt_gewicht = previous_rest
            match.in_printer = spule.in_printer if spule.in_printer is not None else False
            match.printer_serial = assigned_serial if match.in_printer else None
            match.letzte_aktion = "spule_entpackt"
            log_spool_history(db, match, "spule_entpackt", alt=previous_rest, neu=match.restmenge)
            db.commit()
            db.refresh(match)
            generate_qrcode_for_spule(match)
            await notify_dashboard({
                "event": "spule_updated",
                "spule_id": match.spulen_id,
                "restmenge": match.restmenge,
                "gesamtmenge": match.gesamtmenge
            })
            return FilamentSpuleRead.model_validate(match)

    # Prüfung: Maximal 4 Spulen im Drucker
    if spule.in_printer:
        in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
        if in_printer_count >= 4:
            raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    letzte_aktion = "verpackte_spule_hinzugefügt" if spule.verpackt else "spule_hinzugefügt"
    new_spule = FilamentSpule(
        typ=typ,
        gesamtmenge=spule.gesamtmenge,
        restmenge=spule.restmenge,
        in_printer=spule.in_printer if spule.in_printer is not None else False,
        verpackt=spule.verpackt or False,
        printer_serial=assigned_serial if spule.in_printer else None,
        letzte_aktion=letzte_aktion
    )
    db.add(new_spule)
    db.flush()
    log_spool_history(db, new_spule, letzte_aktion, alt=None, neu=new_spule.restmenge)
    db.commit()
    db.refresh(new_spule)
    generate_qrcode_for_spule(new_spule)
    # WebSocket-Dashboard-Benachrichtigung
    await notify_dashboard({"event": "spule_created", "spule_id": new_spule.spulen_id})
    return FilamentSpuleRead.model_validate(new_spule)

@app.get("/spulen/", response_model=List[FilamentSpuleRead])
def read_spulen(db: Session = Depends(get_db)):
    return db.query(FilamentSpule).all()


# Neuer Endpoint: Alle Spulen mit ihren Typ-Informationen

@app.get("/spulen_mit_typen/", response_model=List[FilamentSpuleRead])
def read_spulen_mit_typen(db: Session = Depends(get_db)):
    spulen = db.query(FilamentSpule).options(joinedload(FilamentSpule.typ)).all()
    return spulen

@app.get("/spulen/{spulen_id}")
def read_spule(spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule not found")
    # Rückgabeobjekt ohne das Feld "lagerplatz"
    return {
        "spulen_id": spule.spulen_id,
        "typ_id": spule.typ_id,
        "gesamtmenge": spule.gesamtmenge,
        "restmenge": spule.restmenge,
        "in_printer": spule.in_printer,
        "printer_serial": spule.printer_serial
    }



@app.put("/spulen/{spulen_id}", response_model=FilamentSpuleRead)
async def update_spule(spulen_id: int, spule_update: FilamentSpuleCreate, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule not found")

    old_in_printer = spule.in_printer
    old_verpackt = spule.verpackt
    history_events: List[Tuple[str, Optional[float], Optional[float]]] = []

    if spule_update.gesamtmenge is not None:
        previous_total = spule.gesamtmenge
        spule.gesamtmenge = spule_update.gesamtmenge
        if spule_update.gesamtmenge != previous_total:
            history_events.append(("gesamtmenge_geaendert", previous_total, spule.gesamtmenge))

    if spule_update.restmenge is not None and spule_update.restmenge != spule.restmenge:
        previous_rest = spule.restmenge
        new_rest = spule_update.restmenge
        print(f"Update Spule ID {spule.spulen_id}: alt_gewicht={spule.alt_gewicht}, restmenge={spule.restmenge}, neuer Wert={new_rest}")
        verbrauch = spule.restmenge - new_rest
        spule.alt_gewicht = spule.restmenge
        spule.restmenge = new_rest
        history_events.append(("gewicht_geaendert", previous_rest, new_rest))
        if verbrauch > 0:
            eintrag = FilamentVerbrauch(
                typ_id=spule.typ_id,
                verbrauch_in_g=verbrauch
            )
            db.add(eintrag)

    target_in_printer = spule.in_printer if spule_update.in_printer is None else spule_update.in_printer
    if spule_update.in_printer is not None:
        # Prüfung: Maximal 4 Spulen im Drucker
        if spule_update.in_printer and not old_in_printer:
            in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
            if in_printer_count >= 4:
                raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    new_serial = spule.printer_serial
    if spule_update.printer_serial is not None:
        new_serial = _normalize_printer_serial(db, spule_update.printer_serial)

    if target_in_printer and not new_serial:
        raise HTTPException(status_code=400, detail="Bitte einen Drucker auswählen, solange die Spule im Drucker markiert ist.")

    spule.in_printer = target_in_printer
    spule.printer_serial = new_serial if target_in_printer else None

    if spule_update.in_printer is not None and spule_update.in_printer != old_in_printer:
        history_events.append(("in_drucker_gesetzt" if target_in_printer else "aus_drucker_entfernt", None, None))

    if spule_update.verpackt is not None:
        if spule_update.verpackt != old_verpackt:
            spule.verpackt = spule_update.verpackt
            history_events.append(("spule_verpackt" if spule.verpackt else "spule_entpackt", None, None))
        else:
            spule.verpackt = spule_update.verpackt

    if history_events:
        spule.letzte_aktion = history_events[-1][0]
        for action, alt, neu in history_events:
            log_spool_history(db, spule, action, alt=alt, neu=neu)

    db.commit()
    db.refresh(spule)
    # WebSocket-Dashboard-Benachrichtigung
    await notify_dashboard({
        "event": "spule_updated",
        "spule_id": spule.spulen_id,
        "restmenge": spule.restmenge,
        "gesamtmenge": spule.gesamtmenge
    })
    return spule


@app.patch("/spulen/{spulen_id}", response_model=FilamentSpuleRead)
async def patch_spule(spulen_id: int, update: SpuleUpdate, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")

    old_in_printer = spule.in_printer
    history_events: List[Tuple[str, Optional[float], Optional[float]]] = []

    if update.restmenge is not None:
        previous_rest = spule.restmenge
        new_rest = update.restmenge
        verbrauch = spule.restmenge - new_rest
        if new_rest != spule.restmenge:
            spule.alt_gewicht = spule.restmenge
            spule.restmenge = new_rest
            history_events.append(("gewicht_geaendert", previous_rest, new_rest))
        else:
            spule.restmenge = new_rest

        if verbrauch > 0:
            eintrag = FilamentVerbrauch(typ_id=spule.typ_id, verbrauch_in_g=verbrauch)
            db.add(eintrag)

    # in_printer prüfen/setzen (max. 4)
    target_in_printer = spule.in_printer if update.in_printer is None else update.in_printer
    if update.in_printer is not None and update.in_printer and not spule.in_printer:
        in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
        if in_printer_count >= 4:
            raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    new_serial = spule.printer_serial
    if update.printer_serial is not None:
        new_serial = _normalize_printer_serial(db, update.printer_serial)

    if target_in_printer and not new_serial:
        raise HTTPException(status_code=400, detail="Bitte einen Drucker auswählen, solange die Spule im Drucker markiert ist.")

    spule.in_printer = target_in_printer
    spule.printer_serial = new_serial if target_in_printer else None

    if update.in_printer is not None and update.in_printer != old_in_printer:
        history_events.append(("in_drucker_gesetzt" if target_in_printer else "aus_drucker_entfernt", None, None))

    # gesamtmenge optional setzen
    if update.gesamtmenge is not None:
        previous_total = spule.gesamtmenge
        spule.gesamtmenge = update.gesamtmenge
        if update.gesamtmenge != previous_total:
            history_events.append(("gesamtmenge_geaendert", previous_total, spule.gesamtmenge))

    if history_events:
        spule.letzte_aktion = history_events[-1][0]
        for action, alt, neu in history_events:
            log_spool_history(db, spule, action, alt=alt, neu=neu)

    db.commit()
    db.refresh(spule)

    # Dashboard sicher benachrichtigen
    await notify_dashboard({
        "event": "spule_updated",
        "spule_id": spule.spulen_id,
        "restmenge": spule.restmenge,
        "gesamtmenge": spule.gesamtmenge
    })

    return spule

@app.delete("/spulen/{spulen_id}")
def delete_spule_api(
    spulen_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule not found")

    # Nur den Rest als Verbrauch eintragen, wenn vorhanden
    if spule.restmenge > 0:
        verbrauch_eintrag = FilamentVerbrauch(
            typ_id=spule.typ_id,
            verbrauch_in_g=spule.restmenge
        )
        db.add(verbrauch_eintrag)

    # QR-Code löschen und Spule entfernen
    delete_qrcode_for_spule(spule)
    db.delete(spule)
    db.commit()

    # WebSocket-Dashboard-Benachrichtigung (ohne Starlette BackgroundTasks, um Shutdown zu erleichtern)
    try:
        import asyncio as _asyncio
        if APP_EVENT_LOOP:
            _asyncio.run_coroutine_threadsafe(
                notify_dashboard({"event": "spule_deleted", "spule_id": spulen_id}),
                APP_EVENT_LOOP
            )
    except Exception:
        pass
    return {"detail": f"Spule {spulen_id} wurde gelöscht und Verbrauch von {spule.restmenge}g geloggt"}


# --- Admin: Verbrauchsverwaltung ---

def _serialize_verbrauch_entry(entry: FilamentVerbrauch, typ: Optional[FilamentTyp] | None = None) -> dict:
    typ = typ or entry.typ
    return {
        "id": entry.id,
        "typ_id": entry.typ_id,
        "typ_name": getattr(typ, "name", None),
        "material": getattr(typ, "material", None),
        "farbe": getattr(typ, "farbe", None),
        "durchmesser": getattr(typ, "durchmesser", None),
        "verbrauch_in_g": entry.verbrauch_in_g,
        "datum": entry.datum.isoformat() if entry.datum else None,
    }


@app.get("/admin/verbrauch")
def list_filament_consumption(request: Request, limit: int = 200, db: Session = Depends(get_db)):
    require_roles(request, db, {"mod", "admin"})
    limit = max(1, min(limit, 500))
    rows = (
        db.query(FilamentVerbrauch, FilamentTyp)
        .join(FilamentTyp, FilamentTyp.id == FilamentVerbrauch.typ_id)
        .order_by(FilamentVerbrauch.datum.desc())
        .limit(limit)
        .all()
    )
    entries = [_serialize_verbrauch_entry(entry, typ) for entry, typ in rows]
    total_sum = db.query(func.sum(FilamentVerbrauch.verbrauch_in_g)).scalar() or 0
    total_count = db.query(func.count(FilamentVerbrauch.id)).scalar() or 0
    return {
        "entries": entries,
        "total_sum": total_sum,
        "total_count": total_count,
        "limit": limit,
        "has_more": total_count > len(entries),
    }


@app.patch("/admin/verbrauch/{entry_id}")
def update_filament_consumption(entry_id: int, payload: VerbrauchUpdate, request: Request, db: Session = Depends(get_db)):
    require_roles(request, db, {"mod", "admin"})
    if payload.verbrauch_in_g < 0:
        raise HTTPException(status_code=400, detail="Verbrauch darf nicht negativ sein")

    eintrag = db.get(FilamentVerbrauch, entry_id)
    if not eintrag:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")

    eintrag.verbrauch_in_g = payload.verbrauch_in_g
    db.commit()
    db.refresh(eintrag)
    typ = db.query(FilamentTyp).filter(FilamentTyp.id == eintrag.typ_id).first()
    return {
        "detail": "Verbrauch aktualisiert",
        "entry": _serialize_verbrauch_entry(eintrag, typ),
    }


@app.delete("/admin/verbrauch/{entry_id}")
def delete_filament_consumption(entry_id: int, request: Request, db: Session = Depends(get_db)):
    require_roles(request, db, {"mod", "admin"})
    eintrag = db.get(FilamentVerbrauch, entry_id)
    if not eintrag:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    db.delete(eintrag)
    db.commit()
    return {
        "detail": "Verbrauchseintrag gelöscht"
    }


# Bild-Upload Endpoint
from fastapi import Form

@app.post("/upload-image/")
def upload_image(file: UploadFile = File(...), name: str = Form("")):
    import re
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Wenn kein Name angegeben ist, nimm den Dateinamen ohne Erweiterung
    if not name:
        name = os.path.splitext(file.filename)[0]

    # Bereinige den Namen
    name = re.sub(r"[^\w\-]", "_", name.strip())

    _, ext = os.path.splitext(file.filename)
    if not ext:
        raise HTTPException(status_code=400, detail="Invalid file extension")

    upload_dir = os.path.join(static_dir, "assets", "images")
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = f"{name}{ext.lower()}"
    file_path = os.path.join(upload_dir, safe_name)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Speichern: {str(e)}")

    return {"filename": safe_name}


# Bild zuweisen zu einem Typ nach ID
@app.patch("/typs/{typ_id}/bild", status_code=204)
async def assign_image_to_typ(typ_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    bild_input = form.get("bildname") or request.query_params.get("bildname")

    if bild_input is None:
        raise HTTPException(status_code=400, detail="Kein Bildname angegeben")

    if isinstance(bild_input, UploadFile):
        bildname = bild_input.filename
    else:
        bildname = str(bild_input)

    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail=f"Typ-ID {typ_id} nicht gefunden")

    # Andere Typen, die das Bild nutzen → zurücksetzen
    andere_typs = db.query(FilamentTyp).filter(
        FilamentTyp.bildname == bildname,
        FilamentTyp.id != typ_id
    ).all()
    for anderer in andere_typs:
        anderer.bildname = None

    typ.bildname = bildname
    try:
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Aktualisieren: {str(e)}")
    return Response(status_code=204)


@app.get("/bilder/")
def list_images(db: Session = Depends(get_db)):
    image_dir = os.path.join(static_dir, "assets", "images")
    try:
        all_files = [
            f for f in os.listdir(image_dir)
            if os.path.isfile(os.path.join(image_dir, f)) and f.lower().endswith((".png", ".jpg", ".jpeg"))
        ]
        result = []
        for file in all_files:
            typ = db.query(FilamentTyp).filter_by(bildname=file).first()
            if typ:
                result.append({
                    "bildname": file,
                    "zugewiesen_an": {
                        "id": typ.id,
                        "name": typ.name,
                        "material": typ.material,
                        "farbe": typ.farbe,
                        "durchmesser": typ.durchmesser
                    }
                })
            else:
                result.append({
                    "bildname": file,
                    "zugewiesen_an": None
                })
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Lesen der Bilder: {str(e)}")

# Bild von Typ entfernen, ohne es zu löschen
@app.patch("/bilder/{bildname}/entferne-typ")
def remove_image_from_typ(bildname: str, db: Session = Depends(get_db)):
    typ = db.query(FilamentTyp).filter_by(bildname=bildname).first()
    if not typ:
        raise HTTPException(status_code=404, detail="Kein Typ mit diesem Bild gefunden")

    typ.bildname = None
    db.commit()
    return {"detail": f"Bildzuweisung für Typ '{typ.name}' entfernt"}

# Bild löschen Endpoint (mit Update aller Typen, die das Bild nutzen)
@app.delete("/bilder/{bildname}")
def delete_image(bildname: str, db: Session = Depends(get_db)):
    image_dir = os.path.join(static_dir, "assets", "images")
    file_path = os.path.join(image_dir, bildname)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    # Alle Typen finden, die dieses Bild verwenden
    typs = db.query(FilamentTyp).filter_by(bildname=bildname).all()

    try:
        os.remove(file_path)
        for typ in typs:
            typ.bildname = None  # Zurücksetzen auf Platzhalter
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Löschen: {str(e)}")

    return {"detail": f"Bild '{bildname}' wurde gelöscht und Typen aktualisiert"}

# Bild umbenennen Endpoint (alter und neuer Name als Query-Parameter)
@app.patch("/bilder/rename")
def rename_image(old: str, new: str, db: Session = Depends(get_db)):
    image_dir = os.path.join(static_dir, "assets", "images")
    old_path = os.path.join(image_dir, old)
    new_path = os.path.join(image_dir, new)
    
    # Prüfen, dass die alte Datei existiert
    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail=f"Bild '{old}' nicht gefunden")
    
    # Platzhalter darf nicht umbenannt werden
    if old.lower() == "platzhalter.jpg":
        raise HTTPException(status_code=400, detail="Platzhalter-Bild darf nicht umbenannt werden")
    
    # Umbenennen in Dateisystem
    try:
        os.rename(old_path, new_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Umbenennen: {str(e)}")
    
    # Datenbank-Einträge für FilamentTyp aktualisieren
    typs = db.query(FilamentTyp).filter_by(bildname=old).all()
    for typ in typs:
        typ.bildname = new
    db.commit()
    
    return {"detail": f"Bild '{old}' erfolgreich umbenannt zu '{new}'"}

# Vorschlags-Endpunkt für Namen
@app.get("/namen/")
def get_vorschlaege_namen(q: str = "", db: Session = Depends(get_db)):
    if not q:
        return []
    like_expr = f"%{q.lower()}%"
    namen = db.query(FilamentSpule).join(FilamentTyp).filter(
        FilamentTyp.name.ilike(like_expr)
    ).distinct(FilamentTyp.name).all()
    return list({s.typ.name for s in namen})

def main():
    init_db()
    session = SessionLocal()

    while True:
        print("\nWas möchten Sie tun?")
        print("1. Neue Spule hinzufügen")
        print("2. Spule löschen")
        print("3. Lagerbestand anzeigen")
        print("4. Beenden")
        choice = input("Ihre Auswahl (1-4): ")

        if choice == "1":
            name = input("Typ-Name: ")
            material = input("Material: ")
            farbe = input("Farbe: ")
            durchmesser = float(input("Durchmesser (z.B. 1.75): "))
            hersteller = input("Hersteller (optional): ") or None
            gesamt = float(input("Gesamtmenge in g: "))
            rest = float(input("Restmenge in g: "))
            add_filament_spule(session, name, material, farbe, durchmesser, hersteller, gesamt, rest)
        elif choice == "2":
            spule_id = int(input("ID der zu löschenden Spule: "))
            delete_filament_spule(session, spule_id)
        elif choice == "3":
            list_inventory(session)
        elif choice == "4":
            print("Programm beendet.")
            break
        else:
            print("Ungültige Auswahl, bitte erneut versuchen.")

    session.close()

import netifaces

def get_all_local_ips():
    ips = []
    for iface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface)
        inet_addrs = addrs.get(netifaces.AF_INET, [])
        for addr in inet_addrs:
            ip = addr.get('addr')
            if ip and not ip.startswith("127."):
                ips.append(ip)
    return ips
# API Endpunkt: Alle Spulen eines bestimmten Typs abrufen
@app.get("/typs/{typ_id}/spulen", response_model=List[FilamentSpuleRead])
def get_spulen_for_typ(typ_id: int, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ not found")
    return typ.spulen
from fastapi.responses import HTMLResponse

# Serve filamentseite.html for /typ/{typ_id}
@app.get("/typ/{typ_id}", response_class=HTMLResponse)
def serve_typ_detail_page(typ_id: int):
    path = os.path.join(static_dir, "filamentseite.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    return HTMLResponse(content=content)

# Neuer HTML-Endpoint für /typ/{typ_id}/id{spulen_id}
@app.get("/typ/{typ_id}/id{spulen_id}", response_class=HTMLResponse)
def serve_spulendetails(typ_id: int, spulen_id: int):
    html_path = os.path.join(static_dir, "spulenseite.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="Detailseite nicht gefunden")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content)

# Neue JSON-API-Route für FilamentTyp-Daten
@app.get("/api/typ/{typ_id}", response_model=FilamentTypWithSpulen)
def get_typ_json(typ_id: int, db: Session = Depends(get_db)):
    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail="Typ nicht gefunden")
    return typ



# Neuer API-Endpoint für Spulendetails als JSON
@app.get("/api/typ/{typ_id}/id{spulen_id}")
def get_spule_detail_json(typ_id: int, spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule or spule.typ_id != typ_id:
        raise HTTPException(status_code=404, detail="Spule oder Typ nicht gefunden")
    typ = spule.typ
    return {
        "id": spule.spulen_id,
        "typ": typ.name,
        "farbe": typ.farbe,
        "material": typ.material,
        "durchmesser": typ.durchmesser,
        "gewicht": spule.gesamtmenge,
        "restmenge": spule.restmenge,
        "bild": typ.bildname or "platzhalter.jpg"
    }


# Neuer API-Endpoint: Leere und fast leere Typen
@app.get("/fastleere_data", response_class=JSONResponse)
def get_fastleere_typen(db: Session = Depends(get_db)):
    result = []
    typen = db.query(FilamentTyp).all()
    for typ in typen:
        spulen = typ.spulen
        anzahl_spulen = len(spulen)
        gesamt_restmenge = sum(float(s.restmenge or 0) for s in spulen)
        if anzahl_spulen == 0:
            status = "leer"
        else:
            if gesamt_restmenge < 800:
                status = "fastleer"
            else:
                continue
        result.append({
            "id": typ.id,
            "name": typ.name,
            "material": typ.material,
            "farbe": typ.farbe,
            "durchmesser": typ.durchmesser,
            "hersteller": typ.hersteller,
            "bildname": typ.bildname,
            "anzahl_spulen": anzahl_spulen,
            "restmenge": gesamt_restmenge,
            "status": status
        })
    return result

# HTML-Seite: /status zeigt Lagerstatus (leer & fastleer)
@app.get("/status", response_class=FileResponse)
def serve_status_page():
    path = os.path.join(static_dir, "status.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    return path


# Serve einstellungen.html für /einstellungen
@app.get("/einstellungen", response_class=FileResponse)
def serve_settings_page():
    path = os.path.join(static_dir, "einstellungen.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")
    return path

# Serve einstellungen.html directly at /einstellungen.html
@app.get("/einstellungen.html", response_class=FileResponse)
def serve_settings_html():
    path = os.path.join(static_dir, "einstellungen.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return path


# ---- Printer CRUD API ----
@app.get("/api/printers", response_model=List[PrinterRead])
def list_printers(only_selected: bool = False, db: Session = Depends(get_db)):
    q = db.query(Printer)
    if only_selected:
        q = q.filter(Printer.show_on_dashboard == True)
    return q.all()


@app.post("/api/printers", response_model=PrinterRead)
def create_printer(pr: PrinterCreate, db: Session = Depends(get_db)):
    p = Printer(
        name=pr.name,
        ip=pr.ip,
        serial=pr.serial,
        access_token=pr.access_token,
        show_on_dashboard=pr.show_on_dashboard,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    # Neu geladen, falls Dashboard-Flag aktiv
    reload_dashboard_printers()
    return p


@app.patch("/api/printers/{printer_id}", response_model=PrinterRead)
def update_printer(printer_id: int, pr: PrinterUpdate, db: Session = Depends(get_db)):
    p = db.get(Printer, printer_id)
    if not p:
        raise HTTPException(status_code=404, detail="Printer not found")
    data = pr.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    reload_dashboard_printers()
    return p


@app.delete("/api/printers/{printer_id}")
def delete_printer(printer_id: int, db: Session = Depends(get_db)):
    p = db.get(Printer, printer_id)
    if not p:
        raise HTTPException(status_code=404, detail="Printer not found")
    db.delete(p)
    db.commit()
    reload_dashboard_printers()
    return {"detail": "deleted"}


# Neuer API-Endpunkt: Statistik für Typen und Spulen
@app.get("/stats", response_class=JSONResponse)
def get_typ_and_spulen_stats(db: Session = Depends(get_db)):
    typ_count = db.query(FilamentTyp).count()
    spulen_count = db.query(FilamentSpule).count()
    return {"typen": typ_count, "spulen": spulen_count}


# API-Endpoint: Dashboard-Daten
@app.get("/api/dashboard-details")
def get_dashboard_details(db: Session = Depends(get_db)):
    typ_count = db.query(FilamentTyp).count()
    spulen_count = db.query(FilamentSpule).count()

    fastleere_typen = 0
    typen = db.query(FilamentTyp).all()
    for typ in typen:
        spulen = typ.spulen
        if not spulen or sum(s.restmenge for s in spulen) < 800:
            fastleere_typen += 1

    from datetime import datetime, timedelta
    # Gesamtverbrauch der letzten 7 Tage aus Verbrauchslog
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    verbrauch_7tage_sum = (
        db.query(func.sum(FilamentVerbrauch.verbrauch_in_g))
        .filter(FilamentVerbrauch.datum >= seven_days_ago)
        .scalar()
    ) or 0

    # Zuletzt bearbeitete Spulen (nach Historie-Einträgen)
    letzte_events = (
        db.query(FilamentSpuleHistorie)
        .order_by(FilamentSpuleHistorie.created_at.desc())
        .limit(5)
        .all()
    )
    spulen_liste = [{
        "spulen_id": s.spulen_id,
        "typ_name": s.typ_name,
        "farbe": s.farbe,
        "material": s.material,
        "durchmesser": s.durchmesser,
        "alt_gewicht": s.alt_gewicht,
        "neu_gewicht": s.neu_gewicht,
        "created_at": s.created_at,
        "updated_at": s.created_at,
        "letzte_aktion": s.aktion,
        "verpackt": s.verpackt,
        "in_printer": s.in_printer,
    } for s in letzte_events]

    job_events = (
        db.query(PrinterJobHistory)
        .order_by(PrinterJobHistory.finished_at.desc(), PrinterJobHistory.created_at.desc())
        .limit(3)
        .all()
    )
    job_historie = [{
        "job_name": j.job_name,
        "status": j.status,
        "printer_serial": j.printer_serial,
        "printer_name": j.printer_name,
        "duration_seconds": j.duration_seconds,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "created_at": j.created_at,
    } for j in job_events]

    dashboard_notes = [
        {
            "id": note.id,
            "title": note.title,
            "message": note.message,
            "created_at": note.created_at,
            "author": note.author.username if note.author else None,
            "author_role": note.author.rolle if note.author else None,
        }
        for note in db.query(DashboardNote).order_by(DashboardNote.created_at.desc()).limit(3).all()
    ]
    dashboard_note_total = db.query(func.count(DashboardNote.id)).scalar() or 0

    # Neu hinzugefügte Spulen (nach Erstellzeit)
    neue_spulen = (
        db.query(FilamentSpule)
        .order_by(FilamentSpule.created_at.desc())
        .limit(3)
        .all()
    )
    neue_spulen_liste = [{
        "spulen_id": s.spulen_id,
        "typ_name": s.typ.name,
        "farbe": s.typ.farbe,
        "material": s.typ.material,
        "durchmesser": s.typ.durchmesser,
        "alt_gewicht": s.alt_gewicht,
        "neu_gewicht": s.restmenge,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "letzte_aktion": s.letzte_aktion,
        "verpackt": s.verpackt,
        "in_printer": s.in_printer,
    } for s in neue_spulen]

    # Ergänzung: Spulen, die aktuell im Drucker sind
    im_drucker_spulen = db.query(FilamentSpule).filter(FilamentSpule.in_printer == True).all()
    im_drucker_liste = [{
        "spulen_id": s.spulen_id,
        "typ_name": s.typ.name,
        "typ_id": s.typ.id,
        "farbe": s.typ.farbe,
        "material": s.typ.material,
        "durchmesser": s.typ.durchmesser,
        "alt_gewicht": s.alt_gewicht,
        "neu_gewicht": s.restmenge,
        "gesamtmenge": s.gesamtmenge,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "bildname": s.typ.bildname,
        "printer_serial": s.printer_serial
    } for s in im_drucker_spulen]

    # Auch im Drucker gruppiert nach Typ (wie /drucker_data)
    im_drucker = (
        db.query(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            FilamentTyp.bildname,
            func.count(FilamentSpule.spulen_id).label("spulenanzahl")
        )
        .join(FilamentSpule)
        .filter(FilamentSpule.in_printer == True)
        .group_by(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            FilamentTyp.bildname
        )
        .all()
    )
    im_drucker = [
        {
            "id": row.id,
            "name": row.name,
            "material": row.material,
            "farbe": row.farbe,
            "durchmesser": row.durchmesser,
            "bildname": row.bildname,
            "spulenanzahl": row.spulenanzahl
        }
        for row in im_drucker
    ]

    return {
        "typen": typ_count,
        "spulen": spulen_count,
        "fastleer": fastleere_typen,
        "verbrauch_7tage": int(verbrauch_7tage_sum),
        "im_drucker": im_drucker,
        "im_drucker_spulen": im_drucker_liste,
        "letzte_spulen": spulen_liste,
        "neue_spulen": neue_spulen_liste,
        "job_historie": job_historie,
        "dashboard_notes": dashboard_notes,
        "dashboard_notes_total": dashboard_note_total
    }


@app.get("/api/dashboard-notes")
def list_dashboard_notes(limit: int = Query(0, ge=0), db: Session = Depends(get_db)):
    base_query = db.query(DashboardNote).order_by(DashboardNote.created_at.desc())
    total = db.query(func.count(DashboardNote.id)).scalar() or 0
    notes_query = base_query.limit(limit) if limit else base_query
    notes = notes_query.all()
    def _serialize(note: DashboardNote) -> dict:
        author = note.author.username if note.author else None
        role = note.author.rolle if getattr(note, 'author', None) else None
        return {
            "id": note.id,
            "title": note.title,
            "message": note.message,
            "created_at": note.created_at,
            "author": author,
            "author_role": role,
        }
    return {"total": total, "items": [_serialize(note) for note in notes]}


@app.post("/api/dashboard-notes", status_code=201)
def create_dashboard_note(payload: DashboardNoteCreate, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.rolle not in {"admin", "mod"}:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Nachricht darf nicht leer sein")
    title = (payload.title or None)
    if title:
        title = title.strip() or None
    note = DashboardNote(title=title, message=message, author=user)
    db.add(note)
    db.commit()
    db.refresh(note)
    return {
        "id": note.id,
        "title": note.title,
        "message": note.message,
        "created_at": note.created_at,
        "author": user.username,
        "author_role": user.rolle,
    }


@app.patch("/api/dashboard-notes/{note_id}")
def update_dashboard_note(note_id: int, payload: DashboardNoteCreate, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.rolle not in {"admin", "mod"}:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    note = db.query(DashboardNote).filter(DashboardNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Hinweis nicht gefunden")
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Nachricht darf nicht leer sein")
    title = (payload.title or None)
    if title:
        title = title.strip() or None
    note.message = message
    note.title = title
    db.commit()
    db.refresh(note)
    return {
        "id": note.id,
        "title": note.title,
        "message": note.message,
        "created_at": note.created_at,
        "author": note.author.username if note.author else None,
        "author_role": note.author.rolle if note.author else None,
    }


@app.delete("/api/dashboard-notes/{note_id}", status_code=204)
def delete_dashboard_note(note_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.rolle not in {"admin", "mod"}:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")
    note = db.query(DashboardNote).filter(DashboardNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Hinweis nicht gefunden")
    db.delete(note)
    db.commit()
    return Response(status_code=204)




@app.get("/api/printer_spools")
def get_printer_spools(db: Session = Depends(get_db)):
    printers = {p.serial: p for p in db.query(Printer).all()}
    spulen = db.query(FilamentSpule).filter(FilamentSpule.in_printer == True).all()
    result: dict[str, dict] = {}

    def _bucket(key: str | None) -> dict:
        bucket_key = key or "_unassigned"
        if bucket_key not in result:
            printer_obj = printers.get(key) if key else None
            result[bucket_key] = {
                "serial": key,
                "printer_name": printer_obj.name if printer_obj else None,
                "spools": []
            }
        return result[bucket_key]

    for spule in spulen:
        bucket = _bucket(spule.printer_serial)
        bucket["spools"].append({
            "spulen_id": spule.spulen_id,
            "typ_name": spule.typ.name if spule.typ else None,
            "farbe": spule.typ.farbe if spule.typ else None,
            "material": spule.typ.material if spule.typ else None,
            "durchmesser": spule.typ.durchmesser if spule.typ else None,
            "restmenge": spule.restmenge,
            "gesamtmenge": spule.gesamtmenge,
            "printer_serial": spule.printer_serial,
            "bildname": spule.typ.bildname if spule.typ else None
        })

    return result



# Neuer PATCH-Endpoint: Toggle in_printer für eine Spule
@app.patch("/api/spule/{spulen_id}/toggle_in_printer")
def toggle_in_printer(spulen_id: int, printer_serial: Optional[str] = None, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")

    # Prüfung: Maximal 4 Spulen im Drucker
    if not spule.in_printer:  # will auf True gesetzt werden
        in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
        if in_printer_count >= 4:
            raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    if not spule.in_printer:
        resolved_serial = _normalize_printer_serial(db, printer_serial or spule.printer_serial)
        if not resolved_serial:
            raise HTTPException(status_code=400, detail="Bitte einen Drucker auswählen, solange die Spule im Drucker markiert ist.")
        spule.printer_serial = resolved_serial
    else:
        spule.printer_serial = None

    spule.in_printer = not spule.in_printer
    db.commit()
    db.refresh(spule)
    return {"spulen_id": spulen_id, "in_printer": spule.in_printer, "printer_serial": spule.printer_serial}

# Neuer API-Endpunkt: Nur den in_printer-Status einer Spule abfragen
@app.get("/api/spule/{spulen_id}/in_printer")
def get_in_printer_status(spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")
    return {"in_printer": spule.in_printer, "printer_serial": spule.printer_serial}


# Neuer API-Endpoint: Filamente im Drucker (nach Typ gruppiert, mit Spulenanzahl)
@app.get("/drucker_data")
def get_filamente_im_drucker(db: Session = Depends(get_db)):
    result = (
        db.query(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            FilamentTyp.bildname,
            func.count(FilamentSpule.spulen_id).label("spulenanzahl")
        )
        .join(FilamentSpule)
        .filter(FilamentSpule.in_printer == True)
        .group_by(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            FilamentTyp.bildname
        )
        .all()
    )
    return [
        {
            "id": row.id,
            "name": row.name,
            "material": row.material,
            "farbe": row.farbe,
            "durchmesser": row.durchmesser,
            "bildname": row.bildname,
            "spulenanzahl": row.spulenanzahl
        }
        for row in result
    ]

# QR-Code-Druck-Endpunkt

# Ersetzte QR-Code-Druck-Endpunkt: Leitet den Druckauftrag per HTTP an die Station weiter
import requests

STATION_IP = "172.30.181.116"  # ← IP der Station hier eintragen

@app.post("/print_qrcode/{spulen_id}")
def print_qrcode(spulen_id: int, request: Request):
    try:
        url = f"http://{STATION_IP}:9100/print_qrcode/{spulen_id}"
        response = requests.post(url, timeout=3)
        if response.ok:
            return {"status": "OK"}
        else:
            raise HTTPException(status_code=500, detail=response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fehler beim Aufruf der Station: {e}")

from datetime import datetime, timedelta

# Neuer API-Endpunkt: Typen mit niedrigem Lagerbestand
@app.get("/api/status/low_stock_types")
def get_low_stock_types(db: Session = Depends(get_db)):
    """Gibt alle Typen zurück, die leer oder fast leer sind.
    Kriterium:
      - leer: keine Spulen ODER Gesamt-Rest <= 0
      - fastleer: Rest <= 10% der Gesamtmenge (über alle Spulen) ODER (falls Gesamt unbekannt/0) Rest <= 100g
    """
    typen = db.query(FilamentTyp).all()
    result = []
    for typ in typen:
        spulen = typ.spulen or []
        anzahl_spulen = len(spulen)
        gesamt_rest = sum(float(s.restmenge or 0) for s in spulen)
        gesamt_kap = sum(float(s.gesamtmenge or 0) for s in spulen)

        status = None
        if anzahl_spulen == 0 or gesamt_rest <= 0:
            status = "leer"
        else:
            if gesamt_kap > 0:
                if gesamt_rest <= gesamt_kap * 0.10:
                    status = "fastleer"
            else:
                # Fallback, wenn Gesamtmenge unbekannt ist
                if gesamt_rest <= 100:
                    status = "fastleer"

        if status is not None:
            result.append({
                "id": typ.id,
                "name": typ.name,
                "material": typ.material,
                "farbe": typ.farbe,
                "durchmesser": typ.durchmesser,
                "restmenge": gesamt_rest,
                "gesamtmenge": gesamt_kap,
                "anzahl_spulen": anzahl_spulen,
                "status": status,
            })
    return result

# Neuer API-Endpunkt: Top 10 meistgenutzte Filamente nach Gesamtverbrauch (aus Verbrauchslog)
@app.get("/api/status/top_filaments")
def get_top_filaments(db: Session = Depends(get_db)):
    """Gibt die meistgenutzten Filamente nach Gesamtverbrauch zurück (aus Verbrauchslog)."""
    result = (
        db.query(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            func.sum(FilamentVerbrauch.verbrauch_in_g).label("verbrauch")
        )
        .join(FilamentVerbrauch, FilamentVerbrauch.typ_id == FilamentTyp.id)
        .group_by(FilamentTyp.id, FilamentTyp.name, FilamentTyp.material, FilamentTyp.farbe, FilamentTyp.durchmesser)
        .order_by(func.sum(FilamentVerbrauch.verbrauch_in_g).desc())
        .limit(10)
        .all()
    )
    return [{"id": r.id, "name": r.name, "material": r.material, "farbe": r.farbe, "durchmesser": r.durchmesser, "verbrauch": int(r.verbrauch or 0)} for r in result]


# Interner Helfer für Verbrauchssummen pro Typ ab bestimmtem Zeitpunkt
_USAGE_PERIODS = {
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}


def _usage_since(db: Session, since: datetime):
    query = (
        db.query(
            FilamentTyp.id,
            FilamentTyp.name,
            FilamentTyp.material,
            FilamentTyp.farbe,
            FilamentTyp.durchmesser,
            func.sum(FilamentVerbrauch.verbrauch_in_g).label("verbrauch")
        )
        .join(FilamentVerbrauch, FilamentVerbrauch.typ_id == FilamentTyp.id)
    )

    if since is not None:
        query = query.filter(FilamentVerbrauch.datum >= since)

    result = query.group_by(
        FilamentTyp.id,
        FilamentTyp.name,
        FilamentTyp.material,
        FilamentTyp.farbe,
        FilamentTyp.durchmesser
    ).all()

    return [
        {
            "id": row.id,
            "name": row.name,
            "material": row.material,
            "farbe": row.farbe,
            "durchmesser": row.durchmesser,
            "verbrauch": int(row.verbrauch or 0),
        }
        for row in result
    ]


@app.get("/api/status/usage")
def get_usage(period: str = "week", db: Session = Depends(get_db)):
    """Gibt den Verbrauch pro Typ für den gewünschten Zeitraum zurück."""
    key = (period or "").lower()
    if key not in _USAGE_PERIODS:
        raise HTTPException(status_code=400, detail="Ungültiger Zeitraum. Erlaubt: day, week, month")

    since = datetime.utcnow() - _USAGE_PERIODS[key]
    return _usage_since(db, since)


# Bestehender Endpoint für Abwärtskompatibilität
@app.get("/api/status/weekly_usage")
def get_weekly_usage(db: Session = Depends(get_db)):
    return _usage_since(db, datetime.utcnow() - _USAGE_PERIODS["week"])


# Serve login.html for /login
@app.get("/login", response_class=FileResponse)
def serve_login_page():
    path = os.path.join(static_dir, "login.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Login-Seite nicht gefunden")
    return path

# Serve login.html directly at /login.html
@app.get("/login.html", response_class=FileResponse)
def serve_login_html_direct():
    path = os.path.join(static_dir, "login.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Login-Seite nicht gefunden")
    return path

# Serve register.html for /register
@app.get("/register", response_class=FileResponse)
def serve_register_page():
    path = os.path.join(static_dir, "register.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Registrierungsseite nicht gefunden")
    return path

# Serve register.html directly at /register.html
@app.get("/register.html", response_class=FileResponse)
def serve_register_html_direct():
    path = os.path.join(static_dir, "register.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Registrierungsseite nicht gefunden")
    return path

if __name__ == "__main__":
    import uvicorn

    all_ips = get_all_local_ips()
    print("🚀 Server läuft auf folgenden IPs erreichbar:")
    for ip in all_ips:
        print(f"➡️  http://{ip}:8000")

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
