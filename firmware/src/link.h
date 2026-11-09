#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include "bme680_sensor.h"
#include "sdcp.h"

struct DesiredState {
  bool heater_on = false;
  float target_temp = 0;
  bool light_on = false;
  String vent_mode = "off";
  int vent_speed = 60;
  int vent_interval_s = 1800;
  int vent_duration_s = 120;
  bool vent_on = false;
  int vent_remaining_s = -1;
};

struct LinkResult {
  bool ok = false;
  int http = 0;
  String body;
};

class ServerLink {
 public:
  ServerLink(const char *base_url, const char *token);

  bool wifiOk() const;
  uint32_t lastOkMs() const { return last_ok_ms_; }

  LinkResult postTelemetry(const PrinterState &printer, const BmeReading &bme,
                           const DesiredState &desired, const char *camera_url);
  LinkResult getCommands(int wait_s);
  LinkResult postAck(JsonArrayConst results);

 private:
  String base_;
  String token_;
  uint32_t last_ok_ms_ = 0;

  LinkResult request(const char *method, const String &path, const String &body, int timeout_ms);
};
