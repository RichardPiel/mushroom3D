#pragma once

#include <Arduino.h>

struct PrinterState {
  bool connected = false;
  String ip;
  String machine_name = "—";
  String firmware = "—";
  String mainboard_id;
  float bed_actual = NAN;
  float bed_target = NAN;
  float nozzle_actual = NAN;
  float chamber_c = NAN;
  float chamber_target_c = NAN;
  bool light_on = false;
  bool light_known = false;
  int box_fan = 0;
};

class SdcpClient {
 public:
  explicit SdcpClient(const char *ip);

  bool discover();
  bool ensureConnected();
  void close();

  bool setBedTemperature(float temp_c);
  bool turnOffHeater();
  bool setBoxFan(int percent);
  bool setLight(bool on);
  bool enableVideoStream();
  bool disableVideoStream();
  bool readTemperatures();

  const PrinterState &state() const { return state_; }
  const String &ip() const { return ip_; }

 private:
  String ip_;
  PrinterState state_;
  void *ws_ = nullptr;  // WebSocketsClient*
  int model_fan_ = 0;
  int aux_fan_ = 0;

  bool sendCommand(int cmd, const String &data_json, String *ack_payload = nullptr);
};
