#!/usr/bin/env python3
"""
incubator_ui.py — Interface web distante pour l'incubateur.

    INCUBATOR_PASSWORD=… RELAY_TOKEN=… python3 incubator_ui.py
    → http://127.0.0.1:8765

La carte ESP32 (salon) pousse la télémétrie et tire les ordres.
L'URL caméra (ex. http://IP:3031/video) est configurable : le serveur
proxy le flux si l'URL lui est joignable (LAN via VPN / Tailscale, etc.).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UI_HOST = os.environ.get("UI_HOST", "0.0.0.0")
UI_PORT = int(os.environ.get("UI_PORT", "8765"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent))
CONFIG_PATH = DATA_DIR / "config.json"
OUTPUT_DIR = DATA_DIR / "timelapse_champignons"
METRICS_CSV = OUTPUT_DIR / "metrics.csv"
IMAGE_PREFIX = "champignon"
IMAGE_EXT = ".jpg"
METRICS_FIELDS = (
    "ts_utc",
    "bed_actual_c",
    "bed_target_c",
    "consigne_c",
    "heater_on",
    "incubator_running",
    "vent_mode",
    "vent_on",
    "vent_speed_pct",
    "vent_actual_pct",
    "air_c",
    "humidity_pct",
    "light_on",
    "photo",
)
METRICS_WINDOWS = {
    "5m": 5 * 60,
    "60m": 60 * 60,
    "24h": 24 * 60 * 60,
}
METRICS_SERIES = ("bed_actual_c", "consigne_c", "air_c", "humidity_pct")
METRICS_FLAGS = ("vent_on", "light_on")

AUTH_PASSWORD = os.environ.get("INCUBATOR_PASSWORD", "")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")
_AUTH_KEY = hashlib.sha256(b"mushroom3d|" + AUTH_PASSWORD.encode()).digest()
SESSION_COOKIE = "incubator_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
LINK_STALE_S = 30
COMMAND_LONG_POLL_S = 25
DEFAULT_CAMERA_URL = "http://192.168.1.20:3031/video"
DEFAULT_TARGET_TEMP = 22.0
DEFAULT_PHOTO_INTERVAL_S = 1800
DEFAULT_VENT_SPEED = 60
DEFAULT_VENT_INTERVAL_S = 1800
DEFAULT_VENT_ON_S = 120
DEFAULT_SUN_LAT = 48.8566
DEFAULT_SUN_LON = 2.3522
SUN_UP_ELEVATION_DEG = -0.833

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("incubator_ui")


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_config(patch: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = _load_config()
    data.update(patch)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def load_camera_url() -> str:
    cfg = _load_config()
    url = str(cfg.get("camera_url") or "").strip()
    if url:
        return url
    return (os.environ.get("CAMERA_URL") or DEFAULT_CAMERA_URL).strip()


def parse_camera_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("URL caméra vide")
    parsed = urllib.parse.urlparse(text if "://" in text else f"http://{text}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL caméra invalide (http/https requis)")
    if len(text) > 512:
        raise ValueError("URL caméra trop longue")
    return urllib.parse.urlunparse(parsed)


def _parse_ts_utc(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _float_or_none(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _cfg_float(cfg: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg[key])
    except (KeyError, TypeError, ValueError):
        return default


def _cfg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(cfg[key])
    except (KeyError, TypeError, ValueError):
        return default


def _julian_day(when: datetime) -> float:
    when = when.astimezone(timezone.utc)
    year, month = when.year, when.month
    day = when.day + (when.hour + (when.minute + when.second / 60) / 60) / 24
    if month <= 2:
        year -= 1
        month += 12
    century = year // 100
    return (
        int(365.25 * (year + 4716))
        + int(30.6001 * (month + 1))
        + day
        + (2 - century + century // 4)
        - 1524.5
    )


def solar_elevation_deg(latitude: float, longitude: float, when: datetime) -> float:
    """Élévation du soleil en degrés. Positif : au-dessus de l'horizon géométrique."""
    n = _julian_day(when) - 2451545.0
    mean_lon = math.radians((280.460 + 0.9856474 * n) % 360)
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = (
        mean_lon
        + math.radians(1.915) * math.sin(anomaly)
        + math.radians(0.020) * math.sin(2 * anomaly)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)
    right_asc = math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    gmst = math.radians((280.46061837 + 360.98564736629 * n) % 360)
    hour_angle = gmst + math.radians(longitude) - right_asc
    lat = math.radians(latitude)
    sine = (
        math.sin(lat) * math.sin(declination)
        + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    )
    sine = max(-1.0, min(1.0, sine))
    return math.degrees(math.asin(sine))


def sun_is_up(latitude: float, longitude: float, when: datetime | None = None) -> tuple[bool, float]:
    when = when or datetime.now(timezone.utc)
    elevation = solar_elevation_deg(latitude, longitude, when)
    return elevation >= SUN_UP_ELEVATION_DEG, elevation


