import runpy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src import ui as dashboard
from src.metrics import Measurement


class BatteryStateTests(unittest.TestCase):
    def analyze(self, values: dict[str, float], rows: list[dict[str, object]] | None = None,
                age_seconds: int = 0, failed_poll: bool = False) -> dict[str, object]:
        store = Mock()
        store.latest_measurements.return_value = {
            key: {"value": value} for key, value in values.items()
        }
        store.get_setting.side_effect = lambda name, default: default
        store.get_text_setting.side_effect = lambda name, default="": default
        store.measurements_since.return_value = rows or []
        device = {
            "device_key": "test",
            "last_seen": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
        }
        with patch.object(dashboard, "STORE", store), \
                patch.object(dashboard, "_poll_online", {"test": not failed_poll}, create=True):
            return dashboard._analyze(device)

    def test_standby_with_reported_zero_battery_power(self) -> None:
        self.assertEqual(self.analyze({"battery.power": 0})["state"], "Standby")

    def test_standby_with_zero_outputs_without_battery_power(self) -> None:
        self.assertEqual(self.analyze({
            "mode.grid_power": 0, "mode.offgrid_power": 0,
        })["state"], "Standby")

    def test_missing_power_is_not_standby(self) -> None:
        self.assertEqual(self.analyze({})["state"], "Offline")

    def test_offline_keeps_cached_battery_values_without_stale_power_or_eta(self) -> None:
        result = self.analyze({
            "battery.soc": 73, "battery.remaining_capacity": 3770,
            "battery.rated_capacity": 5120, "battery.power": 2230,
            "mode.grid_power": -2230, "mode.offgrid_power": 0,
            "battery.temperature": 29,
        }, age_seconds=180)
        self.assertTrue(result["offline"])
        self.assertEqual(result["state"], "Offline")
        self.assertEqual((result["soc"], result["remaining"], result["rated"]), (73, 3770, 5120))
        self.assertEqual(result["temperature"], 29)
        self.assertEqual(result["grid_state"], "On-grid")
        self.assertIsNone(result["power"])
        self.assertIsNone(result["eta_hours"])

    def test_failed_poll_marks_recent_cached_values_offline(self) -> None:
        result = self.analyze({"battery.soc": 73, "battery.power": 2230}, failed_poll=True)
        self.assertEqual(result["state"], "Offline")

    def test_capacity_falls_back_to_saved_soc_and_rated_capacity(self) -> None:
        result = self.analyze({"battery.soc": 50, "battery.rated_capacity": 5120}, age_seconds=180)
        self.assertEqual(result["remaining"], 2560)

    def test_grid_charging_without_battery_power(self) -> None:
        result = self.analyze({
            "mode.grid_power": -795, "mode.offgrid_power": 0,
            "battery.soc": 58, "battery.rated_capacity": 5120,
        })
        self.assertEqual(result["state"], "Charging")
        self.assertEqual(result["power"], 795)
        self.assertTrue(result["power_estimated"])
        self.assertIsNotNone(result["eta_hours"])
        self.assertTrue(result["eta"].startswith("Full in "))
        self.assertTrue(result["eta"].endswith(" (100%)"))
        self.assertNotIn("~", result["eta"])

    def test_wrapped_backup_reading_is_not_a_65_kw_discharge(self) -> None:
        result = self.analyze({
            "mode.grid_power": -2240, "mode.offgrid_power": 65521,
            "battery.soc": 67, "battery.rated_capacity": 5120,
        })
        self.assertEqual(result["state"], "Charging")
        self.assertEqual(result["power"], 2240)
        self.assertEqual(result["grid_state"], "On-grid")
        self.assertFalse(any("Less than 1 hour" in text for _, text in result["warnings"]))

    def test_measured_charging_power_takes_priority_over_grid_input(self) -> None:
        result = self.analyze({
            "battery.power": 700, "mode.grid_power": -795, "mode.offgrid_power": 0,
        })
        self.assertEqual(result["power"], 700)
        self.assertFalse(result["power_estimated"])

    def test_chart_includes_grid_charging_without_battery_power(self) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        result = self.analyze({}, rows=[
            {"recorded_at": stamp, "metric_key": "mode.grid_power", "value": -795},
            {"recorded_at": stamp, "metric_key": "mode.offgrid_power", "value": 0},
        ])
        self.assertEqual(result["stats"], [795])
        self.assertEqual(result["series"][0][1:], (795, "Charging"))

    def test_chart_corrects_previously_stored_wrapped_backup_samples(self) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        result = self.analyze({}, rows=[
            {"recorded_at": stamp, "metric_key": "mode.grid_power", "value": -2240},
            {"recorded_at": stamp, "metric_key": "mode.offgrid_power", "value": 65521},
        ])
        self.assertEqual(result["stats"], [2240])
        self.assertEqual(result["series"][0][1:], (2240, "Charging"))

    def test_active_battery_states(self) -> None:
        for watts, expected in ((-100, "Discharging"), (100, "Charging")):
            with self.subTest(watts=watts):
                self.assertEqual(self.analyze({"battery.power": watts})["state"], expected)


class PollingStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_poll_keeps_saved_readings_and_success_recovers(self) -> None:
        device = {"device_key": "test", "model": "VenusE", "designation": "Home",
                  "ip_address": "127.0.0.1"}
        store = Mock()
        store.get_setting.return_value = 30000
        measurements = [Measurement("battery.soc", 73, "%", "Bat.GetStatus")]
        client = Mock()
        client.mode = None
        client.read_snapshot = AsyncMock(side_effect=[[], measurements])
        with patch.object(dashboard, "STORE", store), \
                patch.object(dashboard, "_active_device", return_value=device), \
                patch.object(dashboard, "UdpMarstekClient", return_value=client), \
                patch.object(dashboard, "_poll_online", {"test": True}), \
                patch.object(dashboard, "_poll_failures", 0), \
                patch.object(dashboard, "_check_battery_alerts"):
            await dashboard._poll_active()
            self.assertFalse(dashboard._poll_online["test"])
            store.record_measurements.assert_not_called()
            await dashboard._poll_active()
            self.assertTrue(dashboard._poll_online["test"])
            store.record_measurements.assert_called_once_with("test", measurements)


class PowerFormattingTests(unittest.TestCase):
    def test_compact_offline_shows_saved_capacity_without_progress_or_live_values(self) -> None:
        device = {"model": "VenusE", "designation": "", "ip_address": "127.0.0.1",
                  "last_seen": datetime.now(timezone.utc).isoformat()}
        data = {"soc": 73, "power": None, "state": "Offline", "offline": True,
                "grid_state": "On-grid", "temperature": None, "eta_hours": None,
                "remaining": 3770, "rated": 5120}
        with patch.object(dashboard, "_battery_mode", return_value="Manual"):
            with dashboard.ui.column() as root:
                dashboard._render_compact(device, data)
        self.addCleanup(root.delete)
        metrics = root.default_slot.children[0].default_slot.children[1]
        readings, footer = metrics.default_slot.children
        labels = [child.text for child in readings.default_slot.children]
        self.assertEqual(labels, ["73%", "Offline", "3.77 kWh / 5.12 kWh"])
        self.assertEqual(len(footer.default_slot.children), 1)
        self.assertIn("state-offline", readings.default_slot.children[1].classes)

    def test_compact_estimated_values_have_no_approximation_prefix(self) -> None:
        device = {"model": "VenusE", "designation": "", "ip_address": "127.0.0.1",
                  "last_seen": datetime.now(timezone.utc).isoformat()}
        data = {"soc": 67, "power": 2240, "power_estimated": True, "state": "Charging",
                "grid_state": "On-grid", "temperature": None, "eta_hours": 0.75}
        with patch.object(dashboard, "_battery_mode", return_value="Manual"):
            with dashboard.ui.column() as root:
                dashboard._render_compact(device, data)
        self.addCleanup(root.delete)
        metrics = root.default_slot.children[0].default_slot.children[1]
        readings = metrics.default_slot.children[0]
        labels = [child.text for child in readings.default_slot.children]
        self.assertIn("2.24 kW", labels)
        self.assertIn("45 min", labels)
        self.assertFalse(any("~" in text for text in labels))

    def test_watts_and_kilowatts(self) -> None:
        for watts, expected in ((None, "--"), (0, "0 W"), (795, "795 W"),
                                (1000, "1.00 kW"), (2240, "2.24 kW"), (-2240, "2.24 kW")):
            with self.subTest(watts=watts):
                self.assertEqual(dashboard._format_power(watts), expected)


