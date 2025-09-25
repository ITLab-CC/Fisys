COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 Tage
from fastapi import APIRouter, Form, HTTPException, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from passlib.hash import bcrypt_sha256 as bcrypt
from db import get_db
from models import User, AuthToken
import os
import secrets
import string
from fastapi import Request
from fastapi import HTTPException
from sqlalchemy.orm import Session
from fastapi import Depends
from dotenv import load_dotenv
from itsdangerous import URLSafeSerializer
from datetime import datetime, timezone

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY fehlt in der .env-Datei oder konnte nicht geladen werden.")
serializer = URLSafeSerializer(SECRET_KEY)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def get_login_page():
    return RedirectResponse(url="/login.html")

@router.post("/login")
async def login(
    benutzername: str = Form(...),
    passwort: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == benutzername).first()
    if not user:
        return RedirectResponse("/login?error=user_not_found", status_code=303)

    if not bcrypt.verify(passwort, user.password_hash):
        return RedirectResponse("/login?error=wrong_password", status_code=303)

    user.last_seen = datetime.now(timezone.utc)
    db.commit()

    response = RedirectResponse(url="/", status_code=303)
    cookie_wert = serializer.dumps({"username": benutzername})
    response.set_cookie(
        key="benutzer",
        value=cookie_wert,
        httponly=True,
        max_age=COOKIE_MAX_AGE,
        samesite="lax"
        # secure=True  # bei HTTPS aktivieren
    )
    return response

@router.get("/register", response_class=HTMLResponse)
async def get_register_page():
    return RedirectResponse(url="/register.html")

@router.post("/register")
async def register(
    token: str = Form(...),
    benutzername: str = Form(...),
    passwort: str = Form(...),
    db: Session = Depends(get_db)
):
    token = token.replace("-", "").upper()
    token_obj = db.query(AuthToken).filter(AuthToken.token == token, AuthToken.verwendet == False).first()
    if not token_obj:
        return RedirectResponse("/register?error=invalid_token", status_code=303)

    if db.query(User).filter(User.username == benutzername).first():
        return RedirectResponse("/register?error=user_exists", status_code=303)

    password_hash = bcrypt.hash(passwort)
    neuer_nutzer = User(username=benutzername, password_hash=password_hash, rolle=token_obj.rolle)
    db.add(neuer_nutzer)

    # Token als benutzt markieren
    token_obj.verwendet = True
    db.add(token_obj)
    db.commit()

    # Nach erfolgreicher Registrierung auf Login leiten (mit Erfolgshinweis)
    return RedirectResponse("/login?error=registered_success", status_code=303)


@router.api_route("/logout", methods=["GET", "POST"])
async def logout():
    response = RedirectResponse(url="/login?error=logout_success", status_code=303)
    response.delete_cookie("benutzer")
    return response

from fastapi import Request
from fastapi.responses import JSONResponse

@router.get("/api/userinfo")
async def get_userinfo(request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie, max_age=COOKIE_MAX_AGE)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        return JSONResponse(status_code=401, content={"detail": "Nicht eingeloggt"})

    user = db.query(User).filter(User.username == benutzer).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Benutzer nicht gefunden"})

    user.last_seen = datetime.now(timezone.utc)
    db.commit()

    return {"username": user.username, "rolle": user.rolle}

@router.post("/admin/create-token")
async def create_token(request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    neuer_token = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    data = await request.json()
    rolle = data.get("rolle", "user")
    if rolle not in ["user", "helper", "mod", "admin"]:
        raise HTTPException(status_code=400, detail="Ungültige Rolle")
    token_obj = AuthToken(token=neuer_token, verwendet=False, rolle=rolle)
    db.add(token_obj)
    db.commit()
    return {"token": neuer_token}

@router.get("/admin/tokens")
async def list_tokens(request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    tokens = db.query(AuthToken).all()
    return [{"token": t.token, "verwendet": t.verwendet, "rolle": t.rolle} for t in tokens]

@router.delete("/admin/token/{token_str}")
async def delete_token(token_str: str, request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    token_obj = db.query(AuthToken).filter(AuthToken.token == token_str).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token nicht gefunden")

    db.delete(token_obj)
    db.commit()
    return {"detail": "Token gelöscht"}

@router.patch("/admin/user/{username}/rolle")
async def update_user_role(username: str, request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    daten = await request.json()
    neue_rolle = daten.get("rolle")
    if neue_rolle not in ["user", "helper", "mod", "admin"]:
        raise HTTPException(status_code=400, detail="Ungültige Rolle")

    ziel_user = db.query(User).filter(User.username == username).first()
    if not ziel_user:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")

    ziel_user.rolle = neue_rolle
    db.commit()
    return {"detail": "Rolle aktualisiert"}

@router.delete("/admin/user/{username}")
async def delete_user(username: str, request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    ziel_user = db.query(User).filter(User.username == username).first()
    if not ziel_user:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")

    db.delete(ziel_user)
    db.commit()
    return {"detail": "Benutzer gelöscht"}

@router.get("/admin/users")
async def list_users(request: Request, db: Session = Depends(get_db)):
    raw_cookie = request.cookies.get("benutzer")
    try:
        benutzer = serializer.loads(raw_cookie)["username"] if raw_cookie else None
    except:
        benutzer = None
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    users = db.query(User).all()
    return [
        {
            "username": u.username,
            "rolle": u.rolle,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_seen": u.last_seen.isoformat() if u.last_seen else None,
        }
        for u in users
    ]
