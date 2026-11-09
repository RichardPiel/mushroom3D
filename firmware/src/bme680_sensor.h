#pragma once

#include <Arduino.h>

struct BmeReading {
  bool ok = false;
  float temperature_c = NAN;
  float humidity_pct = NAN;
  float pressure_hpa = NAN;
  float gas_ohms = NAN;
};

bool bme680Begin(int sda, int scl, uint8_t addr);
BmeReading bme680Read();
