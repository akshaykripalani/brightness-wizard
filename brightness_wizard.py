"""
Brightness Wizard - Beyond-minimum brightness control via gamma ramp manipulation.

Uses SetDeviceGammaRamp (Win32 GDI) to scale the Intel iGPU's color lookup table,
providing software-level dimming beyond what the hardware backlight minimum allows.

Safety mechanisms:
  1. Original gamma ramp saved to disk (JSON) on startup — survives crashes.
  2. On startup, checks for a stale ramp file from a previous crash and restores it.
  3. Signal handlers (SIGINT, SIGTERM, SIGBREAK) all restore before exit.
  4. atexit handler as a last-resort fallback.
  5. A lockfile tracks whether the app is running; if it's stale on next launch,
     we know the previous instance crashed and we auto-restore.
  6. Standalone --restore flag to manually reset gamma without launching the tray.

NOTE: Windows (since Vista) enforces a gamma ramp sanity check that rejects ramps
deviating too far from the identity ramp. In practice this clamps the minimum
effective brightness to ~50%.
"""

import ctypes
import ctypes.wintypes
import atexit
import json
import logging
import math
import os
import signal
import sys
import tempfile

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger("brightness_wizard")

# --- Paths for safety files ---

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
RAMP_BACKUP_PATH = os.path.join(_APP_DIR, ".brightness_wizard_ramp_backup.json")
LOCK_PATH = os.path.join(_APP_DIR, ".brightness_wizard.lock")


# --- Win32 gamma ramp types and functions ---

class GAMMA_RAMP(ctypes.Structure):
    _fields_ = [
        ("Red", ctypes.wintypes.WORD * 256),
        ("Green", ctypes.wintypes.WORD * 256),
        ("Blue", ctypes.wintypes.WORD * 256),
    ]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GetDC = user32.GetDC
GetDC.argtypes = [ctypes.wintypes.HWND]
GetDC.restype = ctypes.wintypes.HDC

ReleaseDC = user32.ReleaseDC
ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
ReleaseDC.restype = ctypes.c_int

GetDeviceGammaRamp = gdi32.GetDeviceGammaRamp
GetDeviceGammaRamp.argtypes = [ctypes.wintypes.HDC, ctypes.POINTER(GAMMA_RAMP)]
GetDeviceGammaRamp.restype = ctypes.wintypes.BOOL

SetDeviceGammaRamp = gdi32.SetDeviceGammaRamp
SetDeviceGammaRamp.argtypes = [ctypes.wintypes.HDC, ctypes.POINTER(GAMMA_RAMP)]
SetDeviceGammaRamp.restype = ctypes.wintypes.BOOL


# --- Gamma ramp helpers ---

original_ramp = GAMMA_RAMP()
current_brightness = 100
_last_applied_brightness = 100
_ramp_modified = False  # Track whether we've actually changed the gamma


def build_gamma_ramp(factor: float) -> GAMMA_RAMP:
    """Build a GAMMA_RAMP scaled by the given factor (0.0 – 1.0)."""
    factor = max(0.0, min(1.0, factor))
    ramp = GAMMA_RAMP()
    for i in range(256):
        value = min(65535, int(i * 256 * factor))
        ramp.Red[i] = value
        ramp.Green[i] = value
        ramp.Blue[i] = value
    return ramp


def _ramp_to_lists(ramp: GAMMA_RAMP) -> dict:
    """Serialize a GAMMA_RAMP to JSON-friendly dict."""
    return {
        "Red": [ramp.Red[i] for i in range(256)],
        "Green": [ramp.Green[i] for i in range(256)],
        "Blue": [ramp.Blue[i] for i in range(256)],
    }


def _lists_to_ramp(data: dict) -> GAMMA_RAMP:
    """Deserialize a dict back to a GAMMA_RAMP."""
    ramp = GAMMA_RAMP()
    for i in range(256):
        ramp.Red[i] = data["Red"][i]
        ramp.Green[i] = data["Green"][i]
        ramp.Blue[i] = data["Blue"][i]
    return ramp


def save_ramp_to_disk(ramp: GAMMA_RAMP, path: str = RAMP_BACKUP_PATH):
    """Persist the original gamma ramp to disk so it survives crashes."""
    try:
        data = _ramp_to_lists(ramp)
        # Write atomically via temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=_APP_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            # On Windows, need to remove dest first if it exists
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp_path, path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info("Saved original gamma ramp to disk: %s", path)
        return True
    except Exception as e:
        log.error("Failed to save ramp to disk: %s", e)
        return False


