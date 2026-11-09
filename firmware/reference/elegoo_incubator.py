#!/usr/bin/env python3
"""
elegoo_incubator.py
-------------------
Maintient le plateau d'une Elegoo Centauri Carbon à température constante
(protocole SDCP v3 via WebSocket) et capture des photos périodiques pour un
time-lapse de culture de champignons.

IMPORTANT — modèle détecté sur le réseau :
  L'imprimante à 192.168.1.20 est une Centauri Carbon (1re gén.), firmware
  V1.1.40, SDCP sur le port 3030.
  Ce n'est PAS une Centauri Carbon 2 (MQTT 1883/9001), et Moonraker (7125)
  n'est pas exposé en firmware stock.

Dépendances (sans venv, Python système) :
    sudo apt install python3-opencv
    # ou : python3 -m pip install --user --break-system-packages opencv-python websocket-client
    python3 -m pip install --user --break-system-packages websocket-client
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import cv2
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException, create_connection

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PRINTER_IP: str = "192.168.1.20"
SDCP_WS_PORT: int = 3030          # SDCP WebSocket (Centauri Carbon 1)
SDCP_UDP_DISCOVERY_PORT: int = 3000
TARGET_BED_TEMP_C: float = 52.0

# Intervalle entre deux photos (secondes). Défaut : 30 minutes.
PHOTO_INTERVAL_S: int = 1800

# Ventilation de la chambre (BoxFan, 0–100 %). Dans la boucle tête :
# marche à VENT_SPEED pendant VENT_ON_S, toutes les VENT_INTERVAL_S.
# VENT_SPEED = 0 désactive le cycle automatique.
VENT_SPEED: int = 60
VENT_INTERVAL_S: int = 1800
VENT_ON_S: int = 120

# Ré-envoi de la consigne plateau (secondes). Le firmware coupe souvent
# le chauffage après ~15–30 min d'inactivité → keepalive nécessaire.
HEATER_KEEPALIVE_S: int = 60

# WebSocket ping / reconnexion
WS_RECV_TIMEOUT_S: float = 2.0
WS_RECONNECT_DELAY_S: float = 5.0
UDP_DISCOVERY_TIMEOUT_S: float = 3.0

OUTPUT_DIR: Path = Path("./timelapse_champignons")
IMAGE_PREFIX: str = "champignon"
IMAGE_EXT: str = ".jpg"
# Caméra intégrée de l'imprimante (pas de webcam USB sur ce PC).
# Activée via SDCP Cmd 386 → MJPEG http://IP:3031/video
CAMERA_WARMUP_S: float = 1.5
CAMERA_READ_RETRIES: int = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("elegoo_incubator")

# ---------------------------------------------------------------------------
# SDCP helpers (Centauri Carbon / SDCP v3)
# ---------------------------------------------------------------------------

CMD_CHANGE_PRINT_PARAMS = 403
CMD_VIDEO_STREAM = 386  # Enable/Disable → VideoUrl MJPEG


def discover_mainboard_id(ip: str) -> str:
    """Découverte UDP SDCP (port 3000) → MainboardID."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(UDP_DISCOVERY_TIMEOUT_S)
    try:
        sock.sendto(b"M99999", (ip, SDCP_UDP_DISCOVERY_PORT))
        raw, _addr = sock.recvfrom(4096)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        data = payload.get("Data") or {}
        mb_id = data.get("MainboardID")
        if not mb_id:
            raise RuntimeError(f"Réponse UDP sans MainboardID : {payload!r}")
        log.info(
            "Imprimante : %s | FW %s | MainboardID=%s",
            data.get("MachineName") or data.get("Name"),
            data.get("FirmwareVersion"),
            mb_id,
        )
        return mb_id
    finally:
        sock.close()


