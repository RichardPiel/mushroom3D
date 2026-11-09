#include "ui_monitor.h"

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Arduino.h>
#include <SPI.h>
#include <cmath>

// Waveshare ESP32-S3-Touch-LCD-2.8 — ST7789 240×320
namespace {
constexpr int PIN_MOSI = 45;
constexpr int PIN_SCLK = 40;
constexpr int PIN_CS = 42;
constexpr int PIN_DC = 41;
constexpr int PIN_RST = 39;
constexpr int PIN_BL = 5;

constexpr uint16_t COL_BG = 0x1082;      // #090c0f approx
constexpr uint16_t COL_CARD = 0x18E3;    // #13181d
constexpr uint16_t COL_TEXT = 0xEF7D;    // near white
constexpr uint16_t COL_MUTED = 0x8430;   // grey-green
constexpr uint16_t COL_ACCENT = 0x1ED3;  // #3ddc97-ish
constexpr uint16_t COL_WARN = 0xE560;    // amber
constexpr uint16_t COL_BAD = 0xF945;     // red

Adafruit_ST7789 tft(PIN_CS, PIN_DC, PIN_RST);
bool ready = false;

struct Cache {
  float bed = NAN;
  float target = NAN;
  float hum = NAN;
  float air = NAN;
  bool link = false;
  bool printer = false;
  bool heater = false;
  bool drawn = false;
} cache;

void backlight(uint8_t pct) {
  ledcSetup(1, 20000, 10);
  ledcAttachPin(PIN_BL, 1);
  ledcWrite(1, (uint32_t)pct * 1023 / 100);
}

void fillCard(int16_t x, int16_t y, int16_t w, int16_t h) {
  tft.fillRoundRect(x, y, w, h, 8, COL_CARD);
}

void drawLabel(int16_t x, int16_t y, const char *text) {
  tft.setTextColor(COL_MUTED, COL_CARD);
  tft.setTextSize(1);
  tft.setCursor(x, y);
  tft.print(text);
}

void drawBig(int16_t x, int16_t y, const char *text, uint16_t color) {
  tft.setTextColor(color, COL_CARD);
  tft.setTextSize(3);
  tft.setCursor(x, y);
  tft.print(text);
}

void drawMed(int16_t x, int16_t y, const char *text, uint16_t color) {
  tft.setTextColor(color, COL_CARD);
  tft.setTextSize(2);
  tft.setCursor(x, y);
  tft.print(text);
}

void fmt1(char *buf, size_t n, float v, const char *unit) {
  if (isnan(v)) {
    snprintf(buf, n, "—%s", unit);
  } else {
    snprintf(buf, n, "%.1f%s", v, unit);
  }
}

void fmt0(char *buf, size_t n, float v, const char *unit) {
  if (isnan(v)) {
    snprintf(buf, n, "—%s", unit);
  } else {
    snprintf(buf, n, "%.0f%s", v, unit);
  }
}

void paintChrome() {
  tft.fillScreen(COL_BG);
  tft.setTextColor(COL_TEXT, COL_BG);
  tft.setTextSize(2);
  tft.setCursor(12, 10);
  tft.print("Mycelium");
  tft.setTextColor(COL_ACCENT, COL_BG);
  tft.print(" Lab");

  // 2x2 cards landscape 320x240
  fillCard(8, 40, 148, 88);
  fillCard(164, 40, 148, 88);
  fillCard(8, 136, 148, 88);
  fillCard(164, 136, 148, 88);

  drawLabel(16, 48, "PLATEAU");
  drawLabel(172, 48, "CONSIGNE");
  drawLabel(16, 144, "AIR / HUM");
  drawLabel(172, 144, "ETAT");
}

void paintValues(const PrinterState &printer, const BmeReading &bme, bool link_ok,
                 float target_temp, bool heater_on) {
  char buf[24];

  // Plateau
  fillCard(8, 40, 148, 88);
  drawLabel(16, 48, "PLATEAU");
  fmt1(buf, sizeof(buf), printer.bed_actual, "");
  drawBig(16, 68, buf, COL_TEXT);
  tft.setTextColor(COL_MUTED, COL_CARD);
  tft.setTextSize(2);
  tft.setCursor(16 + strlen(buf) * 18, 76);
  tft.print("C");

  // Consigne
  fillCard(164, 40, 148, 88);
  drawLabel(172, 48, "CONSIGNE");
  fmt1(buf, sizeof(buf), target_temp, "");
  drawBig(172, 68, buf, heater_on ? COL_ACCENT : COL_MUTED);
  tft.setTextColor(COL_MUTED, COL_CARD);
  tft.setTextSize(2);
  tft.setCursor(172 + strlen(buf) * 18, 76);
  tft.print("C");

  // Air / humidité
  fillCard(8, 136, 148, 88);
  drawLabel(16, 144, "AIR / HUM");
  fmt1(buf, sizeof(buf), bme.ok ? bme.temperature_c : NAN, "C");
  drawMed(16, 162, buf, COL_TEXT);
  fmt0(buf, sizeof(buf), bme.ok ? bme.humidity_pct : NAN, "%");
  drawMed(16, 188, buf, COL_ACCENT);

  // État
  fillCard(164, 136, 148, 88);
  drawLabel(172, 144, "ETAT");
  tft.fillCircle(184, 172, 6, printer.connected ? COL_ACCENT : COL_BAD);
  drawMed(198, 164, printer.connected ? "Impr." : "Impr.", COL_TEXT);
  tft.fillCircle(184, 200, 6, link_ok ? COL_ACCENT : COL_BAD);
  drawMed(198, 192, link_ok ? "Serveur" : "Serveur", COL_TEXT);
}
}  // namespace

