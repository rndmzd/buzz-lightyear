#include "credentials_store.h"

#include <Preferences.h>
#include <string.h>

#include "wifi_config.h"

namespace {

constexpr const char* kNvsNamespace = "buzznet";
constexpr const char* kKeySsid = "ssid";
constexpr const char* kKeyPassword = "pass";
constexpr const char* kKeyUdpHost = "udp_host";
constexpr const char* kKeyUdpPort = "udp_port";

void zeroConfig(DeviceNetworkConfig* cfg) {
  memset(cfg, 0, sizeof(*cfg));
  cfg->udpPort = DEFAULT_UDP_PORT;
}

}  // namespace

bool networkConfigIsValid(const DeviceNetworkConfig& cfg) {
  return cfg.ssid[0] != '\0';
}

void applyNetworkConfigDefaults(DeviceNetworkConfig* cfg) {
  if (cfg->udpHost[0] == '\0') {
    strncpy(cfg->udpHost, DEFAULT_UDP_HOST_IP, sizeof(cfg->udpHost) - 1);
    cfg->udpHost[sizeof(cfg->udpHost) - 1] = '\0';
  }
  if (cfg->udpPort == 0) {
    cfg->udpPort = DEFAULT_UDP_PORT;
  }
}

bool loadNetworkConfig(DeviceNetworkConfig* out) {
  zeroConfig(out);

  Preferences prefs;
  if (!prefs.begin(kNvsNamespace, true /* read-only */)) {
    applyNetworkConfigDefaults(out);
    return false;
  }

  const size_t ssidLen =
      prefs.getString(kKeySsid, out->ssid, sizeof(out->ssid));
  prefs.getString(kKeyPassword, out->password, sizeof(out->password));
  prefs.getString(kKeyUdpHost, out->udpHost, sizeof(out->udpHost));
  out->udpPort = static_cast<uint16_t>(
      prefs.getUShort(kKeyUdpPort, DEFAULT_UDP_PORT));
  prefs.end();

  // getString returns length written excluding NUL; treat empty as missing.
  if (ssidLen == 0 || out->ssid[0] == '\0') {
    out->ssid[0] = '\0';
    applyNetworkConfigDefaults(out);
    return false;
  }

  applyNetworkConfigDefaults(out);
  return true;
}

bool saveNetworkConfig(const DeviceNetworkConfig& cfg) {
  if (!networkConfigIsValid(cfg)) {
    return false;
  }

  Preferences prefs;
  if (!prefs.begin(kNvsNamespace, false /* read-write */)) {
    return false;
  }

  // Empty password is valid (open network); putString may return 0 for "".
  const size_t ssidN = prefs.putString(kKeySsid, cfg.ssid);
  prefs.putString(kKeyPassword, cfg.password);
  const size_t hostN = prefs.putString(kKeyUdpHost, cfg.udpHost);
  const size_t portN = prefs.putUShort(kKeyUdpPort, cfg.udpPort);
  prefs.end();
  return ssidN > 0 && hostN > 0 && portN > 0;
}

bool clearNetworkConfig() {
  Preferences prefs;
  if (!prefs.begin(kNvsNamespace, false)) {
    return false;
  }
  const bool ok = prefs.clear();
  prefs.end();
  return ok;
}
