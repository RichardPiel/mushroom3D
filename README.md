# mushroom3D

Incubateur Centauri Carbon : **serveur distant** + **relais ESP32** (salon) + capteur **BME680**.

```
server/     site HTTPS (Dokploy), file d'ordres, proxy caméra
firmware/   Waveshare ESP32-S3-Touch-LCD-2.8 (SDCP, BME680, moniteur)
protocol/   contrat JSON carte ↔ serveur
```

## Serveur

```bash
cd server
INCUBATOR_PASSWORD='…' RELAY_TOKEN='…' CAMERA_URL='http://192.168.1.20:3031/video' \
  python3 incubator_ui.py
```

### Dokploy

L’appli actuelle `incubateur-ui-…` est une **Application** (chemin
`/etc/dokploy/applications/…`). Il faut un service **Compose** à part.

1. Projet → **Create Service** → **Compose** (type Docker Compose)
2. Provider GitHub, repo `mushroom3D`, branche `main`
3. **Compose Path** : `./docker-compose.yml` (comme dans la [doc Dokploy](https://docs.dokploy.com/docs/core/docker-compose/example), sans slash final)
4. Variables : `INCUBATOR_PASSWORD`, `RELAY_TOKEN`, optionnellement `CAMERA_URL`
5. Alerte BME : `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`, `SMTP_TLS` (`starttls`, `ssl` ou `none`), `INCUBATOR_URL`

Sans `SMTP_HOST`, aucun mail n'est envoyé. Une perte de température ou d'humidité BME (60 s) déclenche un mail, deux au plus par heure. Le message contient `INCUBATOR_URL`. Modèle : `.env.example`.
6. Domaines → service `incubator`, port **8765**

Ne pas réutiliser l’ancienne Application Nixpacks/Dockerfile.

L’URL du flux MJPEG se règle aussi dans l’interface (**Caméra**). Elle doit être **joignable depuis le serveur** (VPN / Tailscale). Exemple Centauri : `http://<IP>:3031/video`.

## Firmware

1. Copier `firmware/include/config.example.h` → `firmware/include/config.h`
2. Renseigner Wi-Fi, `SERVER_BASE_URL`, `RELAY_TOKEN`, `PRINTER_IP`, `CAMERA_URL`, broches BME680
3. `cd firmware && pio run -t upload`

La carte ouvre SDCP (UDP 3000 / WS 3030), lit le BME680, pousse `/api/link/telemetry` et tire `/api/link/commands`. Keepalive plateau 60 s. Si le serveur ne répond plus 10 min → plateau à 0.

## Sécurité

Ne pas ouvrir 3030 / 3031 / 3000 sur la box. Seuls 80/443 du serveur public.
