# Brightness Wizard

Beyond-minimum brightness control for Windows laptops. Uses gamma ramp manipulation (`SetDeviceGammaRamp` Win32 API) to dim the display past the hardware backlight minimum — real GPU-level dimming, not a screen overlay.

## How it works

- Gets the device context for the primary display (Intel iGPU via `GetDC(0)`)
- Saves the original 256-entry RGB gamma ramp on startup
- Applies a scaled ramp to reduce pixel brightness
- Restores the original ramp on exit

Does **not** affect the NVIDIA discrete GPU or any external displays.

## Requirements

- Windows 10/11
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
git clone https://github.com/akshaykripalani/brightness-wizard.git
cd brightness-wizard
uv sync
```

Or with pip:

```bash
pip install pystray Pillow
```

## Usage

### Run the app

```bash
uv run brightness_wizard.py
```

A sun icon appears in the system tray. Right-click it to:

- Select a brightness level (100% down to 10%)
- **Restore Default** — reset to original brightness
- **Exit** — restore brightness and quit

The current brightness level is shown with a checkmark. The icon dims to match.

### Manual gamma restore

If the app was killed unexpectedly and your screen is stuck dim:

```bash
uv run brightness_wizard.py --restore
```

This resets the gamma ramp to default immediately and exits.

### Automatic crash recovery

If the app detects a previous crash on startup (via a stale lockfile), it automatically restores the saved gamma ramp before launching the tray icon. No manual intervention needed.

## Limitations

Windows (since Vista) enforces a gamma ramp sanity check that rejects ramps deviating too far from the identity ramp. In practice, the minimum effective brightness is around **50%**. Values below that will be attempted but silently rejected by the OS.

## Safety

This app **cannot damage your monitor**. Gamma ramps are a software color lookup table in the GPU driver — they don't touch the backlight hardware and are fully reset on reboot.

Safety mechanisms:

1. Original gamma ramp saved to disk (JSON) — survives crashes
2. Lockfile-based crash detection with auto-recovery on next launch
3. Signal handlers (SIGINT, SIGTERM, SIGBREAK) restore gamma before exit
4. `atexit` handler as a last-resort fallback
5. Identity ramp fallback if the backup file is missing or corrupt
6. `--restore` CLI flag for manual recovery

## Running tests

```bash
uv run python -m pytest test_brightness_wizard.py -v
```
