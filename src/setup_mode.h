#pragma once

#include <stdint.h>

#include "credentials_store.h"

// Detect double RST within DOUBLE_RESET_WINDOW_MS (RTC-backed).
// Call once at boot before other long work. Returns true if this boot is the
// second reset inside the window → enter setup mode.
bool consumeDoubleResetRequest();

// After a normal boot, call once the double-reset window has elapsed so the
// next single RST does not enter setup.
void clearDoubleResetWindow();

// Status LED (WS2812 on ESP32-S3-Zero). Safe to call before setup mode.
void statusLedInit();
void statusLedSet(uint8_t r, uint8_t g, uint8_t b);
void statusLedOff();

// SoftAP + captive HTTP portal. Blocks until credentials are saved, then reboots.
// Blinks the onboard LED once per second while active.
// existing may be empty (first boot) or pre-filled for editing.
[[noreturn]] void runSetupMode(const DeviceNetworkConfig& existing);
