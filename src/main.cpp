#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <math.h>
#include <string.h>

#include "credentials_store.h"
#include "setup_mode.h"
#include "wifi_config.h"

namespace {

// ESP32-S3-Zero to MPU6050 wiring.
constexpr uint8_t kSdaPin = 8;
constexpr uint8_t kSclPin = 9;
constexpr uint32_t kI2cClockHz = 400000;

constexpr uint32_t kSerialBaud = 115200;
constexpr float kSampleRateHz = 200.0f;
constexpr uint32_t kSamplePeriodUs = 5000;

enum class GyroAxis : uint8_t { X = 0, Y = 1, Z = 2 };

// Change this to X or Y to match the sensor's mounting orientation.
constexpr GyroAxis kWaveAxis = GyroAxis::Z;

// Motion-response tuning. Output is zero at/below the deadband and one at/above
// kFullOutputDps. Filtering happens before fabs(), so direction reversals still
// produce a valley near zero.
constexpr float kFilterCutoffHz = 15.0f;
constexpr float kDeadbandDps = 6.0f;
constexpr float kFullOutputDps = 300.0f;
constexpr float kResponseGamma = 1.0f;

constexpr uint16_t kCalibrationSamples = 400;  // 2 seconds at 200 Hz

// MPU6050 registers.
constexpr uint8_t kRegSampleRateDivider = 0x19;
constexpr uint8_t kRegConfiguration = 0x1A;
constexpr uint8_t kRegGyroConfiguration = 0x1B;
constexpr uint8_t kRegGyroXoutH = 0x43;
constexpr uint8_t kRegPowerManagement1 = 0x6B;
constexpr uint8_t kRegWhoAmI = 0x75;

// ±500 degrees/second: 65.5 LSB per degree/second.
constexpr float kGyroLsbPerDps = 65.5f;

// ---------------------------------------------------------------------------
// UDP packet format (fixed 16 bytes, little-endian on the wire)
//
// Offset  Size  Type     Field
// 0       4     uint32   protocol_id  = kProtocolId (0x425A5531, "BZU1")
// 4       4     uint32   sequence     (increments per successful sample)
// 8       4     uint32   timestamp_us (ESP32 micros() at sample time)
// 12      4     float    value        (normalized 0.0–1.0, IEEE-754 LE)
// ---------------------------------------------------------------------------
constexpr uint32_t kProtocolId = 0x425A5531u;  // 'B','Z','U','1' as LE u32
constexpr size_t kPacketSize = 16;

uint8_t mpuAddress = 0;
float gyroBiasDps = 0.0f;
float filteredGyroDps = 0.0f;
float filterAlpha = 0.0f;
uint32_t nextSampleUs = 0;

DeviceNetworkConfig networkConfig{};
WiFiUDP udp;
IPAddress udpHostIp;
bool udpHostResolved = false;
bool wifiWasConnected = false;
uint32_t nextWifiAttemptMs = 0;
uint32_t nextStatusMs = 0;
uint32_t doubleResetClearMs = 0;
bool doubleResetWindowOpen = false;

uint32_t sampleSequence = 0;
uint32_t packetsSent = 0;
uint32_t sendFailures = 0;
uint32_t sensorReadFailures = 0;
uint32_t lastReportedSent = 0;
uint32_t lastReportedSendFail = 0;
uint32_t lastReportedSensorFail = 0;

bool writeRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(mpuAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission(true) == 0;
}

bool readRegisters(uint8_t startRegister, uint8_t* destination, size_t length) {
  Wire.beginTransmission(mpuAddress);
  Wire.write(startRegister);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  const size_t received = Wire.requestFrom(mpuAddress, length, true);
  if (received != length) {
    while (Wire.available()) {
      Wire.read();
    }
    return false;
  }

  for (size_t i = 0; i < length; ++i) {
    destination[i] = static_cast<uint8_t>(Wire.read());
  }
  return true;
}

bool findMpu6050() {
  for (const uint8_t address : {uint8_t{0x68}, uint8_t{0x69}}) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission(true) != 0) {
      continue;
    }

