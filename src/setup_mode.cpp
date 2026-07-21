#include "setup_mode.h"

#include <Arduino.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>
#include <string.h>

#include "wifi_config.h"

namespace {

// RTC memory survives EN/RST while power is held; magic detects first power-on.
RTC_NOINIT_ATTR uint32_t s_drMagic;
RTC_NOINIT_ATTR uint32_t s_drWaiting;

constexpr uint32_t kDoubleResetMagic = 0xB2225E77u;

DNSServer dnsServer;
WebServer httpServer(80);
DeviceNetworkConfig g_formDefaults;
bool g_saveRequested = false;
DeviceNetworkConfig g_pendingSave;

void ledWriteRgb(uint8_t r, uint8_t g, uint8_t b) {
  // Arduino-ESP32 helper for a single WS2812 (GPIO21 on ESP32-S3-Zero).
  neopixelWrite(STATUS_LED_PIN, r, g, b);
}

// Blink once per second: 100 ms on, 900 ms off.
void serviceSetupLed() {
  static uint32_t phaseStartMs = 0;
  static bool on = false;
  const uint32_t now = millis();
  if (phaseStartMs == 0) {
    phaseStartMs = now;
  }
  const uint32_t elapsed = now - phaseStartMs;
  if (!on && elapsed >= 900) {
    on = true;
    phaseStartMs = now;
    // Dim blue — setup indicator without max brightness.
    statusLedSet(0, 0, 48);
  } else if (on && elapsed >= 100) {
    on = false;
    phaseStartMs = now;
    statusLedOff();
  }
}

String htmlEscape(const char* s) {
  String out;
  if (s == nullptr) {
    return out;
  }
  for (const char* p = s; *p; ++p) {
    switch (*p) {
      case '&':
        out += F("&amp;");
        break;
      case '<':
        out += F("&lt;");
        break;
      case '>':
        out += F("&gt;");
        break;
      case '"':
        out += F("&quot;");
        break;
      default:
        out += *p;
        break;
    }
  }
  return out;
}

String buildSetupPage(const char* message) {
  String page;
  page.reserve(1600);
  page += F(
      "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
      "<title>Buzz Lightyear Setup</title>"
      "<style>"
      "body{font-family:system-ui,sans-serif;max-width:28rem;margin:1.5rem auto;"
      "padding:0 1rem;line-height:1.4}"
      "label{display:block;margin-top:0.75rem;font-weight:600}"
      "input{width:100%;box-sizing:border-box;padding:0.5rem;margin-top:0.25rem}"
      "button{margin-top:1.25rem;padding:0.6rem 1rem;width:100%;font-size:1rem}"
      ".msg{padding:0.75rem;background:#e8f0fe;border-radius:6px;margin-bottom:1rem}"
      "code{background:#f4f4f4;padding:0.1rem 0.3rem}"
      "</style></head><body>"
      "<h1>Buzz Lightyear Setup</h1>"
      "<p>Configure the Wi-Fi network the ESP32 should join for normal "
      "UDP streaming. After save, the device reboots into station mode.</p>");

  if (message && message[0]) {
    page += F("<div class=\"msg\">");
    page += htmlEscape(message);
    page += F("</div>");
  }

  page += F("<form method=\"POST\" action=\"/save\" autocomplete=\"off\">");
  page += F("<label>Wi-Fi SSID<input name=\"ssid\" required maxlength=\"32\" value=\"");
  page += htmlEscape(g_formDefaults.ssid);
  page += F("\"></label>");

  page += F(
      "<label>Wi-Fi password"
      "<input name=\"password\" type=\"password\" maxlength=\"64\" value=\"");
  page += htmlEscape(g_formDefaults.password);
  page += F("\" placeholder=\"(leave blank for open network)\"></label>");

  page += F("<label>Host computer IPv4 (UDP destination)"
            "<input name=\"udp_host\" required maxlength=\"45\" value=\"");
  page += htmlEscape(g_formDefaults.udpHost);
  page += F("\"></label>");

  page += F("<label>UDP port<input name=\"udp_port\" type=\"number\" "
            "min=\"1\" max=\"65535\" required value=\"");
  page += String(g_formDefaults.udpPort ? g_formDefaults.udpPort
                                        : DEFAULT_UDP_PORT);
  page += F("\"></label>");

  page += F(
      "<button type=\"submit\">Save and reboot</button></form>"
      "<p style=\"margin-top:1.5rem;color:#555;font-size:0.9rem\">"
      "AP: <code>");
  page += htmlEscape(SETUP_AP_SSID);
  page += F("</code> · portal <code>http://192.168.4.1/</code></p>"
            "</body></html>");
  return page;
}

void handleRoot() {
  httpServer.send(200, "text/html", buildSetupPage(nullptr));
}

void handleCaptive() {
  // Many OS captive-portal probes hit random paths; serve the form.
  handleRoot();
}

void handleSave() {
  DeviceNetworkConfig cfg;
  memset(&cfg, 0, sizeof(cfg));

  const String ssid = httpServer.arg("ssid");
  const String password = httpServer.arg("password");
  const String udpHost = httpServer.arg("udp_host");
  const String udpPortStr = httpServer.arg("udp_port");

  ssid.toCharArray(cfg.ssid, sizeof(cfg.ssid));
  password.toCharArray(cfg.password, sizeof(cfg.password));
  udpHost.toCharArray(cfg.udpHost, sizeof(cfg.udpHost));

  // Trim leading/trailing spaces on SSID and host.
  auto trimInPlace = [](char* s) {
    char* start = s;
    while (*start == ' ' || *start == '\t') {
      ++start;
    }
    if (start != s) {
      memmove(s, start, strlen(start) + 1);
    }
    size_t n = strlen(s);
    while (n > 0 && (s[n - 1] == ' ' || s[n - 1] == '\t')) {
      s[--n] = '\0';
    }
  };
  trimInPlace(cfg.ssid);
  trimInPlace(cfg.udpHost);

  long port = udpPortStr.toInt();
  if (port < 1 || port > 65535) {
    port = DEFAULT_UDP_PORT;
  }
  cfg.udpPort = static_cast<uint16_t>(port);
  applyNetworkConfigDefaults(&cfg);

  if (!networkConfigIsValid(cfg)) {
    httpServer.send(
        400, "text/html",
        buildSetupPage("SSID is required. Please try again."));
    return;
  }

  if (!saveNetworkConfig(cfg)) {
    httpServer.send(
        500, "text/html",
        buildSetupPage("Failed to save settings to flash. Please try again."));
    return;
  }

  g_pendingSave = cfg;
  g_saveRequested = true;

  httpServer.send(
      200, "text/html",
      F("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Saved</title></head><body style=\"font-family:system-ui;"
        "max-width:28rem;margin:2rem auto;padding:0 1rem\">"
        "<h1>Saved</h1>"
        "<p>Wi-Fi settings stored. Rebooting into normal mode…</p>"
        "<p>Reconnect your computer to your home/office Wi-Fi, then start "
        "the UDP receiver on the host.</p>"
        "</body></html>"));
}

void handleNotFound() {
  // Redirect to the portal so phone/OS captive UIs open the form.
  const String url = String("http://") + WiFi.softAPIP().toString() + "/";
  httpServer.sendHeader("Location", url, true);
  httpServer.send(302, "text/plain", "Redirecting to setup portal");
}

}  // namespace

