#include "link.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include <cmath>

ServerLink::ServerLink(const char *base_url, const char *token)
    : base_(base_url), token_(token) {
  while (base_.endsWith("/")) {
    base_.remove(base_.length() - 1);
  }
}

bool ServerLink::wifiOk() const { return WiFi.status() == WL_CONNECTED; }

LinkResult ServerLink::request(const char *method, const String &path, const String &body,
                               int timeout_ms) {
  LinkResult out;
  if (!wifiOk()) {
    return out;
  }
  String url = base_ + path;
  HTTPClient http;
  WiFiClientSecure secure;
  bool tls = url.startsWith("https://");
  if (tls) {
    secure.setInsecure();
    if (!http.begin(secure, url)) {
      return out;
    }
  } else if (!http.begin(url)) {
    return out;
  }
  http.setTimeout(timeout_ms);
  http.addHeader("Authorization", String("Bearer ") + token_);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Accept", "application/json");

  int code = -1;
  if (strcmp(method, "GET") == 0) {
    code = http.GET();
  } else {
    code = http.POST(body);
  }
  out.http = code;
  out.body = http.getString();
  out.ok = code > 0 && code < 400;
  if (out.ok) {
    last_ok_ms_ = millis();
  }
  http.end();
  return out;
}

static void putFloat(JsonObject obj, const char *key, float value) {
  if (isnan(value)) {
    obj[key] = nullptr;
  } else {
    obj[key] = value;
  }
}

LinkResult ServerLink::postTelemetry(const PrinterState &printer, const BmeReading &bme,
                                     const DesiredState &desired, const char *camera_url) {
  JsonDocument doc;
  doc["ts"] = (long)(millis() / 1000);

  JsonObject p = doc["printer"].to<JsonObject>();
  p["connected"] = printer.connected;
  p["ip"] = printer.ip;
  p["machine_name"] = printer.machine_name;
  p["firmware"] = printer.firmware;
  putFloat(p, "bed_actual", printer.bed_actual);
  putFloat(p, "bed_target", printer.bed_target);
  putFloat(p, "nozzle_actual", printer.nozzle_actual);
  p["light_on"] = printer.light_on;
  p["box_fan"] = printer.box_fan;
  p["camera_url"] = camera_url;

  JsonObject b = doc["bme680"].to<JsonObject>();
  b["ok"] = bme.ok;
  putFloat(b, "temperature_c", bme.temperature_c);
  putFloat(b, "humidity_pct", bme.humidity_pct);
  putFloat(b, "pressure_hpa", bme.pressure_hpa);
  putFloat(b, "gas_ohms", bme.gas_ohms);

  JsonObject d = doc["desired"].to<JsonObject>();
  d["heater_on"] = desired.heater_on;
  d["target_temp"] = desired.target_temp;
  d["light_on"] = desired.light_on;
  d["vent_mode"] = desired.vent_mode;
  d["vent_speed"] = desired.vent_speed;
  d["vent_interval_s"] = desired.vent_interval_s;
  d["vent_duration_s"] = desired.vent_duration_s;
  d["vent_on"] = desired.vent_on;
  if (desired.vent_remaining_s >= 0) {
    d["vent_remaining_s"] = desired.vent_remaining_s;
  }

  String body;
  serializeJson(doc, body);
  return request("POST", "/api/link/telemetry", body, 8000);
}

LinkResult ServerLink::getCommands(int wait_s) {
  String path = String("/api/link/commands?wait=") + String(wait_s);
  return request("GET", path, "", (wait_s + 5) * 1000);
}

LinkResult ServerLink::postAck(JsonArrayConst results) {
  JsonDocument doc;
  JsonArray arr = doc["results"].to<JsonArray>();
  for (JsonVariantConst v : results) {
    arr.add(v);
  }
  String body;
  serializeJson(doc, body);
  return request("POST", "/api/link/ack", body, 8000);
}