def read_metrics_window(window: str = "60m", max_points: int = 480) -> dict[str, Any]:
    """Points CSV dans la fenêtre demandée (5m / 60m / 24h), sous-échantillonnés."""
    key = (window or "60m").strip().lower()
    if key not in METRICS_WINDOWS:
        raise ValueError("Fenêtre invalide (5m, 60m, 24h)")
    span_s = METRICS_WINDOWS[key]
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - span_s
    points: list[dict[str, Any]] = []
    if METRICS_CSV.is_file():
        with METRICS_CSV.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ts = _parse_ts_utc(row.get("ts_utc", ""))
                if ts is None or ts.timestamp() < cutoff:
                    continue
                item: dict[str, Any] = {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
                for field in (*METRICS_SERIES, *METRICS_FLAGS):
                    item[field] = _float_or_none(row.get(field, ""))
                points.append(item)
    if len(points) > max_points and max_points > 1:
        step = len(points) / max_points
        picked: list[dict[str, Any]] = []
        for i in range(max_points - 1):
            start = int(i * step)
            end = max(int((i + 1) * step), start + 1)
            item = dict(points[start])
            chunk = points[start:end]
            for flag in METRICS_FLAGS:
                values = [pt[flag] for pt in chunk if pt.get(flag) is not None]
                if values:
                    item[flag] = 1.0 if any(value >= 0.5 for value in values) else 0.0
            picked.append(item)
        picked.append(dict(points[-1]))
        points = picked
    return {"window": key, "span_s": span_s, "count": len(points), "points": points}


def session_token() -> str:
    return hmac.new(_AUTH_KEY, b"session", hashlib.sha256).hexdigest()


def password_ok(password: str) -> bool:
    given = password.encode()
    expected = AUTH_PASSWORD.encode()
    if len(given) != len(expected):
        return False
    return hmac.compare_digest(given, expected)


def grab_mjpeg_jpeg(url: str, timeout_s: float = 4.0) -> bytes | None:
    """Extrait une frame JPEG d'un flux MJPEG multipart ou d'une image JPEG."""
    req = urllib.request.Request(url, headers={"User-Agent": "MyceliumLab/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "jpeg" in ctype or "jpg" in ctype:
                data = resp.read(2_000_000)
                return data if data[:2] == b"\xff\xd8" else None
            buf = bytearray()
            while len(buf) < 1_500_000:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    if len(buf) > 64_000:
                        del buf[:-2048]
                    continue
                end = buf.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return bytes(buf[start : end + 2])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug("Caméra %s : %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Contrôleur (file d'ordres + télémétrie relais)
# ---------------------------------------------------------------------------


class IncubatorController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._cmd_event = threading.Event()
        self._cam_gate = threading.Lock()
        self._cam_jpeg: bytes | None = None

        self.camera_url = load_camera_url()
        self.connected = False
        self.machine_name = "—"
        self.firmware = "—"
        self.printer_ip = ""
        self.last_error = ""

        cfg = _load_config()
        self.target_temp = float(DEFAULT_TARGET_TEMP)
        self.photo_interval_s = _cfg_int(cfg, "photo_interval_s", DEFAULT_PHOTO_INTERVAL_S)
        if not (10 <= self.photo_interval_s <= 86400):
            self.photo_interval_s = int(DEFAULT_PHOTO_INTERVAL_S)
        self.heater_on = False
        self.incubator_running = False
        self.light_on = False
        self.light_target = False
        self.light_mode = cfg.get("light_mode") if cfg.get("light_mode") in ("manual", "sun") else "manual"
        self.sun_lat = _cfg_float(cfg, "sun_lat", _env_float("SUN_LAT", DEFAULT_SUN_LAT))
        self.sun_lon = _cfg_float(cfg, "sun_lon", _env_float("SUN_LON", DEFAULT_SUN_LON))
        self.sun_elevation = 0.0
        self._last_auto_photo = time.monotonic()
        self._last_light_cmd = 0.0

        self.vent_mode = "off"
        self.vent_on = False
        self.vent_speed = int(DEFAULT_VENT_SPEED)
        self.vent_interval_s = int(DEFAULT_VENT_INTERVAL_S)
        self.vent_duration_s = int(DEFAULT_VENT_ON_S)
        self.vent_actual: float | None = None
        self.vent_remaining_s: int | None = None

        self.bed_actual: float | None = None
        self.bed_target: float | None = None
        self.nozzle_actual: float | None = None
        self.last_photo: str | None = None
        self.photo_count = 0
        self.started_at: float | None = None

        self.bme680: dict[str, Any] = {
            "ok": False,
            "temperature_c": None,
            "humidity_pct": None,
            "pressure_hpa": None,
            "gas_ohms": None,
        }
        self.link_seen_at = 0.0
        self.commands: deque[dict[str, Any]] = deque()
        self._stop = threading.Event()

    def start(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_metrics_csv()
        self._refresh_photo_count()
        threading.Thread(target=self._scheduler, name="incubator-scheduler", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        self._cmd_event.set()

    def _ensure_metrics_csv(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        expected = ",".join(METRICS_FIELDS)
        if METRICS_CSV.is_file() and METRICS_CSV.stat().st_size > 0:
            with METRICS_CSV.open("r", encoding="utf-8", newline="") as fh:
                header = fh.readline().strip()
            if header == expected:
                return
            with METRICS_CSV.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            tmp = METRICS_CSV.with_suffix(".csv.tmp")
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=METRICS_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") or "" for key in METRICS_FIELDS})
            tmp.replace(METRICS_CSV)
            return
        with METRICS_CSV.open("w", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, fieldnames=METRICS_FIELDS).writeheader()

    def _append_metrics(self, *, photo: str | None = None) -> None:
        """Append one CSV row from current state. Caller holds self.lock."""
        try:
            self._ensure_metrics_csv()
            row = {
                "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bed_actual_c": "" if self.bed_actual is None else f"{self.bed_actual:.2f}",
                "bed_target_c": "" if self.bed_target is None else f"{self.bed_target:.2f}",
                "consigne_c": f"{self.target_temp:.2f}",
                "heater_on": int(bool(self.heater_on)),
                "incubator_running": int(bool(self.incubator_running)),
                "vent_mode": self.vent_mode,
                "vent_on": int(bool(self.vent_on)),
                "vent_speed_pct": self.vent_speed,
                "vent_actual_pct": "" if self.vent_actual is None else f"{self.vent_actual:.1f}",
                "air_c": (
                    ""
                    if self.bme680.get("temperature_c") is None
                    else f"{float(self.bme680['temperature_c']):.2f}"
                ),
                "humidity_pct": (
                    ""
                    if self.bme680.get("humidity_pct") is None
                    else f"{float(self.bme680['humidity_pct']):.1f}"
                ),
                "light_on": int(bool(self.light_on)),
                "photo": photo or "",
            }
            with METRICS_CSV.open("a", encoding="utf-8", newline="") as fh:
                csv.DictWriter(fh, fieldnames=METRICS_FIELDS).writerow(row)
        except OSError:
            log.exception("Écriture CSV métriques impossible")

    def _enqueue(self, cmd: dict[str, Any]) -> str:
        cmd_id = cmd.get("id") or f"cmd_{secrets.token_hex(8)}"
        cmd = {**cmd, "id": cmd_id}
        self.commands.append(cmd)
        self._cmd_event.set()
        log.info("Ordre enfilé %s %s", cmd_id, cmd.get("type"))
        return cmd_id

    def status(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            age = (now - self.link_seen_at) if self.link_seen_at else None
            online = age is not None and age < LINK_STALE_S
            return {
                "connected": self.connected and online,
                "printer_ip": self.printer_ip,
                "auth": bool(AUTH_PASSWORD),
                "relay_configured": bool(RELAY_TOKEN),
                "machine_name": self.machine_name,
                "firmware": self.firmware,
                "last_error": self.last_error,
                "target_temp": self.target_temp,
                "photo_interval_s": self.photo_interval_s,
                "photo_next_s": self._photo_next_s(),
                "heater_on": self.heater_on,
                "incubator_running": self.incubator_running,
                "camera_on": bool(self.camera_url),
                "camera_url": self.camera_url,
                "light_on": self.light_on,
                "light_mode": self.light_mode,
                "sun_lat": self.sun_lat,
                "sun_lon": self.sun_lon,
                "sun_elevation": round(self.sun_elevation, 1),
                "sun_up": self.sun_elevation >= SUN_UP_ELEVATION_DEG,
                "vent_mode": self.vent_mode,
                "vent_on": self.vent_on,
                "vent_speed": self.vent_speed,
                "vent_actual": self.vent_actual,
                "vent_interval_s": self.vent_interval_s,
                "vent_duration_s": self.vent_duration_s,
                "vent_remaining_s": self.vent_remaining_s,
                "bed_actual": self.bed_actual,
                "bed_target": self.bed_target,
                "nozzle_actual": self.nozzle_actual,
                "last_photo": self.last_photo,
                "photo_count": self.photo_count,
                "metrics_csv": "/api/metrics.csv",
                "uptime_s": (now - self.started_at) if self.started_at else 0,
                "stream_proxy": "/api/camera.jpg",
                "bme680": dict(self.bme680),
                "link": {
                    "online": online,
                    "age_s": None if age is None else round(age, 1),
                    "pending_commands": len(self.commands),
                },
            }

    def set_camera_url(self, raw: str) -> dict[str, Any]:
        url = parse_camera_url(raw)
        with self.lock:
            self.camera_url = url
            _save_config({"camera_url": url})
            self._enqueue({"type": "camera_url", "url": url})
            self.last_error = ""
        return self.status()

    def ingest_telemetry(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.link_seen_at = time.time()
            printer = body.get("printer") if isinstance(body.get("printer"), dict) else {}
            bme = body.get("bme680") if isinstance(body.get("bme680"), dict) else {}
            desired = body.get("desired") if isinstance(body.get("desired"), dict) else {}

            if "connected" in printer:
                self.connected = bool(printer.get("connected"))
            if printer.get("ip"):
                self.printer_ip = str(printer["ip"])
            if printer.get("machine_name"):
                self.machine_name = str(printer["machine_name"])
            if printer.get("firmware"):
                self.firmware = str(printer["firmware"])
            if printer.get("bed_actual") is not None:
                self.bed_actual = float(printer["bed_actual"])
            if printer.get("bed_target") is not None:
                self.bed_target = float(printer["bed_target"])
            if printer.get("nozzle_actual") is not None:
                self.nozzle_actual = float(printer["nozzle_actual"])
            if "light_on" in printer:
                self.light_on = bool(printer["light_on"])
            if printer.get("box_fan") is not None:
                self.vent_actual = float(printer["box_fan"])
            cam = str(printer.get("camera_url") or "").strip()
            if cam and not _load_config().get("camera_url"):
                # Suggestion relais tant que l'UI n'a pas forcé une URL
                pass

            self.bme680 = {
                "ok": bool(bme.get("ok")),
                "temperature_c": bme.get("temperature_c"),
                "humidity_pct": bme.get("humidity_pct"),
                "pressure_hpa": bme.get("pressure_hpa"),
                "gas_ohms": bme.get("gas_ohms"),
            }

            if "heater_on" in desired:
                self.heater_on = bool(desired["heater_on"])
            if desired.get("target_temp") is not None:
                self.target_temp = float(desired["target_temp"])
            if desired.get("vent_mode") in ("off", "manual", "cycle"):
                self.vent_mode = str(desired["vent_mode"])
            if "vent_on" in desired:
                self.vent_on = bool(desired["vent_on"])
            if desired.get("vent_speed") is not None:
                self.vent_speed = int(desired["vent_speed"])
            if desired.get("vent_interval_s") is not None:
                self.vent_interval_s = int(desired["vent_interval_s"])
            if desired.get("vent_duration_s") is not None:
                self.vent_duration_s = int(desired["vent_duration_s"])
            if desired.get("vent_remaining_s") is not None:
                self.vent_remaining_s = int(desired["vent_remaining_s"])
            if "light_on" in desired:
                self.light_on = bool(desired["light_on"])

            self._append_metrics()
            return {"ok": True}

    def pop_commands(self, wait_s: float = COMMAND_LONG_POLL_S) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            with self.lock:
                if self.commands:
                    out = list(self.commands)
                    self.commands.clear()
                    self._cmd_event.clear()
                    return out
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._stop.is_set():
                return []
            self._cmd_event.wait(timeout=min(remaining, 1.0))
            self._cmd_event.clear()

    def ack_commands(self, results: list[Any]) -> dict[str, Any]:
        with self.lock:
            for item in results:
                if not isinstance(item, dict):
                    continue
                if item.get("ok") is False and item.get("error"):
                    self.last_error = str(item["error"])
                    log.warning("Ack échec %s : %s", item.get("id"), item.get("error"))
                elif item.get("ok"):
                    if self.last_error:
                        self.last_error = ""
            return {"ok": True}

    def set_temperature(self, temp_c: float, keep_on: bool = True) -> dict[str, Any]:
        with self.lock:
            temp_c = float(temp_c)
            if not (0 <= temp_c <= 110):
                self.last_error = "Température hors plage 0–110 °C"
                return self.status()
            self.target_temp = temp_c
            self.heater_on = keep_on and temp_c > 0
            self._enqueue({"type": "heater_set", "temp": temp_c})
            self.last_error = ""
            return self.status()

    def heater_off(self) -> dict[str, Any]:
        with self.lock:
            self.incubator_running = False
            self.heater_on = False
            self.vent_mode = "off"
            self.vent_on = False
            self._enqueue({"type": "heater_off"})
            self._enqueue(
                {
                    "type": "vent",
                    "mode": "off",
                    "speed": self.vent_speed,
                    "interval_s": self.vent_interval_s,
                    "duration_s": self.vent_duration_s,
                }
            )
            return self.status()

    def set_ventilation(
        self,
        mode: str,
        speed: int | None = None,
        interval_s: int | None = None,
        duration_s: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            mode = str(mode or "off")
            if mode not in ("off", "manual", "cycle"):
                self.last_error = "Mode ventilation inconnu"
                return self.status()
            new_speed = self.vent_speed if speed is None else int(speed)
            new_interval = self.vent_interval_s if interval_s is None else max(30, int(interval_s))
            new_duration = self.vent_duration_s if duration_s is None else max(5, int(duration_s))
            if not (0 <= new_speed <= 100):
                self.last_error = "Vitesse hors plage 0–100 %"
                return self.status()
            if mode == "cycle" and new_duration >= new_interval:
                self.last_error = "La durée de ventilation doit être plus courte que l'intervalle"
                return self.status()
            if mode == "manual" and new_speed <= 0:
                mode = "off"
            self.vent_speed = new_speed
            self.vent_interval_s = new_interval
            self.vent_duration_s = new_duration
            self.vent_mode = mode
            self.vent_on = mode in ("manual", "cycle")
            self._enqueue(
                {
                    "type": "vent",
                    "mode": mode,
                    "speed": new_speed,
                    "interval_s": new_interval,
                    "duration_s": new_duration,
                }
            )
            self.last_error = ""
            self._append_metrics()
            return self.status()

    def _assign_photo_interval(self, interval_s: int) -> str | None:
        """Caller holds self.lock. Returns an error message, or None."""
        try:
            interval_s = int(interval_s)
        except (TypeError, ValueError):
            return "Intervalle photo invalide"
        if not (10 <= interval_s <= 86400):
            return "Intervalle photo hors plage 10 s – 24 h"
        self.photo_interval_s = interval_s
        _save_config({"photo_interval_s": interval_s})
        return None

    def set_photo_interval(self, interval_s: int) -> dict[str, Any]:
        with self.lock:
            error = self._assign_photo_interval(interval_s)
            if error:
                self.last_error = error
                return self.status()
            self.last_error = ""
            return self.status()

    def start_incubator(self, temp_c: float | None = None, interval_s: int | None = None) -> dict[str, Any]:
        with self.lock:
            if temp_c is not None:
                self.target_temp = float(temp_c)
            if interval_s is not None:
                error = self._assign_photo_interval(interval_s)
                if error:
                    self.last_error = error
                    return self.status()
            self.heater_on = True
            self.incubator_running = True
            self.started_at = time.time()
            self._last_auto_photo = time.monotonic() - self.photo_interval_s
            self._enqueue({"type": "incubator_start", "temp": self.target_temp})
            self.last_error = ""
            return self.status()

    def stop_incubator(self, turn_off_heat: bool = True) -> dict[str, Any]:
        with self.lock:
            self.incubator_running = False
            if turn_off_heat:
                self.heater_on = False
            self._enqueue({"type": "incubator_stop", "turn_off_heat": bool(turn_off_heat)})
            return self.status()

    def _photo_next_s(self) -> int | None:
        """Caller holds self.lock."""
        if not self.incubator_running:
            return None
        remain = self.photo_interval_s - (time.monotonic() - self._last_auto_photo)
        return max(0, int(remain))

    def _assign_sun_place(self, lat: float, lon: float) -> str | None:
        """Caller holds self.lock."""
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return "Latitude ou longitude invalide"
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return "Latitude hors −90…90 ou longitude hors −180…180"
        self.sun_lat = lat
        self.sun_lon = lon
        _save_config({"sun_lat": lat, "sun_lon": lon})
        up, elevation = sun_is_up(lat, lon)
        self.sun_elevation = elevation
        return None

    def _queue_light(self, on: bool) -> None:
        """Enqueue a light order unless the same one is already waiting. Caller holds self.lock."""
        on = bool(on)
        kept = deque(
            cmd for cmd in self.commands if cmd.get("type") != "light" or bool(cmd.get("on")) == on
        )
        self.commands = kept
        if any(cmd.get("type") == "light" for cmd in self.commands):
            return
        self._last_light_cmd = time.monotonic()
        self._enqueue({"type": "light", "on": on})

    def _ensure_light(self, on: bool, *, force: bool = False) -> None:
        """Hold the lamp on the target. Caller holds self.lock.

        A single order is easy to lose, and the relay then reports the old state.
        Retry quickly while the reported lamp differs, and refresh every 30 s so
        the printer does not drop the LED.
        """
        on = bool(on)
        changed = on != self.light_target
        self.light_target = on
        pending_same = any(
            cmd.get("type") == "light" and bool(cmd.get("on")) == on for cmd in self.commands
        )
        if pending_same and not force:
            return
        elapsed = time.monotonic() - self._last_light_cmd
        mismatch = self.light_on != on
        if force or changed or (mismatch and elapsed >= 5) or elapsed >= 30:
            self._queue_light(on)

    def set_light(
        self,
        on: bool | None = None,
        *,
        mode: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if lat is not None or lon is not None:
                error = self._assign_sun_place(
                    self.sun_lat if lat is None else lat,
                    self.sun_lon if lon is None else lon,
                )
                if error:
                    self.last_error = error
                    return self.status()
            if mode in ("manual", "sun"):
                self.light_mode = mode
                _save_config({"light_mode": mode})
            elif mode is not None:
                self.last_error = "Mode lumière inconnu"
                return self.status()
            if on is None and self.light_mode == "sun":
                up, elevation = sun_is_up(self.sun_lat, self.sun_lon)
                self.sun_elevation = elevation
                on = up
            if on is None:
                self.last_error = ""
                return self.status()
            if self.light_mode != "sun":
                self.light_on = bool(on)
            self._ensure_light(bool(on), force=True)
            self.last_error = ""
            return self.status()

    def _scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_sun()
                self._tick_photos()
            except Exception:
                log.exception("Scheduler")
            self._stop.wait(1.0)

    def _tick_sun(self) -> None:
        up, elevation = sun_is_up(self.sun_lat, self.sun_lon)
        with self.lock:
            self.sun_elevation = elevation
            if self.light_mode != "sun":
                return
            self._ensure_light(up)

    def _tick_photos(self) -> None:
        with self.lock:
            if not self.incubator_running:
                return
            if time.monotonic() - self._last_auto_photo < self.photo_interval_s:
                return
            self._last_auto_photo = time.monotonic()
        result = self.take_snapshot()
        if not result.get("ok"):
            with self.lock:
                retry = min(30, self.photo_interval_s)
                self._last_auto_photo = time.monotonic() - self.photo_interval_s + retry

    def camera_enable(self) -> dict[str, Any]:
        # Activation locale côté carte via SDCP ; le preview passe par camera_url.
        with self.lock:
            if not self.camera_url:
                self.last_error = "URL caméra non configurée"
            else:
                self.last_error = ""
            return self.status()

    def camera_disable(self) -> dict[str, Any]:
        return self.status()

    def take_snapshot(self) -> dict[str, Any]:
        data = self.grab_jpeg_bytes()
        if not data:
            return {"ok": False, "error": self.last_error or "Caméra indisponible"}
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        idx = self._next_image_index()
        name = f"{IMAGE_PREFIX}_{idx:04d}{IMAGE_EXT}"
        path = OUTPUT_DIR / name
        path.write_bytes(data)
        with self.lock:
            self.last_photo = name
            self._refresh_photo_count()
            self._append_metrics(photo=name)
        return {"ok": True, "file": name, "url": f"/photos/{name}"}

    def grab_jpeg_bytes(self) -> bytes | None:
        if not self._cam_gate.acquire(blocking=False):
            return self._cam_jpeg
        try:
            with self.lock:
                url = self.camera_url
            if not url:
                return self._cam_jpeg
            frame = grab_mjpeg_jpeg(url)
            if frame:
                self._cam_jpeg = frame
            else:
                with self.lock:
                    self.last_error = f"Caméra injoignable : {url}"
            return self._cam_jpeg
        finally:
            self._cam_gate.release()

    def list_photos(self) -> list[dict[str, Any]]:
        files = sorted(OUTPUT_DIR.glob(f"{IMAGE_PREFIX}_*{IMAGE_EXT}"), reverse=True)
        out = []
        for p in files[:60]:
            st = p.stat()
            out.append(
                {
                    "name": p.name,
                    "url": f"/photos/{p.name}",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        return out

    def _refresh_photo_count(self) -> None:
        self.photo_count = len(list(OUTPUT_DIR.glob(f"{IMAGE_PREFIX}_*{IMAGE_EXT}")))

    def _next_image_index(self) -> int:
        best = 0
        for p in OUTPUT_DIR.glob(f"{IMAGE_PREFIX}_*{IMAGE_EXT}"):
            stem = p.stem
            try:
                best = max(best, int(stem.rsplit("_", 1)[-1]))
            except ValueError:
                continue
        return best + 1


CTRL = IncubatorController()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Mycelium Lab — Incubateur</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@460;560;650&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #090c0f;
    --card: #13181d;
    --line: rgba(255,255,255,.08);
    --text: #f3f6f4;
    --muted: #8d9893;
    --accent: #3ddc97;
    --accent-ink: #062117;
    --danger: #ff6b5a;
    --ok: #3ddc97;
    --warn: #e6b35a;
    --radius: 18px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: Outfit, system-ui, sans-serif;
    color: var(--text);
    background:
      radial-gradient(900px 420px at 0% -10%, rgba(61,220,151,.12), transparent 55%),
      var(--bg);
  }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 22px 18px 48px; }
  header {
    display: flex; flex-wrap: wrap; align-items: center;
    justify-content: space-between; gap: 14px; margin-bottom: 18px;
  }
  .brand { font-size: 1.35rem; font-weight: 650; letter-spacing: -0.03em; margin: 0; }
  .brand span { color: var(--accent); }
  .sub { color: var(--muted); margin: 4px 0 0; font-size: .86rem; }
  .pills { display: flex; flex-wrap: wrap; gap: 8px; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 7px 12px; border: 1px solid var(--line); border-radius: 999px;
    background: var(--card); font-size: .84rem;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); }
  .dot.ok { background: var(--ok); }
  .dot.bad { background: var(--danger); }
  .layout {
    display: grid; grid-template-columns: 1.45fr .9fr; gap: 14px; align-items: start;
  }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
  .stack { display: grid; gap: 14px; }
  .panel {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); overflow: hidden;
  }
  .panel h2 {
    font-size: .78rem; font-weight: 560; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted);
    margin: 0; padding: 16px 16px 0;
  }
  .panel .body { padding: 12px 16px 16px; }
  .cam-frame { position: relative; background: #07090b; aspect-ratio: 16/10; }
  .cam-frame img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cam-meta {
    position: absolute; left: 12px; bottom: 12px;
    padding: 5px 10px; border-radius: 999px;
    background: rgba(9,12,15,.72); border: 1px solid var(--line);
    font-size: .75rem; color: var(--text); backdrop-filter: blur(8px);
  }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 12px; }
  @media (max-width: 700px) { .stats { grid-template-columns: repeat(2, 1fr); } }
  .stat { padding: 10px 12px; border-radius: 12px; background: rgba(255,255,255,.03); }
  .stat .label { color: var(--muted); font-size: .72rem; letter-spacing: .04em; text-transform: uppercase; }
  .stat .value { font-size: 1.45rem; font-weight: 650; letter-spacing: -0.03em; margin-top: 2px; }
  .stat .value small { font-size: .78rem; color: var(--muted); font-weight: 460; }
  label { display: block; font-size: .78rem; color: var(--muted); margin-bottom: 6px; }
  .row { display: flex; gap: 10px; align-items: end; }
  .row > div { flex: 1 1 0; min-width: 0; }
  input[type=number], input[type=text], input[type=password], input[type=url] {
    width: 100%; padding: 11px 12px; border-radius: 12px;
    border: 1px solid var(--line); background: #0c1014; color: var(--text); font: inherit;
  }
  input:focus { outline: 2px solid rgba(61,220,151,.45); border-color: transparent; }
  button {
    font: inherit; font-weight: 560; border: 0; border-radius: 12px;
    padding: 10px 14px; cursor: pointer;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .btn-primary, .btn-gold { background: var(--accent); color: var(--accent-ink); }
  .btn-ghost { background: transparent; color: var(--text); border: 1px solid var(--line); }
  .btn-danger { background: rgba(255,107,90,.16); color: #ffd0ca; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .actions.split { display: grid; grid-template-columns: 1fr 1fr; }
  .hint { color: var(--muted); font-size: .8rem; margin: 10px 0 0; line-height: 1.45; }
  .err {
    margin-top: 10px; padding: 10px 12px; border-radius: 12px;
    background: rgba(255,107,90,.12); color: #ffd0ca; font-size: .84rem; display: none;
  }
  .err.show { display: block; }
  .gallery {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr));
    gap: 8px; max-height: 240px; overflow: auto;
  }
  .gallery a { display: block; border-radius: 12px; overflow: hidden; background: #000; }
  .gallery img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }
  .seg { display: inline-flex; flex-wrap: wrap; gap: 6px; }
  .seg button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
  .chart-wrap {
    position: relative; width: 100%; height: 320px; margin-top: 8px;
    border-radius: 12px; background: rgba(255,255,255,.03); border: 1px solid var(--line);
  }
  .chart-wrap canvas { width: 100%; height: 100%; display: block; }
  .chart-legend {
    display: flex; flex-wrap: wrap; gap: 12px 16px; margin-top: 10px;
    font-size: .78rem; color: var(--muted);
  }
  .chart-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  footer { margin-top: 18px; color: var(--muted); font-size: .75rem; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1 class="brand">Mycelium <span>Lab</span></h1>
        <p class="sub" id="identity">En attente du relais…</p>
      </div>
      <div class="pills">
        <div class="pill"><span class="dot" id="dotLink"></span><span id="linkLabel">Relais</span></div>
        <div class="pill"><span class="dot" id="dot"></span><span id="connLabel">—</span></div>
      </div>
    </header>

    <div class="layout">
      <section class="panel">
        <div class="cam-frame">
          <img id="cam" alt="Caméra incubateur" src="/api/camera.jpg" />
          <div class="cam-meta" id="camMeta">Flux</div>
        </div>
        <div class="body">
          <div class="stats">
            <div class="stat">
              <div class="label">Plateau</div>
              <div class="value" id="bedActual">—<small> °C</small></div>
            </div>
            <div class="stat">
              <div class="label">Consigne</div>
              <div class="value" id="bedTarget">—<small> °C</small></div>
            </div>
            <div class="stat">
              <div class="label">Air BME</div>
              <div class="value" id="bmeTemp">—<small> °C</small></div>
            </div>
            <div class="stat">
              <div class="label">Humidité</div>
              <div class="value" id="bmeHum">—<small> %</small></div>
            </div>
          </div>
          <div class="stats" style="margin-top:8px">
            <div class="stat">
              <div class="label">Photos</div>
              <div class="value" id="photoCount">0</div>
            </div>
            <div class="stat">
              <div class="label">Air box</div>
              <div class="value" id="ventActual">—<small> %</small></div>
            </div>
          </div>
          <div class="actions">
            <button class="btn-gold" id="btnLight">Flash</button>
            <button class="btn-ghost" id="btnSnap">Photo</button>
            <button class="btn-ghost" id="btnRefresh">Rafraîchir</button>
          </div>
          <div class="actions">
            <div class="seg" id="lightMode" role="group" aria-label="Mode du flash">
              <button type="button" class="btn-ghost" data-mode="manual" aria-pressed="true">Manuel</button>
              <button type="button" class="btn-ghost" data-mode="sun">Soleil</button>
            </div>
            <span class="hint" id="sunState" style="margin:0">—</span>
          </div>
          <div class="row">
            <div>
              <label for="sunLat">Latitude</label>
              <input id="sunLat" type="number" min="-90" max="90" step="0.01" value="48.86" />
            </div>
            <div>
              <label for="sunLon">Longitude</label>
              <input id="sunLon" type="number" min="-180" max="180" step="0.01" value="2.35" />
            </div>
          </div>
          <p class="hint">Soleil allume le flash tant que le soleil est levé à ces coordonnées. Défaut : Paris.</p>
        </div>
      </section>

      <div class="stack">
        <section class="panel">
          <h2>Caméra</h2>
          <div class="body">
            <label for="cameraUrl">URL du flux (MJPEG)</label>
            <input id="cameraUrl" type="url" placeholder="http://192.168.1.20:3031/video" autocomplete="off" spellcheck="false" />
            <div class="actions">
              <button class="btn-gold" id="btnCameraUrl" type="button">Enregistrer</button>
              <button class="btn-ghost" id="btnLogout" type="button" hidden>Sortir</button>
            </div>
            <p class="hint">Doit être joignable depuis le serveur (VPN / Tailscale). Exemple Centauri : port 3031.</p>
          </div>
        </section>

        <section class="panel">
          <h2>Chaleur</h2>
          <div class="body">
            <div class="row">
              <div>
                <label for="temp">Plateau °C</label>
                <input id="temp" type="number" min="0" max="110" step="0.5" value="22" />
              </div>
              <div>
                <label for="interval">Entre photos (s)</label>
                <input id="interval" type="number" min="10" step="10" value="1800" />
              </div>
            </div>
            <div class="actions split">
              <button class="btn-gold" id="btnSetTemp">Appliquer</button>
              <button class="btn-primary" id="btnStart">Démarrer</button>
              <button class="btn-ghost" id="btnStopKeep">Pause</button>
              <button class="btn-danger" id="btnOff">Éteindre</button>
            </div>
            <p class="hint" id="runState">Veille</p>
            <div class="err" id="err"></div>
          </div>
        </section>

        <section class="panel">
          <h2>Air</h2>
          <div class="body">
            <div class="row">
              <div>
                <label for="ventSpeed">%</label>
                <input id="ventSpeed" type="number" min="0" max="100" step="5" value="60" />
              </div>
              <div>
                <label for="ventInterval">Tous les s</label>
                <input id="ventInterval" type="number" min="30" step="30" value="1800" />
              </div>
              <div>
                <label for="ventDuration">Pendant s</label>
                <input id="ventDuration" type="number" min="5" step="5" value="120" />
              </div>
            </div>
            <div class="actions">
              <button class="btn-gold" id="btnVentManual">Manuel</button>
              <button class="btn-primary" id="btnVentCycle">Cycle</button>
              <button class="btn-ghost" id="btnVentOff">Stop</button>
            </div>
            <p class="hint" id="ventState">Arrêt</p>
          </div>
        </section>
      </div>
    </div>

    <section class="panel" style="margin-top:14px">
      <h2>Historique</h2>
      <div class="body">
        <div class="actions" style="margin-top:0;justify-content:space-between;align-items:center">
          <div class="seg" id="chartWindow" role="group" aria-label="Fenêtre">
            <button type="button" class="btn-ghost" data-window="5m">5 min</button>
            <button type="button" class="btn-ghost" data-window="60m" aria-pressed="true">60 min</button>
            <button type="button" class="btn-ghost" data-window="24h">24 h</button>
          </div>
          <span class="hint" id="chartMeta" style="margin:0">—</span>
        </div>
        <div class="chart-wrap"><canvas id="metricsChart" width="1100" height="260"></canvas></div>
        <div class="chart-legend">
          <span><i class="swatch" style="background:#3ddc97"></i>Plateau</span>
          <span><i class="swatch" style="background:#e6b35a"></i>Consigne</span>
          <span><i class="swatch" style="background:#6ec8ff"></i>Air BME</span>
          <span><i class="swatch" style="background:#c9a0ff"></i>Humidité %</span>
          <span><i class="swatch" style="background:#5ee0c2"></i>Ventilation</span>
          <span><i class="swatch" style="background:#ffd07a"></i>Flash</span>
        </div>
      </div>
    </section>

    <section class="panel" style="margin-top:14px">
      <h2>Time-lapse</h2>
      <div class="body">
        <p class="hint" style="margin-top:0">Photos + journal CSV (températures, consigne, ventilation, flash) à chaque télémétrie.</p>
        <div class="actions" style="margin-top:0;margin-bottom:10px">
          <button type="button" class="btn-gold" id="btnMetrics">Télécharger metrics.csv</button>
        </div>
        <div class="gallery" id="gallery"></div>
      </div>
    </section>

    <footer>Relais ESP32 · Centauri Carbon · BME680</footer>
  </div>

<script>
const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("Authentification requise");
  }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function setIfIdle(id, value) {
  const el = $(id);
  if (!el || document.activeElement === el || el.dataset.dirty === "1") return;
  el.value = value ?? "";
}
function watchDirty(id) {
  const el = $(id);
  if (el) el.addEventListener("input", () => { el.dataset.dirty = "1"; });
}
["interval", "sunLat", "sunLon"].forEach(watchDirty);

function fmtRemain(s) {
  if (s == null) return "—";
  const n = Math.max(0, Math.round(s));
  const m = Math.floor(n / 60);
  const r = n % 60;
  return m > 0 ? `${m} min ${String(r).padStart(2, "0")} s` : `${r} s`;
}

function fmtNum(v, digits=1) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

function applyStatus(s) {
  const linkOnline = !!(s.link && s.link.online);
  $("identity").textContent = `${s.machine_name} · FW ${s.firmware} · ${s.printer_ip || "—"}`;
  $("dot").className = "dot " + (s.connected ? "ok" : "bad");
  $("connLabel").textContent = s.connected
    ? (s.incubator_running ? "Incubateur actif" : (s.heater_on ? "Chauffe manuelle" : "Imprimante OK"))
    : "Imprimante absente";
  $("dotLink").className = "dot " + (linkOnline ? "ok" : "bad");
  $("linkLabel").textContent = linkOnline
    ? `Relais · ${fmtRemain(s.link.age_s)}`
    : (s.relay_configured ? "Relais hors ligne" : "RELAY_TOKEN manquant");

  $("bedActual").innerHTML = (s.bed_actual ?? "—") + "<small> °C</small>";
  $("bedTarget").innerHTML = (s.target_temp ?? "—") + "<small> °C</small>";
  $("photoCount").textContent = s.photo_count ?? 0;
  $("ventActual").innerHTML = (s.vent_actual != null ? Math.round(s.vent_actual) : "—") + "<small> %</small>";

  const b = s.bme680 || {};
  $("bmeTemp").innerHTML = fmtNum(b.temperature_c) + "<small> °C</small>";
  $("bmeHum").innerHTML = fmtNum(b.humidity_pct, 0) + "<small> %</small>";

  setIfIdle("temp", s.target_temp);
  setIfIdle("interval", s.photo_interval_s);
  setIfIdle("ventSpeed", s.vent_speed);
  setIfIdle("ventInterval", s.vent_interval_s);
  setIfIdle("ventDuration", s.vent_duration_s);
  setIfIdle("cameraUrl", s.camera_url);
  $("camMeta").textContent = s.camera_url ? "Flux configuré" : "Pas d'URL";
  $("btnLogout").hidden = !s.auth;

  const err = $("err");
  if (s.last_error) { err.textContent = s.last_error; err.classList.add("show"); }
  else err.classList.remove("show");

  $("runState").textContent = s.incubator_running
    ? `État : incubateur ON · photo / ${s.photo_interval_s} s · prochaine ${fmtRemain(s.photo_next_s)}`
    : (s.heater_on ? "État : chauffe manuelle" : "État : veille");

  const lightBtn = $("btnLight");
  lightBtn.textContent = s.light_on ? "Flash off" : "Flash";
  lightBtn.className = s.light_on ? "btn-ghost" : "btn-gold";
  lightBtn.dataset.on = s.light_on ? "1" : "0";
  const sunEl = Number(s.sun_elevation);
  const lamp = s.light_on ? "allumé" : "éteint";
  $("sunState").textContent = Number.isFinite(sunEl)
    ? (s.light_mode === "sun"
      ? `${s.sun_up ? "Jour" : "Nuit"} · soleil ${sunEl.toFixed(0)}° · flash ${lamp}`
      : `${s.sun_up ? "Jour" : "Nuit"} · soleil ${sunEl.toFixed(0)}°`)
    : "—";
  for (const b of $("lightMode").querySelectorAll("button")) {
    b.setAttribute("aria-pressed", b.dataset.mode === (s.light_mode || "manual") ? "true" : "false");
  }
  setIfIdle("sunLat", s.sun_lat);
  setIfIdle("sunLon", s.sun_lon);

  const manualBtn = $("btnVentManual");
  const cycleBtn = $("btnVentCycle");
  manualBtn.className = s.vent_mode === "manual" ? "btn-gold" : "btn-ghost";
  cycleBtn.className = s.vent_mode === "cycle" ? "btn-primary" : "btn-ghost";
  cycleBtn.textContent = s.vent_mode === "cycle" ? "En cycle" : "Cycle";
  let ventTxt = "Ventilation : arrêt";
  if (s.vent_mode === "manual" && s.vent_on) {
    ventTxt = `Ventilation : manuelle ${s.vent_speed} %`;
  } else if (s.vent_mode === "cycle") {
    ventTxt = s.vent_on
      ? `Ventilation : cycle en marche · encore ${fmtRemain(s.vent_remaining_s)}`
      : `Ventilation : cycle en pause · prochain dans ${fmtRemain(s.vent_remaining_s)}`;
  }
  if (s.vent_actual != null) ventTxt += ` · mesuré ${Math.round(s.vent_actual)} %`;
  $("ventState").textContent = ventTxt;
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    applyStatus(s);
  } catch (e) {
    $("err").textContent = String(e.message || e);
    $("err").classList.add("show");
  }
}

async function refreshGallery() {
  const data = await api("/api/photos");
  const g = $("gallery");
  g.innerHTML = data.photos.map(p =>
    `<a href="${p.url}" target="_blank" title="${p.name}"><img src="${p.url}" alt="${p.name}" loading="lazy" /></a>`
  ).join("") || "<p class='hint'>Aucune photo pour l'instant.</p>";
}

$("btnMetrics").onclick = async () => {
  const r = await fetch("/api/metrics.csv", { credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login"; return; }
  if (!r.ok) {
    $("err").textContent = "CSV indisponible";
    $("err").classList.add("show");
    return;
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "metrics.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

let chartWindow = "60m";
const SERIES = [
  { key: "bed_actual_c", color: "#3ddc97", axis: "temp" },
  { key: "consigne_c", color: "#e6b35a", axis: "temp" },
  { key: "air_c", color: "#6ec8ff", axis: "temp" },
  { key: "humidity_pct", color: "#c9a0ff", axis: "hum" },
];
const LANES = [
  { key: "vent_on", color: "rgba(94, 224, 194, .9)", label: "Vent" },
  { key: "light_on", color: "rgba(255, 208, 122, .95)", label: "Flash" },
];

function niceRange(min, max, pad = 0.08) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1 };
  if (min === max) { min -= 1; max += 1; }
  const span = max - min || 1;
  return { min: min - span * pad, max: max + span * pad };
}

function drawMetricsChart(points) {
  const canvas = $("metricsChart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1100;
  const cssH = canvas.clientHeight || 320;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const laneH = 12;
  const laneGap = 4;
  const pad = { l: 48, r: 44, t: 16, b: 22 + LANES.length * (laneH + laneGap) };
  const w = cssW - pad.l - pad.r;
  const h = cssH - pad.t - pad.b;

  ctx.fillStyle = "rgba(255,255,255,.03)";
  ctx.fillRect(0, 0, cssW, cssH);

  if (!points.length) {
    ctx.fillStyle = "#8d9893";
    ctx.font = "14px Outfit, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Pas encore de données sur cette fenêtre", cssW / 2, cssH / 2);
    return;
  }

  const temps = [];
  const hums = [];
  for (const p of points) {
    for (const s of SERIES) {
      const v = p[s.key];
      if (v == null) continue;
      (s.axis === "temp" ? temps : hums).push(v);
    }
  }
  const tR = niceRange(Math.min(...temps, 15), Math.max(...temps, 30));
  const hR = niceRange(Math.min(...hums, 0), Math.max(...hums, 100));
  const xAt = (i) => pad.l + (points.length === 1 ? w / 2 : (i / (points.length - 1)) * w);
  const yTemp = (v) => pad.t + (1 - (v - tR.min) / (tR.max - tR.min)) * h;
  const yHum = (v) => pad.t + (1 - (v - hR.min) / (hR.max - hR.min)) * h;

  ctx.strokeStyle = "rgba(255,255,255,.08)";
  ctx.lineWidth = 1;
  ctx.font = "11px Outfit, system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.fillStyle = "#8d9893";
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (i / 4) * h;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + w, y); ctx.stroke();
    const tv = tR.max - (i / 4) * (tR.max - tR.min);
    const hv = hR.max - (i / 4) * (hR.max - hR.min);
    const labelY = i === 4 ? y - 3 : y + 3;
    ctx.fillText(tv.toFixed(1), pad.l - 8, labelY);
    ctx.textAlign = "left";
    ctx.fillText(hv.toFixed(0) + "%", pad.l + w + 8, labelY);
    ctx.textAlign = "right";
  }

  ctx.textAlign = "center";
  const labelIdx = [0, Math.floor((points.length - 1) / 2), points.length - 1];
  for (const i of labelIdx) {
    const t = new Date(points[i].t);
    const label = chartWindow === "24h"
      ? t.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      : t.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: chartWindow === "5m" ? "2-digit" : undefined });
    if (points.length > 1 && i === 0) ctx.textAlign = "left";
    else if (points.length > 1 && i === points.length - 1) ctx.textAlign = "right";
    else ctx.textAlign = "center";
    ctx.fillText(label, xAt(i), pad.t + h + 13);
  }

  for (const s of SERIES) {
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < points.length; i++) {
      const v = points[i][s.key];
      if (v == null) { started = false; continue; }
      const x = xAt(i);
      const y = s.axis === "temp" ? yTemp(v) : yHum(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.key === "consigne_c" ? 1.5 : 2;
    if (s.key === "consigne_c") ctx.setLineDash([5, 4]);
    else ctx.setLineDash([]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  LANES.forEach((lane, row) => {
    const y = pad.t + h + 20 + row * (laneH + laneGap);
    ctx.fillStyle = "rgba(255,255,255,.05)";
    ctx.fillRect(pad.l, y, w, laneH);
    ctx.fillStyle = lane.color;
    let start = null;
    const flush = (end) => {
      if (start == null) return;
      const x1 = xAt(start);
      const x2 = xAt(end);
      ctx.fillRect(x1, y, Math.max(3, x2 - x1), laneH);
      start = null;
    };
    for (let i = 0; i < points.length; i++) {
      const v = points[i][lane.key];
      const on = v != null && v >= 0.5;
      if (on && start == null) start = i;
      if (!on && start != null) flush(i - 1);
    }
    if (start != null) flush(points.length - 1);
    ctx.fillStyle = "#8d9893";
    ctx.textAlign = "right";
    ctx.font = "10px Outfit, system-ui, sans-serif";
    ctx.fillText(lane.label, pad.l - 6, y + laneH - 2);
  });
}

async function refreshChart() {
  try {
    const data = await api("/api/metrics?window=" + encodeURIComponent(chartWindow));
    drawMetricsChart(data.points || []);
    $("chartMeta").textContent = data.count
      ? `${data.count} point${data.count > 1 ? "s" : ""}`
      : "Aucune donnée";
  } catch (e) {
    $("chartMeta").textContent = String(e.message || e);
  }
}

$("chartWindow").onclick = (ev) => {
  const btn = ev.target.closest("button[data-window]");
  if (!btn) return;
  chartWindow = btn.dataset.window;
  for (const b of $("chartWindow").querySelectorAll("button")) {
    b.setAttribute("aria-pressed", b === btn ? "true" : "false");
  }
  refreshChart();
};
window.addEventListener("resize", () => refreshChart());

function bumpCam() {
  $("cam").src = "/api/camera.jpg?t=" + Date.now();
}

async function savePhotoInterval() {
  await api("/api/photo_interval", {
    method: "POST",
    body: JSON.stringify({ interval_s: +$("interval").value }),
  });
  $("interval").dataset.dirty = "";
}
$("interval").addEventListener("change", async () => {
  try {
    await savePhotoInterval();
    refreshStatus();
  } catch (e) {
    $("err").textContent = e.message || String(e);
    $("err").classList.add("show");
  }
});
$("btnSetTemp").onclick = async () => {
  await api("/api/heater", { method: "POST", body: JSON.stringify({ temp: +$("temp").value }) });
  await savePhotoInterval();
  refreshStatus();
};
$("btnStart").onclick = async () => {
  await api("/api/incubator/start", {
    method: "POST",
    body: JSON.stringify({ temp: +$("temp").value, interval_s: +$("interval").value }),
  });
  $("interval").dataset.dirty = "";
  refreshStatus(); bumpCam(); refreshGallery();
};
$("btnStopKeep").onclick = async () => {
  await api("/api/incubator/stop", { method: "POST", body: JSON.stringify({ turn_off_heat: false }) });
  refreshStatus();
};
$("btnOff").onclick = async () => {
  await api("/api/heater/off", { method: "POST", body: "{}" });
  refreshStatus();
};
function ventBody(mode) {
  return JSON.stringify({
    mode,
    speed: +$("ventSpeed").value,
    interval_s: +$("ventInterval").value,
    duration_s: +$("ventDuration").value,
  });
}
$("btnVentManual").onclick = async () => {
  await api("/api/vent", { method: "POST", body: ventBody("manual") });
  refreshStatus();
};
$("btnVentCycle").onclick = async () => {
  await api("/api/vent", { method: "POST", body: ventBody("cycle") });
  refreshStatus();
};
$("btnVentOff").onclick = async () => {
  await api("/api/vent", { method: "POST", body: ventBody("off") });
  refreshStatus();
};
function sunPlace() {
  return { lat: +$("sunLat").value, lon: +$("sunLon").value };
}
$("btnLight").onclick = async () => {
  const next = $("btnLight").dataset.on !== "1";
  await api("/api/light", { method: "POST", body: JSON.stringify({ on: next, mode: "manual", ...sunPlace() }) });
  $("sunLat").dataset.dirty = "";
  $("sunLon").dataset.dirty = "";
  bumpCam(); refreshStatus();
};
$("lightMode").onclick = async (ev) => {
  const btn = ev.target.closest("button[data-mode]");
  if (!btn) return;
  try {
    const body = { mode: btn.dataset.mode, ...sunPlace() };
    if (body.mode === "manual") body.on = $("btnLight").dataset.on === "1";
    await api("/api/light", { method: "POST", body: JSON.stringify(body) });
    $("sunLat").dataset.dirty = "";
    $("sunLon").dataset.dirty = "";
    refreshStatus();
  } catch (e) {
    $("err").textContent = e.message || String(e);
    $("err").classList.add("show");
  }
};
$("btnSnap").onclick = async () => {
  const r = await api("/api/snapshot", { method: "POST", body: "{}" });
  if (!r.ok) { $("err").textContent = r.error || "Échec"; $("err").classList.add("show"); }
  refreshStatus(); refreshGallery(); bumpCam();
};
$("btnRefresh").onclick = () => bumpCam();
$("btnCameraUrl").onclick = async () => {
  await api("/api/camera", { method: "POST", body: JSON.stringify({ url: $("cameraUrl").value }) });
  refreshStatus(); bumpCam();
};
$("btnLogout").onclick = async () => {
  await api("/api/logout", { method: "POST", body: "{}" });
  location.href = "/login";
};

$("cam").onerror = () => { setTimeout(bumpCam, 2000); };

refreshStatus();
refreshGallery();
refreshChart();
setInterval(refreshStatus, 4000);
setInterval(bumpCam, 2500);
setInterval(refreshGallery, 15000);
setInterval(refreshChart, 5000);
</script>
</body>
</html>
"""

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Mycelium Lab — Connexion</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@460;650&display=swap" rel="stylesheet" />
<style>
  :root { --text:#f3f6f4; --muted:#8d9893; --line:rgba(255,255,255,.08); --accent:#3ddc97; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: Outfit, system-ui, sans-serif; color: var(--text); background: #090c0f;
  }
  form {
    width: min(400px, calc(100% - 32px)); padding: 28px 22px;
    border: 1px solid var(--line); border-radius: 18px; background: #13181d;
  }
  h1 { font-size: 1.5rem; font-weight: 650; letter-spacing: -0.03em; margin: 0 0 6px; }
  h1 span { color: var(--accent); }
  p { color: var(--muted); margin: 0 0 18px; font-size: .9rem; }
  label { display: block; font-size: .78rem; color: var(--muted); margin-bottom: 6px; }
  input {
    width: 100%; padding: 11px 12px; border-radius: 12px;
    border: 1px solid var(--line); background: #0c1014; color: var(--text); font: inherit;
  }
  button {
    margin-top: 14px; width: 100%; font: inherit; font-weight: 560; border: 0;
    border-radius: 12px; padding: 11px 14px; cursor: pointer;
    background: var(--accent); color: #062117;
  }
  .err { display: none; margin-top: 12px; color: #ffd0ca; font-size: .85rem; }
  .err.show { display: block; }
</style>
</head>
<body>
  <form id="f">
    <h1>Mycelium <span>Lab</span></h1>
    <p>Mot de passe de l'incubateur.</p>
    <label for="password">Mot de passe</label>
    <input id="password" type="password" autocomplete="current-password" required />
    <button type="submit">Entrer</button>
    <div class="err" id="err"></div>
  </form>
<script>
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: document.getElementById("password").value }),
  });
  if (!r.ok) {
    err.textContent = "Mot de passe incorrect";
    err.classList.add("show");
    return;
  }
  location.href = "/";
};
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "MyceliumLab/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _json(self, code: int, payload: dict | list, *, cookie: str | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self._write(raw)

    def _secure_cookie(self) -> bool:
        if os.environ.get("COOKIE_SECURE") == "1":
            return True
        if os.environ.get("COOKIE_SECURE") == "0":
            return False
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _session_cookie(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self._secure_cookie() else ""
        return (
            f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={max_age}{secure}"
        )

    def _authorized(self) -> bool:
        if not AUTH_PASSWORD:
            return True
        token = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            part = part.strip()
            if part.startswith(SESSION_COOKIE + "="):
                token = part.split("=", 1)[1]
                break
        expected = session_token()
        if len(token) != len(expected):
            return False
        return hmac.compare_digest(token, expected)

    def _relay_ok(self) -> bool:
        if not RELAY_TOKEN:
            return False
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        given = header[7:].strip().encode()
        expected = RELAY_TOKEN.encode()
        if len(given) != len(expected):
            return False
        return hmac.compare_digest(given, expected)

    def _reject(self, path: str) -> bool:
        if self._authorized():
            return False
        if path.startswith("/api/") or path.startswith("/photos/"):
            self._json(401, {"error": "Authentification requise"})
            return True
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return True

    def _bytes(self, code: int, data: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(data)

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.debug("Client parti avant la fin de la réponse")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/login":
            if AUTH_PASSWORD and self._authorized():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            return self._bytes(200, LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/api/link/commands":
            if not self._relay_ok():
                return self._json(401, {"error": "Jeton relais invalide"})
            wait = COMMAND_LONG_POLL_S
            if "wait" in qs:
                try:
                    wait = max(0, min(COMMAND_LONG_POLL_S, float(qs["wait"][0])))
                except ValueError:
                    pass
            cmds = CTRL.pop_commands(wait_s=wait)
            return self._json(200, {"commands": cmds})

        if self._reject(path):
            return

        if path in ("/", "/index.html"):
            return self._bytes(200, HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/api/status":
            return self._json(200, CTRL.status())

        if path == "/api/photos":
            return self._json(200, {"photos": CTRL.list_photos()})

        if path == "/api/metrics":
            window = (qs.get("window") or ["60m"])[0]
            try:
                return self._json(200, read_metrics_window(window))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})

        if path in ("/metrics.csv", "/api/metrics.csv"):
            if not METRICS_CSV.is_file():
                with CTRL.lock:
                    CTRL._ensure_metrics_csv()
            data = METRICS_CSV.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="metrics.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self._write(data)

        if path == "/api/camera.jpg":
            data = CTRL.grab_jpeg_bytes()
            if not data:
                return self._json(503, {"error": CTRL.last_error or "Caméra indisponible"})
            return self._bytes(200, data, "image/jpeg")

        if path.startswith("/photos/"):
            name = Path(path).name
            if ".." in name or "/" in name:
                return self._json(400, {"error": "Nom invalide"})
            fp = OUTPUT_DIR / name
            if not fp.is_file():
                return self._json(404, {"error": "Introuvable"})
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self._bytes(200, fp.read_bytes(), ctype)

        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._json(400, {"error": "JSON invalide"})

        try:
            if path == "/api/login":
                if not AUTH_PASSWORD or password_ok(str(body.get("password") or "")):
                    return self._json(
                        200,
                        {"ok": True},
                        cookie=self._session_cookie(session_token(), SESSION_MAX_AGE),
                    )
                return self._json(401, {"error": "Mot de passe incorrect"})
            if path == "/api/logout":
                return self._json(200, {"ok": True}, cookie=self._session_cookie("", 0))

            if path in ("/api/link/telemetry", "/api/link/ack"):
                if not self._relay_ok():
                    return self._json(401, {"error": "Jeton relais invalide"})
                if path == "/api/link/telemetry":
                    return self._json(200, CTRL.ingest_telemetry(body))
                results = body.get("results")
                if not isinstance(results, list):
                    return self._json(400, {"error": "results doit être une liste"})
                return self._json(200, CTRL.ack_commands(results))

            if self._reject(path):
                return

            if path == "/api/camera":
                return self._json(200, CTRL.set_camera_url(str(body.get("url") or "")))
            if path == "/api/heater":
                return self._json(200, CTRL.set_temperature(float(body.get("temp", CTRL.target_temp))))
            if path == "/api/heater/off":
                return self._json(200, CTRL.heater_off())
            if path == "/api/incubator/start":
                return self._json(
                    200,
                    CTRL.start_incubator(
                        temp_c=body.get("temp"),
                        interval_s=body.get("interval_s"),
                    ),
                )
            if path == "/api/incubator/stop":
                return self._json(
                    200,
                    CTRL.stop_incubator(turn_off_heat=bool(body.get("turn_off_heat", True))),
                )
            if path == "/api/camera/on":
                return self._json(200, CTRL.camera_enable())
            if path == "/api/camera/off":
                return self._json(200, CTRL.camera_disable())
            if path == "/api/photo_interval":
                if "interval_s" not in body:
                    return self._json(400, {"error": "interval_s requis"})
                return self._json(200, CTRL.set_photo_interval(body.get("interval_s")))
            if path == "/api/light":
                mode = body.get("mode")
                return self._json(
                    200,
                    CTRL.set_light(
                        body.get("on") if "on" in body else None,
                        mode=None if mode is None else str(mode),
                        lat=body.get("lat") if "lat" in body else None,
                        lon=body.get("lon") if "lon" in body else None,
                    ),
                )
            if path == "/api/vent":
                return self._json(
                    200,
                    CTRL.set_ventilation(
                        mode=str(body.get("mode", "off")),
                        speed=body.get("speed"),
                        interval_s=body.get("interval_s"),
                        duration_s=body.get("duration_s"),
                    ),
                )
            if path == "/api/snapshot":
                return self._json(200, CTRL.take_snapshot())
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            log.exception("API error")
            return self._json(500, {"error": str(exc)})

        self._json(404, {"error": "Not found"})


def main() -> int:
    if AUTH_PASSWORD:
        log.info("Authentification navigateur activée")
    else:
        log.warning("INCUBATOR_PASSWORD vide — interface ouverte")
    if RELAY_TOKEN:
        log.info("Lien relais activé (RELAY_TOKEN)")
    else:
        log.warning("RELAY_TOKEN vide — la carte ESP32 ne pourra pas s'authentifier")
    log.info("URL caméra : %s", CTRL.camera_url)
    log.info("Démarrage UI sur http://127.0.0.1:%s", UI_PORT)
    CTRL.start()
    server = ThreadingHTTPServer((UI_HOST, UI_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Arrêt demandé…")
    finally:
        CTRL.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