    mpuAddress = address;
    uint8_t identity = 0;
    if (readRegisters(kRegWhoAmI, &identity, 1) &&
        (identity & 0x7E) == 0x68) {
      return true;
    }
  }

  mpuAddress = 0;
  return false;
}

bool configureMpu6050() {
  // Use the X-gyro PLL clock and wake the sensor.
  if (!writeRegister(kRegPowerManagement1, 0x01)) {
    return false;
  }
  delay(100);

  // DLPF_CFG=3 gives a 1 kHz gyro output rate and approximately 42 Hz gyro
  // bandwidth. SMPLRT_DIV=4 then produces 1000 / (1 + 4) = 200 samples/s.
  return writeRegister(kRegConfiguration, 0x03) &&
         writeRegister(kRegSampleRateDivider, 0x04) &&
         writeRegister(kRegGyroConfiguration, 0x08);  // FS_SEL=1, ±500 dps
}

bool readSelectedGyroDps(float& gyroDps) {
  const uint8_t axisOffset = 2 * static_cast<uint8_t>(kWaveAxis);
  uint8_t bytes[2];
  if (!readRegisters(kRegGyroXoutH + axisOffset, bytes, sizeof(bytes))) {
    return false;
  }

  const int16_t raw = static_cast<int16_t>(
      (static_cast<uint16_t>(bytes[0]) << 8) | bytes[1]);
  gyroDps = static_cast<float>(raw) / kGyroLsbPerDps;
  return true;
}

void waitForNextSample() {
  const uint32_t now = micros();
  const int32_t timeRemaining = static_cast<int32_t>(nextSampleUs - now);
  if (timeRemaining > 0) {
    delayMicroseconds(static_cast<uint32_t>(timeRemaining));
  } else if (timeRemaining < -static_cast<int32_t>(kSamplePeriodUs)) {
    // Recover cleanly if serial or I2C ever stalls for more than one period.
    nextSampleUs = now;
  }
  nextSampleUs += kSamplePeriodUs;
}

bool calibrateGyro() {
  double sum = 0.0;
  nextSampleUs = micros();

  for (uint16_t i = 0; i < kCalibrationSamples; ++i) {
    waitForNextSample();
    float sampleDps = 0.0f;
    if (!readSelectedGyroDps(sampleDps)) {
      return false;
    }
    sum += sampleDps;
  }

  gyroBiasDps = static_cast<float>(sum / kCalibrationSamples);
  filteredGyroDps = 0.0f;
  return true;
}

float calculateNormalizedOutput(float rawGyroDps) {
  const float corrected = rawGyroDps - gyroBiasDps;
  filteredGyroDps += filterAlpha * (corrected - filteredGyroDps);

  const float speedDps = fabsf(filteredGyroDps);
  if (speedDps <= kDeadbandDps) {
    return 0.0f;
  }

  float output = (speedDps - kDeadbandDps) /
                 (kFullOutputDps - kDeadbandDps);
  output = constrain(output, 0.0f, 1.0f);
  return (kResponseGamma == 1.0f) ? output : powf(output, kResponseGamma);
}

[[noreturn]] void haltWithError(const char* message) {
  while (true) {
    Serial.println(message);
    delay(1000);
  }
}

// Explicit little-endian writers — independent of host/compiler struct layout.
void writeU32Le(uint8_t* dest, uint32_t value) {
  dest[0] = static_cast<uint8_t>(value & 0xFFu);
  dest[1] = static_cast<uint8_t>((value >> 8) & 0xFFu);
  dest[2] = static_cast<uint8_t>((value >> 16) & 0xFFu);
  dest[3] = static_cast<uint8_t>((value >> 24) & 0xFFu);
}

void writeF32Le(uint8_t* dest, float value) {
  uint32_t bits = 0;
  static_assert(sizeof(float) == 4, "float must be 32-bit IEEE-754");
  memcpy(&bits, &value, sizeof(bits));
  writeU32Le(dest, bits);
}