def load_ramp_from_disk(path: str = RAMP_BACKUP_PATH) -> GAMMA_RAMP | None:
    """Load a previously saved gamma ramp from disk."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Validate structure
        for channel in ("Red", "Green", "Blue"):
            if channel not in data or len(data[channel]) != 256:
                log.error("Corrupt ramp backup file — missing/invalid %s channel", channel)
                return None
        ramp = _lists_to_ramp(data)
        log.info("Loaded gamma ramp from disk backup")
        return ramp
    except Exception as e:
        log.error("Failed to load ramp from disk: %s", e)
        return None


def remove_ramp_backup(path: str = RAMP_BACKUP_PATH):
    """Remove the on-disk ramp backup (called on clean exit)."""
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Removed ramp backup file")
    except OSError as e:
        log.warning("Could not remove ramp backup: %s", e)


def create_lockfile():
    """Create a lockfile with our PID to detect crashes."""
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
        log.info("Created lockfile with PID %d", os.getpid())
    except OSError as e:
        log.warning("Could not create lockfile: %s", e)


def remove_lockfile():
    """Remove the lockfile on clean exit."""
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
            log.info("Removed lockfile")
    except OSError as e:
        log.warning("Could not remove lockfile: %s", e)


def is_stale_lockfile() -> bool:
    """Check if a lockfile exists from a previous crashed instance."""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, "r") as f:
            pid = int(f.read().strip())
        # Check if that PID is still running
        import ctypes as _ctypes
        kernel32 = _ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            # Process exists — might be us or another instance
            if pid == os.getpid():
                return False
            log.warning("Another instance (PID %d) may still be running", pid)
            return False
        else:
            log.warning("Found stale lockfile from crashed PID %d", pid)
            return True
    except (ValueError, OSError) as e:
        log.warning("Lockfile corrupt or unreadable: %s", e)
        return True


def save_original_ramp():
    hdc = GetDC(0)
    try:
        if not GetDeviceGammaRamp(hdc, ctypes.byref(original_ramp)):
            log.error("GetDeviceGammaRamp failed — could not save original ramp")
            return False
        log.info("Saved original gamma ramp in memory (first 5 red entries: %s)",
                 [original_ramp.Red[i] for i in range(5)])
        # Also persist to disk for crash recovery
        save_ramp_to_disk(original_ramp)
        return True
    finally:
        ReleaseDC(0, hdc)


def _apply_ramp(ramp: GAMMA_RAMP) -> bool:
    """Low-level: apply a gamma ramp to the primary display."""
    hdc = GetDC(0)
    try:
        ok = SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
        return bool(ok)
    finally:
        ReleaseDC(0, hdc)


def restore_original_ramp():
    global _ramp_modified
    ok = _apply_ramp(original_ramp)
    if ok:
        _ramp_modified = False
        log.info("Restored original gamma ramp")
    else:
        log.error("Failed to restore original gamma ramp")
    return ok


def restore_identity_ramp() -> bool:
    """Restore the standard identity gamma ramp (linear 0–65535).

    This is the nuclear option — applies the mathematically correct default
    ramp regardless of what the original was. Use when the original ramp
    backup is unavailable.
    """
    identity = build_gamma_ramp(1.0)
    ok = _apply_ramp(identity)
    if ok:
        log.info("Restored identity (linear) gamma ramp")
    else:
        log.error("Failed to restore identity gamma ramp")
    return ok


def set_brightness(factor: float) -> bool:
    """Apply a brightness factor (0.1 – 1.0) via the gamma ramp.

    Returns True if the ramp was accepted by Windows, False otherwise.
    """
    global _last_applied_brightness, _ramp_modified
    factor = max(0.1, min(1.0, factor))
    ramp = build_gamma_ramp(factor)
    ok = _apply_ramp(ramp)
    if ok:
        _ramp_modified = True
        _last_applied_brightness = int(factor * 100)
        log.info("Set brightness to %d%% (factor=%.2f)", int(factor * 100), factor)
    else:
        log.warning(
            "SetDeviceGammaRamp REJECTED brightness %d%% (factor=%.2f) — "
            "Windows gamma ramp sanity check blocked this value. "
            "Last successful brightness: %d%%",
            int(factor * 100), factor, _last_applied_brightness,
        )
    return bool(ok)


# --- Cleanup orchestration ---

_cleanup_done = False


def cleanup():
    """Central cleanup: restore gamma, remove lockfile and backup.

    Safe to call multiple times — only runs once.
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    log.info("Running cleanup...")
    if _ramp_modified:
        restore_original_ramp()
    remove_lockfile()
    remove_ramp_backup()
    log.info("Cleanup complete")


