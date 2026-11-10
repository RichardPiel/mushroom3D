#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>

#include "bme680_sensor.h"
#include "config.h"
#include "link.h"
#include "sdcp.h"
#include "ui_monitor.h"

namespace {
SdcpClient printer(PRINTER_IP);
ServerLink server_link(SERVER_BASE_URL, RELAY_TOKEN);
DesiredState desired;
String camera_url = CAMERA_URL;

uint32_t last_telemetry_ms = 0;
uint32_t last_heater_ms = 0;
uint32_t last_temp_ms = 0;
uint32_t last_ui_ms = 0;
BmeReading last_bme;
bool last_link_ok = false;
uint32_t vent_cycle_start_ms = 0;
uint32_t vent_off_at_ms = 0;
uint32_t last_vent_cmd_ms = 0;
uint32_t last_light_ms = 0;

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("[wifi] connexion à %s …\n", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] OK %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[wifi] échec");
  }
}

bool applyVent(bool on) {
  bool ok = printer.setBoxFan(on ? desired.vent_speed : 0);
  last_vent_cmd_ms = millis();
  if (ok) {
    desired.vent_on = on;
  }
  return ok;
}

void tickVentilation() {
  const uint32_t now = millis();
  if (desired.vent_mode == "cycle") {
    if (desired.vent_on) {
      desired.vent_remaining_s = (int)max(0L, (long)(vent_off_at_ms - now) / 1000L);
      if (now >= vent_off_at_ms) {
        applyVent(false);
      } else if (now - last_vent_cmd_ms >= HEATER_KEEPALIVE_MS) {
        applyVent(true);
      }
    } else {
      desired.vent_remaining_s =
          (int)max(0L, (long)(vent_cycle_start_ms + desired.vent_interval_s * 1000UL - now) / 1000L);
      if (now - vent_cycle_start_ms >= (uint32_t)desired.vent_interval_s * 1000UL) {
        vent_cycle_start_ms = now;
        vent_off_at_ms = now + (uint32_t)desired.vent_duration_s * 1000UL;
        applyVent(true);
      }
    }
  } else if (desired.vent_mode == "manual" && desired.vent_on) {
    desired.vent_remaining_s = -1;
    if (now - last_vent_cmd_ms >= HEATER_KEEPALIVE_MS) {
      applyVent(true);
    }
  } else {
    desired.vent_remaining_s = -1;
  }
}

void handleCommand(JsonObjectConst cmd, JsonObject result) {
  const char *id = cmd["id"] | "";
  const char *type = cmd["type"] | "";
  result["id"] = id;
  result["ok"] = false;
  result["error"] = "";

  if (strcmp(type, "heater_set") == 0) {
    float temp = cmd["temp"] | 0.0f;
    if (printer.setBedTemperature(temp)) {
      desired.target_temp = temp;
      desired.heater_on = temp > 0;
      last_heater_ms = millis();
      result["ok"] = true;
    } else {
      result["error"] = "heater_set failed";
    }
  } else if (strcmp(type, "heater_off") == 0) {
    printer.turnOffHeater();
    applyVent(false);
    desired.heater_on = false;
    desired.target_temp = 0;
    desired.vent_mode = "off";
    desired.vent_on = false;
    result["ok"] = true;
  } else if (strcmp(type, "light") == 0) {
    bool on = false;
    if (cmd["on"].is<bool>()) {
      on = cmd["on"].as<bool>();
    } else if (!cmd["on"].isNull()) {
      on = cmd["on"].as<int>() != 0;
    }
    // Cmd 403 bascule la lampe : renvoyer le même état l'éteint.
    const bool known = printer.state().light_known;
    const bool same = known && printer.state().light_on == on;
    const bool recent = desired.light_on == on && last_light_ms != 0 &&
                        millis() - last_light_ms < 8000;
    if (same || recent) {
      desired.light_on = on;
      result["ok"] = true;
    } else if (printer.setLight(on)) {
      desired.light_on = on;
      last_light_ms = millis();
      result["ok"] = true;
    } else {
      result["error"] = "light failed";
    }
  } else if (strcmp(type, "vent") == 0) {
    desired.vent_mode = String(cmd["mode"] | "off");
    desired.vent_speed = cmd["speed"] | desired.vent_speed;
    desired.vent_interval_s = cmd["interval_s"] | desired.vent_interval_s;
    desired.vent_duration_s = cmd["duration_s"] | desired.vent_duration_s;
    if (desired.vent_mode == "off") {
      applyVent(false);
    } else if (desired.vent_mode == "manual") {
      applyVent(desired.vent_speed > 0);
    } else if (desired.vent_mode == "cycle") {
      vent_cycle_start_ms = millis();
      vent_off_at_ms = vent_cycle_start_ms + (uint32_t)desired.vent_duration_s * 1000UL;
      applyVent(true);
    }
    result["ok"] = true;
  } else if (strcmp(type, "incubator_start") == 0) {
    float temp = cmd["temp"] | desired.target_temp;
    if (printer.setBedTemperature(temp)) {
      desired.target_temp = temp;
      desired.heater_on = true;
      last_heater_ms = millis();
      printer.enableVideoStream();
      result["ok"] = true;
    } else {
      result["error"] = "incubator_start failed";
    }
  } else if (strcmp(type, "incubator_stop") == 0) {
    bool turn_off = cmd["turn_off_heat"] | true;
    if (turn_off) {
      printer.turnOffHeater();
      desired.heater_on = false;
      desired.target_temp = 0;
    }
    result["ok"] = true;
  } else if (strcmp(type, "camera_url") == 0) {
    const char *url = cmd["url"] | "";
    if (url[0]) {
      camera_url = url;
      printer.enableVideoStream();
      result["ok"] = true;
    } else {
      result["error"] = "empty camera url";
    }
  } else {
    result["error"] = "unknown command";
  }
}

