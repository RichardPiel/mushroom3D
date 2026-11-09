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

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
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
IMAGE_PREFIX = "champignon"
IMAGE_EXT = ".jpg"

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

        self.target_temp = float(DEFAULT_TARGET_TEMP)
        self.photo_interval_s = int(DEFAULT_PHOTO_INTERVAL_S)
        self.heater_on = False
        self.incubator_running = False
        self.light_on = False

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
        self._refresh_photo_count()

    def shutdown(self) -> None:
        self._stop.set()
        self._cmd_event.set()

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
                "heater_on": self.heater_on,
                "incubator_running": self.incubator_running,
                "camera_on": bool(self.camera_url),
                "camera_url": self.camera_url,
                "light_on": self.light_on,
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
            return self.status()

    def start_incubator(self, temp_c: float | None = None, interval_s: int | None = None) -> dict[str, Any]:
        with self.lock:
            if temp_c is not None:
                self.target_temp = float(temp_c)
            if interval_s is not None:
                self.photo_interval_s = max(10, int(interval_s))
            self.heater_on = True
            self.incubator_running = True
            self.started_at = time.time()
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

    def set_light(self, on: bool) -> dict[str, Any]:
        with self.lock:
            self.light_on = bool(on)
            self._enqueue({"type": "light", "on": bool(on)})
            self.last_error = ""
            return self.status()

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
                <label for="interval">Photo / s</label>
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
      <h2>Time-lapse</h2>
      <div class="body">
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
    ? `État : incubateur ON · dernière photo ${s.last_photo || "—"}`
    : (s.heater_on ? "État : chauffe manuelle" : "État : veille");

  const lightBtn = $("btnLight");
  lightBtn.textContent = s.light_on ? "Flash off" : "Flash";
  lightBtn.className = s.light_on ? "btn-ghost" : "btn-gold";
  lightBtn.dataset.on = s.light_on ? "1" : "0";

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

function bumpCam() {
  $("cam").src = "/api/camera.jpg?t=" + Date.now();
}

$("btnSetTemp").onclick = async () => {
  await api("/api/heater", { method: "POST", body: JSON.stringify({ temp: +$("temp").value }) });
  refreshStatus();
};
$("btnStart").onclick = async () => {
  await api("/api/incubator/start", {
    method: "POST",
    body: JSON.stringify({ temp: +$("temp").value, interval_s: +$("interval").value }),
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
$("btnLight").onclick = async () => {
  const next = $("btnLight").dataset.on !== "1";
  await api("/api/light", { method: "POST", body: JSON.stringify({ on: next }) });
  bumpCam(); refreshStatus();
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
setInterval(refreshStatus, 4000);
setInterval(bumpCam, 2500);
setInterval(refreshGallery, 15000);
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
            if path == "/api/light":
                return self._json(200, CTRL.set_light(bool(body.get("on", True))))
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
