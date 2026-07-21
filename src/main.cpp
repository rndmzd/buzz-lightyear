#include <Arduino.h>
#include <Wire.h>
#include <math.h>

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

uint8_t mpuAddress = 0;
float gyroBiasDps = 0.0f;
float filteredGyroDps = 0.0f;
float filterAlpha = 0.0f;
uint32_t nextSampleUs = 0;

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

}  // namespace

void setup() {
  Serial.begin(kSerialBaud);
  delay(1000);

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
}

void loop() {
  waitForNextSample();

  float gyroDps = 0.0f;
  if (!readSelectedGyroDps(gyroDps)) {
    // Do not output a plausible-looking value for a failed sensor read.
    Serial.println("nan");
    return;
  }

  // One scalar per line works directly with a terminal, logger, or the Arduino
  // Serial Plotter. Values are always in the inclusive range 0.0 to 1.0.
  Serial.println(calculateNormalizedOutput(gyroDps), 4);
}
