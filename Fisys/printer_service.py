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
# Mehrere Instanzen parallel verwalten (keyed by serial)
_instances: dict[str, dict[str, Any]] = {}


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

    # --- AMS / Filament detection (current loaded slot) ---
    filament_name = None
    filament_tray = None
    try:
        if isinstance(data, dict):
            ams = data.get("ams")
            if isinstance(ams, dict):
                tray_now = ams.get("tray_now")
                trays = ams.get("tray") or ams.get("trays") or []
                if isinstance(tray_now, int) and isinstance(trays, list) and 0 <= tray_now < len(trays):
                    slot = trays[tray_now] or {}
                    filament_tray = tray_now
                    filament_name = (
                        slot.get("name")
                        or slot.get("brand_name")
                        or slot.get("color_name")
                        or slot.get("filament_name")
                    )
    except Exception:
        pass

    return {
        "serial": serial,
        "state": str(state),
        "percent": percent,
        "eta_min": eta_min,
        "job_name": job_name,
        "filament_tray": filament_tray,
        "filament_name": filament_name,
    }


# ----------------------------
# Core: MQTT-Loop + 15s-Sender
# ----------------------------

def _mqtt_loop(serial: str):
    inst = _instances.get(serial)
    if not inst:
        return
    ip = inst.get("ip")
    access_code = inst.get("access_code")
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
        try:
            parsed = _parse_payload(serial, m.payload)
            # _log.info(f"[MQTT] Message on {m.topic}: {parsed}")
            with inst["latest_lock"]:
                # Nur sinnvolle Payloads übernehmen. "unknown" überschreibt keinen vorhandenen guten Snapshot.
                if parsed.get("state") == "unknown" and inst.get("latest_payload") is not None:
                    # _log.debug("[MQTT] Ignoring 'unknown' payload to preserve last valid snapshot")
                    pass
                else:
                    inst["latest_payload"] = parsed
                    inst["last_seen"] = time.time()
                    inst["offline_emitted"] = False
        except Exception as e:
            # _log.exception(f"[MQTT] on_message parse error: {e}")
            pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_log = on_log
    client.on_subscribe = on_subscribe

    # Non-blocking loop and graceful stop
    try:
        client.reconnect_delay_set(min_delay=1, max_delay=5)
    except Exception:
        pass

    inst["client"] = client
    try:
        client.connect_async(ip, 8883, keepalive=60)
    except Exception:
        try:
            client.connect(ip, 8883, keepalive=60)
        except Exception:
            pass
    try:
        client.loop_start()
    except Exception:
        pass

    while inst.get("running"):
        time.sleep(0.2)

    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.loop_stop(force=True)
    except Exception:
        pass


def _sender_loop(serial: str):
    inst = _instances.get(serial)
    if not inst:
        return
    on_push: Callable[[dict[str, Any]], None] = inst.get("on_push")
    interval_seconds: int = inst.get("interval_seconds", 15)
    offline_timeout: int = inst.get("offline_timeout", 30)
    # Schicke alle interval_seconds den *zuletzt gesehenen* Status, wenn vorhanden.
    # Es wird nichts persistiert – nur der flüchtige Snapshot wird genutzt.
    while inst.get("running"):
        payload = None
        last_seen = None
        with inst["latest_lock"]:
            payload = inst.get("latest_payload")
            last_seen = inst.get("last_seen")
        now = time.time()
        if last_seen and offline_timeout and (now - last_seen) > offline_timeout:
            if not inst.get("offline_emitted"):
                try:
                    offline_payload = {
                        "serial": serial,
                        "state": "offline",
                        "offline": True,
                        "printer_name": inst.get("name"),
                    }
                    on_push(offline_payload)
                except Exception:
                    pass
                inst["offline_emitted"] = True
        elif payload:
            try:
                # Anreichern mit Name (falls vorhanden)
                enriched = dict(payload)
                name = inst.get("name")
                if name:
                    enriched.setdefault("printer_name", name)
                    enriched.setdefault("name", name)
                if last_seen:
                    enriched.setdefault("last_seen_ts", last_seen)
                enriched.pop("offline", None)
                on_push(enriched)
                inst["offline_emitted"] = False
            except Exception as e:
                # _log.exception(f"[SENDER] on_push failed: {e}")
                pass
        else:
            # _log.debug("[SENDER] no payload yet")
            pass
        # Sendeintervall
        for _ in range(interval_seconds):
            if not inst.get("running"):
                return
            time.sleep(1)


# ----------------------------
# Public API
# ----------------------------

def start_printer_service(*, ip: str, serial: str, access_code: str, on_push: Callable[[dict[str, Any]], None], interval_seconds: int = 15, name: Optional[str] = None, offline_timeout: int = 30) -> None:
    # Starte (falls nicht vorhanden) eine Instanz je Serial
    if serial in _instances and _instances[serial].get("running"):
        return
    inst = {
        "ip": ip,
        "serial": serial,
        "access_code": access_code,
        "on_push": on_push,
        "interval_seconds": interval_seconds,
        "offline_timeout": max(10, offline_timeout),
        "latest_lock": threading.Lock(),
        "latest_payload": None,
        "last_seen": None,
        "offline_emitted": False,
        "running": True,
        "name": name,
    }
    _instances[serial] = inst

    mqtt_thread = threading.Thread(target=_mqtt_loop, args=(serial,), name=f"PrinterMQTT-{serial}", daemon=True)
    sender_thread = threading.Thread(target=_sender_loop, args=(serial,), name=f"PrinterSender-{serial}", daemon=True)
    inst["mqtt_thread"] = mqtt_thread
    inst["sender_thread"] = sender_thread

    mqtt_thread.start()
    sender_thread.start()


def stop_printer_service() -> None:
    # Stoppt alle laufenden Instanzen (soft) und beendet MQTT-Loops zuverlässig
    for serial, inst in list(_instances.items()):
        inst["running"] = False
        client = inst.get("client")
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop(force=True)
            except Exception:
                pass
    _instances.clear()
