#include "ui_monitor.h"

#include <Arduino.h>
#include <cmath>

// Affichage console + hooks LVGL. Le panneau tactile Waveshare (ST7789 + LVGL)
// se branche ici à partir de l'exemple officiel de la carte.

namespace {
bool link_ok_cached = false;
}  // namespace

void uiBegin() {
  Serial.println("[ui] moniteur prêt (Serial ; brancher LVGL Waveshare ensuite)");
}

void uiLoop() {}

void uiUpdate(const PrinterState &printer, const BmeReading &bme, bool link_ok, float target_temp) {
  static uint32_t last = 0;
  if (millis() - last < 1000 && link_ok == link_ok_cached) {
    return;
  }
  last = millis();
  link_ok_cached = link_ok;
  Serial.printf(
      "[ui] bed=%.1f/%.1f tgt=%.1f hum=%.0f%% P=%.1f link=%s printer=%s\n",
      isnan(printer.bed_actual) ? -1.0 : printer.bed_actual,
      isnan(printer.bed_target) ? -1.0 : printer.bed_target, target_temp,
      isnan(bme.humidity_pct) ? -1.0 : bme.humidity_pct,
      isnan(bme.pressure_hpa) ? -1.0 : bme.pressure_hpa, link_ok ? "ok" : "down",
      printer.connected ? "ok" : "down");
}
