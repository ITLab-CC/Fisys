import qrcode
from qrcode.image.pil import PilImage
import os

# Zielordner für QR-Codes
QR_DIR = os.path.join(os.path.dirname(__file__), "html", "assets", "images", "qrcodes")
os.makedirs(QR_DIR, exist_ok=True)

def build_qr_filename(spulen_id: int):
    return f"qrcode_spule_id_{spulen_id}.png"

def generate_qrcode_for_spule(spule):
    filename = build_qr_filename(spule.spulen_id)
    filepath = os.path.join(QR_DIR, filename)

    if os.path.exists(filepath):
        return

    qr_data = f"https://fisys.it-lab.cc/spulen.html?spule_id={spule.spulen_id}"
    img = qrcode.make(qr_data, image_factory=PilImage)
    img.save(filepath)
    print(f"📷 QR-Code erstellt für Spule {spule.spulen_id}: {filepath}")

def delete_qrcode_for_spule(spule):
    filename = build_qr_filename(spule.spulen_id)
    filepath = os.path.join(QR_DIR, filename)

    if os.path.exists(filepath):
        os.remove(filepath)
        print(f"🗑️ QR-Code gelöscht für Spule {spule.spulen_id}")