void packSamplePacket(uint8_t out[kPacketSize],
                      uint32_t sequence,
                      uint32_t timestampUs,
                      float value) {
  writeU32Le(out + 0, kProtocolId);
  writeU32Le(out + 4, sequence);
  writeU32Le(out + 8, timestampUs);
  writeF32Le(out + 12, value);
}

bool resolveUdpHost() {
  if (!udpHostIp.fromString(networkConfig.udpHost)) {
    Serial.print("ERROR: Invalid UDP host IP: ");
    Serial.println(networkConfig.udpHost);
    udpHostResolved = false;
    return false;
  }
  udpHostResolved = true;
  return true;
}

void startWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency; no modem power-save idle
  WiFi.begin(networkConfig.ssid, networkConfig.password);
  nextWifiAttemptMs = millis() + WIFI_RECONNECT_INTERVAL_MS;
  Serial.print("Wi-Fi: connecting to SSID \"");
  Serial.print(networkConfig.ssid);
  Serial.println("\"…");
}

// Non-blocking: at most one begin() per reconnect interval.
void maintainWifi() {
  const bool connected = (WiFi.status() == WL_CONNECTED);
  const uint32_t nowMs = millis();

  if (connected) {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      Serial.print("Wi-Fi: connected, IP ");
      Serial.println(WiFi.localIP());
      Serial.print("UDP destination ");
      Serial.print(networkConfig.udpHost);
      Serial.print(":");
      Serial.println(networkConfig.udpPort);
    }
    return;
  }

  if (wifiWasConnected) {
    wifiWasConnected = false;
    Serial.println("Wi-Fi: disconnected — sampling continues; will retry");
  }

  if (static_cast<int32_t>(nowMs - nextWifiAttemptMs) >= 0) {
    nextWifiAttemptMs = nowMs + WIFI_RECONNECT_INTERVAL_MS;
    Serial.println("Wi-Fi: reconnect attempt…");
    WiFi.disconnect();
    WiFi.begin(networkConfig.ssid, networkConfig.password);
  }
}

// Fire-and-forget: no ACK wait. Returns false if not connected or send failed.
bool sendSampleUdp(uint32_t sequence, uint32_t timestampUs, float value) {
  if (WiFi.status() != WL_CONNECTED || !udpHostResolved) {
    return false;
  }

  uint8_t packet[kPacketSize];
  packSamplePacket(packet, sequence, timestampUs, value);

  if (!udp.beginPacket(udpHostIp, networkConfig.udpPort)) {
    return false;
  }
  const size_t written = udp.write(packet, kPacketSize);
  if (written != kPacketSize) {
    udp.endPacket();
    return false;
  }
  // endPacket() is best-effort on ESP32 UDP; do not block for delivery.
  if (!udp.endPacket()) {
    return false;
  }
  return true;
}

void maybePrintStatus() {
  if (SERIAL_STATUS_INTERVAL_MS == 0) {
    return;
  }
  const uint32_t nowMs = millis();
  if (static_cast<int32_t>(nowMs - nextStatusMs) < 0) {
    return;
  }
  nextStatusMs = nowMs + SERIAL_STATUS_INTERVAL_MS;

  const uint32_t sentDelta = packetsSent - lastReportedSent;
  const uint32_t sendFailDelta = sendFailures - lastReportedSendFail;
  const uint32_t sensorFailDelta = sensorReadFailures - lastReportedSensorFail;
  lastReportedSent = packetsSent;
  lastReportedSendFail = sendFailures;
  lastReportedSensorFail = sensorReadFailures;

  Serial.print("status: wifi=");
  Serial.print(WiFi.status() == WL_CONNECTED ? "up" : "down");
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print(" ip=");
    Serial.print(WiFi.localIP());
  }
  Serial.print(" sent+");
  Serial.print(sentDelta);
  Serial.print(" send_fail+");
  Serial.print(sendFailDelta);
  Serial.print(" sensor_fail+");
  Serial.print(sensorFailDelta);
  Serial.print(" seq=");
  Serial.println(sampleSequence);
}