class WindowFrameTests(unittest.TestCase):
    def test_default_regular_window_size(self) -> None:
        path = Path(dashboard.__file__).resolve().parents[1] / "app.py"
        with patch.object(dashboard.STORE, "get_setting", side_effect=lambda name, default: default), \
                patch.object(dashboard.STORE, "get_bool_setting", side_effect=lambda name, default=False: default), \
                patch.object(dashboard.STORE, "get_text_setting", side_effect=lambda name, default="": default), \
                patch("nicegui.ui.run") as run:
            runpy.run_path(str(path), run_name="__main__")
        self.assertEqual(dashboard.DEFAULT_WINDOW_SIZE,
                         (min(600, dashboard.MONITOR_WIDTH), min(680, dashboard.MONITOR_HEIGHT)))
        self.assertEqual(run.call_args.kwargs["window_size"], dashboard.DEFAULT_WINDOW_SIZE)

    def test_saved_regular_window_size_is_preserved(self) -> None:
        path = Path(dashboard.__file__).resolve().parents[1] / "app.py"
        settings = {"window_width": 455, "window_height": 535}
        with patch.object(dashboard.STORE, "get_setting", side_effect=lambda name, default: settings.get(name, default)), \
                patch.object(dashboard.STORE, "get_bool_setting", side_effect=lambda name, default=False: default), \
                patch.object(dashboard.STORE, "get_text_setting", side_effect=lambda name, default="": default), \
                patch("nicegui.ui.run") as run:
            runpy.run_path(str(path), run_name="__main__")
        self.assertEqual(run.call_args.kwargs["window_size"],
                         (min(455, dashboard.MONITOR_WIDTH), min(535, dashboard.MONITOR_HEIGHT)))

    def test_native_drag_region_is_limited_to_compact_view(self) -> None:
        path = Path(dashboard.__file__).resolve().parents[1] / "app.py"
        with patch("nicegui.ui.run"), patch.dict(dashboard.app.native.settings, {}, clear=True):
            runpy.run_path(str(path), run_name="__main__")
            self.assertEqual(dashboard.app.native.settings["DRAG_REGION_SELECTOR"],
                             ".page-content.compact .view-box")

    def test_compact_frame_has_no_caption_or_resize_border(self) -> None:
        style = dashboard._window_frame_style(0x10CF0000, True, True)
        self.assertEqual(style & 0x00CF0000, 0)
        self.assertNotEqual(style & 0x10000000, 0)

    def test_regular_frame_restores_title_and_resize_controls(self) -> None:
        compact = dashboard._window_frame_style(0x10CF0000, True, True)
        regular = dashboard._window_frame_style(compact, False, True)
        self.assertEqual(regular & 0x00CF0000, 0x00CF0000)

    def test_regular_frame_respects_resize_disabled(self) -> None:
        regular = dashboard._window_frame_style(0x10000000, False, False)
        self.assertEqual(regular & 0x00C00000, 0x00C00000)
        self.assertEqual(regular & 0x00050000, 0)


class TemperatureTests(unittest.TestCase):
    def test_temperature_conversion(self) -> None:
        self.assertEqual(dashboard._temperature_value(30, "F"), 86)
        self.assertEqual(dashboard._temperature_celsius(86, "F"), 30)
        self.assertEqual(dashboard._temperature_value(30, "C"), 30)

    def test_temperature_formatting(self) -> None:
        store = Mock()
        for unit, expected in (("C", "30°C"), ("F", "86°F")):
            store.get_text_setting.return_value = unit
            with patch.object(dashboard, "STORE", store):
                self.assertEqual(dashboard._format_temperature(30), expected)
                self.assertEqual(dashboard._format_temperature(None), "")

    def test_temperature_alert_without_soc_deduplicates_and_rearms(self) -> None:
        store = Mock()
        settings = {"alert_temperature_c": "40", "temperature_unit": "F"}
        store.get_text_setting.side_effect = lambda name, default="": settings.get(name, default)
        store.get_bool_setting.side_effect = lambda name, default=False: settings.get(name, default)
        store.set_bool_setting.side_effect = lambda name, value: settings.update({name: value})
        device = {"device_key": "test"}
        with patch.object(dashboard, "STORE", store), patch.object(dashboard, "_notify") as notify:
            for temperature in (39, 40, 41, 38, 40):
                store.latest_measurements.return_value = {"battery.temperature": {"value": temperature}}
                dashboard._check_battery_alerts(device)
            self.assertEqual(notify.call_count, 2)
            self.assertIn("104°F", notify.call_args.args[2])


if __name__ == "__main__":
    unittest.main()