def build_sdcp_request(cmd: int, data: dict, mainboard_id: str) -> dict:
    req_id = secrets.token_hex(8)
    return {
        "Id": mainboard_id,
        "Data": {
            "Cmd": int(cmd),
            "Data": data,
            "RequestID": req_id,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time() * 1000),
            "From": 1,  # SDCP_FROM_WEB_PC
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


class SdcpPrinter:
    """Client WebSocket SDCP minimal (consigne plateau + keepalive)."""

    def __init__(self, ip: str, mainboard_id: str) -> None:
        self.ip = ip
        self.mainboard_id = mainboard_id
        self.ws = None
        self.model_fan = 0
        self.aux_fan = 0
        self.box_fan = 0
        self.last_machine_status: list[int] = []
        self._io = threading.Lock()

    @property
    def url(self) -> str:
        return f"ws://{self.ip}:{SDCP_WS_PORT}/websocket"

    def connect(self) -> None:
        self.close()
        log.info("Connexion SDCP %s …", self.url)
        self.ws = create_connection(self.url, timeout=10)
        self.ws.settimeout(WS_RECV_TIMEOUT_S)
        log.info("WebSocket connecté")

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def ensure_connected(self) -> bool:
        if self.ws is not None:
            return True
        try:
            self.connect()
            return True
        except OSError as exc:
            log.warning("Imprimante injoignable (%s) : %s", self.url, exc)
            return False
        except Exception as exc:
            log.warning("Échec connexion SDCP : %s", exc)
            return False

    def send_command(self, cmd: int, data: dict, timeout_s: float = 8.0) -> dict | None:
        """Envoie une commande SDCP et attend l'Ack du même Cmd.

        L'imprimante pousse des états en continu sur le même socket. Le premier
        message n'est presque jamais la réponse : on l'ignore jusqu'à Data.Cmd
        égal à la commande envoyée et un champ Ack.
        """
        with self._io:
            return self._send_command(cmd, data, timeout_s)

    def _send_command(self, cmd: int, data: dict, timeout_s: float) -> dict | None:
        if not self.ensure_connected():
            return None
        envelope = build_sdcp_request(cmd, data, self.mainboard_id)
        try:
            assert self.ws is not None
            self.ws.send(json.dumps(envelope))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    raw = self.ws.recv()
                except WebSocketTimeoutException:
                    continue
                resp = json.loads(raw)
                wrap = resp.get("Data")
                if not isinstance(wrap, dict) or wrap.get("Cmd") != cmd:
                    continue
                payload = wrap.get("Data") or {}
                if not isinstance(payload, dict) or "Ack" not in payload:
                    continue
                ack = payload.get("Ack")
                if ack != 0:
                    log.warning("Ack SDCP=%s pour Cmd %s", ack, cmd)
                else:
                    log.debug("Cmd %s OK (Ack=%s)", cmd, ack)
                return payload
            log.warning("Pas de réponse Cmd %s", cmd)
            return {}
        except (OSError, WebSocketConnectionClosedException, WebSocketTimeoutException) as exc:
            log.warning("Erreur réseau SDCP : %s — reconnexion au prochain cycle", exc)
            self.close()
            return None
        except Exception as exc:
            log.warning("Envoi SDCP échoué : %s", exc)
            self.close()
            return None

    def set_bed_temperature(self, temp_c: float) -> bool:
        if not (0.0 <= temp_c <= 110.0):
            raise ValueError(f"Température plateau hors plage 0–110 °C : {temp_c}")
        ok = self.send_command(
            CMD_CHANGE_PRINT_PARAMS,
            {
                "TempTargetHotbed": float(round(temp_c, 1)),
                "TempTargetNozzle": 0.0,
            },
        )
        if ok is not None:
            log.info("Consigne plateau : %.1f °C (Cmd 403)", temp_c)
            return True
        return False

    def turn_off_heater(self) -> bool:
        log.info("Extinction du plateau (TempTargetHotbed=0)…")
        return self.set_bed_temperature(0.0)

    def set_box_fan(self, percent: int) -> bool:
        """Ventilation chambre (BoxFan). Préserve ModelFan et AuxiliaryFan."""
        percent = max(0, min(100, int(percent)))
        payload = self.send_command(
            CMD_CHANGE_PRINT_PARAMS,
            {
                "TargetFanSpeed": {
                    "ModelFan": int(self.model_fan),
                    "AuxiliaryFan": int(self.aux_fan),
                    "BoxFan": percent,
                }
            },
        )
        if payload is None:
            return False
        self.box_fan = percent
        log.info("Ventilation chambre (BoxFan) : %s%%", percent)
        return True

    def set_light(self, on: bool) -> bool:
        """Allume / éteint l'éclairage chambre (Cmd 403 LightStatus.SecondLight)."""
        payload = self.send_command(
            CMD_CHANGE_PRINT_PARAMS,
            {"LightStatus": {"SecondLight": 1 if on else 0}},
        )
        if payload is None:
            return False
        log.info("Flash / lumière chambre : %s", "ON" if on else "OFF")
        return True

    def enable_video_stream(self) -> str | None:
        """
        Active le flux MJPEG intégré (Cmd 386).
        Retourne l'URL http://…:3031/video ou None.
        """
        payload = self.send_command(CMD_VIDEO_STREAM, {"Enable": 1})
        if payload is None:
            return None
        video_url = payload.get("VideoUrl")
        if not video_url:
            log.error("Cmd 386 sans VideoUrl (Ack=%s)", payload.get("Ack"))
            return None
        if not str(video_url).startswith("http"):
            video_url = f"http://{video_url}"
        log.info("Flux caméra activé : %s", video_url)
        return str(video_url)

    def disable_video_stream(self) -> None:
        log.info("Extinction du flux caméra (Cmd 386 Enable=0)…")
        self.send_command(CMD_VIDEO_STREAM, {"Enable": 0})

    def read_temperatures(self) -> dict[str, Any]:
        """
        Demande un status (Cmd 0) et lit TempOfHotbed / TempTargetHotbed
        depuis la réponse ou le prochain push status.
        """
        with self._io:
            return self._read_temperatures()

    def _read_temperatures(self) -> dict[str, Any]:
        if not self.ensure_connected():
            return {"bed_actual": None, "bed_target": None, "nozzle_actual": None}
        envelope = build_sdcp_request(0, {}, self.mainboard_id)
        result: dict[str, Any] = {
            "bed_actual": None,
            "bed_target": None,
            "nozzle_actual": None,
            "box_fan": None,
            "z_pos": None,
            "machine_status": list(self.last_machine_status),
        }
        try:
            assert self.ws is not None
            self.ws.send(json.dumps(envelope))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    raw = self.ws.recv()
                except WebSocketTimeoutException:
                    continue
                msg = json.loads(raw)
                topic = str(msg.get("Topic") or "")
                status = msg.get("Status")
                if status is None and "status" in topic:
                    status = (msg.get("Data") or {}).get("Status") or msg.get("Data")
                if not isinstance(status, dict):
                    # parfois Status est à la racine Data
                    data = msg.get("Data")
                    if isinstance(data, dict) and "TempOfHotbed" in data:
                        status = data
                    elif isinstance(data, dict) and isinstance(data.get("Data"), dict):
                        inner = data["Data"]
                        if "TempOfHotbed" in inner:
                            status = inner
                if isinstance(status, dict) and (
                    "TempOfHotbed" in status or "TempTargetHotbed" in status
                ):
                    result["bed_actual"] = _as_float(status.get("TempOfHotbed"))
                    result["bed_target"] = _as_float(status.get("TempTargetHotbed"))
                    result["nozzle_actual"] = _as_float(status.get("TempOfNozzle"))
                    fans = status.get("CurrentFanSpeed")
                    if isinstance(fans, dict):
                        model = _as_float(fans.get("ModelFan"))
                        aux = _as_float(fans.get("AuxiliaryFan"))
                        box = _as_float(fans.get("BoxFan"))
                        if model is not None:
                            self.model_fan = int(round(model))
                        if aux is not None:
                            self.aux_fan = int(round(aux))
                        result["box_fan"] = box
                    coord = status.get("CurrenCoord") or status.get("CurrentCoord")
                    if isinstance(coord, str) and coord.count(",") >= 2:
                        result["z_pos"] = _as_float(coord.split(",")[2])
                    raw_state = status.get("CurrentStatus")
                    if isinstance(raw_state, list):
                        self.last_machine_status = [
                            int(x) for x in raw_state if isinstance(x, (int, float))
                        ]
                    elif isinstance(raw_state, int):
                        self.last_machine_status = [raw_state]
                    result["machine_status"] = list(self.last_machine_status)
                    break
        except (OSError, WebSocketConnectionClosedException) as exc:
            log.warning("Lecture températures impossible : %s", exc)
            self.close()
        return result


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Capture photo
# ---------------------------------------------------------------------------


def next_image_index(directory: Path) -> int:
    existing = sorted(directory.glob(f"{IMAGE_PREFIX}_*{IMAGE_EXT}"))
    if not existing:
        return 1
    last_name = existing[-1].stem
    try:
        return int(last_name.rsplit("_", 1)[-1]) + 1
    except ValueError:
        return len(existing) + 1


def capture_photo(directory: Path, index: int, stream_url: str) -> Path | None:
    """Capture une frame depuis le flux MJPEG de l'imprimante."""
    filename = f"{IMAGE_PREFIX}_{index:04d}{IMAGE_EXT}"
    filepath = directory / filename

    cap = cv2.VideoCapture(stream_url)
    if not cap.isOpened():
        log.error("Impossible d'ouvrir le flux caméra : %s", stream_url)
        return None

    try:
        time.sleep(CAMERA_WARMUP_S)
        frame = None
        for attempt in range(1, CAMERA_READ_RETRIES + 1):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
            log.warning("Frame vide (essai %s/%s)", attempt, CAMERA_READ_RETRIES)
            time.sleep(0.4)
        else:
            log.error("Échec de la lecture de frame depuis %s", stream_url)
            return None

        if not cv2.imwrite(str(filepath), frame):
            log.error("Échec d'écriture : %s", filepath)
            return None
        log.info("Photo enregistrée : %s", filepath)
        return filepath
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------


def run() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    photo_index = next_image_index(OUTPUT_DIR)

    try:
        mainboard_id = discover_mainboard_id(PRINTER_IP)
    except (OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        log.error("Découverte UDP impossible : %s", exc)
        log.error(
            "Vérifiez l'IP, le Wi-Fi, et que l'imprimante est bien une "
            "Centauri Carbon (SDCP port 3030), pas une Carbon 2 (MQTT 1883)."
        )
        raise

    printer = SdcpPrinter(PRINTER_IP, mainboard_id)

    log.info(
        "Démarrage — IP=%s temp=%s°C intervalle_photo=%ss keepalive=%ss "
        "ventilation=%s%% on=%ss / %ss",
        PRINTER_IP,
        TARGET_BED_TEMP_C,
        PHOTO_INTERVAL_S,
        HEATER_KEEPALIVE_S,
        VENT_SPEED,
        VENT_ON_S,
        VENT_INTERVAL_S,
    )

    printer.set_bed_temperature(TARGET_BED_TEMP_C)
    stream_url = printer.enable_video_stream()
    if not stream_url:
        raise RuntimeError(
            "Impossible d'activer la caméra imprimante (Cmd 386). "
            "Vérifiez qu'aucun autre client (slicer, app) n'utilise déjà le flux."
        )

    last_heater_t = time.monotonic()
    last_photo_t = time.monotonic()
    vent_enabled = VENT_SPEED > 0 and VENT_ON_S > 0 and VENT_INTERVAL_S > VENT_ON_S
    vent_on = False
    vent_cycle_start = time.monotonic()
    vent_off_at = vent_cycle_start
    last_vent_cmd = 0.0
    if vent_enabled:
        now0 = time.monotonic()
        vent_on = printer.set_box_fan(VENT_SPEED)
        last_vent_cmd = now0
        if vent_on:
            vent_cycle_start = now0
            vent_off_at = now0 + VENT_ON_S
        else:
            vent_cycle_start = now0 - VENT_INTERVAL_S

    if capture_photo(OUTPUT_DIR, photo_index, stream_url) is not None:
        photo_index += 1

    try:
        while True:
            now = time.monotonic()

            if now - last_heater_t >= HEATER_KEEPALIVE_S:
                printer.set_bed_temperature(TARGET_BED_TEMP_C)
                last_heater_t = now

            if vent_enabled:
                if vent_on:
                    if now >= vent_off_at:
                        if printer.set_box_fan(0):
                            vent_on = False
                        last_vent_cmd = now
                    elif now - last_vent_cmd >= HEATER_KEEPALIVE_S:
                        printer.set_box_fan(VENT_SPEED)
                        last_vent_cmd = now
                elif now - vent_cycle_start >= VENT_INTERVAL_S:
                    vent_cycle_start = now
                    vent_off_at = now + VENT_ON_S
                    vent_on = printer.set_box_fan(VENT_SPEED)
                    last_vent_cmd = now

            if now - last_photo_t >= PHOTO_INTERVAL_S:
                # Réactive le flux si un autre client l'a coupé
                if stream_url is None:
                    stream_url = printer.enable_video_stream()
                if stream_url and capture_photo(OUTPUT_DIR, photo_index, stream_url) is not None:
                    photo_index += 1
                elif not stream_url:
                    stream_url = printer.enable_video_stream()
                last_photo_t = now

            time.sleep(1.0)
    finally:
        printer.set_box_fan(0)
        printer.disable_video_stream()
        printer.turn_off_heater()
        printer.close()


def main() -> int:
    try:
        run()
    except KeyboardInterrupt:
        log.info("Interruption clavier — arrêt propre…")
    except Exception:
        log.exception("Erreur inattendue")
        return 1

    log.info("Terminé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
