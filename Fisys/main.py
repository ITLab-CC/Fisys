from contextlib import asynccontextmanager
import os
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from db import SessionLocal, init_db
from models import FilamentTyp, FilamentSpule
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Response
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
from sqlalchemy.orm import Session
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from qrcode_utils import generate_qrcode_for_spule, delete_qrcode_for_spule
import shutil

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

# --- WebSocket Dashboard Support ---
from fastapi import WebSocket, WebSocketDisconnect
import asyncio

dashboard_connections: list[WebSocket] = []

@app.websocket("/ws/dashboard")
async def websocket_dashboard(ws: WebSocket):
    await ws.accept()
    dashboard_connections.append(ws)
    try:
        while True:
            await asyncio.sleep(10)  # Verbindung offen halten
    except WebSocketDisconnect:
        dashboard_connections.remove(ws)

async def notify_dashboard(data: dict):
    for conn in dashboard_connections:
        try:
            await conn.send_json(data)
        except Exception:
            pass

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

# PATCH-Modell für Spule
class SpuleUpdate(BaseModel):
    restmenge: float
    in_printer: Optional[bool] = None
    gesamtmenge: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# API Endpoints

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
    if not is_verpackt:
        match = db.query(FilamentSpule).join(FilamentTyp).filter(
            FilamentSpule.verpackt == True,
            FilamentTyp.name == spule.name,
            FilamentTyp.material == spule.material,
            FilamentTyp.farbe == spule.farbe,
            FilamentTyp.durchmesser == spule.durchmesser
        ).first()
        if match:
            match.verpackt = False
            match.gesamtmenge = spule.gesamtmenge
            match.restmenge = spule.restmenge
            match.in_printer = spule.in_printer if spule.in_printer is not None else False
            db.commit()
            db.refresh(match)
            generate_qrcode_for_spule(match)
            return FilamentSpuleRead.model_validate(match)

    # Prüfung: Maximal 4 Spulen im Drucker
    if spule.in_printer:
        in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
        if in_printer_count >= 4:
            raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    new_spule = FilamentSpule(
        typ=typ,
        gesamtmenge=spule.gesamtmenge,
        restmenge=spule.restmenge,
        in_printer=spule.in_printer if spule.in_printer is not None else False,
        verpackt=spule.verpackt or False
    )
    db.add(new_spule)
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
from sqlalchemy.orm import joinedload

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
        "in_printer": spule.in_printer
    }