void pollCommands() {
  // Attente courte pour laisser tourner télémétrie / keepalive ; le serveur accepte jusqu'à 25 s.
  LinkResult res = server_link.getCommands(3);
  if (!res.ok) {
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, res.body)) {
    return;
  }
  JsonArray cmds = doc["commands"].as<JsonArray>();
  if (cmds.isNull() || cmds.size() == 0) {
    return;
  }
  JsonDocument ack_doc;
  JsonArray results = ack_doc.to<JsonArray>();
  for (JsonObject cmd : cmds) {
    JsonObject result = results.add<JsonObject>();
    handleCommand(cmd, result);
  }
  server_link.postAck(results);
}

void safetyWatchdog() {
  if (!desired.heater_on) {
    return;
  }
  uint32_t last = server_link.lastOkMs();
  if (last == 0) {
    return;
  }
  if (millis() - last < SERVER_WATCHDOG_MS) {
    return;
  }
  Serial.println("[safety] serveur muet 10 min — extinction plateau");
  printer.turnOffHeater();
  desired.heater_on = false;
  desired.target_temp = 0;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] mushroom3D relay");

  uiBegin();
  connectWifi();

  if (!bme680Begin(BME680_I2C_SDA, BME680_I2C_SCL, BME680_I2C_ADDR)) {
    Serial.println("[bme680] absent — télémétrie sans capteur");
  } else {
    Serial.println("[bme680] OK");
  }

  if (printer.discover() && printer.ensureConnected()) {
    Serial.printf("[sdcp] %s FW %s\n", printer.state().machine_name.c_str(),
                  printer.state().firmware.c_str());
  } else {
    Serial.println("[sdcp] imprimante injoignable au démarrage");
  }
}

void loop() {
  uiLoop();

  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }

  const uint32_t now = millis();

  if (now - last_temp_ms >= 8000) {
    last_temp_ms = now;
    printer.readTemperatures();
  }

  tickVentilation();

  if (desired.heater_on && now - last_heater_ms >= HEATER_KEEPALIVE_MS) {
    if (printer.setBedTemperature(desired.target_temp)) {
      last_heater_ms = now;
    }
  }

  if (now - last_telemetry_ms >= TELEMETRY_INTERVAL_MS) {
    last_telemetry_ms = now;
    last_bme = bme680Read();
    LinkResult tel =
        server_link.postTelemetry(printer.state(), last_bme, desired, camera_url.c_str());
    last_link_ok = tel.ok;
  }

  if (now - last_ui_ms >= 1000) {
    last_ui_ms = now;
    uiUpdate(printer.state(), last_bme, last_link_ok, desired.target_temp);
  }

  pollCommands();
  safetyWatchdog();
  delay(20);
}
