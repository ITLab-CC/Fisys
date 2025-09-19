import os
import sys
import atexit

lockfile = '/tmp/fisys-station.lock'

if os.path.exists(lockfile):
    print("FISYS-Station läuft schon, zweiter Start wird beendet.")
    sys.exit(0)

with open(lockfile, 'w') as f:
    f.write(str(os.getpid()))

def remove_lock():
    if os.path.exists(lockfile):
        os.remove(lockfile)

atexit.register(remove_lock)

import tkinter as tk
import cv2
import ctypes
ctypes.cdll.LoadLibrary("libzbar.so.0")  # korrekt für Linux
import requests
import hid
import logging

# Brother QL-800 Drucker-Integration
from brother_ql.raster import BrotherQLRaster
from brother_ql.backends.helpers import send
from PIL import ImageDraw, ImageFont
from brother_ql.conversion import convert

# Server-IP-Konstante
SERVER_IP = "172.30.41.35"  # proxmox "172.30.41.35" oder "localhost"
from PIL import Image, ImageTk
# Backward compatibility for Pillow >= 10: define ANTIALIAS alias
try:
    Image.ANTIALIAS
except AttributeError:
    Image.ANTIALIAS = Image.Resampling.LANCZOS
import qrcode

TESTGEWICHT_GRAMM = 0

from tkinter import messagebox
# Save last selection
last_selected_typ = None

def create_spool_typ(typ):
    leergewicht = int(typ.get("leergewicht") or 0)
    nettogewicht = typ.get("gesamtmenge", 0)
    payload = {
        "name": typ["name"],
        "material": typ.get("material", ""),
        "farbe": typ.get("farbe", ""),
        "durchmesser": typ.get("durchmesser", 0),
        "hersteller": typ.get("hersteller", ""),
        "leergewicht": leergewicht,
        "gesamtmenge": typ.get("gesamtmenge", nettogewicht),
        "restmenge": typ.get("restmenge", nettogewicht)
    }
    logging.debug(f"📦 Finaler Payload an API: {payload}")
    try:
        resp = requests.post(
            f"http://{SERVER_IP}:8000/spulen/",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=3
        )
        resp.raise_for_status()
        data = resp.json()
        logging.debug(f"🛰️ Antwort vom Server: {data}")
        return data.get("spulen_id", data.get("id"))
    except requests.RequestException as e:
        messagebox.showerror("Fehler", f"❌ Server nicht erreichbar oder Fehler beim Speichern:\n{e}")
        return None

def zeige_uebersichtansicht(typ, spool_id):
    # Clear previous view
    for w in center_frame.winfo_children():
        w.destroy()
    overview_frame = tk.Frame(center_frame, bg="#1e1e1e")
    overview_frame.pack(expand=True, fill="both")

    tk.Label(
        overview_frame,
        text="Neue Spule hinzugefügt!",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    ).pack(pady=20)

    # Details untereinander
    tk.Label(
        overview_frame,
        text=f"Filamenttyp: {typ['name']}",
        font=("Helvetica Neue", 22), fg="white", bg="#1e1e1e"
    ).pack(pady=(0,5))
    tk.Label(
        overview_frame,
        text=f"Spulen-ID: {spool_id}",
        font=("Helvetica Neue", 22), fg="white", bg="#1e1e1e"
    ).pack(pady=(0,5))

    # Gesamt- und Restgewicht aus API abrufen
    spool = hole_spule(spool_id)
    if not spool:
        messagebox.showerror("Fehler", "❌ Neue Spule konnte nicht geladen werden. Zurück zur Auswahl.")
        zeige_auswahlansicht()
        return

    gesamt = spool.get("gesamtmenge", 0)
    rest = spool.get("restmenge", 0)
    tk.Label(
        overview_frame,
        text=f"Gesamtgewicht: {gesamt} g",
        font=("Helvetica Neue", 22), fg="white", bg="#1e1e1e"
    ).pack(pady=(0,5))
    tk.Label(
        overview_frame,
        text=f"Restgewicht: {rest} g",
        font=("Helvetica Neue", 22), fg="white", bg="#1e1e1e"
    ).pack(pady=(0,20))

    # Nach 5 Sekunden zurück zur Auswahl und Overview schließen
    def end_overview():
        overview_frame.destroy()
        zeige_auswahlansicht()

    root.after(5000, end_overview)

