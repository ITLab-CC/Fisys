from __future__ import annotations

import json
import ssl
import threading
import time
import os
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
    # set a deterministic client_id (many brokers/devices require this)
    client = mqtt.Client(client_id=f"{serial}-fisys-{os.getpid()}", clean_session=True)

    # Authentication
    client.username_pw_set("bblp", access_code)

    # TLS — for initial debugging keep handshake permissive; switch to CA-pinned once working
    # To secure later: set cert_reqs=ssl.CERT_REQUIRED and provide ca_certs
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    topic = f"device/{serial}/report"

    def on_connect(_c, _u, flags, rc):
        if rc == 0:
            # _log.info(f"[MQTT] Connected to {ip}:8883 as {serial}")
            try:
                _c.subscribe(topic, qos=0)
                # _log.info(f"[MQTT] Subscribed: {topic}")
            except Exception as e:
                # _log.exception(f"[MQTT] Subscribe failed: {e}")
                pass
        else:
            # _log.error(f"[MQTT] Connect failed rc={rc}")
            pass

    def on_disconnect(_c, _u, rc):
        # _log.warning(f"[MQTT] Disconnected rc={rc}")
        pass

    def on_log(_c, _u, level, buf):
        # paho internal logs; kept at DEBUG verbosity only
        # _log.debug(f"[MQTT] {buf}")
        pass

    def on_subscribe(_c, _u, mid, granted_qos):
        # _log.info(f"[MQTT] Suback mid={mid} qos={granted_qos}")
        pass

    def on_message(_c, _u, m):
        global _latest_payload
        try:
            parsed = _parse_payload(serial, m.payload)
            # _log.info(f"[MQTT] Message on {m.topic}: {parsed}")
            with _latest_lock:
                # Nur sinnvolle Payloads übernehmen. "unknown" überschreibt keinen vorhandenen guten Snapshot.
                if parsed.get("state") == "unknown" and _latest_payload is not None:
                    # _log.debug("[MQTT] Ignoring 'unknown' payload to preserve last valid snapshot")
                    pass
                else:
                    _latest_payload = parsed
        except Exception as e:
            # _log.exception(f"[MQTT] on_message parse error: {e}")
            pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_log = on_log
    client.on_subscribe = on_subscribe

    while _running:
        try:
            # _log.info(f"[MQTT] Connecting to {ip}:8883 ...")
            client.connect(ip, 8883, keepalive=60)
            # retry_first_connection ensures the loop keeps trying if the first connect fails
            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            if not _running:
                break
            # _log.exception(f"[MQTT] loop error: {e}")
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
            except Exception as e:
                # _log.exception(f"[SENDER] on_push failed: {e}")
                pass
        else:
            # _log.debug("[SENDER] no payload yet")
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
    # Threads sind als daemon=True gestartet; kein join, damit Shutdown/Reload nicht blockiert.
    _mqtt_thread = None
    _sender_thread = None