@app.put("/spulen/{spulen_id}", response_model=FilamentSpuleRead)
async def update_spule(spulen_id: int, spule_update: FilamentSpuleCreate, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule not found")
    if spule_update.gesamtmenge is not None:
        spule.gesamtmenge = spule_update.gesamtmenge
    if spule_update.restmenge is not None and spule_update.restmenge != spule.restmenge:
        print(f"Update Spule ID {spule.spulen_id}: alt_gewicht={spule.alt_gewicht}, restmenge={spule.restmenge}, neuer Wert={spule_update.restmenge}")
        spule.alt_gewicht = spule.restmenge
        spule.restmenge = spule_update.restmenge
    if spule_update.in_printer is not None:
        # Prüfung: Maximal 4 Spulen im Drucker
        if spule_update.in_printer and not spule.in_printer:
            in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
            if in_printer_count >= 4:
                raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")
        spule.in_printer = spule_update.in_printer
    if spule_update.verpackt is not None:
        spule.verpackt = spule_update.verpackt
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

# PATCH-Endpoint für Spule
@app.patch("/spulen/{spulen_id}", response_model=FilamentSpuleRead)
def patch_spule(spulen_id: int, update: SpuleUpdate, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")
    spule.alt_gewicht = spule.restmenge
    spule.restmenge = update.restmenge
    if update.in_printer is not None:
        # Prüfung: Maximal 4 Spulen im Drucker
        if update.in_printer and not spule.in_printer:
            in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
            if in_printer_count >= 4:
                raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")
        spule.in_printer = update.in_printer
    if update.gesamtmenge is not None:
        spule.gesamtmenge = update.gesamtmenge
    db.commit()
    db.refresh(spule)
    # WebSocket-Dashboard-Benachrichtigung
    import asyncio
    asyncio.create_task(notify_dashboard({
        "event": "spule_updated",
        "spule_id": spule.spulen_id,
        "restmenge": spule.restmenge,
        "gesamtmenge": spule.gesamtmenge
    }))
    return spule

@app.delete("/spulen/{spulen_id}")
def delete_spule_api(spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule not found")
    delete_qrcode_for_spule(spule)
    db.delete(spule)
    db.commit()
    # WebSocket-Dashboard-Benachrichtigung
    import asyncio
    asyncio.create_task(notify_dashboard({"event": "spule_deleted", "spule_id": spulen_id}))
    return {"detail": f"Spule {spulen_id} wurde gelöscht"}


# Bild-Upload Endpoint
from fastapi import Form, Request

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

    # Robustere Typprüfung: .filename nur bei UploadFile verwenden
    if isinstance(bild_input, UploadFile):
        bildname = bild_input.filename
    else:
        bildname = str(bild_input)

    typ = db.get(FilamentTyp, typ_id)
    if not typ:
        raise HTTPException(status_code=404, detail=f"Typ-ID {typ_id} nicht gefunden")

    if not bildname:
        raise HTTPException(status_code=400, detail="Kein Bildname angegeben")

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
            result.append({
                "bildname": file,
                "typ_name": typ.name if typ else None,
                "typ_farbe": typ.farbe if typ else None
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
        if len(spulen) == 0:
            status = "leer"
        else:
            gesamt_restmenge = sum(s.restmenge for s in spulen)
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
    eine_woche = datetime.utcnow() - timedelta(days=7)
    verbraucht_letzte_woche = (
        db.query(FilamentSpule)
        .filter(FilamentSpule.updated_at != None)
        .filter(FilamentSpule.updated_at > eine_woche)
        .all()
    )
    gesamtverbrauch = sum(
        max(0, s.gesamtmenge - s.restmenge)
        for s in verbraucht_letzte_woche
        if s.gesamtmenge and s.restmenge
    )

    letzte_spulen = (
        db.query(FilamentSpule)
        .order_by(FilamentSpule.updated_at.desc())
        .limit(3)
        .all()
    )
    spulen_liste = [{
        "spulen_id": s.spulen_id,
        "typ_name": s.typ.name,
        "farbe": s.typ.farbe,
        "material": s.typ.material,
        "durchmesser": s.typ.durchmesser,
        "alt_gewicht": s.alt_gewicht,
        "neu_gewicht": s.restmenge,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    } for s in letzte_spulen]

    # Ergänzung: Spulen, die aktuell im Drucker sind
    im_drucker_spulen = db.query(FilamentSpule).filter(FilamentSpule.in_printer == True).all()
    im_drucker_liste = [{
        "spulen_id": s.spulen_id,
        "typ_name": s.typ.name,
        "farbe": s.typ.farbe,
        "material": s.typ.material,
        "durchmesser": s.typ.durchmesser,
        "alt_gewicht": s.alt_gewicht,
        "neu_gewicht": s.restmenge,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
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
        "verbrauch_7tage": gesamtverbrauch,
        "im_drucker": im_drucker,
        "im_drucker_spulen": im_drucker_liste,
        "letzte_spulen": spulen_liste
    }



# Neuer PATCH-Endpoint: Toggle in_printer für eine Spule
@app.patch("/api/spule/{spulen_id}/toggle_in_printer")
def toggle_in_printer(spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")

    # Prüfung: Maximal 4 Spulen im Drucker
    if not spule.in_printer:  # will auf True gesetzt werden
        in_printer_count = db.query(FilamentSpule).filter_by(in_printer=True).count()
        if in_printer_count >= 4:
            raise HTTPException(status_code=400, detail="Maximale Anzahl an Spulen im Drucker erreicht. Entferne zuerst eine.")

    spule.in_printer = not spule.in_printer
    db.commit()
    db.refresh(spule)
    return {"spulen_id": spulen_id, "in_printer": spule.in_printer}

# Neuer API-Endpunkt: Nur den in_printer-Status einer Spule abfragen
@app.get("/api/spule/{spulen_id}/in_printer")
def get_in_printer_status(spulen_id: int, db: Session = Depends(get_db)):
    spule = db.get(FilamentSpule, spulen_id)
    if not spule:
        raise HTTPException(status_code=404, detail="Spule nicht gefunden")
    return {"in_printer": spule.in_printer}


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

if __name__ == "__main__":
    import uvicorn

    all_ips = get_all_local_ips()
    print("🚀 Server läuft auf folgenden IPs erreichbar:")
    for ip in all_ips:
        print(f"➡️  http://{ip}:8000")

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
