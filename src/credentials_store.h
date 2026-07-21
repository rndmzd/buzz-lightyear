#pragma once

#include <stddef.h>
#include <stdint.h>

// Station Wi-Fi + UDP destination persisted in NVS (setup portal).
struct DeviceNetworkConfig {
  char ssid[33];
  char password[65];
  char udpHost[46];  // IPv4 or long enough for hostname if needed later
  uint16_t udpPort;
};

// Returns true if NVS holds a non-empty SSID (ready for station mode).
bool loadNetworkConfig(DeviceNetworkConfig* out);

// Persist config; returns true on success.
bool saveNetworkConfig(const DeviceNetworkConfig& cfg);

// Wipe stored station credentials (forces setup on next normal boot).
bool clearNetworkConfig();

// True when ssid is non-empty after load or fill.
bool networkConfigIsValid(const DeviceNetworkConfig& cfg);

// Fill defaults for empty fields (UDP host/port) without touching SSID.
void applyNetworkConfigDefaults(DeviceNetworkConfig* cfg);