void uiBegin() {
  pinMode(PIN_BL, OUTPUT);
  backlight(80);

  SPI.begin(PIN_SCLK, -1, PIN_MOSI, PIN_CS);
  tft.init(240, 320);
  tft.setRotation(1);  // paysage 320×240
  tft.setSPISpeed(40000000);

  paintChrome();
  ready = true;
  cache.drawn = false;
  Serial.println("[ui] dashboard ST7789 320x240");
}

void uiLoop() {}

void uiUpdate(const PrinterState &printer, const BmeReading &bme, bool link_ok, float target_temp) {
  if (!ready) {
    return;
  }

  const bool heater = !isnan(target_temp) && target_temp > 0.05f;
  const bool same =
      cache.drawn && cache.link == link_ok && cache.printer == printer.connected &&
      cache.heater == heater &&
      (isnan(cache.bed) ? isnan(printer.bed_actual)
                        : fabsf(cache.bed - printer.bed_actual) < 0.05f) &&
      (isnan(cache.target) ? isnan(target_temp) : fabsf(cache.target - target_temp) < 0.05f) &&
      (isnan(cache.hum) ? isnan(bme.humidity_pct)
                        : fabsf(cache.hum - bme.humidity_pct) < 0.5f) &&
      (isnan(cache.air) ? isnan(bme.temperature_c)
                        : fabsf(cache.air - bme.temperature_c) < 0.1f);

  static uint32_t last_force = 0;
  if (same && millis() - last_force < 2000) {
    return;
  }
  last_force = millis();

  paintValues(printer, bme, link_ok, target_temp, heater);

  cache.bed = printer.bed_actual;
  cache.target = target_temp;
  cache.hum = bme.humidity_pct;
  cache.air = bme.temperature_c;
  cache.link = link_ok;
  cache.printer = printer.connected;
  cache.heater = heater;
  cache.drawn = true;

  Serial.printf(
      "[ui] bed=%.1f tgt=%.1f hum=%.0f link=%s printer=%s\n",
      isnan(printer.bed_actual) ? -1.0 : printer.bed_actual, target_temp,
      isnan(bme.humidity_pct) ? -1.0 : bme.humidity_pct, link_ok ? "ok" : "down",
      printer.connected ? "ok" : "down");
}
