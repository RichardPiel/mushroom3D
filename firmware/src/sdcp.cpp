#include "sdcp.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebSocketsClient.h>

#include <cmath>

namespace {
constexpr uint16_t kUdpPort = 3000;
constexpr uint16_t kWsPort = 3030;
constexpr int kCmdParams = 403;
constexpr int kCmdVideo = 386;
constexpr int kCmdStatus = 0;

WebSocketsClient g_ws;
SdcpClient *g_owner = nullptr;
String g_last_raw;
volatile bool g_got_msg = false;

void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {
  if (type == WStype_TEXT && payload && length) {
    g_last_raw = String(reinterpret_cast<char *>(payload), length);
    g_got_msg = true;
  } else if (type == WStype_DISCONNECTED && g_owner) {
    // state updated by ensureConnected callers
  }
}
}  // namespace

SdcpClient::SdcpClient(const char *ip) : ip_(ip) { state_.ip = ip; }

bool SdcpClient::discover() {
  WiFiUDP udp;
  if (!udp.begin(0)) {
    return false;
  }
  IPAddress dest;
  if (!dest.fromString(ip_)) {
    udp.stop();
    return false;
  }
  const char *probe = "M99999";
  udp.beginPacket(dest, kUdpPort);
  udp.write(reinterpret_cast<const uint8_t *>(probe), strlen(probe));
  udp.endPacket();

  const uint32_t deadline = millis() + 2500;
  while (millis() < deadline) {
    int n = udp.parsePacket();
    if (n <= 0) {
      delay(20);
      continue;
    }
    char buf[1024];
    int got = udp.read(buf, sizeof(buf) - 1);
    if (got <= 0) {
      continue;
    }
    buf[got] = 0;
    JsonDocument doc;
    if (deserializeJson(doc, buf)) {
      continue;
    }
    JsonObject data = doc["Data"].as<JsonObject>();
    const char *mb = data["MainboardID"] | "";
    if (!mb[0]) {
      continue;
    }
    state_.mainboard_id = mb;
    state_.machine_name = String(data["MachineName"] | data["Name"] | "Centauri");
    state_.firmware = String(data["FirmwareVersion"] | "—");
    udp.stop();
    return true;
  }
  udp.stop();
  return false;
}

bool SdcpClient::ensureConnected() {
  if (state_.mainboard_id.isEmpty() && !discover()) {
    state_.connected = false;
    return false;
  }
  if (g_ws.isConnected()) {
    state_.connected = true;
    return true;
  }
  g_owner = this;
  g_ws.onEvent(onWsEvent);
  g_ws.begin(ip_.c_str(), kWsPort, "/websocket");
  g_ws.setReconnectInterval(3000);
  const uint32_t deadline = millis() + 8000;
  while (millis() < deadline) {
    g_ws.loop();
    if (g_ws.isConnected()) {
      state_.connected = true;
      return true;
    }
    delay(20);
  }
  state_.connected = false;
  return false;
}

void SdcpClient::close() {
  g_ws.disconnect();
  state_.connected = false;
}

bool SdcpClient::sendCommand(int cmd, const String &data_json, String *ack_payload) {
  if (!ensureConnected()) {
    return false;
  }
  JsonDocument env;
  env["Id"] = state_.mainboard_id;
  JsonObject data = env["Data"].to<JsonObject>();
  data["Cmd"] = cmd;
  JsonDocument inner;
  if (deserializeJson(inner, data_json)) {
    return false;
  }
  data["Data"] = inner.as<JsonVariant>();
  data["RequestID"] = String(esp_random(), HEX);
  data["MainboardID"] = state_.mainboard_id;
  data["TimeStamp"] = (long long)millis();
  data["From"] = 1;
  env["Topic"] = String("sdcp/request/") + state_.mainboard_id;

  String out;
  serializeJson(env, out);
  g_got_msg = false;
  g_last_raw = "";
  g_ws.sendTXT(out);

  const uint32_t deadline = millis() + 8000;
  while (millis() < deadline) {
    g_ws.loop();
    if (!g_got_msg) {
      delay(10);
      continue;
    }
    g_got_msg = false;
    JsonDocument resp;
    if (deserializeJson(resp, g_last_raw)) {
      continue;
    }
    JsonObject wrap = resp["Data"].as<JsonObject>();
    if ((int)(wrap["Cmd"] | -1) != cmd) {
      continue;
    }
    JsonObject payload = wrap["Data"].as<JsonObject>();
    if (!payload["Ack"].is<int>()) {
      continue;
    }
    if (ack_payload) {
      serializeJson(payload, *ack_payload);
    }
    return (int)(payload["Ack"] | -1) == 0;
  }
  return false;
}

