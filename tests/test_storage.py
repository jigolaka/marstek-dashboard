import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.discovery import DeviceScanResult
from src.metrics import Measurement
from src.protocol import DeviceIdentity, DiscoveryHint
from src.storage import DashboardStore


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.store = DashboardStore(
            Path(self.temp_directory.name) / "dashboard.sqlite3")
        identity = DeviceIdentity(
            device="VenusE",
            version=150,
            ble_mac="24215edb178f",
            wifi_mac="620b0c877705",
            wifi_name="Solar",
            ip="192.168.1.11",
            source="VenusE-24215edb178f",
        )
        hint = DiscoveryHint(identity.source, identity.ip,
                             identity.device, identity.ble_mac)
        self.device_key = self.store.save_device(
            DeviceScanResult(hint, identity), "Garage")

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_saves_verified_device_and_user_designation(self) -> None:
        device = self.store.list_devices()[0]

        self.assertEqual(device["device_key"], "620b0c877705")
        self.assertEqual(device["designation"], "Garage")
        self.assertEqual(device["model"], "VenusE")

    def test_records_and_reads_measurements(self) -> None:
        timestamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.store.record_measurements(
            self.device_key,
            [Measurement("battery.soc", 84.0, "%", "Bat.GetStatus")],
            timestamp,
        )

        self.assertEqual(self.store.latest_measurements(
            self.device_key)["battery.soc"]["value"], 84.0)
        self.assertEqual(len(self.store.measurements_since(
            self.device_key, timestamp - timedelta(seconds=1)
        )), 1)

    def test_purges_expired_measurements(self) -> None:
        old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.store.record_measurements(
            self.device_key,
            [Measurement("battery.soc", 84.0, "%", "Bat.GetStatus")],
            old_timestamp,
        )

        removed = self.store.purge_expired(
            30,
            now=datetime(2026, 2, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(removed, 1)
        self.assertEqual(self.store.latest_measurements(self.device_key), {})

    def test_tracks_setup_complete_state_and_factory_reset(self) -> None:
        self.assertFalse(self.store.get_bool_setting("setup_complete", False))

        self.store.set_bool_setting("setup_complete", True)
        self.store.set_bool_setting("scan_authorized", True)

        self.assertTrue(self.store.get_bool_setting("setup_complete", False))
        self.assertTrue(self.store.get_bool_setting("scan_authorized", False))

        self.store.reset_factory_state()

        self.assertFalse(self.store.get_bool_setting("setup_complete", False))
        self.assertFalse(self.store.get_bool_setting("scan_authorized", False))
        self.assertEqual(self.store.list_devices(), [])


if __name__ == "__main__":
    unittest.main()
