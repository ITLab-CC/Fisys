from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Callable, Optional, Any

from paho.mqtt import client as mqtt

# ----------------------------
# Interner Zustand (keine Persistenz)
# ----------------------------
_mqtt_thread: Optional[threading.Thread] = None
_sender_thread: Optional[threading.Thread] = None
_running = False
_latest_lock = threading.Lock()
_latest_payload: Optional[dict[str, Any]] = None  # nur flüchtig zwischen den Sendungen


# ----------------------------
# Parser (defensiv, gibt ein schlankes Dict für das Dashboard zurück)
# ----------------------------

def _parse_payload(serial: str, raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {"serial": serial, "state": "unknown"}

    pr: dict[str, Any] = {}
    if isinstance(data, dict):
        pr = data.get("print", {}) if isinstance(data.get("print"), dict) else data

    state = (
        pr.get("stage")
        or pr.get("gcode_state")
        or pr.get("print_status")
        or pr.get("state")
        or "unknown"
    )

    percent = pr.get("mc_percent") or pr.get("progress") or pr.get("percent")
    eta_min = pr.get("mc_remaining_time") or pr.get("remain_time") or pr.get("time_remaining")
    job_name = pr.get("subtask_name") or pr.get("task_name")

    # normalize
    try:
        percent = None if percent is None else float(percent)
    except Exception:
        percent = None
    try:
        eta_min = None if eta_min is None else int(eta_min)
    except Exception:
        eta_min = None

    return {
        "serial": serial,
        "state": str(state),
        "percent": percent,
        "eta_min": eta_min,
        "job_name": job_name,
    }


# ----------------------------
# Core: MQTT-Loop + 15s-Sender
# ----------------------------

def _mqtt_loop(ip: str, serial: str, access_code: str):
    global _latest_payload, _running
    client = mqtt.Client()
    client.username_pw_set("bblp", access_code)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)  # optional: ca_certs für Pinning
    topic = f"device/{serial}/report"

    def on_message(_c, _u, m):
        nonlocal serial
        parsed = _parse_payload(serial, m.payload)
        with _latest_lock:
            _latest_payload = parsed

    client.on_message = on_message

    while _running:
        try:
            client.connect(ip, 8883, keepalive=60)
            client.subscribe(topic, qos=0)
            client.loop_forever()
        except Exception:
            if not _running:
                break
            time.sleep(2)
    try:
        client.disconnect()
    except Exception:
        pass


def _sender_loop(on_push: Callable[[dict[str, Any]], None], interval_seconds: int):
    global _latest_payload, _running
    # Schicke alle interval_seconds den *zuletzt gesehenen* Status, wenn vorhanden.
    # Es wird nichts persistiert – nur der flüchtige Snapshot wird genutzt.
    while _running:
        payload = None
        with _latest_lock:
            payload = _latest_payload
        if payload:
            try:
                on_push(payload)
            except Exception:
                pass
        # Sendeintervall
        for _ in range(interval_seconds):
            if not _running:
                return
            time.sleep(1)


# ----------------------------
# Public API
# ----------------------------

def start_printer_service(*, ip: str, serial: str, access_code: str, on_push: Callable[[dict[str, Any]], None], interval_seconds: int = 15) -> None:
    global _mqtt_thread, _sender_thread, _running
    if _running:
        return
    _running = True

    _mqtt_thread = threading.Thread(target=_mqtt_loop, args=(ip, serial, access_code), name="PrinterMQTT", daemon=True)
    _sender_thread = threading.Thread(target=_sender_loop, args=(on_push, interval_seconds), name="PrinterSender", daemon=True)

    _mqtt_thread.start()
    _sender_thread.start()


def stop_printer_service() -> None:
    global _running, _mqtt_thread, _sender_thread
    _running = False
    # threads beenden lassen
    for t in (_mqtt_thread, _sender_thread):
        if t and t.is_alive():
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
    _mqtt_thread = None
    _sender_thread = None