def generate_qr_code(data):
    global last_selected_typ
    # Immer einen vollständigen Link erzeugen – unabhängig vom Aufrufer
    try:
        spulen_id = int(data)
        typ_id = last_selected_typ["id"] if last_selected_typ else None

        if typ_id is None:
            spule = hole_spule(spulen_id)
            typ_id = spule["typ_id"] if spule else 0

        qr_data = f"https://fisys.it-lab.cc/spulen.html?spule_id={spulen_id}"
    except:
        qr_data = str(data)

    # Use QRCode object with increased box_size
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=18,  # Increased by ~25%
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Bereite Druckbild mit QR links und ID rechts vor
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
    except:
        font = ImageFont.load_default()

    # Zielbreite für 62mm-Label: ca. 696 Pixel
    label_width = 696
    qr_img = qr_img.resize((400, 400))
    text = f"ID: {data}"

    # Erstelle leere weiße Fläche
    label_height = 400  # erhöht, um größeren QR-Code zu unterstützen
    image = Image.new("RGB", (label_width, label_height), "white")
    image.paste(qr_img, (20, (label_height - qr_img.height) // 2))

    # Text rechts daneben zentriert setzen
    draw = ImageDraw.Draw(image)
    text_x = 420  # nach rechts verschoben, um Platz für größeren QR
    text_y = (label_height - font.getbbox(text)[3]) // 2
    draw.text((text_x, text_y), text, fill="black", font=font)
    return image

def send_to_printer(image):
    try:
        logging.debug("📠 Druckvorgang gestartet")
        qlr = BrotherQLRaster("QL-800")
        qlr.exception_on_warning = True
        image = image.convert("1")
        instructions = convert(
            qlr, [image],
            label='62', cut=True, rotate='0',
            threshold=70, compress=True, dither=False,
            red=True, dpi_600=False, hq=True,
        )
        result = send(
            instructions=instructions,
            printer_identifier='usb://0x04f9:0x209b',
            backend_identifier='pyusb',
            blocking=True
        )
        logging.debug(f"📠 Ergebnis vom send(): {result}")
        if not result or (isinstance(result, str) and "errors" in result.lower()):
            raise RuntimeError("Drucker hat keine erfolgreiche Bestätigung geliefert.")
        logging.debug("✅ Druck erfolgreich abgeschlossen")
        return True
    except Exception as e:
        logging.error(f"❌ Druckfehler: {e}")
        messagebox.showerror("Fehler", f"❌ Druckvorgang fehlgeschlagen:\n{e}")
        zeige_auswahlansicht()
        return False

import time

def lese_gewicht(pfad):
    try:
        waage = hid.device()
        waage.open_path(pfad)
        # Blockierend lesen für zuverlässigere Daten
        gewicht = None
        start = time.time()
        timeout = 1  # Sekunden, um schneller auf Aus/Ein zu reagieren
        while time.time() - start < timeout:
            daten = waage.read(6)
            if len(daten) < 6:
                continue

            einheit = daten[2]
            gewicht = daten[4] + (daten[5] << 8)

            print(f"📊 Daten empfangen: {daten}")
            print(f"📏 Einheit: {einheit}, Gewicht: {gewicht}")

            if einheit == 2:
                return gewicht
            elif einheit == 11:
                return round(gewicht * 28.3495)
            else:
                print("⚠️ Unbekannte Einheit:", einheit)
                continue  # Unbekannte Einheit ignorieren und weiter versuchen

        print("⏱️ Timeout beim Lesen der Waage (keine gültigen Daten).")
        return None
    except Exception as e:
        print("❌ Fehler beim Lesen von der Waage:", e)
        return None

cap: cv2.VideoCapture | None = None
detector: cv2.QRCodeDetector | None = None
scanner_state = {"active": False, "on_scan": None}

# Store scheduled after() tasks for cancellation
scheduled_tasks = []

ausgabe_label = None
kamera_label = None
scanner_frame = None

start_frame = None
auswahl_frame = None

zuletzt_gescannte_spule = None

def abbrechen(zurueck_zu_start=False):
    global cap, scanner_state, scanner_frame, typauswahl_frame

    # Stop scanner and release camera
    scanner_state["active"] = False
    try:
        for task in scheduled_tasks:
            root.after_cancel(task)
    except Exception:
        pass
    scheduled_tasks.clear()

    if cap and cap.isOpened():
        cap.release()
        cap = None


    # Cleanup scanner_frame if exists
    if scanner_frame:
        scanner_frame.destroy()
        scanner_frame = None

    # Cleanup typauswahl_frame if exists
    if typauswahl_frame:
        typauswahl_frame.destroy()
        typauswahl_frame = None

    # Force UI redraw
    root.update_idletasks()

    # Show desired view
    if zurueck_zu_start:
        zeige_startansicht()
    else:
        zeige_auswahlansicht()

def zeige_startansicht():
    global start_frame
    # Clear all current views
    for widget in center_frame.winfo_children():
        widget.destroy()

    # Build fresh start frame
    start_frame = tk.Frame(center_frame, bg="#1e1e1e")
    start_frame.pack(expand=True, fill="both")
    headline_label = tk.Label(
    start_frame, text="Filament-Station IT-Lab",
    font=("Helvetica Neue", 42, "bold"),
    fg="white", bg="#1e1e1e"
    )
    headline_label.pack(pady=(0, 0))

    starten_button = tk.Button(
        start_frame,
        text="▶️ Beginnen",
        command=zeige_auswahlansicht,
        font=("Helvetica Neue", 36, "bold"),
        bg="white",
        fg="#1e1e1e",
        activebackground="#eeeeee",
        activeforeground="#1e1e1e",
        relief="flat",
        padx=80,
        pady=40,
        cursor="hand2",
        bd=0
    )
    starten_button.pack(expand=True)

    # Refresh UI
    root.update()

def zeige_sticker_druckansicht(nachher_callback):
    for widget in center_frame.winfo_children():
        widget.destroy()
    druck_frame = tk.Frame(center_frame, bg="#1e1e1e")
    druck_frame.pack(expand=True, fill="both")

    status_label = tk.Label(
        druck_frame,
        text="Sticker wird gedruckt...",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    status_label.pack(expand=True)
    root.update()

    # Nach 2 Sekunden weiter
    root.after(2000, nachher_callback)

def zeige_wiegehinweis(callback):
    for widget in center_frame.winfo_children():
        widget.destroy()
    gewicht = None
    hinweis_frame = tk.Frame(center_frame, bg="#1e1e1e")
    hinweis_frame.pack(expand=True, fill="both")
    hinweis_frame.grid_rowconfigure(0, weight=1)
    hinweis_frame.grid_rowconfigure(1, weight=1)
    hinweis_frame.grid_rowconfigure(2, weight=1)
    hinweis_frame.grid_columnconfigure(0, weight=1)
    # Add extra row to push content up
    hinweis_frame.grid_rowconfigure(3, weight=1)

    # --- Cancel all scheduled waagenstatus checks ---
    for task in scheduled_tasks:
        root.after_cancel(task)
    scheduled_tasks.clear()

    headline = tk.Label(
        hinweis_frame,
        text="Bitte Waage einschalten, schauen ob sie genullt ist \nund danach Spule auf die Waage legen.",
        font=("Helvetica Neue", 30, "bold"),
        fg="white", bg="#1e1e1e"
    )
    headline.grid(row=0, column=0, pady=(30, 10))

    # --- Waagenstatus-Anzeige ---
    status_frame = tk.Frame(hinweis_frame, bg="#1e1e1e")
    status_frame.grid(row=1, column=0, pady=(0, 10))

    # Labels for connection and activity
    verbunden_label = tk.Label(
        status_frame,
        text="USB-Verbindung: wird geprüft...",
        font=("Helvetica Neue", 20),
        fg="white", bg="#1e1e1e"
    )
    verbunden_label.pack(side="left", padx=20)

    aktiv_label = tk.Label(
        status_frame,
        text="⚖️ Aktiv: wird geprüft...",
        font=("Helvetica Neue", 20),
        fg="white", bg="#1e1e1e"
    )
    aktiv_label.pack(side="left", padx=20)

    bestaetigen_btn = tk.Button(
        hinweis_frame,
        text="⚠️ Waage nicht bereit",
        font=("Helvetica Neue", 24, "bold"),
        bg="white",
        fg="#1e1e1e",
        disabledforeground="red",
        relief="flat",
        padx=30, pady=20,
        state="disabled"
    )
    bestaetigen_btn.grid(row=2, column=0, pady=(0, 10))
    # Speichere den Button als Attribut des Frames
    hinweis_frame.bestaetigen_btn = bestaetigen_btn

    # --- Abbrechen-Button ergänzen (row=3) ---
    def abbrechen_und_schliessen():
        abbrechen(zurueck_zu_start=False)
        if hinweis_frame.winfo_exists():
            hinweis_frame.destroy()

    abbrechen_btn = tk.Button(
        hinweis_frame,
        text="❌ Abbrechen",
        font=("Helvetica Neue", 18, "bold"),
        bg="white", fg="#1e1e1e", relief="flat",
        padx=20, pady=10,
        command=abbrechen_und_schliessen
    )
    abbrechen_btn.grid(row=3, column=0, pady=(0, 30))

    def check_waagenstatus():
        # Gewichtsstatus zu Beginn immer zurücksetzen
        gewicht = None
        # Sofort Anzeige und Button deaktivieren beim Start der Funktion
        verbunden_label.config(text="USB-Verbindung: ❌", fg="red")
        aktiv_label.config(text="Bereit: ❌", fg="red")
        bestaetigen_btn.config(text="⚠️ Waage nicht bereit", state="disabled", command=lambda: None)
        try:
            from hid import enumerate
            dymo = next((d for d in enumerate() if "DYMO" in d.get("manufacturer_string", "")), None)
            if dymo:
                verbunden_label.config(text="Verbindung: ✅", fg="lightgreen")
                try:
                    gewicht = lese_gewicht(dymo["path"])
                    if gewicht is not None and gewicht > 0:
                        aktiv_label.config(text="Bereit: ✅", fg="lightgreen")
                        bestaetigen_btn.config(text="✅ Bestätigen", state="normal", command=callback)
                    else:
                        aktiv_label.config(text="Bereit: ❌", fg="orange")
                        bestaetigen_btn.config(text="⚠️ Waage nicht bereit", state="disabled", command=lambda: None)
                except Exception:
                    aktiv_label.config(text="Bereit: ❌", fg="red")
                    bestaetigen_btn.config(text="⚠️ Waage nicht bereit", state="disabled", command=lambda: None)
            else:
                verbunden_label.config(text="Verbindung: ❌", fg="red")
                aktiv_label.config(text="Bereit: ❌", fg="red")
                bestaetigen_btn.config(text="⚠️ Waage nicht bereit", state="disabled", command=lambda: None)
        except Exception as e:
            verbunden_label.config(text=f"Fehler: {e}", fg="red")
            aktiv_label.config(text="Bereit: ❌", fg="red")
            bestaetigen_btn.config(text="⚠️ Waage nicht bereit", state="disabled", command=lambda: None)
        # Schedule and track the after() call for cancellation
        scheduled_tasks.append(hinweis_frame.after(100, check_waagenstatus))

    check_waagenstatus()

def zeige_druckerhinweis(callback):
    # Ansicht leeren
    for widget in center_frame.winfo_children():
        widget.destroy()

    # Frame anlegen
    hinweis_frame = tk.Frame(center_frame, bg="#1e1e1e")
    hinweis_frame.pack(expand=True, fill="both")
    hinweis_frame.grid_rowconfigure(0, weight=1)
    hinweis_frame.grid_rowconfigure(1, weight=1)
    hinweis_frame.grid_rowconfigure(2, weight=1)
    hinweis_frame.grid_rowconfigure(3, weight=1)
    hinweis_frame.grid_columnconfigure(0, weight=1)

    # Überschrift
    headline = tk.Label(
        hinweis_frame,
        text="Bitte Drucker einschalten",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    headline.grid(row=0, column=0, pady=(30, 10))

    # Statuszeile
    status_label = tk.Label(
        hinweis_frame,
        text="Drucker: wird geprüft...",
        font=("Helvetica Neue", 22),
        fg="white", bg="#1e1e1e"
    )
    status_label.grid(row=1, column=0, pady=(0, 10))

    # Button (initial: nicht bereit)
    bestaetigen_btn = tk.Button(
        hinweis_frame,
        text="Nicht bereit",
        font=("Helvetica Neue", 24, "bold"),
        bg="white",
        fg="red",
        disabledforeground="red",
        relief="flat",
        padx=30, pady=20,
        state="disabled"
    )
    bestaetigen_btn.grid(row=2, column=0, pady=(0, 10))

    # Abbrechen
    def abbrechen_und_zurueck():
        abbrechen(zurueck_zu_start=False)
        if hinweis_frame.winfo_exists():
            hinweis_frame.destroy()

    abbrechen_btn = tk.Button(
        hinweis_frame,
        text="❌ Abbrechen",
        font=("Helvetica Neue", 18, "bold"),
        bg="white", fg="#1e1e1e", relief="flat",
        padx=20, pady=10,
        command=abbrechen_und_zurueck
    )
    abbrechen_btn.grid(row=3, column=0, pady=(0, 30))

    # Wiederholter Check des Druckerstatus
    def check_druckerstatus():
        # Default zurücksetzen
        status_label.config(text="Drucker: ❌ Aus", fg="red")
        bestaetigen_btn.config(text="Nicht bereit", state="disabled", fg="red", command=lambda: None)

        try:
            # USB-Check für Brother QL-800
            try:
                import usb.core  # PyUSB
            except Exception:
                usb = None
                usb_core = None
            else:
                usb_core = usb.core

            bereit = False
            if usb_core is not None:
                dev = usb_core.find(idVendor=0x04F9, idProduct=0x209B)
                bereit = dev is not None

            if bereit:
                status_label.config(text="Drucker: ✅ Aktiv", fg="lightgreen")
                # Button freigeben – klick führt zur nächsten Ansicht
                bestaetigen_btn.config(
                    text="Bereit",
                    state="normal",
                    fg="#1e1e1e",
                    command=lambda: (hinweis_frame.destroy(), callback())
                )
            else:
                status_label.config(text="Drucker: ❌ Aus", fg="red")
                bestaetigen_btn.config(text="Nicht bereit", state="disabled", fg="red")
        except Exception as e:
            status_label.config(text=f"Fehler: {e}", fg="red")
            bestaetigen_btn.config(text="Nicht bereit", state="disabled", fg="red")

        # Erneut prüfen
        scheduled_tasks.append(hinweis_frame.after(500, check_druckerstatus))

    # Evtl. alte Tasks abbrechen und starten
    for t in scheduled_tasks:
        try:
            root.after_cancel(t)
        except Exception:
            pass
    scheduled_tasks.clear()

    check_druckerstatus()

def lese_barcode():
    global cap, detector
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    if not cap.isOpened():
        if ausgabe_label:
            ausgabe_label.config(text="❌ Kamera konnte nicht geöffnet werden.")
        return

    if ausgabe_label:
        ausgabe_label.config(text="📷 Scanne QR-Code...")
    detector = cv2.QRCodeDetector()
    scanner_state["active"] = True
    update_frame()

def update_frame():
    if not scanner_state["active"] or cap is None or detector is None:
        return

    ret, frame = cap.read()
    if not ret:
        if ausgabe_label:
            ausgabe_label.config(text="❌ Fehler beim Kamerabild.")
        cap.release()
        scanner_state["active"] = False
        return

    # Bild direkt in Graustufen umwandeln, dann Kontrast verbessern
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)  # Kontrast verbessern
    # --- CLAHE für adaptive Kontrastverstärkung ---
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    data, bbox, _ = detector.detectAndDecode(gray)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image = image.resize((960, 540), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(image=image)
    if kamera_label:
        setattr(kamera_label, "imgtk", photo)
        kamera_label.configure(image=photo)

    if data:
        cap.release()
        scanner_state["active"] = False
        if kamera_label:
            kamera_label.configure(image="")
        handler = scanner_state.get("on_scan") or verarbeite_qr_code
        # Nach der Verwendung zurücksetzen, damit andere Flows wieder Standard nutzen
        scanner_state["on_scan"] = None
        handler(data)
    else:
        scheduled_tasks.append(root.after(5, update_frame))

def zeige_wiegeansicht():
    # Clear all current views
    for widget in center_frame.winfo_children():
        widget.destroy()
    global TESTGEWICHT_GRAMM
    TESTGEWICHT_GRAMM = None

    # Frame für Wiegen
    wiege_frame = tk.Frame(center_frame, bg="#1e1e1e")
    wiege_frame.pack(expand=True)


    headline = tk.Label(
        wiege_frame, text="⚖️ Spule wird gewogen...", font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    headline.pack(pady=20)
    #hinzufügen
    from hid import enumerate

    dymo = next((d for d in enumerate() if "DYMO" in d.get("manufacturer_string", "")), None)
    if dymo:
        gewicht = lese_gewicht(dymo["path"])
        if gewicht is not None:
            TESTGEWICHT_GRAMM = gewicht
        else:
            print("⚠️ Gewicht konnte nicht gelesen werden.")
            TESTGEWICHT_GRAMM = None
    else:
        print("❌ DYMO-Waage nicht gefunden.")
        TESTGEWICHT_GRAMM = None
    # Direkt zur Druckansicht nach Wiegen
    scheduled_tasks.append(root.after(2000, zeige_druckansicht))


def zeige_wiegeansicht_neue_spule():
    for widget in center_frame.winfo_children():
        widget.destroy()
    global TESTGEWICHT_GRAMM
    TESTGEWICHT_GRAMM = None

    wiege_frame = tk.Frame(center_frame, bg="#1e1e1e")
    wiege_frame.pack(expand=True)

    headline = tk.Label(
        wiege_frame, text="⚖️ Spule wird gewogen...", font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    headline.pack(pady=20)

    from hid import enumerate

    dymo = next((d for d in enumerate() if "DYMO" in d.get("manufacturer_string", "")), None)
    if dymo:
        gewicht = lese_gewicht(dymo["path"])
        if gewicht is not None:
            TESTGEWICHT_GRAMM = gewicht  # <--- Nur das gewogene Gewicht!
            logging.debug(f"⚖️ TESTGEWICHT_GRAMM gesetzt auf: {TESTGEWICHT_GRAMM}")
        else:
            print("⚠️ Gewicht konnte nicht gelesen werden.")
            TESTGEWICHT_GRAMM = None
    else:
        print("❌ DYMO-Waage nicht gefunden.")
        TESTGEWICHT_GRAMM = None

    scheduled_tasks.append(root.after(2000, lambda: finish_add_spool(volle_spule=False)))


# --- Druck- und Tutorialansicht ---
def zeige_druckansicht():
    # Druck-Status anzeigen
    for widget in center_frame.winfo_children():
        widget.destroy()
    druck_frame = tk.Frame(center_frame, bg="#1e1e1e")
    druck_frame.pack(expand=True, fill="both")

    status_label = tk.Label(
        druck_frame,
        text="Sticker wird gedruckt...",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    status_label.pack(expand=True)

    # UI sofort aktualisieren, damit neue Ansicht sichtbar ist
    root.update_idletasks()
    root.update()

    # Spule-ID aus dem zuletzt gewählten Typ berechnen
    try:
        list_resp = requests.get(f"http://{SERVER_IP}:8000/spulen/", timeout=3)
        if list_resp.ok:
            spulen_list = list_resp.json()
            if spulen_list:
                next_id = max(item.get("spulen_id", item.get("id", 0)) for item in spulen_list) + 1
            else:
                next_id = 1
        else:
            print(f"❌ Fehler beim Laden der Spulenliste: {list_resp.status_code}")
            next_id = "unbekannt"
    except requests.RequestException as e:
        print(f"❌ Netzwerkfehler beim Laden der Spulenliste:\n{e}")
        next_id = "unbekannt"

    # QR-Code erzeugen und drucken
    qr_image = generate_qr_code(next_id)

    def druck_und_weiter():
        if send_to_printer(qr_image):
            root.after(500, lambda: zeige_tutorialansicht(zeige_auswahlansicht))

    # Verzögere Druck um 200ms, damit UI vorher aktualisiert wird und Anzeige sichtbar ist
    root.after(200, druck_und_weiter)


def zeige_tutorialansicht(on_done=None):
    # Tutorial-Ansicht anzeigen
    for widget in center_frame.winfo_children():
        widget.destroy()
    tut_frame = tk.Frame(center_frame, bg="#1e1e1e")
    tut_frame.pack(expand=True, fill="both")

    # Anweisungstext zum Kleben
    instruction_label = tk.Label(
        tut_frame,
        text="Sticker bitte an die Seite der Filament Spule kleben",
        font=("Helvetica Neue", 22),
        fg="white", bg="#1e1e1e"
    )
    instruction_label.pack(pady=(10, 10))

    # Bild anzeigen
    img_path = os.path.join(script_dir, "tutorial.jpg")
    img = Image.open(img_path)
    img = img.resize((600, 400), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    img_label = tk.Label(tut_frame, image=photo, bg="#1e1e1e")
    setattr(img_label, "image", photo)
    img_label.pack(pady=10)

    # Erledigt-Button
    erledigt_btn = tk.Button(
        tut_frame,
        text="Erledigt",
        font=("Helvetica Neue", 20, "bold"),
        bg="white", fg="#1e1e1e", relief="flat", padx=20, pady=10,
        command=on_done if on_done else finish_add_spool
    )
    erledigt_btn.pack(pady=10)

def zeige_sticker_entfernen_tutorial(spulen_id):
    for widget in center_frame.winfo_children():
        widget.destroy()

    tut_frame = tk.Frame(center_frame, bg="#1e1e1e")
    tut_frame.pack(expand=True, fill="both")

    instruction_label = tk.Label(
        tut_frame,
        text="Bitte entferne den Sticker von der Spule",
        font=("Helvetica Neue", 22, "bold"),
        fg="white", bg="#1e1e1e"
    )
    instruction_label.pack(pady=(10, 10))

    img_path = os.path.join(script_dir, "tutorial2.jpg")
    img = Image.open(img_path)
    img = img.resize((600, 400), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    img_label = tk.Label(tut_frame, image=photo, bg="#1e1e1e")
    setattr(img_label, "image", photo)
    img_label.pack(pady=10)

    def erledigt_und_zeige_bestaetigung():
        loesche_spule(spulen_id, silent=True)

        for widget in center_frame.winfo_children():
            widget.destroy()

        geloescht_frame = tk.Frame(center_frame, bg="#1e1e1e")
        geloescht_frame.pack(expand=True, fill="both")

        geloescht_label = tk.Label(
            geloescht_frame,
            text="Spule wurde gelöscht.",
            font=("Helvetica Neue", 34, "bold"),
            fg="white", bg="#1e1e1e"
        )
        geloescht_label.pack(expand=True)

        def zur_auswahl():
            for widget in center_frame.winfo_children():
                widget.destroy()
            zeige_auswahlansicht()

        root.after(3000, zur_auswahl)

    erledigt_btn = tk.Button(
        tut_frame,
        text="Erledigt",
        font=("Helvetica Neue", 20, "bold"),
        bg="white", fg="#1e1e1e", relief="flat", padx=10, pady=10,
        command=erledigt_und_zeige_bestaetigung
    )
    erledigt_btn.pack(pady=10)
    tut_frame.update()


# Neue Funktion zum Anzeigen der Löschbestätigung
def zeige_geloescht_bestaetigung():
    for widget in center_frame.winfo_children():
        widget.destroy()

    geloescht_frame = tk.Frame(center_frame, bg="#1e1e1e")
    geloescht_frame.pack(expand=True, fill="both")

    geloescht_label = tk.Label(
        geloescht_frame,
        text="🗑️ Spule wurde gelöscht.",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    geloescht_label.pack(expand=True)

    def zur_auswahl():
        for widget in center_frame.winfo_children():
            widget.destroy()
        zeige_auswahlansicht()

def finish_add_spool(volle_spule=False):
    global last_selected_typ, TESTGEWICHT_GRAMM
    try:
        leergewicht = int(last_selected_typ.get("leergewicht") or 0)
        typ_id = last_selected_typ.get("id")
        gesamtgewicht = 1000  # Default

        # Gesamtgewicht bestimmen
        try:
            resp = requests.get(f"http://{SERVER_IP}:8000/spulen/", timeout=3)
            if resp.ok:
                spulen = resp.json()
                gleiche_typen = [s for s in spulen if s.get("typ_id") == typ_id]
                if gleiche_typen:
                    gesamtgewicht = max((s.get("gesamtmenge") or 0) for s in gleiche_typen)
        except:
            pass

        last_selected_typ["gesamtmenge"] = gesamtgewicht
        if volle_spule:
            last_selected_typ["restmenge"] = gesamtgewicht
        else:
            restgewicht = max(0, TESTGEWICHT_GRAMM - leergewicht)
            last_selected_typ["restmenge"] = restgewicht

        # Spule anlegen (POST an /spulen/) und dann QR-Code mit vom Server zurückgegebener spulen_id erzeugen
        sid = create_spool_typ(last_selected_typ)
        if not sid:
            zeige_auswahlansicht()
            return

        spulen_id = sid
        qr_image = generate_qr_code(spulen_id)
        if not send_to_printer(qr_image):
            return

        # Jetzt Druckansicht zeigen, dann Tutorial, dann Übersicht
        def nach_druck():
            zeige_tutorialansicht(lambda: zeige_uebersichtansicht(last_selected_typ, spulen_id))
        zeige_sticker_druckansicht(nach_druck)

        TESTGEWICHT_GRAMM = None
    except Exception as e:
        messagebox.showerror("Fehler", f"❌ Unerwarteter Fehler beim Erstellen der Spule:\n{e}")
        zeige_auswahlansicht()


def verarbeite_qr_code(barcode):
    global zuletzt_gescannte_spule
    # Unterstützt neues Format ...?spule_id=123 (nur Ziffern extrahieren)
    if isinstance(barcode, str) and "spule_id=" in barcode:
        frag = barcode.split("spule_id=")[-1]
        nummer = ""
        for ch in frag:
            if ch.isdigit():
                nummer += ch
            else:
                break
        barcode = nummer
    # Fallback: altes Format .../id123 oder .../id=123
    elif isinstance(barcode, str) and "/id" in barcode:
        id_teil = barcode.rstrip("/").split("/id")[-1]
        if id_teil.startswith("="):
            id_teil = id_teil[1:]
        barcode = id_teil

    try:
        spulen_id = int(barcode)
    except (ValueError, TypeError):
        if ausgabe_label:
            ausgabe_label.config(text="❌ QR-Code enthält keine gültige ID.")
        return

    spule = hole_spule(spulen_id)
    if spule is None:
        if ausgabe_label:
            ausgabe_label.config(text="❌ Verbindung zum Server fehlgeschlagen.")
        messagebox.showerror("Fehler", "❌ Server nicht erreichbar oder Spule nicht gefunden.")
        zeige_auswahlansicht()
        return
    zuletzt_gescannte_spule = spule
    if spule:
        text = (
            f"📦 Spulendaten:\n"
            f"ID: {spule.get('spulen_id', '-')}\n"
            f"Typ-ID: {spule.get('typ_id', '-')}\n"
            f"Gesamt: {spule.get('gesamtmenge', '-')} g\n"
            f"Rest: {spule.get('restmenge', '-')} g\n"
            f"In Printer: {spule.get('in_printer', '-')}"
        )
    else:
        if ausgabe_label:
            ausgabe_label.config(text=f"❌ Spule mit ID {spulen_id} nicht gefunden.")
        messagebox.showerror("Fehler", f"❌ Spule mit ID {spulen_id} nicht gefunden.")
        zeige_auswahlansicht()
        return

    if kamera_label:
        kamera_label.pack_forget()
    # Entferne die Überschrift „📷 Spule scannen“ aus dem Scanner-Frame, sobald der QR-Code erkannt wurde
    if scanner_frame:
        for widget in scanner_frame.winfo_children():
            if isinstance(widget, tk.Label) and "Spule scannen" in widget.cget("text"):
                widget.pack_forget()

    if ausgabe_label:
        ausgabe_label.pack_forget()

    # Neues Label mit gleichem Stil wie das Wiegelabel für "✅ Spule erkannt"
    erkannt_label = tk.Label(scanner_frame, text="✅ Spule erkannt", font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e")
    erkannt_label.pack(pady=100)
    if scanner_frame:
        scanner_frame.update_idletasks()

    # Nach 2.5 Sekunden zur Wiegeansicht wechseln
    def wiegeansicht():
        if scanner_frame:
            scanner_frame.pack_forget()

        wiege_frame = tk.Frame(center_frame, bg="#1e1e1e")
        wiege_frame.pack(expand=True)

        global TESTGEWICHT_GRAMM
        TESTGEWICHT_GRAMM = None
        headline = tk.Label(wiege_frame, text="⚖️ Spule wird gewogen...", font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e")
        headline.pack(pady=20)

        # Testgewicht bearbeiten (ersetzt durch echtes Wiegen)
        from hid import enumerate

        dymo = next((d for d in enumerate() if "DYMO" in d.get("manufacturer_string", "")), None)
        if dymo:
            gewicht = lese_gewicht(dymo["path"])
            if gewicht is not None:
                TESTGEWICHT_GRAMM = gewicht
            else:
                print("⚠️ Gewicht konnte nicht gelesen werden.")
                TESTGEWICHT_GRAMM = None
        else:
            print("❌ DYMO-Waage nicht gefunden.")
            TESTGEWICHT_GRAMM = None

        def zeige_detailinfos():
            # Sicherer Zugriff auf zuletzt_gescannte_spule
            if not zuletzt_gescannte_spule:
                return

            for widget in wiege_frame.winfo_children():
                if not isinstance(widget, tk.Button):
                    widget.destroy()

            detail_headline = tk.Label(wiege_frame, text="Spulendetails", font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e")
            detail_headline.pack(pady=(0, 10))

            info_label = None

            # Typdaten abrufen
            typ_id = zuletzt_gescannte_spule["typ_id"]
            typ_daten = hole_typ(typ_id)

            typtext = "Typdaten:\n"
            if typ_daten:
                typtext += (
                    f"Name: {typ_daten.get('name', '-')}\n"
                    f"Farbe: {typ_daten.get('farbe', '-')}\n"
                    f"Hersteller: {typ_daten.get('hersteller', '-')}\n"
                    f"Material: {typ_daten.get('material', '-')}\n"
                    f"Durchmesser: {typ_daten.get('durchmesser', '-')}\n"
                )
            else:
                typtext += "❌ Keine Typdaten gefunden.\n"

            spulentext = (
                f"Spulendaten:\n"
                f"ID: {zuletzt_gescannte_spule['spulen_id']}\n"
                f"Typ-ID: {zuletzt_gescannte_spule['typ_id']}\n"
                f"Gesamt: {zuletzt_gescannte_spule['gesamtmenge']} g\n"
                f"Rest: {zuletzt_gescannte_spule['restmenge']} g\n"
                f"In Printer: {zuletzt_gescannte_spule['in_printer']}"
            )

            info_frame = tk.Frame(wiege_frame, bg="#1e1e1e")
            info_frame.pack(pady=20)

            typ_label = tk.Label(info_frame, text=typtext, font=("Helvetica Neue", 18), fg="white", bg="#1e1e1e", justify="left", anchor="nw")
            typ_label.grid(row=0, column=0, padx=10, sticky="n")

            spule_label = tk.Label(info_frame, text=spulentext, font=("Helvetica Neue", 18), fg="white", bg="#1e1e1e", justify="left", anchor="nw")
            spule_label.grid(row=0, column=1, padx=10, sticky="n")

            # --- Insert calculation of leergewicht and nettogewicht before creating gewicht_label ---
            leergewicht = (typ_daten.get("leergewicht") if typ_daten else 0) or 0
            nettogewicht = max(0, TESTGEWICHT_GRAMM - leergewicht)

            # NEU: Leererkennung und entsprechendes Label
            if nettogewicht <= 5:
                gewicht_label = tk.Label(
                    wiege_frame,
                    text="⚠️ Spule wurde als leer erkannt.",
                    font=("Helvetica Neue", 28, "bold"),
                    fg="white",
                    bg="#1e1e1e",
                    justify="center"
                )
                gewicht_label.pack(pady=(5, 5))
                spule_leer = True
            else:
                gewicht_label = tk.Label(
                    wiege_frame,
                    text=(
                        f"Bruttogewicht (mit Spule): {TESTGEWICHT_GRAMM} g\n"
                        f"Leergewicht der Spule: {leergewicht} g\n"
                        f"Nettogewicht (Filament): {nettogewicht} g"
                    ),
                    font=("Helvetica Neue", 15),
                    fg="white",
                    bg="#1e1e1e",
                    justify="left"
                )
                gewicht_label.pack(pady=(5, 5))
                spule_leer = False


            def erneut_starten():
                wiege_frame.destroy()
                zeige_auswahlansicht()

            def bestaetigen():
                # Sicherer Zugriff auf zuletzt_gescannte_spule["typ_id"]
                if not zuletzt_gescannte_spule:
                    return
                typ_daten = hole_typ(zuletzt_gescannte_spule["typ_id"])
                leergewicht = (typ_daten.get("leergewicht") if typ_daten else 0) or 0
                netto = max(0, TESTGEWICHT_GRAMM - leergewicht)

                if zuletzt_gescannte_spule:
                    spulen_id = zuletzt_gescannte_spule['spulen_id']
                    # --- NEU: Bedingter Block für DELETE, sonst PATCH wie gehabt ---
                    if spule_leer:
                        zeige_sticker_entfernen_tutorial(spulen_id)
                        return
                    # PATCH wie vorher, wenn nicht leer
                    payload = {"restmenge": netto, "in_printer": False}
                    try:
                        payload = {
                            "restmenge": float(netto),
                            "in_printer": False
                        }
                        headers = {"Content-Type": "application/json", "Accept": "application/json"}
                        r = requests.patch(
                            f"http://{SERVER_IP}:8000/spulen/{spulen_id}",
                            headers=headers,
                            json=payload,
                            timeout=5
                        )
                        if not r.ok:
                            raise RuntimeError(f"PATCH fehlgeschlagen ({r.status_code}): {r.text}")

                        for widget in center_frame.winfo_children():
                            widget.destroy()
                        bestaetigt_frame = tk.Frame(center_frame, bg="#1e1e1e")
                        bestaetigt_frame.pack(expand=True, fill="both")
                        bestaetigt_label = tk.Label(
                            bestaetigt_frame,
                            text="✅ Gewicht wurde übernommen.",
                            font=("Helvetica Neue", 34, "bold"),
                            fg="white", bg="#1e1e1e"
                        )
                        bestaetigt_label.pack(expand=True)

                        def cleanup_and_return():
                            for widget in center_frame.winfo_children():
                                widget.destroy()
                            zeige_auswahlansicht()

                        root.after(2000, cleanup_and_return)
                    except Exception as e:
                        messagebox.showerror("Fehler", f"❌ Aktualisierung fehlgeschlagen:\n{e}")

            buttons_frame = tk.Frame(wiege_frame, bg="#1e1e1e")
            buttons_frame.pack(pady=30, expand=True)

            retry_button = tk.Button(buttons_frame, text="Zurück", font=("Helvetica Neue", 24, "bold"),
                                     bg="white", fg="#1e1e1e", relief="flat", padx=40, pady=20,
                                     command=erneut_starten, width=20)
            retry_button.grid(row=0, column=0, padx=40, pady=10, sticky="e")

            ok_button = tk.Button(
                buttons_frame,
                text="Spule löschen" if spule_leer else "Bestätigen",
                font=("Helvetica Neue", 24, "bold"),
                bg="white", fg="#1e1e1e", relief="flat", padx=40, pady=20,
                command=bestaetigen, width=20
            )
            ok_button.grid(row=0, column=1, padx=40, pady=10, sticky="w")

        scheduled_tasks.append(root.after(2000, zeige_detailinfos))

    scheduled_tasks.append(root.after(2500, wiegeansicht))

def verarbeite_qr_code_in_drucker(barcode):
    # URL-Format unterstützen (.../spule_id=123)
    if isinstance(barcode, str) and "spule_id=" in barcode:
        frag = barcode.split("spule_id=")[-1]
        # nur führende Ziffern übernehmen (bis erstes Nicht‑Ziffern-Zeichen)
        nummer = ""
        for ch in frag:
            if ch.isdigit():
                nummer += ch
            else:
                break
        barcode = nummer

    try:
        spulen_id = int(barcode)
    except (ValueError, TypeError):
        messagebox.showerror("Fehler", "❌ QR-Code enthält keine gültige Spulen-ID.")
        zeige_auswahlansicht()
        return

    # Optional: Existenz prüfen (robustere Fehlermeldungen)
    spule = hole_spule(spulen_id)
    if spule is None:
        messagebox.showerror("Fehler", "❌ Server nicht erreichbar oder Spule nicht gefunden.")
        zeige_auswahlansicht()
        return

    printer_liste = hole_printer_liste()
    if not printer_liste:
        zeige_auswahlansicht()
        return

    # Auswahloberfläche vorbereiten
    for widget in center_frame.winfo_children():
        widget.destroy()

    auswahl_frame = tk.Frame(center_frame, bg="#1e1e1e")
    auswahl_frame.pack(expand=True, fill="both")

    tk.Label(
        auswahl_frame,
        text=f"Spule #{spulen_id} in Drucker setzen",
        font=("Helvetica Neue", 30, "bold"),
        fg="white", bg="#1e1e1e"
    ).pack(pady=(30, 10))

    if spule.get("in_printer") and spule.get("printer_serial"):
        tk.Label(
            auswahl_frame,
            text=f"Aktuell zugeordnet: {spule.get('printer_serial')}",
            font=("Helvetica Neue", 20),
            fg="white", bg="#1e1e1e"
        ).pack(pady=(0, 10))

    tk.Label(
        auswahl_frame,
        text="Bitte Drucker auswählen:",
        font=("Helvetica Neue", 22),
        fg="#d0d0d0", bg="#1e1e1e"
    ).pack(pady=(0, 20))

    buttons_frame = tk.Frame(auswahl_frame, bg="#1e1e1e")
    buttons_frame.pack(pady=(0, 30))

    aktuelle_rest = int(spule.get("restmenge") or 0)

    def setze_spule_in_printer(printer_obj):
        serial = printer_obj.get("serial")
        if not serial:
            messagebox.showerror("Fehler", "❌ Dieser Drucker hat keine Seriennummer.")
            return
        try:
            payload = {
                "restmenge": float(aktuelle_rest),
                "in_printer": True,
                "printer_serial": serial
            }
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            r = requests.patch(
                f"http://{SERVER_IP}:8000/spulen/{spulen_id}",
                headers=headers,
                json=payload,
                timeout=5
            )
            if not r.ok:
                raise RuntimeError(f"PATCH fehlgeschlagen ({r.status_code}): {r.text}")
        except Exception as e:
            messagebox.showerror("Fehler", f"❌ Aktualisierung fehlgeschlagen:\n{e}")
            zeige_auswahlansicht()
            return

        for widget in center_frame.winfo_children():
            widget.destroy()

        bestaetigt_frame = tk.Frame(center_frame, bg="#1e1e1e")
        bestaetigt_frame.pack(expand=True, fill="both")

        drucker_name = printer_obj.get("name") or serial
        tk.Label(
            bestaetigt_frame,
            text=f"✅ Spule im Drucker {drucker_name} gesetzt",
            font=("Helvetica Neue", 34, "bold"),
            fg="white", bg="#1e1e1e"
        ).pack(expand=True)

        def cleanup_and_return():
            for widget in center_frame.winfo_children():
                widget.destroy()
            zeige_auswahlansicht()

        root.after(2000, cleanup_and_return)

    printer_sorted = sorted(
        printer_liste,
        key=lambda p: (p.get("name") or p.get("serial") or "").lower()
    )

    for idx, printer_obj in enumerate(printer_sorted):
        name = printer_obj.get("name") or "Ohne Namen"
        serial = printer_obj.get("serial") or "–"
        btn = tk.Button(
            buttons_frame,
            text=f"{name}\n({serial})",
            font=("Helvetica Neue", 24, "bold"),
            bg="white", fg="#1e1e1e",
            relief="flat", padx=40, pady=20,
            width=22,
            command=lambda p=printer_obj: setze_spule_in_printer(p)
        )
        btn.grid(row=idx // 2, column=idx % 2, padx=20, pady=15, sticky="nsew")
        buttons_frame.grid_columnconfigure(idx % 2, weight=1)

    tk.Button(
        auswahl_frame,
        text="Abbrechen",
        font=("Helvetica Neue", 22, "bold"),
        bg="#2d2d2d", fg="white",
        relief="flat", padx=30, pady=15,
        command=zeige_auswahlansicht
    ).pack(pady=(0, 30))

def hole_spule(spulen_id):
    try:
        url = f"http://{SERVER_IP}:8000/spulen/{spulen_id}"
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Spule mit ID {spulen_id} nicht gefunden. Status: {response.status_code}")
            return None
    except requests.RequestException as e:
        print("❌ Server nicht erreichbar:", e)
        return None

def hole_typ(typ_id):
    try:
        url = f"http://{SERVER_IP}:8000/api/typ/{typ_id}"
        response = requests.get(url, timeout=3)
        print(f"🛰️ Anfrage an {url}, Antwortcode: {response.status_code}")
        print(f"Antwortinhalt: {response.text}")
        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ Typ mit ID {typ_id} nicht gefunden.")
            return None
    except requests.RequestException as e:
        print("❌ Fehler bei API-Aufruf (Typdaten):", e)
        return None


def hole_printer_liste():
    try:
        url = f"http://{SERVER_IP}:8000/api/printers?only_selected=1"
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            logging.warning("⚠️ Unerwartetes Printer-API-Format: %s", data)
        else:
            logging.error("❌ Printerliste konnte nicht geladen werden (Status %s)", response.status_code)
    except requests.RequestException as e:
        logging.error("❌ Fehler bei API-Aufruf (Printerliste): %s", e)

    messagebox.showerror(
        "Fehler",
        "❌ Drucker konnten nicht geladen werden. Bitte Verbindung prüfen."
    )
    return []

def starte_scan():
    if not scanner_state["active"]:
        lese_barcode()

def zeige_auswahlansicht():
    global auswahl_frame
    # Andere Views verstecken
    if start_frame:
        start_frame.pack_forget()
    if scanner_frame:
        scanner_frame.pack_forget()
    if typauswahl_frame:
        typauswahl_frame.pack_forget()

    # Frame neu erstellen
    if auswahl_frame:
        auswahl_frame.destroy()
    auswahl_frame = tk.Frame(center_frame, bg="#1e1e1e")
    auswahl_frame.pack(fill="both", expand=True)

    # Layout: Titel (0), In-Drucker-Button (1), Zwei-Button-Zeile (2), Abbrechen (3)
    for r in range(4):
        auswahl_frame.grid_rowconfigure(r, weight=1)
    auswahl_frame.grid_columnconfigure(0, weight=1)

    # Titel
    auswahl_label = tk.Label(
        auswahl_frame,
        text="Was möchtest du tun?",
        font=("Helvetica Neue", 34, "bold"),
        fg="white",
        bg="#1e1e1e"
    )
    auswahl_label.grid(row=0, column=0, pady=10)

    # --- Zeile 1: "In Drucker setzen" (zentriert, allein) ---
    top_row = tk.Frame(auswahl_frame, bg="#1e1e1e")
    top_row.grid(row=1, column=0, pady=(10, 0))

    in_drucker_button = tk.Button(
        top_row,
        text="In Drucker setzen",
        font=("Helvetica Neue", 30, "bold"),
        bg="white", fg="#1e1e1e",
        relief="flat",
        padx=50, pady=25,
        cursor="hand2",
        activebackground="#eeeeee", activeforeground="#1e1e1e",
        # vorerst gleicher Flow wie "Spule bearbeiten"
        command=zeige_scanneransicht_in_drucker
    )
    in_drucker_button.pack(padx=20, pady=10)

    # --- Zeile 2: Zwei Buttons nebeneinander ---
    buttons_row = tk.Frame(auswahl_frame, bg="#1e1e1e")
    buttons_row.grid(row=2, column=0, pady=(10, 0))

    neu_button = tk.Button(
        buttons_row,
        text="Neue Spule hinzufügen",
        font=("Helvetica Neue", 30, "bold"),
        bg="white", fg="#1e1e1e",
        relief="flat",
        padx=50, pady=25,
        cursor="hand2",
        activebackground="#eeeeee", activeforeground="#1e1e1e",
        command=lambda: zeige_druckerhinweis(zeige_neue_spule_typauswahl)
    )
    neu_button.pack(side="left", padx=20, pady=10)

    bearbeiten_button = tk.Button(
        buttons_row,
        text="Spule bearbeiten",
        font=("Helvetica Neue", 30, "bold"),
        bg="white", fg="#1e1e1e",
        relief="flat",
        padx=50, pady=25,
        cursor="hand2",
        activebackground="#eeeeee", activeforeground="#1e1e1e",
        command=zeige_scanneransicht
    )
    bearbeiten_button.pack(side="left", padx=20, pady=10)

    # --- Zeile 3: Abbrechen separat unten ---
    abbrechen_btn = tk.Button(
        auswahl_frame,
        text="❌ Abbrechen",
        font=("Helvetica Neue", 16),
        bg="white", fg="#1e1e1e",
        relief="flat",
        padx=20, pady=10,
        command=zeige_startansicht
    )
    abbrechen_btn.grid(row=3, column=0, pady=(30, 20))

    # Timeout zurück zur Startansicht
    def auswahl_timeout():
        if auswahl_frame and auswahl_frame.winfo_exists():
            zeige_startansicht()
    root.after(60000, auswahl_timeout)

    root.update()



def zeige_filament_details(typ):
    global last_selected_typ
    typ["leergewicht"] = int(typ.get("leergewicht") or 0)
    last_selected_typ = typ
    # Verstecke Typauswahl
    if typauswahl_frame:
        typauswahl_frame.pack_forget()
    # Neues Detail-Frame
    details_frame = tk.Frame(center_frame, bg="#1e1e1e")
    details_frame.pack(expand=True, fill="both")

    # Überschrift
    heading = tk.Label(
        details_frame,
        text="Neue Rolle zu Filamenttyp hinzufügen?",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    heading.pack(pady=20)

    # Daten-Text
    details_text = (
        f"Name: {typ['name']}\n"
        f"Material: {typ['material']}\n"
        f"Farbe: {typ['farbe']}\n"
        f"Durchmesser: {typ['durchmesser']} mm\n"
        f"Hersteller: {typ['hersteller']}"
    )
    details_label = tk.Label(
        details_frame,
        text=details_text,
        font=("Helvetica Neue", 22),
        fg="white", bg="#1e1e1e",
        justify="left"
    )
    details_label.pack(pady=20)

    # Button-Leiste
    button_frame = tk.Frame(details_frame, bg="#1e1e1e")
    button_frame.pack(pady=30)

    tk.Button(
        button_frame,
        text="Anderes Filament wählen",
        font=("Helvetica Neue", 15, "bold"),
        bg="white", fg="#1e1e1e", relief="flat",
        padx=20, pady=10,
        command=zeige_neue_spule_typauswahl
    ).grid(row=0, column=0, padx=10)

    tk.Button(
        button_frame,
        text="Volle Spule hinzufügen",
        font=("Helvetica Neue", 15, "bold"),
        bg="white", fg="#1e1e1e", relief="flat",
        padx=20, pady=10,
        command=lambda: (details_frame.destroy(), finish_add_spool(volle_spule=True))
    ).grid(row=0, column=1, padx=10)

    tk.Button(
        button_frame,
        text="Benutzte Spule hinzufügen",
        font=("Helvetica Neue", 15, "bold"),
        bg="white", fg="#1e1e1e", relief="flat",
        padx=20, pady=10,
        command=lambda: zeige_wiegehinweis(lambda: (details_frame.destroy(), zeige_wiegeansicht_neue_spule()))
    ).grid(row=0, column=2, padx=10)


def loesche_spule(spulen_id, silent=False):
    try:
        response = requests.delete(f"http://{SERVER_IP}:8000/spulen/{spulen_id}", timeout=3)
        if not silent:
            if response.status_code == 200:
                messagebox.showinfo("Erfolg", "✅ Spule wurde gelöscht.")
            else:
                messagebox.showerror("Fehler", f"❌ Löschen fehlgeschlagen: {response.status_code}")
    except Exception as e:
        if not silent:
            messagebox.showerror("Fehler", f"❌ Fehler beim Löschen:\n{e}")
    pass

script_dir = os.path.dirname(os.path.abspath(__file__))
logo_path = os.path.join(script_dir, "logo.png")

root = tk.Tk()
root.attributes('-fullscreen', True)
root.configure(bg="#1e1e1e")
root.title("Spulenstation")

# Sicherheits-Verzögerung, um das Vollbild nochmal durchzusetzen
root.after(200, lambda: root.attributes('-fullscreen', True))

# Bild-Logo laden und anzeigen
logo_image = Image.open(logo_path)
logo_image = logo_image.resize((140, 140))
logo_photo = ImageTk.PhotoImage(logo_image)

logo_label = tk.Label(root, image=logo_photo, bg="#1e1e1e")
setattr(logo_label, "image", logo_photo)
logo_label.pack(anchor="w", padx=40, pady=(5, 5))


center_frame = tk.Frame(root, bg="#1e1e1e")
center_frame.pack(expand=True, fill="both")


zeige_startansicht()

auswahl_frame = tk.Frame(center_frame, bg="#1e1e1e")
auswahl_frame.pack_forget()

auswahl_label = tk.Label(
    auswahl_frame, text="Was möchtest du tun?",
    font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e"
)
auswahl_label.pack(pady=30)

# --- Typauswahl für neue Spule ---
typauswahl_frame = None

def zeige_neue_spule_typauswahl():
    global typauswahl_frame
    # Clear all current views
    for widget in center_frame.winfo_children():
        widget.destroy()

    # Äußere Frame-Struktur für Symmetrie
    typauswahl_frame = tk.Frame(center_frame, bg="#1e1e1e")
    typauswahl_frame.pack(expand=True, fill="both")

    # Headline mittig oben
    headline = tk.Label(typauswahl_frame, text="Wähle Filamenttyp aus", font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e")
    headline.pack(pady=(30, 10))

    # Canvas-basiertes scrollbares Frame, symmetrisch eingebettet
    canvas = tk.Canvas(
        typauswahl_frame,
        bg="#1e1e1e",
        highlightthickness=0,
        width=int(root.winfo_screenwidth() * 0.8)
    )
    scrollbar = tk.Scrollbar(typauswahl_frame, orient="vertical", command=canvas.yview, width=40)
    scrollable_frame = tk.Frame(canvas, bg="#1e1e1e")

    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")
        )
    )

    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    # Symmetrische horizontale Abstände (zentriert die Spalten)
    canvas.pack(side="left", fill="both", expand=True, padx=(60, 60), pady=(10, 50))
    scrollbar.pack(side="right", fill="y", padx=(0, 40))

    # Optional: Touchscreen scroll per MouseWheel support (limited)
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1*(event.delta/120)), "units")
    canvas.bind("<MouseWheel>", _on_mousewheel)

    try:
        import tkinter.font as tkFont
        try:
            response = requests.get(f"http://{SERVER_IP}:8000/typs/", timeout=3)
            if response.status_code == 200:
                typen = response.json()
                # Grid column configure for expanding buttons, equal width and uniform distribution
                scrollable_frame.grid_columnconfigure(0, weight=1, minsize=0, uniform="column")
                scrollable_frame.grid_columnconfigure(1, weight=1, minsize=0, uniform="column")
                for idx, typ in enumerate(typen):
                    max_font_size = 20
                    min_font_size = 5
                    text = f"{typ['name']} ({typ['material']}, {typ['farbe']}, {typ['durchmesser']}mm, {typ['hersteller']})"
                    wrap_length = 400

                    for font_size in range(max_font_size, min_font_size - 1, -1):
                        font = tkFont.Font(family="Helvetica Neue", size=font_size)
                        lines = []
                        current_line = ""
                        for word in text.split():
                            test_line = current_line + " " + word if current_line else word
                            if font.measure(test_line) > wrap_length:
                                lines.append(current_line)
                                current_line = word
                            else:
                                current_line = test_line
                        lines.append(current_line)
                        if len(lines) <= 3:
                            break
                    else:
                        font_size = min_font_size

                    typ_button = tk.Button(
                        scrollable_frame,
                        text=text,
                        font=("Helvetica Neue", font_size),
                        bg="white", fg="#1e1e1e", relief="flat",
                        padx=20, pady=20,
                        anchor="center",
                        wraplength=wrap_length,
                        justify="center",
                        width=25,
                        height=2,
                        command=lambda t=typ: zeige_filament_details(t)
                    )
                    row = idx // 2
                    col = idx % 2
                    typ_button.grid(
                        row=row,
                        column=col,
                        padx=20,
                        pady=20
                    )
            else:
                tk.Label(scrollable_frame, text="❌ Keine Typdaten verfügbar", font=("Helvetica Neue", 20), fg="white", bg="#1e1e1e").pack(pady=10)
        except Exception as e:
            messagebox.showerror("Fehler", f"❌ Fehler beim Laden der Typdaten:\n{e}")
            zeige_auswahlansicht()
            return
    except Exception as e:
        tk.Label(scrollable_frame, text=f"Fehler: {e}", font=("Helvetica Neue", 20), fg="white", bg="#1e1e1e").pack(pady=10)

    def abbrechen_und_zurueck():
        if typauswahl_frame:
            typauswahl_frame.pack_forget()
        zeige_auswahlansicht()

    abbrechen_btn = tk.Button(typauswahl_frame, text="❌ Abbrechen", font=("Helvetica Neue", 16),
                              bg="white", fg="#1e1e1e", relief="flat", padx=20, pady=10,
                              command=abbrechen_und_zurueck)
    # Place the button at the top right
    abbrechen_btn.place(relx=1.0, y=10, anchor="ne")

def zeige_scanneransicht():
    zeige_wiegehinweis(zeige_scanneransicht_fortsetzen)

def zeige_scanneransicht_fortsetzen():
    for widget in center_frame.winfo_children():
        widget.destroy()

    global scanner_frame
    scanner_frame = tk.Frame(center_frame, bg="#1e1e1e")
    scanner_frame.pack(expand=True)

    abbrechen_btn = tk.Button(scanner_frame, text="❌ Abbrechen", font=("Helvetica Neue", 16),
                              bg="white", fg="#1e1e1e", relief="flat", padx=20, pady=10,
                              command=abbrechen)
    abbrechen_btn.pack(side="bottom", pady=40)

    headline = tk.Label(scanner_frame, text="Spule scannen", font=("Helvetica Neue", 34, "bold"), fg="white", bg="#1e1e1e")
    headline.pack(pady=30)

    global kamera_label
    kamera_label = tk.Label(scanner_frame, bg="black", width=600, height=400)
    kamera_label.pack(pady=10)

    global ausgabe_label
    ausgabe_label = tk.Label(scanner_frame, text="", justify="left",
                             font=("Helvetica Neue", 20), fg="white", bg="#1e1e1e", wraplength=1000)
    ausgabe_label.pack(pady=10)

    starte_scan()

def zeige_scanneransicht_in_drucker():
    # Direkte Scanner-Ansicht (ohne Waage-Hinweis)
    for widget in center_frame.winfo_children():
        widget.destroy()

    global scanner_frame
    scanner_frame = tk.Frame(center_frame, bg="#1e1e1e")
    scanner_frame.pack(expand=True)

    abbrechen_btn = tk.Button(
        scanner_frame, text="❌ Abbrechen", font=("Helvetica Neue", 16),
        bg="white", fg="#1e1e1e", relief="flat", padx=20, pady=10,
        command=abbrechen
    )
    abbrechen_btn.pack(side="bottom", pady=40)

    headline = tk.Label(
        scanner_frame,
        text="Spule scannen (In Drucker setzen)",
        font=("Helvetica Neue", 34, "bold"),
        fg="white", bg="#1e1e1e"
    )
    headline.pack(pady=30)

    global kamera_label, ausgabe_label
    kamera_label = tk.Label(scanner_frame, bg="black", width=600, height=400)
    kamera_label.pack(pady=10)

    ausgabe_label = tk.Label(
        scanner_frame, text="📷 Bitte QR-Code scannen...",
        justify="left", font=("Helvetica Neue", 20),
        fg="white", bg="#1e1e1e", wraplength=1000
    )
    ausgabe_label.pack(pady=10)

    # WICHTIG: Spezifischen Handler für diesen Flow setzen
    scanner_state["on_scan"] = verarbeite_qr_code_in_drucker

    starte_scan()


kamera_label = None

ausgabe_label = tk.Label(
    auswahl_frame, text="", justify="left",
    font=("Helvetica Neue", 16), fg="white", bg="#1e1e1e", wraplength=1000
)
ausgabe_label.pack(pady=10)

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class DruckRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/print_qrcode/"):
            spulen_id = self.path.split("/")[-1]
            try:
                spulen_id = int(spulen_id)
                img = generate_qr_code(spulen_id)
                send_to_printer(img)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Fehler: {e}".encode())
        else:
            self.send_response(404)
            self.end_headers()

def starte_http_server():
    server = HTTPServer(("0.0.0.0", 9100), DruckRequestHandler)
    print("🛰️ HTTP-Druckserver läuft auf Port 9100")
    server.serve_forever()

threading.Thread(target=starte_http_server, daemon=True).start()
root.mainloop()
