#include "bme680_sensor.h"

#include <Adafruit_BME680.h>
#include <Wire.h>

namespace {
Adafruit_BME680 bme(&Wire);
bool ready = false;
bool wire_up = false;
int sda_pin = -1;
int scl_pin = -1;
uint8_t addr_ = 0x76;
uint8_t miss = 0;

void configure() {
  bme.setTemperatureOversampling(BME680_OS_8X);
  bme.setHumidityOversampling(BME680_OS_2X);
  bme.setPressureOversampling(BME680_OS_4X);
  bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
  // Température et humidité seulement. Le chauffage fausse la température.
  bme.setGasHeater(0, 0);
}

void startWire() {
  Wire.begin(sda_pin, scl_pin);
  Wire.setClock(100000);
  wire_up = true;
}

// Ne sert que si une lecture a déjà échoué. Au boot, ces impulsions
// empêchent le BME680 de répondre.
void recoverBus() {
  if (wire_up) {
    Wire.end();
    wire_up = false;
  }
  pinMode(scl_pin, OUTPUT_OPEN_DRAIN);
  pinMode(sda_pin, INPUT_PULLUP);
  digitalWrite(scl_pin, HIGH);
  delayMicroseconds(5);
  for (int i = 0; i < 16 && digitalRead(sda_pin) == LOW; i++) {
    digitalWrite(scl_pin, LOW);
    delayMicroseconds(5);
    digitalWrite(scl_pin, HIGH);
    delayMicroseconds(5);
  }
  pinMode(sda_pin, OUTPUT_OPEN_DRAIN);
  digitalWrite(sda_pin, LOW);
  delayMicroseconds(5);
  digitalWrite(scl_pin, HIGH);
  delayMicroseconds(5);
  digitalWrite(sda_pin, HIGH);
  delayMicroseconds(5);
  delay(2);
  startWire();
}

bool probe() {
  ready = bme.begin(addr_);
  if (!ready) {
    ready = bme.begin(addr_ == 0x76 ? 0x77 : 0x76);
  }
  if (!ready) {
    return false;
  }
  configure();
  return true;
}

bool fill(BmeReading &out) {
  if (!bme.performReading()) {
    return false;
  }
  if (isnan(bme.temperature) || isnan(bme.humidity)) {
    return false;
  }
  out.ok = true;
  out.temperature_c = bme.temperature;
  out.humidity_pct = bme.humidity;
  out.pressure_hpa = bme.pressure / 100.0f;
  return true;
}

void scanBus() {
  Serial.printf("[i2c] bus SDA=%d SCL=%d — scan…\n", sda_pin, scl_pin);
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[i2c] trouvé 0x%02X\n", a);
    }
  }
}
}  // namespace

bool bme680Begin(int sda, int scl, uint8_t addr) {
  sda_pin = sda;
  scl_pin = scl;
  addr_ = addr;
  startWire();
  scanBus();
  if (!probe()) {
    Serial.println("[bme680] pas de réponse sur 0x76/0x77");
    return false;
  }
  return true;
}

BmeReading bme680Read() {
  BmeReading out;
  if (sda_pin < 0) {
    return out;
  }
  if (ready && fill(out)) {
    if (miss) {
      Serial.println("[bme680] rétabli");
    }
    miss = 0;
    return out;
  }

  if (miss == 0) {
    Serial.println("[bme680] plus de réponse, nouvel essai");
  }
  ready = false;
  if (!probe() || !fill(out)) {
    recoverBus();
    if (!probe() || !fill(out)) {
      ready = false;
      miss++;
      if (miss >= 12) {
        Serial.println("[bme680] toujours muet");
        miss = 1;
      }
      return BmeReading{};
    }
  }
  Serial.println("[bme680] rétabli");
  miss = 0;
  return out;
}