void maybeClearDoubleResetWindow() {
  if (!doubleResetWindowOpen) {
    return;
  }
  if (static_cast<int32_t>(millis() - doubleResetClearMs) >= 0) {
    clearDoubleResetWindow();
    doubleResetWindowOpen = false;
    Serial.println("Double-reset window closed (single RST will not enter setup)");
  }
}

}  // namespace

void setup() {
  Serial.begin(kSerialBaud);
  delay(200);
  statusLedInit();

  // Double RST (within DOUBLE_RESET_WINDOW_MS) forces setup even if NVS is set.
  const bool doubleReset = consumeDoubleResetRequest();
  const bool haveCredentials = loadNetworkConfig(&networkConfig);

  if (doubleReset) {
    Serial.println("Double-reset detected — entering setup mode");
    runSetupMode(networkConfig);
  }
  if (!haveCredentials) {
    Serial.println("No Wi-Fi credentials in flash — entering setup mode");
    runSetupMode(networkConfig);
  }

  // Arm expiry for the double-reset window opened by this normal boot.
  doubleResetWindowOpen = true;
  doubleResetClearMs = millis() + DOUBLE_RESET_WINDOW_MS;

  Serial.print("Loaded SSID=\"");
  Serial.print(networkConfig.ssid);
  Serial.print("\" UDP ");
  Serial.print(networkConfig.udpHost);
  Serial.print(":");
  Serial.println(networkConfig.udpPort);

  if (!resolveUdpHost()) {
    // Still allow sensor bring-up for local Serial debugging.
    Serial.println("WARNING: UDP host IP invalid; packets will not be sent");
  }

  startWifi();
  // begin() allocates a local ephemeral port for the UDP socket.
  udp.begin(0);

  Wire.begin(kSdaPin, kSclPin, kI2cClockHz);
  Wire.setTimeOut(20);

  if (!findMpu6050()) {
    haltWithError("ERROR: MPU6050 not found at I2C address 0x68 or 0x69");
  }
  if (!configureMpu6050()) {
    haltWithError("ERROR: MPU6050 configuration failed");
  }

  // alpha = 1 - exp(-2*pi*fc*dt), for a first-order low-pass filter.
  filterAlpha = 1.0f -
                expf(-2.0f * PI * kFilterCutoffHz / kSampleRateHz);

  // Keep the sensor completely still during the first two seconds after this
  // point. No numeric output is emitted until calibration is complete.
  if (!calibrateGyro()) {
    haltWithError("ERROR: MPU6050 read failed during calibration");
  }

  nextSampleUs = micros() + kSamplePeriodUs;
  nextStatusMs = millis() + SERIAL_STATUS_INTERVAL_MS;

  Serial.println("Sensor ready — streaming UDP samples when Wi-Fi is up");
  Serial.println("Tip: press RST twice within 3s to re-enter setup mode");
}

void loop() {
  maybeClearDoubleResetWindow();

  // Service Wi-Fi without delaying the sample schedule.
  maintainWifi();

  waitForNextSample();

  float gyroDps = 0.0f;
  if (!readSelectedGyroDps(gyroDps)) {
    // Do not emit a plausible-looking value for a failed sensor read.
    ++sensorReadFailures;
#if SERIAL_SAMPLE_OUTPUT
    Serial.println("nan");
#endif
    maybePrintStatus();
    return;
  }

  const float value = calculateNormalizedOutput(gyroDps);
  const uint32_t timestampUs = micros();
  ++sampleSequence;

  // Prefer newest data: send immediately or drop; never queue backlog.
  if (sendSampleUdp(sampleSequence, timestampUs, value)) {
    ++packetsSent;
  } else if (WiFi.status() == WL_CONNECTED) {
    ++sendFailures;
  }

#if SERIAL_SAMPLE_OUTPUT
  // Optional debug path — disabled by default to protect 200 Hz timing.
  Serial.println(value, 4);
#endif

  maybePrintStatus();
}
