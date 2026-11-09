#pragma once

// Copier ce fichier vers include/config.h et renseigner les valeurs.

#define WIFI_SSID "votre-wifi"
#define WIFI_PASSWORD "votre-mot-de-passe"

// Serveur distant (Dokploy), sans slash final.
#define SERVER_BASE_URL "https://incubateur.example.com"
#define RELAY_TOKEN "change-me"

#define PRINTER_IP "192.168.1.20"

// Flux MJPEG Centauri (Cmd 386 active le serveur ; l'URL reste configurable).
#define CAMERA_URL "http://192.168.1.20:3031/video"

// BME680 sur le connecteur I2C Waveshare (même bus que IMU/RTC : SDA=11, SCL=10).
// Touch CST328 est sur un autre bus (GPIO 1/3) — ne pas mélanger.
#define BME680_I2C_SDA 11
#define BME680_I2C_SCL 10
#define BME680_I2C_ADDR 0x76

#define TELEMETRY_INTERVAL_MS 5000
#define HEATER_KEEPALIVE_MS 60000
#define SERVER_WATCHDOG_MS (10UL * 60UL * 1000UL)
#define COMMAND_LONG_POLL_S 25
