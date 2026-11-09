#pragma once

#include "bme680_sensor.h"
#include "sdcp.h"

void uiBegin();
void uiLoop();
void uiUpdate(const PrinterState &printer, const BmeReading &bme, bool link_ok, float target_temp);
