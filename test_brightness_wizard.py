"""Tests for brightness_wizard gamma ramp logic, safety mechanisms, and icon generation."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open

import brightness_wizard


class TestBuildGammaRamp(unittest.TestCase):
    """Test the pure gamma ramp construction logic."""

    def test_full_brightness_is_identity(self):
        ramp = brightness_wizard.build_gamma_ramp(1.0)
        for i in range(256):
            expected = min(65535, i * 256)
            self.assertEqual(ramp.Red[i], expected, f"Red[{i}]")
            self.assertEqual(ramp.Green[i], expected, f"Green[{i}]")
            self.assertEqual(ramp.Blue[i], expected, f"Blue[{i}]")

    def test_half_brightness(self):
        ramp = brightness_wizard.build_gamma_ramp(0.5)
        for i in range(256):
            expected = min(65535, int(i * 256 * 0.5))
            self.assertEqual(ramp.Red[i], expected, f"Red[{i}]")
            self.assertEqual(ramp.Green[i], expected, f"Green[{i}]")
            self.assertEqual(ramp.Blue[i], expected, f"Blue[{i}]")

    def test_zero_brightness_clamps_to_zero(self):
        ramp = brightness_wizard.build_gamma_ramp(0.0)
        for i in range(256):
            self.assertEqual(ramp.Red[i], 0)
            self.assertEqual(ramp.Green[i], 0)
            self.assertEqual(ramp.Blue[i], 0)

    def test_factor_clamped_above_one(self):
        ramp = brightness_wizard.build_gamma_ramp(2.0)
        ramp_normal = brightness_wizard.build_gamma_ramp(1.0)
        for i in range(256):
            self.assertEqual(ramp.Red[i], ramp_normal.Red[i])

    def test_factor_clamped_below_zero(self):
        ramp = brightness_wizard.build_gamma_ramp(-0.5)
        for i in range(256):
            self.assertEqual(ramp.Red[i], 0)

    def test_ramp_values_never_exceed_65535(self):
        ramp = brightness_wizard.build_gamma_ramp(1.0)
        for i in range(256):
            self.assertLessEqual(ramp.Red[i], 65535)
            self.assertLessEqual(ramp.Green[i], 65535)
            self.assertLessEqual(ramp.Blue[i], 65535)

    def test_ramp_monotonically_increasing(self):
        for factor in [0.1, 0.3, 0.5, 0.7, 1.0]:
            ramp = brightness_wizard.build_gamma_ramp(factor)
            for i in range(1, 256):
                self.assertGreaterEqual(
                    ramp.Red[i], ramp.Red[i - 1],
                    f"Red not monotonic at i={i}, factor={factor}",
                )

    def test_ten_percent_brightness(self):
        ramp = brightness_wizard.build_gamma_ramp(0.1)
        expected = min(65535, int(128 * 256 * 0.1))
        self.assertEqual(ramp.Red[128], expected)

    def test_rgb_channels_equal(self):
        """All three channels should be identical (no color shift)."""
        ramp = brightness_wizard.build_gamma_ramp(0.6)
        for i in range(256):
            self.assertEqual(ramp.Red[i], ramp.Green[i])
            self.assertEqual(ramp.Green[i], ramp.Blue[i])


class TestRampSerialization(unittest.TestCase):
    """Test ramp <-> dict conversion for disk persistence."""

    def test_roundtrip(self):
        """Serializing and deserializing should produce identical ramp."""
        original = brightness_wizard.build_gamma_ramp(0.7)
        data = brightness_wizard._ramp_to_lists(original)
        restored = brightness_wizard._lists_to_ramp(data)
        for i in range(256):
            self.assertEqual(original.Red[i], restored.Red[i])
            self.assertEqual(original.Green[i], restored.Green[i])
            self.assertEqual(original.Blue[i], restored.Blue[i])

    def test_to_lists_structure(self):
        ramp = brightness_wizard.build_gamma_ramp(1.0)
        data = brightness_wizard._ramp_to_lists(ramp)
        self.assertIn("Red", data)
        self.assertIn("Green", data)
        self.assertIn("Blue", data)
        self.assertEqual(len(data["Red"]), 256)
        self.assertEqual(len(data["Green"]), 256)
        self.assertEqual(len(data["Blue"]), 256)

    def test_to_lists_is_json_serializable(self):
        ramp = brightness_wizard.build_gamma_ramp(0.5)
        data = brightness_wizard._ramp_to_lists(ramp)
        serialized = json.dumps(data)
        deserialized = json.loads(serialized)
        self.assertEqual(data, deserialized)


class TestDiskPersistence(unittest.TestCase):
    """Test saving/loading ramp backups to disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.backup_path = os.path.join(self.tmpdir, "test_ramp_backup.json")

    def tearDown(self):
        if os.path.exists(self.backup_path):
            os.remove(self.backup_path)
        os.rmdir(self.tmpdir)

    def test_save_creates_file(self):
        ramp = brightness_wizard.build_gamma_ramp(0.8)
        ok = brightness_wizard.save_ramp_to_disk(ramp, self.backup_path)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(self.backup_path))

    def test_save_and_load_roundtrip(self):
        original = brightness_wizard.build_gamma_ramp(0.6)
        brightness_wizard.save_ramp_to_disk(original, self.backup_path)
        loaded = brightness_wizard.load_ramp_from_disk(self.backup_path)
        self.assertIsNotNone(loaded)
        for i in range(256):
            self.assertEqual(original.Red[i], loaded.Red[i])
            self.assertEqual(original.Green[i], loaded.Green[i])
            self.assertEqual(original.Blue[i], loaded.Blue[i])

    def test_load_nonexistent_returns_none(self):
        result = brightness_wizard.load_ramp_from_disk("/nonexistent/path.json")
        self.assertIsNone(result)

    def test_load_corrupt_file_returns_none(self):
        with open(self.backup_path, "w") as f:
            f.write("not valid json{{{")
        result = brightness_wizard.load_ramp_from_disk(self.backup_path)
        self.assertIsNone(result)

    def test_load_missing_channel_returns_none(self):
        with open(self.backup_path, "w") as f:
            json.dump({"Red": [0] * 256, "Green": [0] * 256}, f)  # missing Blue
        result = brightness_wizard.load_ramp_from_disk(self.backup_path)
        self.assertIsNone(result)

    def test_load_wrong_length_returns_none(self):
        with open(self.backup_path, "w") as f:
            json.dump({"Red": [0] * 100, "Green": [0] * 256, "Blue": [0] * 256}, f)
        result = brightness_wizard.load_ramp_from_disk(self.backup_path)
        self.assertIsNone(result)

    def test_remove_backup(self):
        with open(self.backup_path, "w") as f:
            f.write("test")
        brightness_wizard.remove_ramp_backup(self.backup_path)
        self.assertFalse(os.path.exists(self.backup_path))

    def test_remove_backup_nonexistent_is_noop(self):
        brightness_wizard.remove_ramp_backup("/nonexistent/path.json")  # should not raise

    def test_save_overwrites_existing(self):
        ramp1 = brightness_wizard.build_gamma_ramp(0.5)
        ramp2 = brightness_wizard.build_gamma_ramp(0.9)
        brightness_wizard.save_ramp_to_disk(ramp1, self.backup_path)
        brightness_wizard.save_ramp_to_disk(ramp2, self.backup_path)
        loaded = brightness_wizard.load_ramp_from_disk(self.backup_path)
        self.assertEqual(loaded.Red[128], ramp2.Red[128])