def _signal_handler(signum, frame):
    """Handle termination signals by restoring gamma before exit."""
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    log.warning("Received signal %s — restoring gamma and exiting", sig_name)
    cleanup()
    sys.exit(0)


def recover_from_crash():
    """Check for and recover from a previous crashed instance.

    If a stale lockfile + ramp backup exist, restore the saved ramp.
    """
    if not is_stale_lockfile():
        return False

    log.warning("Detected previous crash — attempting to restore saved gamma ramp")
    saved = load_ramp_from_disk()
    if saved:
        ok = _apply_ramp(saved)
        if ok:
            log.info("Successfully restored gamma ramp from crash backup")
        else:
            log.error("Could not apply saved ramp — falling back to identity ramp")
            restore_identity_ramp()
    else:
        log.warning("No ramp backup found — restoring identity ramp as fallback")
        restore_identity_ramp()

    # Clean up stale files
    remove_lockfile()
    remove_ramp_backup()
    return True


# --- Tray icon helpers ---

def create_icon_image(brightness_pct: int) -> Image.Image:
    """Draw a simple sun icon that dims with the current brightness level."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    shade = int(255 * (brightness_pct / 100))
    color = (shade, shade, 0, 255)
    margin = 16
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    center = size // 2
    ray_len = 10
    for angle_deg in range(0, 360, 45):
        rad = math.radians(angle_deg)
        x1 = center + int((size // 2 - margin) * math.cos(rad))
        y1 = center + int((size // 2 - margin) * math.sin(rad))
        x2 = center + int((size // 2 - margin + ray_len) * math.cos(rad))
        y2 = center + int((size // 2 - margin + ray_len) * math.sin(rad))
        draw.line([x1, y1, x2, y2], fill=color, width=2)
    return img


def make_on_click(pct, icon_ref):
    """Return a callback that sets brightness to the given percentage."""
    def on_click(icon, item):
        global current_brightness
        ok = set_brightness(pct / 100.0)
        if ok:
            current_brightness = pct
            icon.icon = create_icon_image(pct)
        else:
            log.warning("Brightness %d%% rejected, keeping current %d%%",
                        pct, current_brightness)
    return on_click


def on_restore(icon, item):
    global current_brightness
    current_brightness = 100
    restore_original_ramp()
    icon.icon = create_icon_image(100)
    log.info("User restored default brightness")


def on_exit(icon, item):
    log.info("User exiting via tray menu")
    cleanup()
    icon.stop()


def build_menu(icon_ref):
    items = []
    for pct in range(100, 0, -10):
        items.append(
            pystray.MenuItem(
                f"{pct}%",
                make_on_click(pct, icon_ref),
                checked=lambda item, p=pct: current_brightness == p,
            )
        )
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Restore Default", on_restore))
    items.append(pystray.MenuItem("Exit", on_exit))
    return pystray.Menu(*items)


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    # Handle --restore flag for manual recovery
    if "--restore" in sys.argv:
        log.info("Manual restore requested via --restore flag")
        saved = load_ramp_from_disk()
        if saved:
            ok = _apply_ramp(saved)
            if ok:
                log.info("Restored gamma ramp from backup file")
            else:
                log.error("Backup ramp failed — applying identity ramp")
                restore_identity_ramp()
        else:
            log.info("No backup file — applying identity ramp")
            restore_identity_ramp()
        remove_lockfile()
        remove_ramp_backup()
        print("Gamma ramp restored to default.", file=sys.stderr)
        return

    log.info("Brightness Wizard starting")

    # Safety: recover from previous crash if needed
    recover_from_crash()

    # Save the current (clean) ramp
    save_original_ramp()
    create_lockfile()

    # Register all cleanup hooks
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    icon = pystray.Icon("brightness_wizard")
    icon.icon = create_icon_image(100)
    icon.title = "Brightness Wizard"
    icon.menu = build_menu(icon)
    log.info("System tray icon ready — right-click to adjust brightness")
    icon.run()

    # If icon.run() returns normally (shouldn't usually), still clean up
    cleanup()


if __name__ == "__main__":
    main()