bool SdcpClient::setBedTemperature(float temp_c) {
  if (temp_c < 0 || temp_c > 110) {
    return false;
  }
  char buf[96];
  snprintf(buf, sizeof(buf), "{\"TempTargetHotbed\":%.1f,\"TempTargetNozzle\":0.0}", temp_c);
  if (!sendCommand(kCmdParams, buf)) {
    return false;
  }
  state_.bed_target = temp_c;
  return true;
}

bool SdcpClient::turnOffHeater() { return setBedTemperature(0.0f); }

bool SdcpClient::setBoxFan(int percent) {
  if (percent < 0) {
    percent = 0;
  }
  if (percent > 100) {
    percent = 100;
  }
  char buf[160];
  snprintf(buf, sizeof(buf),
           "{\"TargetFanSpeed\":{\"ModelFan\":%d,\"AuxiliaryFan\":%d,\"BoxFan\":%d}}",
           model_fan_, aux_fan_, percent);
  if (!sendCommand(kCmdParams, buf)) {
    return false;
  }
  state_.box_fan = percent;
  return true;
}

bool SdcpClient::setLight(bool on) {
  char buf[64];
  snprintf(buf, sizeof(buf), "{\"LightStatus\":{\"SecondLight\":%d}}", on ? 1 : 0);
  if (!sendCommand(kCmdParams, buf)) {
    return false;
  }
  state_.light_on = on;
  return true;
}

bool SdcpClient::enableVideoStream() {
  String ack;
  if (!sendCommand(kCmdVideo, "{\"Enable\":1}", &ack)) {
    return false;
  }
  return true;
}

bool SdcpClient::disableVideoStream() {
  return sendCommand(kCmdVideo, "{\"Enable\":0}");
}

bool SdcpClient::readTemperatures() {
  String ack;
  // Status push: ask Cmd 0 then parse next status-like message from the socket.
  sendCommand(kCmdStatus, "{}", &ack);
  const uint32_t deadline = millis() + 3000;
  while (millis() < deadline) {
    g_ws.loop();
    if (!g_got_msg) {
      delay(10);
      continue;
    }
    g_got_msg = false;
    JsonDocument resp;
    if (deserializeJson(resp, g_last_raw)) {
      continue;
    }
    JsonObject status = resp["Status"].as<JsonObject>();
    if (status.isNull()) {
      JsonObject data = resp["Data"].as<JsonObject>();
      status = data["Status"].as<JsonObject>();
    }
    if (status.isNull()) {
      continue;
    }
    if (!status["TempOfHotbed"].isNull()) {
      state_.bed_actual = status["TempOfHotbed"].as<float>();
    }
    if (!status["TempTargetHotbed"].isNull()) {
      state_.bed_target = status["TempTargetHotbed"].as<float>();
    }
    if (!status["TempOfNozzle"].isNull()) {
      state_.nozzle_actual = status["TempOfNozzle"].as<float>();
    }
    JsonObject fans = status["CurrentFanSpeed"].as<JsonObject>();
    if (!fans.isNull() && !fans["BoxFan"].isNull()) {
      state_.box_fan = fans["BoxFan"].as<int>();
    }
    state_.connected = true;
    return true;
  }
  return state_.connected;
}
