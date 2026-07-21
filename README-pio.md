# ESP32-S3-Zero MPU6050 wrist-wave output

This firmware reads one MPU6050 gyroscope axis and outputs a normalized motion
value over USB serial. Each directional half-stroke produces a positive peak;
the value returns toward zero at a direction reversal. Direction itself is
discarded.

## Wiring

| ESP32-S3-Zero | MPU6050 / GY-521 |
|---|---|
| 3V3 | VCC |
| GND | GND |
| GPIO8 | SDA |
| GPIO9 | SCL |

Leave `AD0` low for address `0x68`, or connect it high for `0x69`. The firmware
detects either address. Powering the module from 3.3 V avoids depending on the
details of a particular breakout board's regulator and pull-ups.

## Build and upload

This is a PlatformIO Arduino project. The Waveshare-recommended PlatformIO board
target is `esp32-s3-devkitm-1`, already configured in `platformio.ini`.

1. Connect the board by USB-C.
2. Keep the MPU6050 completely still during startup calibration.
3. Run `pio run -t upload`.
4. Open a serial monitor at 115200 baud with `pio device monitor`, or use the
   Arduino IDE Serial Plotter.

If upload does not start, hold **BOOT**, press and release **RESET**, then release
**BOOT** and try again.

The serial stream contains exactly one normalized value per line at 200 Hz:

```text
0.0000
0.0378
0.1421
0.3184
0.1072
0.0000
```

The first numeric output appears after the startup delay and two-second gyro
calibration. An error message repeats once per second if the sensor cannot be
found or configured. A failed read after startup outputs `nan` rather than a
misleading motion value.

## Tuning

All important settings are near the top of `src/main.cpp`:

- `kWaveAxis`: change `Z` to `X` or `Y` to match the physical mounting.
- `kDeadbandDps`: raise it if stationary noise creates output; lower it to sense
  gentler motion.
- `kFullOutputDps`: angular speed that maps to `1.0`. Lower it for stronger peaks.
- `kFilterCutoffHz`: lower it for smoother output or raise it for faster response.
- `kResponseGamma`: use less than `1.0` to emphasize slow motion, or greater than
  `1.0` to emphasize fast motion.

Filtering is applied while the gyro signal is still signed. The firmware takes
the absolute value afterward, which preserves a distinct dip at each reversal.
