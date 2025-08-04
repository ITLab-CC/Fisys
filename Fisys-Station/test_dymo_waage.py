import time
import struct
import os

HIDRAW_PATH = "/dev/hidraw0"

def read_gewicht():
    try:
        if not os.path.exists(HIDRAW_PATH):
            print("Waage nicht angeschlossen.")
            return

        with open(HIDRAW_PATH, "rb") as f:
            print("Waage geöffnet. Lese Daten...")
            while True:
                data = f.read(6)
                if len(data) < 6:
                    print("Unvollständige Antwort:", data)
                    continue

                # Daten analysieren
                report_id, status, unit, scaling, low, high = struct.unpack("6B", data)
                gewicht = low + (high << 8)

                if unit == 11:
                    einheit = "g"
                elif unit == 12:
                    einheit = "oz"
                else:
                    einheit = f"Unbekannt ({unit})"

                print(f"Gewicht: {gewicht} {einheit}")
                time.sleep(1)
    except PermissionError:
        print(f"Zugriff verweigert auf {HIDRAW_PATH}. Prüfe udev-Regeln & Gruppenrechte.")
    except FileNotFoundError:
        print("Gerät nicht gefunden.")
    except OSError as e:
        print("Fehler beim Zugriff auf Waage:", e)
    except KeyboardInterrupt:
        print("Abbruch durch Benutzer.")

if __name__ == "__main__":
    read_gewicht()