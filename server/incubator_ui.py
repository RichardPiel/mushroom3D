#!/usr/bin/env python3
"""
incubator_ui.py — Interface web distante pour l'incubateur.

    INCUBATOR_PASSWORD=os.environ.get( RELAY_TOKEN=os.environ.get( python3 incubator_ui.py
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
    "chamber_c",
    "humidity_pct",
    "light_on",
    "photo",
)
METRICS_WINDOWS = {
    "5m": 5 * 60,
    "60m": 60 * 60,
    "24h": 24 * 60 * 60,
}
METRICS_SERIES = ("bed_actual_c", "consigne_c", "air_c", "chamber_c", "humidity_pct")
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
DEFAULT_CULTURE_TEMP_MIN = 18.0
DEFAULT_CULTURE_TEMP_MAX = 26.0
DEFAULT_CULTURE_HUM_MIN = 70.0
DEFAULT_CULTURE_HUM_MAX = 95.0
DEFAULT_PHOTO_INTERVAL_S = 1800
DEFAULT_VENT_SPEED = 60
DEFAULT_VENT_INTERVAL_S = 1800
DEFAULT_VENT_ON_S = 120
DEFAULT_SUN_LAT = 48.8566
DEFAULT_SUN_LON = 2.3522
SUN_UP_ELEVATION_DEG = -0.833
# Asservissement plateau ← chambre (incubateur allumé, sans forçage).
CHAMBER_KP = 2.0
CHAMBER_KI = 0.004
CHAMBER_DEADBAND_C = 0.3
CHAMBER_SANE_MIN = 5.0
CHAMBER_SANE_MAX = 70.0
BED_AUTO_MIN = 0.0
BED_AUTO_MAX = 60.0
BED_SLEW_C = 2.0
HEAT_I_MAX = 12.0 / CHAMBER_KI
HEAT_I_MIN = -15.0 / CHAMBER_KI

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


def chamber_bed_command(
    target_c: float, chamber_c: float, integral: float, dt_s: float
) -> tuple[float, float]:
    """Consigne plateau pour amener la chambre sur target_c.

    Retourne (plateau_°C, intégrale). Le plateau automatique reste dans
    BED_AUTO_MIN…BED_AUTO_MAX ; un forçage manuel peut dépasser ce plafond.
    """
    error = float(target_c) - float(chamber_c)
    integral = float(integral)
    if abs(error) >= CHAMBER_DEADBAND_C:
        integral += error * max(0.0, float(dt_s))
        integral = max(HEAT_I_MIN, min(HEAT_I_MAX, integral))
        bed = float(target_c) + CHAMBER_KP * error + CHAMBER_KI * integral
    else:
        bed = float(target_c) + CHAMBER_KI * integral
    if bed > BED_AUTO_MAX and error > 0 and abs(error) >= CHAMBER_DEADBAND_C:
        integral -= error * max(0.0, float(dt_s))
        bed = BED_AUTO_MAX
    elif bed < BED_AUTO_MIN and error < 0 and abs(error) >= CHAMBER_DEADBAND_C:
        integral -= error * max(0.0, float(dt_s))
        bed = BED_AUTO_MIN
    bed = max(BED_AUTO_MIN, min(BED_AUTO_MAX, bed))
    return round(bed, 1), integral


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
        self._target_saved = "target_temp" in cfg
        self._vent_saved = "vent_speed" in cfg
        self.target_temp = _cfg_float(cfg, "target_temp", DEFAULT_TARGET_TEMP)
        self.heat_forced = bool(cfg.get("heat_forced"))
        self.forced_bed_temp = _cfg_float(cfg, "forced_bed_temp", self.target_temp)
        self._heat_i = 0.0
        self._heat_tick_at = 0.0
        self._bed_command: float | None = None
        self._chamber_sane = True
        self.photo_interval_s = _cfg_int(cfg, "photo_interval_s", DEFAULT_PHOTO_INTERVAL_S)
        if not (10 <= self.photo_interval_s <= 86400):
            self.photo_interval_s = int(DEFAULT_PHOTO_INTERVAL_S)
        self.heater_on = False
        self.incubator_running = False
        self.light_on = False
        self.light_known = False
        self.light_target = False
        self.light_mode = cfg.get("light_mode") if cfg.get("light_mode") in ("manual", "sun") else "manual"
        self.sun_lat = _cfg_float(cfg, "sun_lat", _env_float("SUN_LAT", DEFAULT_SUN_LAT))
        self.sun_lon = _cfg_float(cfg, "sun_lon", _env_float("SUN_LON", DEFAULT_SUN_LON))
        self.sun_elevation = 0.0
        self._last_auto_photo = time.monotonic()
        self._last_light_cmd = 0.0

        saved_mode = cfg.get("vent_mode")
        self.vent_mode = saved_mode if saved_mode in ("off", "manual", "cycle") else "off"
        self.vent_on = False
        self.vent_speed = _cfg_int(cfg, "vent_speed", DEFAULT_VENT_SPEED)
        self.vent_interval_s = _cfg_int(cfg, "vent_interval_s", DEFAULT_VENT_INTERVAL_S)
        self.vent_duration_s = _cfg_int(cfg, "vent_duration_s", DEFAULT_VENT_ON_S)
        self.culture_temp_min = _cfg_float(cfg, "culture_temp_min", DEFAULT_CULTURE_TEMP_MIN)
        self.culture_temp_max = _cfg_float(cfg, "culture_temp_max", DEFAULT_CULTURE_TEMP_MAX)
        self.culture_hum_min = _cfg_float(cfg, "culture_hum_min", DEFAULT_CULTURE_HUM_MIN)
        self.culture_hum_max = _cfg_float(cfg, "culture_hum_max", DEFAULT_CULTURE_HUM_MAX)
        self.vent_actual: float | None = None
        self.vent_remaining_s: int | None = None

        self.bed_actual: float | None = None
        self.bed_target: float | None = None
        self.nozzle_actual: float | None = None
        self.chamber_c: float | None = None
        self.chamber_target_c: float | None = None
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
                "chamber_c": "" if self.chamber_c is None else f"{self.chamber_c:.2f}",
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
                "heat_forced": self.heat_forced,
                "forced_bed_temp": self.forced_bed_temp,
                "bed_command": self._bed_command,
                "culture_temp_min": self.culture_temp_min,
                "culture_temp_max": self.culture_temp_max,
                "culture_hum_min": self.culture_hum_min,
                "culture_hum_max": self.culture_hum_max,
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
                "chamber_c": self.chamber_c,
                "chamber_target_c": self.chamber_target_c,
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

    def _persist_setpoints(self) -> None:
        """Caller holds self.lock."""
        _save_config(
            {
                "target_temp": self.target_temp,
                "heat_forced": self.heat_forced,
                "forced_bed_temp": self.forced_bed_temp,
                "vent_mode": self.vent_mode,
                "vent_speed": self.vent_speed,
                "vent_interval_s": self.vent_interval_s,
                "vent_duration_s": self.vent_duration_s,
            }
        )

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            patch: dict[str, Any] = {}
            range_keys = (
                "culture_temp_min",
                "culture_temp_max",
                "culture_hum_min",
                "culture_hum_max",
            )
            if any(key in body for key in range_keys):
                try:
                    tmin = float(body.get("culture_temp_min", self.culture_temp_min))
                    tmax = float(body.get("culture_temp_max", self.culture_temp_max))
                    hmin = float(body.get("culture_hum_min", self.culture_hum_min))
                    hmax = float(body.get("culture_hum_max", self.culture_hum_max))
                except (TypeError, ValueError):
                    self.last_error = "Plage culture invalide"
                    return self.status()
                if not (0 <= tmin <= tmax <= 110):
                    self.last_error = "Plage température : 0–110 °C, min ≤ max"
                    return self.status()
                if not (0 <= hmin <= hmax <= 100):
                    self.last_error = "Plage humidité : 0–100 %, min ≤ max"
                    return self.status()
                self.culture_temp_min = tmin
                self.culture_temp_max = tmax
                self.culture_hum_min = hmin
                self.culture_hum_max = hmax
                patch.update(
                    {
                        "culture_temp_min": tmin,
                        "culture_temp_max": tmax,
                        "culture_hum_min": hmin,
                        "culture_hum_max": hmax,
                    }
                )
            cam = body.get("camera_url")
            if isinstance(cam, str) and cam.strip():
                url = parse_camera_url(cam)
                self.camera_url = url
                patch["camera_url"] = url
                self._enqueue({"type": "camera_url", "url": url})
            if patch:
                _save_config(patch)
            if "sun_lat" in body or "sun_lon" in body:
                error = self._assign_sun_place(
                    self.sun_lat if "sun_lat" not in body else body.get("sun_lat"),
                    self.sun_lon if "sun_lon" not in body else body.get("sun_lon"),
                )
                if error:
                    self.last_error = error
                    return self.status()
            self.last_error = ""
            return self.status()

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
            saw_chamber = printer.get("chamber_c") is not None
            if saw_chamber:
                self.chamber_c = float(printer["chamber_c"])
            if printer.get("chamber_target_c") is not None:
                self.chamber_target_c = float(printer["chamber_target_c"])
            if printer.get("light_known"):
                self.light_known = True
                self.light_on = bool(printer.get("light_on"))
            elif "light_known" not in printer and "light_on" in printer:
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
            if "vent_on" in desired:
                self.vent_on = bool(desired["vent_on"])
            if desired.get("vent_remaining_s") is not None:
                self.vent_remaining_s = int(desired["vent_remaining_s"])
            # La carte remet target_temp à 0 dès que le plateau s'éteint.
            # Les consignes vivent dans config.json ; on n'adopte la télémétrie qu'une fois,
            # tant qu'aucune valeur n'a encore été enregistrée.
            if not self._target_saved and desired.get("heater_on"):
                try:
                    adopted = float(desired.get("target_temp"))
                except (TypeError, ValueError):
                    adopted = 0.0
                if adopted > 0:
                    self.target_temp = adopted
                    self._target_saved = True
                    _save_config({"target_temp": adopted})
            if not self._vent_saved and desired.get("vent_speed") is not None:
                mode = desired.get("vent_mode")
                if mode not in ("off", "manual", "cycle"):
                    mode = self.vent_mode
                try:
                    self.vent_speed = int(desired["vent_speed"])
                    if desired.get("vent_interval_s") is not None:
                        self.vent_interval_s = int(desired["vent_interval_s"])
                    if desired.get("vent_duration_s") is not None:
                        self.vent_duration_s = int(desired["vent_duration_s"])
                    self.vent_mode = str(mode)
                    self._vent_saved = True
                    self._persist_setpoints()
                except (TypeError, ValueError):
                    pass

            if saw_chamber:
                self._control_bed()
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

    def _drop_bed_commands(self) -> None:
        """Caller holds self.lock."""
        self.commands = deque(
            cmd for cmd in self.commands if cmd.get("type") not in ("heater_set", "incubator_start")
        )

    def _queue_bed(self, temp_c: float, *, start: bool = False) -> None:
        """Caller holds self.lock. Remplace un ordre plateau déjà en file."""
        temp_c = round(max(0.0, min(110.0, float(temp_c))), 1)
        self._bed_command = temp_c
        kind = "incubator_start" if start else "heater_set"
        for cmd in self.commands:
            if cmd.get("type") in ("heater_set", "incubator_start"):
                if start:
                    cmd["type"] = "incubator_start"
                cmd["temp"] = temp_c
                self._cmd_event.set()
                return
        self._enqueue({"type": kind, "temp": temp_c})

    def _auto_bed_now(self) -> float:
        """Caller holds self.lock. Consigne plateau immédiate, sans avancer l'intégrale."""
        chamber = self.chamber_c
        if chamber is None or not (CHAMBER_SANE_MIN <= chamber <= CHAMBER_SANE_MAX):
            return round(max(BED_AUTO_MIN, min(BED_AUTO_MAX, self.target_temp)), 1)
        bed, _integral = chamber_bed_command(self.target_temp, chamber, self._heat_i, 0.0)
        return bed

    def _control_bed(self) -> None:
        """Caller holds self.lock. Ajuste le plateau d'après la chambre."""
        if not self.incubator_running or self.heat_forced:
            return
        chamber = self.chamber_c
        if chamber is None:
            return
        now = time.monotonic()
        if self._heat_tick_at <= 0:
            dt = 5.0
        else:
            dt = min(30.0, max(0.5, now - self._heat_tick_at))
        if not (CHAMBER_SANE_MIN <= chamber <= CHAMBER_SANE_MAX):
            if self._chamber_sane:
                log.warning("Chambre %.1f °C hors plage utile — plateau non modifié", chamber)
                self._chamber_sane = False
            return
        self._chamber_sane = True
        self._heat_tick_at = now
        bed, self._heat_i = chamber_bed_command(self.target_temp, chamber, self._heat_i, dt)
        if self._bed_command is not None:
            bed = min(self._bed_command + BED_SLEW_C, max(self._bed_command - BED_SLEW_C, bed))
            bed = round(bed, 1)
        if self._bed_command is not None and abs(bed - self._bed_command) < 0.3 and self.heater_on == (bed > 0):
            return
        self.heater_on = bed > 0
        self._queue_bed(bed)
        log.info(
            "Plateau %.1f °C (chambre %.1f °C, consigne %.1f °C)",
            bed,
            chamber,
            self.target_temp,
        )

    def _parse_temp(self, temp_c: float) -> float | None:
        try:
            temp_c = float(temp_c)
        except (TypeError, ValueError):
            return None
        if not (0 <= temp_c <= 110):
            return None
        return temp_c

    def set_chamber_target(self, temp_c: float) -> dict[str, Any]:
        """Consigne de la chambre. Le plateau n'est pas forcé."""
        with self.lock:
            parsed = self._parse_temp(temp_c)
            if parsed is None:
                self.last_error = "Température hors plage 0–110 °C"
                return self.status()
            self.target_temp = parsed
            self._target_saved = True
            self._persist_setpoints()
            self.last_error = ""
            if self.incubator_running and not self.heat_forced and self.chamber_c is not None:
                self._control_bed()
            return self.status()

    def force_bed(self, temp_c: float) -> dict[str, Any]:
        """Impose le plateau, incubateur allumé ou non."""
        with self.lock:
            parsed = self._parse_temp(temp_c)
            if parsed is None:
                self.last_error = "Température hors plage 0–110 °C"
                return self.status()
            self.forced_bed_temp = round(parsed, 1)
            self.heat_forced = True
            self.heater_on = self.forced_bed_temp > 0
            self._persist_setpoints()
            self._queue_bed(self.forced_bed_temp)
            self.last_error = ""
            log.info("Plateau forcé à %.1f °C", self.forced_bed_temp)
            return self.status()

    def release_heat_force(self) -> dict[str, Any]:
        """Rend la main à la chambre si l'incubateur tourne."""
        with self.lock:
            self.heat_forced = False
            self._heat_i = 0.0
            self._heat_tick_at = 0.0
            self._persist_setpoints()
            self.last_error = ""
            if self.incubator_running:
                bed = self._auto_bed_now()
                self.heater_on = bed > 0
                self._queue_bed(bed)
                log.info("Pilotage chambre repris, plateau %.1f °C", bed)
            return self.status()

    def heater_off(self) -> dict[str, Any]:
        with self.lock:
            self.incubator_running = False
            self.heater_on = False
            self.heat_forced = False
            self._heat_i = 0.0
            self.vent_mode = "off"
            self._vent_saved = True
            self.vent_on = False
            self.light_mode = "manual"
            self._persist_setpoints()
            _save_config({"light_mode": "manual"})
            self._ensure_light(False, force=True)
            self._drop_bed_commands()
            self._bed_command = 0.0
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
            self._vent_saved = True
            self.vent_on = mode in ("manual", "cycle")
            self._persist_setpoints()
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
            parsed_temp: float | None = None
            if temp_c is not None:
                parsed_temp = self._parse_temp(temp_c)
                if parsed_temp is None:
                    self.last_error = "Température hors plage 0–110 °C"
                    return self.status()
            if interval_s is not None:
                error = self._assign_photo_interval(interval_s)
                if error:
                    self.last_error = error
                    return self.status()
            if not (0 < self.vent_speed <= 100):
                self.last_error = "Vitesse d'air hors plage 1–100 %"
                return self.status()
            if self.vent_duration_s >= self.vent_interval_s:
                self.last_error = "L'air doit rester allumé moins longtemps qu'il ne se relance"
                return self.status()
            if parsed_temp is not None:
                self.target_temp = parsed_temp
            self.incubator_running = True
            if self.heat_forced:
                bed = round(max(0.0, min(110.0, self.forced_bed_temp)), 1)
            else:
                self._heat_i = 0.0
                self._heat_tick_at = 0.0
                bed = self._auto_bed_now()
            self.heater_on = bed > 0
            self.vent_mode = "cycle"
            self.vent_on = True
            self._vent_saved = True
            self.light_mode = "sun"
            self.started_at = time.time()
            self._target_saved = True
            self._persist_setpoints()
            _save_config({"light_mode": "sun"})
            self._last_auto_photo = time.monotonic() - self.photo_interval_s
            self._queue_bed(bed, start=True)
            self._enqueue(
                {
                    "type": "vent",
                    "mode": "cycle",
                    "speed": self.vent_speed,
                    "interval_s": self.vent_interval_s,
                    "duration_s": self.vent_duration_s,
                }
            )
            up, elevation = sun_is_up(self.sun_lat, self.sun_lon)
            self.sun_elevation = elevation
            self._ensure_light(up, force=True)
            self.last_error = ""
            return self.status()

    def stop_incubator(self, turn_off_heat: bool = True) -> dict[str, Any]:
        with self.lock:
            self.incubator_running = False
            if turn_off_heat:
                self.heater_on = False
                self.heat_forced = False
                self._heat_i = 0.0
                self._drop_bed_commands()
                self._bed_command = 0.0
            self.vent_mode = "off"
            self.vent_on = False
            self._vent_saved = True
            self.light_mode = "manual"
            self._persist_setpoints()
            _save_config({"light_mode": "manual"})
            self._enqueue({"type": "incubator_stop", "turn_off_heat": bool(turn_off_heat)})
            self._enqueue(
                {
                    "type": "vent",
                    "mode": "off",
                    "speed": self.vent_speed,
                    "interval_s": self.vent_interval_s,
                    "duration_s": self.vent_duration_s,
                }
            )
            if turn_off_heat:
                self._ensure_light(False, force=True)
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

        Cmd 403 toggles the chamber lamp, so a periodic refresh turns it off.
        Send only when the target changes or the printer reports the other state.
        """
        on = bool(on)
        changed = on != self.light_target
        self.light_target = on
        if self.light_known and self.light_on == on:
            return
        pending_same = any(
            cmd.get("type") == "light" and bool(cmd.get("on")) == on for cmd in self.commands
        )
        if pending_same and not force:
            return
        elapsed = time.monotonic() - self._last_light_cmd
        if force or changed or elapsed >= 12:
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
<title>Mycelium Lab — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@460;560;650&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #070a0d;
    --panel: #10151a;
    --line: rgba(255,255,255,.08);
    --text: #eef3f0;
    --muted: #8b9691;
    --accent: #3ddc97;
    --accent-ink: #062117;
    --danger: #ff6b5a;
    --ok: #3ddc97;
    --warn: #e6b35a;
    --radius: 16px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: Outfit, system-ui, sans-serif; color: var(--text);
    background:
      radial-gradient(1000px 480px at 8% -12%, rgba(61,220,151,.10), transparent 55%),
      radial-gradient(800px 420px at 100% 0%, rgba(110,200,255,.06), transparent 50%),
      var(--bg);
  }
  .shell { max-width: 1280px; margin: 0 auto; padding: 18px 16px 40px; }
  .topbar {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 12px; margin-bottom: 14px;
  }
  .brand { margin: 0; font-size: 1.4rem; font-weight: 650; letter-spacing: -.03em; }
  .brand span { color: var(--accent); }
  .sub { margin: 4px 0 0; color: var(--muted); font-size: .84rem; }
  .pills { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 7px 12px; border: 1px solid var(--line); border-radius: 999px;
    background: var(--panel); font-size: .82rem;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); }
  .dot.ok { background: var(--ok); }
  .dot.bad { background: var(--danger); }

  .kpi {
    display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px; margin-bottom: 14px;
  }
  @media (max-width: 1100px) { .kpi { grid-template-columns: repeat(4, 1fr); } }
  @media (max-width: 700px) { .kpi { grid-template-columns: repeat(2, 1fr); } }
  .kpi-card {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 12px 14px; min-height: 78px;
  }
  .kpi-card .label {
    color: var(--muted); font-size: .7rem; letter-spacing: .06em; text-transform: uppercase;
  }
  .kpi-card .value {
    margin-top: 4px; font-size: 1.45rem; font-weight: 650; letter-spacing: -.03em;
  }
  .kpi-card .value small { font-size: .75rem; color: var(--muted); font-weight: 460; }
  .kpi-card .band { margin-top: 2px; color: var(--muted); font-size: .72rem; }
  .kpi-card.in { border-color: rgba(61,220,151,.65); }
  .kpi-card.in .value { color: var(--ok); }
  .kpi-card.out { border-color: rgba(230,179,90,.75); }
  .kpi-card.out .value { color: var(--warn); }

  .dash {
    display: grid; grid-template-columns: 1.2fr 1fr; gap: 14px; align-items: start;
  }
  @media (max-width: 980px) { .dash { grid-template-columns: 1fr; } }

  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: var(--radius); overflow: hidden;
  }
  .panel-h {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 14px 16px 0;
  }
  .panel-h h2 {
    margin: 0; font-size: .74rem; font-weight: 560; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted);
  }
  .panel .body { padding: 12px 16px 16px; }

  .cam-frame { position: relative; background: #050709; aspect-ratio: 16/10; }
  .cam-frame img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cam-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .cam-actions button {
    width: 42px; height: 42px; padding: 0; display: grid; place-items: center;
  }
  .cam-actions svg { display: block; }

  .set-list { display: grid; gap: 8px; }
  .set-row {
    display: grid; grid-template-columns: minmax(0, 1fr) 96px 36px; align-items: center;
    gap: 8px;
  }
  .set-row .name { font-weight: 560; font-size: .84rem; }
  .set-row input {
    width: 100%; padding: 7px 8px; border-radius: 10px;
    border: 1px solid var(--line); background: #0b1015; color: var(--text); font: inherit;
  }
  .set-row input:focus { outline: 2px solid rgba(61,220,151,.4); border-color: transparent; }
  .btn-icon {
    width: 36px; height: 36px; padding: 0; display: grid; place-items: center;
  }
  .btn-icon svg { display: block; }
  .mode-block {
    display: grid; gap: 8px; padding-top: 12px;
    border-top: 1px solid var(--line);
  }
  .mode-block .head {
    display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
  }
  .mode-block .name { font-weight: 560; font-size: .92rem; }
  .mode-block .state { color: var(--muted); font-size: .78rem; text-align: right; }
  .trio-modes { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  dialog {
    width: min(480px, calc(100% - 24px)); padding: 0; border: 1px solid var(--line);
    border-radius: 16px; background: var(--panel); color: var(--text);
  }
  dialog::backdrop { background: rgba(0,0,0,.55); }
  dialog form { padding: 16px; display: grid; gap: 12px; }
  dialog h2 { margin: 0; font-size: 1.05rem; }
  .dlg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  dialog label { display: block; font-size: .75rem; color: var(--muted); margin-bottom: 4px; }
  dialog input {
    width: 100%; padding: 9px 10px; border-radius: 10px;
    border: 1px solid var(--line); background: #0b1015; color: var(--text); font: inherit;
  }
  .dlg-actions { display: flex; justify-content: flex-end; gap: 8px; }
  .modes { display: grid; gap: 12px; margin-top: 14px; }

  button {
    font: inherit; font-weight: 560; border: 0; border-radius: 12px;
    padding: 10px 12px; cursor: pointer;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .btn-gold, .btn-primary { background: var(--accent); color: var(--accent-ink); }
  .btn-ghost { background: transparent; color: var(--text); border: 1px solid var(--line); }
  .btn-danger { background: rgba(255,107,90,.16); color: #ffd0ca; border: 1px solid rgba(255,107,90,.25); }
  .seg { display: inline-flex; flex-wrap: wrap; gap: 6px; }
  .seg button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: transparent; }

  .hint { color: var(--muted); font-size: .8rem; margin: 10px 0 0; line-height: 1.45; }
  .err {
    margin-top: 10px; padding: 10px 12px; border-radius: 12px;
    background: rgba(255,107,90,.12); color: #ffd0ca; font-size: .84rem; display: none;
  }
  .err.show { display: block; }

  .below { display: grid; gap: 14px; margin-top: 14px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; }
  .chart-wrap {
    position: relative; width: 100%; height: 320px; margin-top: 10px;
    border-radius: 12px; background: rgba(255,255,255,.03); border: 1px solid var(--line);
  }
  .chart-wrap canvas { width: 100%; height: 100%; display: block; cursor: crosshair; }
  .chart-tip {
    position: absolute; z-index: 3; display: none; pointer-events: none;
    min-width: 180px; padding: 8px 10px; border-radius: 10px;
    background: rgba(16, 22, 28, .94); border: 1px solid var(--line);
    font-size: .75rem; line-height: 1.4; color: var(--text);
  }
  .chart-tip.show { display: block; }
  .chart-tip .when { color: var(--muted); margin-bottom: 4px; }
  .chart-tip .row { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
  .chart-tip .row b { font-weight: 560; font-variant-numeric: tabular-nums; }
  .chart-tip .dot { width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; display: inline-block; }
  .chart-legend {
    display: flex; flex-wrap: wrap; gap: 12px 16px; margin-top: 10px;
    font-size: .78rem; color: var(--muted);
  }
  .chart-legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .gallery {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
    gap: 8px; max-height: 260px; overflow: auto;
  }
  .gallery a { display: block; border-radius: 12px; overflow: hidden; background: #000; }
  .gallery img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }
  footer { margin-top: 16px; color: var(--muted); font-size: .75rem; }
</style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div>
        <h1 class="brand">Mycelium <span>Lab</span></h1>
        <p class="sub" id="identity">En attente du relais…</p>
      </div>
      <div class="pills">
        <div class="pill"><span class="dot" id="dotLink"></span><span id="linkLabel">Relais</span></div>
        <div class="pill"><span class="dot" id="dot"></span><span id="connLabel">—</span></div>
        <button type="button" class="btn-ghost" id="btnSettings">Réglages</button>
        <button type="button" class="btn-ghost" id="btnLogout" hidden>Sortir</button>
      </div>
    </header>

    <section class="kpi" aria-label="Mesures">
      <div class="kpi-card"><div class="label">Plateau</div><div class="value" id="bedActual">—<small> °C</small></div><div class="band" id="bedCmd"></div></div>
      <div class="kpi-card"><div class="label">Consigne</div><div class="value" id="bedTarget">—<small> °C</small></div></div>
      <div class="kpi-card"><div class="label">Chambre</div><div class="value" id="chamberTemp">—<small> °C</small></div></div>
      <div class="kpi-card" id="kpiCultureTemp">
        <div class="label">Température culture</div>
        <div class="value" id="bmeTemp">—<small> °C</small></div>
        <div class="band" id="cultureTempBand"></div>
      </div>
      <div class="kpi-card" id="kpiCultureHum">
        <div class="label">Humidité culture</div>
        <div class="value" id="bmeHum">—<small> %</small></div>
        <div class="band" id="cultureHumBand"></div>
      </div>
      <div class="kpi-card"><div class="label">Air box</div><div class="value" id="ventActual">—<small> %</small></div></div>
      <div class="kpi-card"><div class="label">Photos</div><div class="value" id="photoCount">0</div></div>
    </section>

    <div class="dash">
      <section class="panel">
        <div class="panel-h"><h2>Live</h2><span class="hint" id="camMeta" style="margin:0">Flux</span></div>
        <div class="cam-frame">
          <img id="cam" alt="Caméra incubateur" src="/api/camera.jpg" />
        </div>
        <div class="body">
          <div class="cam-actions">
            <button type="button" class="btn-gold" id="btnLight" title="Flash" aria-label="Allumer le flash" aria-pressed="false"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/></svg></button>
            <button type="button" class="btn-gold" id="btnVent" title="Ventilation" aria-label="Allumer la ventilation" aria-pressed="false"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/><path d="M12 10c-1.8-3.2-1.6-6.2.2-8 2.6 2 2.4 5.2-.2 8zM14 12c3.2-1.8 6.2-1.6 8 .2-2 2.6-5.2 2.4-8-.2zM12 14c1.8 3.2 1.6 6.2-.2 8-2.6-2-2.4-5.2.2-8zM10 12c-3.2 1.8-6.2 1.6-8-.2 2-2.6 5.2-2.4 8 .2z"/></svg></button>
            <button type="button" class="btn-ghost" id="btnSnap" title="Photo" aria-label="Prendre une photo"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M9 4h6l1.2 2H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3.8L9 4zm3 5a5 5 0 1 0 .01 10.01A5 5 0 0 0 12 9zm0 2a3 3 0 1 1 0 6 3 3 0 0 1 0-6z"/></svg></button>
            <button type="button" class="btn-ghost" id="btnRefresh" title="Rafraîchir" aria-label="Rafraîchir le flux"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A8 8 0 1 0 19.73 14h-2.09a6 6 0 1 1-1.58-5.9L13 11V4h7l-2.35 2.35z"/></svg></button>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-h"><h2>Consignes</h2></div>
        <div class="body">
          <div class="set-list">
            <div class="set-row">
              <span class="name">Chambre °C</span>
              <input id="temp" type="number" min="0" max="110" step="0.5" value="22" title="Consigne de la chambre" />
              <button type="button" class="btn-gold btn-icon" id="btnSetTemp" title="Enregistrer la consigne chambre" aria-label="Enregistrer la consigne chambre"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
            <div class="set-row">
              <span class="name">Forcer plateau °C</span>
              <input id="forceTemp" type="number" min="0" max="110" step="0.5" value="22" title="Consigne plateau imposée" />
              <button type="button" class="btn-gold btn-icon" id="btnForce" title="Forcer le plateau" aria-label="Forcer la température du plateau"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
            <div class="set-row">
              <span class="name">Photo / s</span>
              <input id="interval" type="number" min="10" max="86400" step="10" value="1800" />
              <button type="button" class="btn-gold btn-icon" id="btnSetInterval" title="Enregistrer" aria-label="Enregistrer l'intervalle photo"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
            <div class="set-row">
              <span class="name">Ventilation %</span>
              <input id="ventSpeed" type="number" min="0" max="100" step="5" value="60" />
              <button type="button" class="btn-gold btn-icon" id="btnSetVentSpeed" title="Enregistrer" aria-label="Enregistrer la vitesse"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
            <div class="set-row">
              <span class="name" title="En mode cycle, l'air se relance toutes les N secondes">Air repris toutes les s</span>
              <input id="ventInterval" type="number" min="30" step="30" value="1800" title="Secondes entre deux mises en route de l'air" />
              <button type="button" class="btn-gold btn-icon" id="btnSetVentInterval" title="Enregistrer" aria-label="Enregistrer la reprise de l'air"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
            <div class="set-row">
              <span class="name" title="En mode cycle, l'air reste allumé pendant N secondes">Air allumé pendant s</span>
              <input id="ventDuration" type="number" min="5" step="5" value="120" title="Secondes pendant lesquelles l'air reste allumé" />
              <button type="button" class="btn-gold btn-icon" id="btnSetVentDuration" title="Enregistrer" aria-label="Enregistrer la durée d'air allumé"><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7l-4-4zm2 16H5V5h11.17L19 7.83V19zM12 12a3 3 0 1 0 .002 6.002A3 3 0 0 0 12 12zM6 6h9v4H6V6z"/></svg></button>
            </div>
          </div>

          <div class="modes">
            <div class="mode-block">
              <div class="head">
                <span class="name">Incubateur</span>
                <span class="state" id="runState">Veille</span>
              </div>
              <div class="trio-modes">
                <button type="button" class="btn-gold" id="btnStart">Démarrer</button>
                <button type="button" class="btn-ghost" id="btnStopKeep">Pause</button>
                <button type="button" class="btn-danger" id="btnOff">Éteindre</button>
              </div>
              <p class="hint">Démarrer asservit le plateau à la chambre (plafond 60 °C), lance le cycle d'air et l'éclairage soleil. Forcer plateau impose la chauffe, jusqu'à 110 °C.</p>
              <button type="button" class="btn-ghost" id="btnHeatAuto" hidden>Reprendre la chambre</button>
            </div>
          </div>

          <div class="err" id="err"></div>
        </div>
      </section>
    </div>

    <div class="below">
      <section class="panel">
        <div class="panel-h">
          <h2>Historique</h2>
          <span class="hint" id="chartMeta" style="margin:0">—</span>
        </div>
        <div class="body">
          <div class="toolbar">
            <div class="seg" id="chartWindow" role="group" aria-label="Fenêtre">
              <button type="button" class="btn-ghost" data-window="5m">5 min</button>
              <button type="button" class="btn-ghost" data-window="60m" aria-pressed="true">60 min</button>
              <button type="button" class="btn-ghost" data-window="24h">24 h</button>
            </div>
            <button type="button" class="btn-ghost" id="btnMetrics">Télécharger CSV</button>
          </div>
          <div class="chart-wrap">
            <canvas id="metricsChart" width="1100" height="320"></canvas>
            <div id="chartTip" class="chart-tip" role="tooltip"></div>
          </div>
          <div class="chart-legend">
            <span><i class="swatch" style="background:#3ddc97"></i>Plateau</span>
            <span><i class="swatch" style="background:#e6b35a"></i>Consigne</span>
            <span><i class="swatch" style="background:#6ec8ff"></i>Température culture</span>
            <span><i class="swatch" style="background:#ff8f6b"></i>Chambre</span>
            <span><i class="swatch" style="background:#c9a0ff"></i>Humidité culture</span>
            <span><i class="swatch" style="background:#5ee0c2"></i>Ventilation</span>
            <span><i class="swatch" style="background:#ffd07a"></i>Flash</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-h"><h2>Time-lapse</h2></div>
        <div class="body">
          <div class="gallery" id="gallery"></div>
        </div>
      </section>
    </div>

    <footer>Relais ESP32 · Centauri Carbon · BME680</footer>
  </div>

  <dialog id="cfgDialog">
    <form id="cfgForm">
      <h2>Configuration</h2>
      <div>
        <div class="name" style="font-weight:560;margin-bottom:6px">Plage température culture</div>
        <div class="dlg-grid">
          <div><label for="cfgTempMin">Min °C</label><input id="cfgTempMin" type="number" min="0" max="110" step="0.5" /></div>
          <div><label for="cfgTempMax">Max °C</label><input id="cfgTempMax" type="number" min="0" max="110" step="0.5" /></div>
        </div>
      </div>
      <div>
        <div class="name" style="font-weight:560;margin-bottom:6px">Plage humidité culture</div>
        <div class="dlg-grid">
          <div><label for="cfgHumMin">Min %</label><input id="cfgHumMin" type="number" min="0" max="100" step="1" /></div>
          <div><label for="cfgHumMax">Max %</label><input id="cfgHumMax" type="number" min="0" max="100" step="1" /></div>
        </div>
      </div>
      <div>
        <label for="cameraUrl">URL vidéo</label>
        <input id="cameraUrl" type="url" placeholder="http://192.168.1.20:3031/video" autocomplete="off" spellcheck="false" />
      </div>
      <div>
        <div class="name" style="font-weight:560;margin-bottom:6px">Position géographique</div>
        <div class="dlg-grid">
          <div><label for="sunLat">Latitude</label><input id="sunLat" type="number" min="-90" max="90" step="0.0001" /></div>
          <div><label for="sunLon">Longitude</label><input id="sunLon" type="number" min="-180" max="180" step="0.0001" /></div>
        </div>
      </div>
      <div class="dlg-actions">
        <button type="button" class="btn-ghost" id="cfgCancel">Annuler</button>
        <button type="submit" class="btn-gold">Enregistrer</button>
      </div>
    </form>
  </dialog>

<script>
const $ = (id) => document.getElementById(id);
const DEFAULT_CAMERA_URL = "http://192.168.1.20:3031/video";
let ventMode = "off";

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
  if (document.activeElement !== el) el.value = value ?? "";
}

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

function clampInput(id) {
  const el = $(id);
  const min = el.min !== "" ? Number(el.min) : -Infinity;
  const max = el.max !== "" ? Number(el.max) : Infinity;
  const step = el.step !== "" ? Number(el.step) : 1;
  let v = Number(el.value);
  if (!Number.isFinite(v)) v = Number.isFinite(min) ? min : 0;
  v = Math.min(max, Math.max(min, v));
  if (Number.isFinite(step) && step > 0) {
    const precision = String(step).includes(".") ? String(step).split(".")[1].length : 0;
    v = Number(v.toFixed(precision));
  }
  el.value = v;
  return v;
}

function paintBand(id, value, min, max) {
  const card = $(id);
  const v = Number(value);
  const lo = Number(min);
  const hi = Number(max);
  if (!Number.isFinite(v) || !Number.isFinite(lo) || !Number.isFinite(hi)) {
    card.className = "kpi-card";
    return;
  }
  card.className = "kpi-card " + (v >= lo && v <= hi ? "in" : "out");
}

function showErr(msg) {
  const err = $("err");
  if (!msg) { err.classList.remove("show"); err.textContent = ""; return; }
  err.textContent = msg;
  err.classList.add("show");
}

function sunPlace() {
  return { lat: clampInput("sunLat"), lon: clampInput("sunLon") };
}

function applyStatus(s) {
  const linkOnline = !!(s.link && s.link.online);
  ventMode = s.vent_mode || "off";

  $("identity").textContent = `${s.machine_name} · FW ${s.firmware} · ${s.printer_ip || "—"}`;
  $("dot").className = "dot " + (s.connected ? "ok" : "bad");
  $("connLabel").textContent = s.connected
    ? (s.incubator_running
        ? (s.heat_forced ? "Incubateur · plateau forcé" : "Incubateur · chambre")
        : (s.heat_forced || s.heater_on ? "Chauffe forcée" : "Imprimante OK"))
    : "Imprimante absente";
  $("dotLink").className = "dot " + (linkOnline ? "ok" : "bad");
  $("linkLabel").textContent = linkOnline
    ? `Relais · ${fmtRemain(s.link.age_s)}`
    : (s.relay_configured ? "Relais hors ligne" : "RELAY_TOKEN manquant");

  $("bedActual").innerHTML = (s.bed_actual ?? "—") + "<small> °C</small>";
  const bedOrder = s.bed_command != null ? s.bed_command : s.bed_target;
  $("bedCmd").textContent = s.heat_forced
    ? `forcé ${fmtNum(bedOrder, 1)} °C`
    : (s.incubator_running ? `ordre ${fmtNum(bedOrder, 1)} °C` : (s.heater_on ? `manuel ${fmtNum(bedOrder, 1)} °C` : ""));
  $("bedTarget").innerHTML = (s.target_temp ?? "—") + "<small> °C</small>";
  $("chamberTemp").innerHTML = fmtNum(s.chamber_c) + "<small> °C</small>";
  $("photoCount").textContent = s.photo_count ?? 0;
  $("ventActual").innerHTML = (s.vent_actual != null ? Math.round(s.vent_actual) : "—") + "<small> %</small>";

  const b = s.bme680 || {};
  $("bmeTemp").innerHTML = fmtNum(b.temperature_c) + "<small> °C</small>";
  $("bmeHum").innerHTML = fmtNum(b.humidity_pct, 0) + "<small> %</small>";
  paintBand("kpiCultureTemp", b.temperature_c, s.culture_temp_min, s.culture_temp_max);
  paintBand("kpiCultureHum", b.humidity_pct, s.culture_hum_min, s.culture_hum_max);
  $("cultureTempBand").textContent = `${fmtNum(s.culture_temp_min, 1)} – ${fmtNum(s.culture_temp_max, 1)} °C`;
  $("cultureHumBand").textContent = `${fmtNum(s.culture_hum_min, 0)} – ${fmtNum(s.culture_hum_max, 0)} %`;

  setIfIdle("temp", s.target_temp);
  setIfIdle("forceTemp", s.forced_bed_temp);
  setIfIdle("interval", s.photo_interval_s);
  setIfIdle("ventSpeed", s.vent_speed);
  setIfIdle("ventInterval", s.vent_interval_s);
  setIfIdle("ventDuration", s.vent_duration_s);
  setIfIdle("cameraUrl", s.camera_url);
  setIfIdle("sunLat", s.sun_lat);
  setIfIdle("sunLon", s.sun_lon);
  setIfIdle("cfgTempMin", s.culture_temp_min);
  setIfIdle("cfgTempMax", s.culture_temp_max);
  setIfIdle("cfgHumMin", s.culture_hum_min);
  setIfIdle("cfgHumMax", s.culture_hum_max);
  $("camMeta").textContent = s.camera_url ? "Flux configuré" : "Pas d'URL";
  $("btnLogout").hidden = !s.auth;

  if (s.last_error) showErr(s.last_error);
  else showErr("");

  $("runState").textContent = s.incubator_running
    ? `ON · ${s.heat_forced ? "plateau forcé" : "chambre"} · photo / ${s.photo_interval_s} s · prochaine ${fmtRemain(s.photo_next_s)}`
    : (s.heat_forced || s.heater_on ? "Chauffe forcée" : "Veille");
  $("btnHeatAuto").hidden = !s.heat_forced;

  const lightBtn = $("btnLight");
  lightBtn.className = s.light_on ? "btn-ghost" : "btn-gold";
  lightBtn.dataset.on = s.light_on ? "1" : "0";
  lightBtn.setAttribute("aria-pressed", s.light_on ? "true" : "false");
  lightBtn.setAttribute("aria-label", s.light_on ? "Éteindre le flash" : "Allumer le flash");

  const ventOn = s.vent_mode !== "off";
  const ventBtn = $("btnVent");
  ventBtn.className = ventOn ? "btn-ghost" : "btn-gold";
  ventBtn.setAttribute("aria-pressed", ventOn ? "true" : "false");
  ventBtn.setAttribute("aria-label", ventOn ? "Arrêter la ventilation" : "Allumer la ventilation");
}

async function refreshStatus() {
  try { applyStatus(await api("/api/status")); }
  catch (e) { showErr(String(e.message || e)); }
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
  if (!r.ok) { showErr("CSV indisponible"); return; }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "metrics.csv";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
};

let chartWindow = "60m";
let chartPoints = [];
let chartHover = -1;
const SERIES = [
  { key: "bed_actual_c", color: "#3ddc97", axis: "temp", label: "Plateau", digits: 2, unit: "°C" },
  { key: "consigne_c", color: "#e6b35a", axis: "temp", label: "Consigne", digits: 2, unit: "°C" },
  { key: "air_c", color: "#6ec8ff", axis: "temp", label: "Température culture", digits: 2, unit: "°C" },
  { key: "chamber_c", color: "#ff8f6b", axis: "temp", label: "Chambre", digits: 2, unit: "°C" },
  { key: "humidity_pct", color: "#c9a0ff", axis: "hum", label: "Humidité culture", digits: 1, unit: "%" },
];
const LANES = [
  { key: "vent_on", color: "rgba(94, 224, 194, .9)", label: "Vent", tip: "Ventilation", swatch: "#5ee0c2" },
  { key: "light_on", color: "rgba(255, 208, 122, .95)", label: "Flash", tip: "Flash", swatch: "#ffd07a" },
];

function niceRange(min, max, pad = 0.08) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1 };
  if (min === max) { min -= 1; max += 1; }
  const span = max - min || 1;
  return { min: min - span * pad, max: max + span * pad };
}

function chartGeom(canvas, n) {
  const cssW = canvas.clientWidth || 1100;
  const cssH = canvas.clientHeight || 320;
  const laneH = 12;
  const laneGap = 4;
  const pad = { l: 48, r: 44, t: 16, b: 22 + LANES.length * (laneH + laneGap) };
  const w = cssW - pad.l - pad.r;
  const h = cssH - pad.t - pad.b;
  const xAt = (i) => pad.l + (n <= 1 ? w / 2 : (i / (n - 1)) * w);
  return { cssW, cssH, laneH, laneGap, pad, w, h, xAt };
}

function drawMetricsChart(points, hoverIdx = -1) {
  const canvas = $("metricsChart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const g = chartGeom(canvas, points.length);
  const { cssW, cssH, laneH, laneGap, pad, w, h, xAt } = g;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  ctx.fillStyle = "rgba(255,255,255,.03)";
  ctx.fillRect(0, 0, cssW, cssH);

  if (!points.length) {
    ctx.fillStyle = "#8b9691";
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
  const yTemp = (v) => pad.t + (1 - (v - tR.min) / (tR.max - tR.min)) * h;
  const yHum = (v) => pad.t + (1 - (v - hR.min) / (hR.max - hR.min)) * h;

  ctx.strokeStyle = "rgba(255,255,255,.08)";
  ctx.lineWidth = 1;
  ctx.font = "11px Outfit, system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.fillStyle = "#8b9691";
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
    ctx.setLineDash(s.key === "consigne_c" ? [5, 4] : []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  LANES.forEach((lane, row) => {
    const y = pad.t + h + 20 + row * (laneH + laneGap);
    ctx.fillStyle = "rgba(255,255,255,.05)";
    ctx.fillRect(pad.l, y, w, laneH);
    ctx.fillStyle = lane.color;
    let start = null;
    const flush = (endIdx) => {
      if (start == null) return;
      const x1 = xAt(start);
      const x2 = xAt(endIdx);
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
    ctx.fillStyle = "#8b9691";
    ctx.textAlign = "right";
    ctx.font = "10px Outfit, system-ui, sans-serif";
    ctx.fillText(lane.label, pad.l - 6, y + laneH - 2);
  });

  if (hoverIdx >= 0 && hoverIdx < points.length) {
    const x = xAt(hoverIdx);
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,.4)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + h);
    ctx.stroke();
    ctx.setLineDash([]);
    for (const s of SERIES) {
      const v = points[hoverIdx][s.key];
      if (v == null) continue;
      const y = s.axis === "temp" ? yTemp(v) : yHum(v);
      ctx.beginPath();
      ctx.fillStyle = s.color;
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }
}

function chartIndexAt(clientX) {
  const n = chartPoints.length;
  if (!n) return -1;
  const canvas = $("metricsChart");
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const g = chartGeom(canvas, n);
  if (x < g.pad.l || x > g.pad.l + g.w) return -1;
  if (n === 1) return 0;
  return Math.max(0, Math.min(n - 1, Math.round(((x - g.pad.l) / g.w) * (n - 1))));
}

function showChartTip(idx, clientX, clientY) {
  const tip = $("chartTip");
  if (idx < 0 || !chartPoints[idx]) {
    tip.classList.remove("show");
    return;
  }
  const p = chartPoints[idx];
  const when = new Date(p.t).toLocaleString("fr-FR", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const rows = [
    ...SERIES.map((s) => {
      const v = p[s.key];
      const text = v == null ? "—" : `${Number(v).toFixed(s.digits)} ${s.unit}`;
      return `<div class="row"><span><i class="dot" style="background:${s.color}"></i>${s.label}</span><b>${text}</b></div>`;
    }),
    ...LANES.map((lane) => {
      const v = p[lane.key];
      const text = v == null ? "—" : (v >= 0.5 ? "ON" : "OFF");
      return `<div class="row"><span><i class="dot" style="background:${lane.swatch}"></i>${lane.tip}</span><b>${text}</b></div>`;
    }),
  ].join("");
  tip.innerHTML = `<div class="when">${when}</div>${rows}`;
  tip.classList.add("show");
  const wrap = tip.parentElement;
  const bounds = wrap.getBoundingClientRect();
  let left = clientX - bounds.left + 14;
  let top = clientY - bounds.top + 14;
  if (left + tip.offsetWidth > bounds.width - 8) left = clientX - bounds.left - tip.offsetWidth - 14;
  if (top + tip.offsetHeight > bounds.height - 8) top = Math.max(8, bounds.height - tip.offsetHeight - 8);
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

async function refreshChart() {
  try {
    const data = await api("/api/metrics?window=" + encodeURIComponent(chartWindow));
    chartPoints = data.points || [];
    chartHover = -1;
    $("chartTip").classList.remove("show");
    drawMetricsChart(chartPoints, -1);
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

$("metricsChart").addEventListener("pointermove", (ev) => {
  const idx = chartIndexAt(ev.clientX);
  if (idx !== chartHover) {
    chartHover = idx;
    drawMetricsChart(chartPoints, idx);
  }
  showChartTip(idx, ev.clientX, ev.clientY);
});
$("metricsChart").addEventListener("pointerleave", () => {
  if (chartHover !== -1) {
    chartHover = -1;
    drawMetricsChart(chartPoints, -1);
  }
  $("chartTip").classList.remove("show");
});

function bumpCam() {
  $("cam").src = "/api/camera.jpg?t=" + Date.now();
}

function ventBody(mode) {
  return JSON.stringify({
    mode,
    speed: clampInput("ventSpeed"),
    interval_s: clampInput("ventInterval"),
    duration_s: clampInput("ventDuration"),
  });
}

async function applyVent(mode) {
  await api("/api/vent", { method: "POST", body: ventBody(mode) });
  refreshStatus();
}

async function applyVentParams() {
  await applyVent(ventMode || "off");
}

$("btnSetTemp").onclick = async () => {
  await api("/api/heater", { method: "POST", body: JSON.stringify({ temp: clampInput("temp") }) });
  refreshStatus();
};
$("btnForce").onclick = async () => {
  await api("/api/heater", {
    method: "POST",
    body: JSON.stringify({ temp: clampInput("forceTemp"), force: true }),
  });
  refreshStatus();
};
$("btnHeatAuto").onclick = async () => {
  await api("/api/heater", { method: "POST", body: JSON.stringify({ force: false }) });
  refreshStatus();
};
$("btnSetInterval").onclick = async () => {
  await api("/api/photo_interval", {
    method: "POST",
    body: JSON.stringify({ interval_s: clampInput("interval") }),
  });
  refreshStatus();
};
$("btnSetVentSpeed").onclick = () => applyVentParams();
$("btnSetVentInterval").onclick = () => applyVentParams();
$("btnSetVentDuration").onclick = () => applyVentParams();

$("btnStart").onclick = async () => {
  await api("/api/incubator/start", {
    method: "POST",
    body: JSON.stringify({ temp: clampInput("temp"), interval_s: clampInput("interval") }),
  });
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
$("btnVent").onclick = () => applyVent(ventMode === "off" ? "manual" : "off");

$("btnLight").onclick = async () => {
  const next = $("btnLight").dataset.on !== "1";
  await api("/api/light", {
    method: "POST",
    body: JSON.stringify({ on: next, mode: "manual", ...sunPlace() }),
  });
  bumpCam(); refreshStatus();
};
$("btnSettings").onclick = () => $("cfgDialog").showModal();
$("cfgCancel").onclick = () => $("cfgDialog").close();
$("cfgForm").onsubmit = async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        culture_temp_min: Number($("cfgTempMin").value),
        culture_temp_max: Number($("cfgTempMax").value),
        culture_hum_min: Number($("cfgHumMin").value),
        culture_hum_max: Number($("cfgHumMax").value),
        camera_url: $("cameraUrl").value,
        sun_lat: Number($("sunLat").value),
        sun_lon: Number($("sunLon").value),
      }),
    });
    $("cfgDialog").close();
    refreshStatus();
    bumpCam();
  } catch (e) {
    showErr(String(e.message || e));
  }
};

$("btnSnap").onclick = async () => {
  const r = await api("/api/snapshot", { method: "POST", body: "{}" });
  if (!r.ok) showErr(r.error || "Échec");
  refreshStatus(); refreshGallery(); bumpCam();
};
$("btnRefresh").onclick = () => bumpCam();
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
setInterval(refreshChart, 15000);
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

            if path == "/api/settings":
                return self._json(200, CTRL.update_settings(body))
            if path == "/api/camera":
                return self._json(200, CTRL.set_camera_url(str(body.get("url") or "")))
            if path == "/api/heater":
                if "force" in body and not body.get("force"):
                    return self._json(200, CTRL.release_heat_force())
                if body.get("force"):
                    return self._json(200, CTRL.force_bed(body.get("temp", CTRL.forced_bed_temp)))
                return self._json(200, CTRL.set_chamber_target(body.get("temp", CTRL.target_temp)))
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
            if path == "/api/incubator/interval":
                return self._json(
                    200,
                    CTRL.set_photo_interval(int(body.get("interval_s", CTRL.photo_interval_s))),
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
