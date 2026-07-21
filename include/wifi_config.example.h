// Copy this file to wifi_config.h and adjust defaults if needed:
//
//   copy include\wifi_config.example.h include\wifi_config.h   (Windows)
//   cp include/wifi_config.example.h include/wifi_config.h     (Unix)
//
// wifi_config.h is gitignored. Station Wi-Fi credentials are stored in NVS
// via setup mode (double-reset or first boot) — not hard-coded here.

#pragma once

// SoftAP presented while the device is in setup mode (host joins this network).
static constexpr const char* SETUP_AP_SSID = "BuzzLightyear-Setup";
// Empty password = open network. Set a password (8+ chars) if you prefer.
static constexpr const char* SETUP_AP_PASSWORD = "";

// Second RST within this window after boot re-enters setup mode.
static constexpr uint32_t DOUBLE_RESET_WINDOW_MS = 3000;

// Waveshare ESP32-S3-Zero onboard WS2812 data pin.
static constexpr uint8_t STATUS_LED_PIN = 21;

// Defaults suggested in the setup web form (overwritten by NVS after save).
static constexpr const char* DEFAULT_UDP_HOST_IP = "192.168.1.100";
static constexpr uint16_t DEFAULT_UDP_PORT = 5005;

// How often to retry station Wi-Fi when disconnected (milliseconds).
static constexpr uint32_t WIFI_RECONNECT_INTERVAL_MS = 5000;

// Periodic Serial status line interval (milliseconds). 0 = disable.
static constexpr uint32_t SERIAL_STATUS_INTERVAL_MS = 5000;

// Print one normalized value per sample on USB Serial (debug only; costs time).
#ifndef SERIAL_SAMPLE_OUTPUT
#define SERIAL_SAMPLE_OUTPUT 0
#endif
