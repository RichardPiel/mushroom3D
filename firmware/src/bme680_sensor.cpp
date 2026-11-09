#include "bme680_sensor.h"

#include <Adafruit_BME680.h>
#include <Wire.h>

namespace {
Adafruit_BME680 bme(&Wire);
bool ready = false;
}  // namespace

bool bme680Begin(int sda, int scl, uint8_t addr) {
  Wire.begin(sda, scl);
  Wire.setClock(100000);
  Serial.printf("[i2c] bus SDA=%d SCL=%d — scan…\n", sda, scl);
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[i2c] trouvé 0x%02X\n", a);
    }
  }
  ready = bme.begin(addr);
  if (!ready) {
    ready = bme.begin(addr == 0x76 ? 0x77 : 0x76);
  }
  if (!ready) {
    Serial.println("[bme680] pas de réponse sur 0x76/0x77");
    return false;
  }
  bme.setTemperatureOversampling(BME680_OS_8X);
  bme.setHumidityOversampling(BME680_OS_2X);
  bme.setPressureOversampling(BME680_OS_4X);
  bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
  bme.setGasHeater(320, 150);
  return true;
}

BmeReading bme680Read() {
  BmeReading out;
  if (!ready) {
    return out;
  }
  if (!bme.performReading()) {
    return out;
  }
  out.ok = true;
  out.temperature_c = bme.temperature;
  out.humidity_pct = bme.humidity;
  out.pressure_hpa = bme.pressure / 100.0f;
  out.gas_ohms = bme.gas_resistance;
  return out;
}