class TestLockfile(unittest.TestCase):
    """Test lockfile creation and stale detection."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_lock_path = brightness_wizard.LOCK_PATH
        brightness_wizard.LOCK_PATH = os.path.join(self.tmpdir, "test.lock")

    def tearDown(self):
        if os.path.exists(brightness_wizard.LOCK_PATH):
            os.remove(brightness_wizard.LOCK_PATH)
        brightness_wizard.LOCK_PATH = self.orig_lock_path
        os.rmdir(self.tmpdir)

    def test_create_lockfile(self):
        brightness_wizard.create_lockfile()
        self.assertTrue(os.path.exists(brightness_wizard.LOCK_PATH))
        with open(brightness_wizard.LOCK_PATH) as f:
            pid = int(f.read().strip())
        self.assertEqual(pid, os.getpid())

    def test_remove_lockfile(self):
        brightness_wizard.create_lockfile()
        brightness_wizard.remove_lockfile()
        self.assertFalse(os.path.exists(brightness_wizard.LOCK_PATH))

    def test_remove_lockfile_nonexistent_is_noop(self):
        brightness_wizard.remove_lockfile()  # should not raise

    def test_no_lockfile_is_not_stale(self):
        self.assertFalse(brightness_wizard.is_stale_lockfile())

    def test_own_pid_is_not_stale(self):
        brightness_wizard.create_lockfile()
        self.assertFalse(brightness_wizard.is_stale_lockfile())

    def test_dead_pid_is_stale(self):
        # PID 99999999 almost certainly doesn't exist
        with open(brightness_wizard.LOCK_PATH, "w") as f:
            f.write("99999999")
        self.assertTrue(brightness_wizard.is_stale_lockfile())

    def test_corrupt_lockfile_is_stale(self):
        with open(brightness_wizard.LOCK_PATH, "w") as f:
            f.write("not_a_number")
        self.assertTrue(brightness_wizard.is_stale_lockfile())


class TestSetBrightness(unittest.TestCase):
    """Test set_brightness with mocked Win32 calls."""

    def setUp(self):
        brightness_wizard._last_applied_brightness = 100
        brightness_wizard.current_brightness = 100
        brightness_wizard._ramp_modified = False

    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    def test_successful_set(self, mock_apply):
        result = brightness_wizard.set_brightness(0.7)
        self.assertTrue(result)
        self.assertEqual(brightness_wizard._last_applied_brightness, 70)
        self.assertTrue(brightness_wizard._ramp_modified)

    @patch.object(brightness_wizard, "_apply_ramp", return_value=False)
    def test_rejected_set(self, mock_apply):
        result = brightness_wizard.set_brightness(0.2)
        self.assertFalse(result)
        self.assertEqual(brightness_wizard._last_applied_brightness, 100)
        self.assertFalse(brightness_wizard._ramp_modified)

    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    def test_factor_clamped_to_min(self, mock_apply):
        brightness_wizard.set_brightness(0.01)
        self.assertEqual(brightness_wizard._last_applied_brightness, 10)

    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    def test_factor_clamped_to_max(self, mock_apply):
        brightness_wizard.set_brightness(1.5)
        self.assertEqual(brightness_wizard._last_applied_brightness, 100)


class TestApplyRamp(unittest.TestCase):
    """Test _apply_ramp with mocked Win32 calls."""

    @patch.object(brightness_wizard, "ReleaseDC")
    @patch.object(brightness_wizard, "SetDeviceGammaRamp", return_value=1)
    @patch.object(brightness_wizard, "GetDC", return_value=42)
    def test_dc_acquired_and_released(self, mock_get, mock_set, mock_release):
        ramp = brightness_wizard.build_gamma_ramp(0.5)
        result = brightness_wizard._apply_ramp(ramp)
        self.assertTrue(result)
        mock_get.assert_called_once_with(0)
        mock_release.assert_called_once_with(0, 42)

    @patch.object(brightness_wizard, "ReleaseDC")
    @patch.object(brightness_wizard, "SetDeviceGammaRamp", return_value=0)
    @patch.object(brightness_wizard, "GetDC", return_value=42)
    def test_returns_false_on_failure(self, mock_get, mock_set, mock_release):
        ramp = brightness_wizard.build_gamma_ramp(0.5)
        result = brightness_wizard._apply_ramp(ramp)
        self.assertFalse(result)

    @patch.object(brightness_wizard, "ReleaseDC")
    @patch.object(brightness_wizard, "SetDeviceGammaRamp", side_effect=Exception("boom"))
    @patch.object(brightness_wizard, "GetDC", return_value=42)
    def test_dc_released_on_exception(self, mock_get, mock_set, mock_release):
        ramp = brightness_wizard.build_gamma_ramp(0.5)
        with self.assertRaises(Exception):
            brightness_wizard._apply_ramp(ramp)
        mock_release.assert_called_once()


class TestSaveRestoreRamp(unittest.TestCase):
    """Test save/restore with mocked Win32 calls."""

    @patch.object(brightness_wizard, "save_ramp_to_disk")
    @patch.object(brightness_wizard, "ReleaseDC")
    @patch.object(brightness_wizard, "GetDeviceGammaRamp", return_value=1)
    @patch.object(brightness_wizard, "GetDC", return_value=42)
    def test_save_success(self, mock_get, mock_gamma, mock_release, mock_disk):
        result = brightness_wizard.save_original_ramp()
        self.assertTrue(result)
        mock_disk.assert_called_once()

    @patch.object(brightness_wizard, "ReleaseDC")
    @patch.object(brightness_wizard, "GetDeviceGammaRamp", return_value=0)
    @patch.object(brightness_wizard, "GetDC", return_value=42)
    def test_save_failure(self, mock_get, mock_gamma, mock_release):
        result = brightness_wizard.save_original_ramp()
        self.assertFalse(result)
        mock_release.assert_called_once()

    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    def test_restore_success(self, mock_apply):
        brightness_wizard._ramp_modified = True
        result = brightness_wizard.restore_original_ramp()
        self.assertTrue(result)
        self.assertFalse(brightness_wizard._ramp_modified)

    @patch.object(brightness_wizard, "_apply_ramp", return_value=False)
    def test_restore_failure(self, mock_apply):
        brightness_wizard._ramp_modified = True
        result = brightness_wizard.restore_original_ramp()
        self.assertFalse(result)
        # _ramp_modified should remain True on failure
        self.assertTrue(brightness_wizard._ramp_modified)


class TestRestoreIdentityRamp(unittest.TestCase):
    """Test the fallback identity ramp restoration."""

    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    def test_applies_identity_ramp(self, mock_apply):
        result = brightness_wizard.restore_identity_ramp()
        self.assertTrue(result)
        # Verify the ramp passed is an identity ramp
        applied_ramp = mock_apply.call_args[0][0]
        for i in range(256):
            expected = min(65535, i * 256)
            self.assertEqual(applied_ramp.Red[i], expected)

    @patch.object(brightness_wizard, "_apply_ramp", return_value=False)
    def test_returns_false_on_failure(self, mock_apply):
        result = brightness_wizard.restore_identity_ramp()
        self.assertFalse(result)


class TestCleanup(unittest.TestCase):
    """Test the central cleanup function."""

    def setUp(self):
        brightness_wizard._cleanup_done = False
        brightness_wizard._ramp_modified = False

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "restore_original_ramp")
    def test_cleanup_restores_when_modified(self, mock_restore, mock_lock, mock_backup):
        brightness_wizard._ramp_modified = True
        brightness_wizard.cleanup()
        mock_restore.assert_called_once()
        mock_lock.assert_called_once()
        mock_backup.assert_called_once()

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "restore_original_ramp")
    def test_cleanup_skips_restore_when_not_modified(self, mock_restore, mock_lock, mock_backup):
        brightness_wizard._ramp_modified = False
        brightness_wizard.cleanup()
        mock_restore.assert_not_called()
        mock_lock.assert_called_once()
        mock_backup.assert_called_once()

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "restore_original_ramp")
    def test_cleanup_runs_only_once(self, mock_restore, mock_lock, mock_backup):
        brightness_wizard._ramp_modified = True
        brightness_wizard.cleanup()
        brightness_wizard.cleanup()
        brightness_wizard.cleanup()
        mock_restore.assert_called_once()
        mock_lock.assert_called_once()


class TestRecoverFromCrash(unittest.TestCase):
    """Test crash recovery logic."""

    @patch.object(brightness_wizard, "is_stale_lockfile", return_value=False)
    def test_no_stale_lockfile_does_nothing(self, mock_stale):
        result = brightness_wizard.recover_from_crash()
        self.assertFalse(result)

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "_apply_ramp", return_value=True)
    @patch.object(brightness_wizard, "load_ramp_from_disk")
    @patch.object(brightness_wizard, "is_stale_lockfile", return_value=True)
    def test_restores_saved_ramp_on_crash(self, mock_stale, mock_load, mock_apply,
                                           mock_lock, mock_backup):
        saved_ramp = brightness_wizard.build_gamma_ramp(1.0)
        mock_load.return_value = saved_ramp
        result = brightness_wizard.recover_from_crash()
        self.assertTrue(result)
        mock_apply.assert_called_once_with(saved_ramp)
        mock_lock.assert_called_once()
        mock_backup.assert_called_once()

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "restore_identity_ramp")
    @patch.object(brightness_wizard, "load_ramp_from_disk", return_value=None)
    @patch.object(brightness_wizard, "is_stale_lockfile", return_value=True)
    def test_falls_back_to_identity_when_no_backup(self, mock_stale, mock_load,
                                                     mock_identity, mock_lock, mock_backup):
        result = brightness_wizard.recover_from_crash()
        self.assertTrue(result)
        mock_identity.assert_called_once()

    @patch.object(brightness_wizard, "remove_ramp_backup")
    @patch.object(brightness_wizard, "remove_lockfile")
    @patch.object(brightness_wizard, "restore_identity_ramp")
    @patch.object(brightness_wizard, "_apply_ramp", return_value=False)
    @patch.object(brightness_wizard, "load_ramp_from_disk")
    @patch.object(brightness_wizard, "is_stale_lockfile", return_value=True)
    def test_falls_back_to_identity_when_apply_fails(self, mock_stale, mock_load, mock_apply,
                                                       mock_identity, mock_lock, mock_backup):
        mock_load.return_value = brightness_wizard.build_gamma_ramp(1.0)
        result = brightness_wizard.recover_from_crash()
        self.assertTrue(result)
        mock_identity.assert_called_once()


class TestCreateIconImage(unittest.TestCase):
    """Test tray icon generation."""

    def test_returns_rgba_image(self):
        img = brightness_wizard.create_icon_image(100)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (64, 64))

    def test_full_brightness_has_bright_pixels(self):
        img = brightness_wizard.create_icon_image(100)
        center_pixel = img.getpixel((32, 32))
        self.assertEqual(center_pixel, (255, 255, 0, 255))

    def test_zero_brightness_has_dark_pixels(self):
        img = brightness_wizard.create_icon_image(0)
        center_pixel = img.getpixel((32, 32))
        self.assertEqual(center_pixel, (0, 0, 0, 255))

    def test_fifty_percent(self):
        img = brightness_wizard.create_icon_image(50)
        center_pixel = img.getpixel((32, 32))
        self.assertEqual(center_pixel[0], 127)  # R
        self.assertEqual(center_pixel[1], 127)  # G


class TestMakeOnClick(unittest.TestCase):
    """Test the tray menu callback factory."""

    def setUp(self):
        brightness_wizard.current_brightness = 100

    @patch.object(brightness_wizard, "set_brightness", return_value=True)
    def test_updates_brightness_on_success(self, mock_set):
        icon = MagicMock()
        callback = brightness_wizard.make_on_click(70, icon)
        callback(icon, None)
        self.assertEqual(brightness_wizard.current_brightness, 70)
        mock_set.assert_called_once_with(0.7)

    @patch.object(brightness_wizard, "set_brightness", return_value=False)
    def test_keeps_brightness_on_failure(self, mock_set):
        icon = MagicMock()
        callback = brightness_wizard.make_on_click(20, icon)
        callback(icon, None)
        self.assertEqual(brightness_wizard.current_brightness, 100)


if __name__ == "__main__":
    unittest.main()
