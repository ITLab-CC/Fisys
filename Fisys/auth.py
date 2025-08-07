from fastapi import APIRouter, Form, HTTPException, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from passlib.hash import bcrypt_sha256 as bcrypt
from db import get_db
from models import User, AuthToken
import os
import secrets
import string

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
    if not user or not bcrypt.verify(passwort, user.password_hash):
        raise HTTPException(status_code=401, detail="Ungültige Zugangsdaten")
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="benutzer", value=benutzername, httponly=True)
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
        raise HTTPException(status_code=403, detail="Ungültiger Registrierungscode")
    token_obj.verwendet = True
    db.add(token_obj)

    if db.query(User).filter(User.username == benutzername).first():
        raise HTTPException(status_code=409, detail="Benutzername existiert bereits")

    password_hash = bcrypt.hash(passwort)
    neuer_nutzer = User(username=benutzername, password_hash=password_hash, rolle=token_obj.rolle)
    db.add(neuer_nutzer)
    db.commit()
    return RedirectResponse(url="/login", status_code=303)


@router.api_route("/logout", methods=["GET", "POST"])
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("benutzer")
    return response

from fastapi import Request
from fastapi.responses import JSONResponse

@router.get("/api/userinfo")
async def get_userinfo(request: Request, db: Session = Depends(get_db)):
    benutzer = request.cookies.get("benutzer")
    if not benutzer:
        return JSONResponse(status_code=401, content={"detail": "Nicht eingeloggt"})

    user = db.query(User).filter(User.username == benutzer).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Benutzer nicht gefunden"})

    return {"username": user.username, "rolle": user.rolle}

@router.post("/admin/create-token")
async def create_token(request: Request, db: Session = Depends(get_db)):
    benutzer = request.cookies.get("benutzer")
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
    benutzer = request.cookies.get("benutzer")
    if not benutzer:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    user = db.query(User).filter(User.username == benutzer).first()
    if not user or user.rolle not in ["admin", "mod"]:
        raise HTTPException(status_code=403, detail="Keine Berechtigung")

    tokens = db.query(AuthToken).all()
    return [{"token": t.token, "verwendet": t.verwendet, "rolle": t.rolle} for t in tokens]

@router.delete("/admin/token/{token_str}")
async def delete_token(token_str: str, request: Request, db: Session = Depends(get_db)):
    benutzer = request.cookies.get("benutzer")
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