bool consumeDoubleResetRequest() {
  bool doubleReset = false;

  if (s_drMagic != kDoubleResetMagic) {
    // Cold power-on or corrupted RTC — start a fresh window, not setup.
    s_drMagic = kDoubleResetMagic;
    s_drWaiting = 0;
  } else if (s_drWaiting != 0) {
    doubleReset = true;
    s_drWaiting = 0;
  }

  if (!doubleReset) {
    // Arm window: a second RST before clearDoubleResetWindow() enters setup.
    s_drWaiting = 1;
  }

  return doubleReset;
}

void clearDoubleResetWindow() {
  s_drWaiting = 0;
}

void statusLedInit() {
  statusLedOff();
}

void statusLedSet(uint8_t r, uint8_t g, uint8_t b) {
  ledWriteRgb(r, g, b);
}

void statusLedOff() {
  ledWriteRgb(0, 0, 0);
}

[[noreturn]] void runSetupMode(const DeviceNetworkConfig& existing) {
  // Do not leave a pending double-reset arm across setup reboots.
  clearDoubleResetWindow();

  g_formDefaults = existing;
  applyNetworkConfigDefaults(&g_formDefaults);
  g_saveRequested = false;

  statusLedInit();

  Serial.println();
  Serial.println(F("=== SETUP MODE ==="));
  Serial.println(F("Connect the host PC to the SoftAP, then open the portal."));
  Serial.print(F("  SSID: "));
  Serial.println(SETUP_AP_SSID);
  if (SETUP_AP_PASSWORD[0] != '\0') {
    Serial.print(F("  Password: "));
    Serial.println(SETUP_AP_PASSWORD);
  } else {
    Serial.println(F("  Password: (open network)"));
  }
  Serial.println(F("  Portal: http://192.168.4.1/"));
  Serial.println(F("Onboard LED blinks once per second while in setup."));
  Serial.println();

  WiFi.persistent(false);
  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);

  bool apOk = false;
  if (SETUP_AP_PASSWORD[0] == '\0') {
    apOk = WiFi.softAP(SETUP_AP_SSID);
  } else {
    apOk = WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASSWORD);
  }
  if (!apOk) {
    Serial.println(F("ERROR: SoftAP failed to start"));
  }

  delay(100);
  const IPAddress apIp = WiFi.softAPIP();
  Serial.print(F("SoftAP IP: "));
  Serial.println(apIp);

  // Captive DNS: resolve any hostname to the AP so OS portals open.
  dnsServer.start(53, "*", apIp);

  httpServer.on("/", HTTP_GET, handleRoot);
  httpServer.on("/save", HTTP_POST, handleSave);
  httpServer.on("/generate_204", HTTP_GET, handleCaptive);       // Android
  httpServer.on("/gen_204", HTTP_GET, handleCaptive);
  httpServer.on("/hotspot-detect.html", HTTP_GET, handleCaptive);  // Apple
  httpServer.on("/connecttest.txt", HTTP_GET, handleCaptive);      // Windows
  httpServer.on("/fwlink", HTTP_GET, handleCaptive);
  httpServer.onNotFound(handleNotFound);
  httpServer.begin();

  uint32_t lastClientLogMs = 0;

  while (true) {
    dnsServer.processNextRequest();
    httpServer.handleClient();
    serviceSetupLed();

    const uint32_t now = millis();
    if (now - lastClientLogMs >= 5000) {
      lastClientLogMs = now;
      Serial.print(F("setup: AP stations="));
      Serial.println(WiFi.softAPgetStationNum());
    }

    if (g_saveRequested) {
      Serial.print(F("Saved SSID=\""));
      Serial.print(g_pendingSave.ssid);
      Serial.print(F("\" UDP "));
      Serial.print(g_pendingSave.udpHost);
      Serial.print(F(":"));
      Serial.println(g_pendingSave.udpPort);
      Serial.println(F("Rebooting…"));
      statusLedOff();
      delay(500);
      ESP.restart();
    }

    delay(2);  // yield to Wi-Fi / TCP stack
  }
}
