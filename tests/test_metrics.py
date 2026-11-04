import unittest

from src.metrics import (
    UnsupportedModelError,
    classify_power_flows,
    estimate_battery_runtime_hours,
    extract_measurements,
    model_profile,
    resolve_battery_power,
)


class MetricTests(unittest.TestCase):
    def test_wrapped_backup_value_does_not_override_grid_charging(self) -> None:
        self.assertEqual(resolve_battery_power(None, -2240, 65521), (2240, True))
        self.assertEqual(classify_power_flows(-2240, 65521), "Grid flow active")
        self.assertEqual(resolve_battery_power(None, -1000, 200), (800, True))
        self.assertEqual(resolve_battery_power(None, -200, 300), (-100, True))

    def test_wrapped_backup_power_is_decoded_at_extraction(self) -> None:
        for method, key in (("ES.GetMode", "mode.offgrid_power"), ("ES.GetStatus", "offgrid.power")):
            with self.subTest(method=method):
                measurements = extract_measurements(method, {"offgrid_power": 65521})
                self.assertEqual([(item.key, item.value) for item in measurements], [(key, -15)])

    def test_grid_input_is_an_estimated_charge_rate_only_with_known_backup_output(self) -> None:
        self.assertEqual(resolve_battery_power(None, -795, 0), (795, True))
        self.assertEqual(resolve_battery_power(None, -795, None), (None, False))
        self.assertEqual(resolve_battery_power(None, 795, 0), (None, False))

    def test_reported_power_and_standby_take_priority(self) -> None:
        self.assertEqual(resolve_battery_power(700, -795, 0), (700, False))
        self.assertEqual(resolve_battery_power(0, -795, 0), (0, False))
        self.assertEqual(resolve_battery_power(None, 0, 170), (-170, False))
        self.assertEqual(resolve_battery_power(None, 0, 0), (0, False))

    def test_maps_documented_device_families(self) -> None:
        self.assertTrue(model_profile("VenusD").supports_pv_inputs)
        self.assertFalse(model_profile("VenusE").supports_pv_inputs)
        self.assertFalse(model_profile("Venus E mini").supports_pv_inputs)

    def test_rejects_models_not_in_reference(self) -> None:
        with self.assertRaises(UnsupportedModelError):
            model_profile("Jupiter C Plus")

    def test_converts_documented_scaled_energy_fields(self) -> None:
        measurements = extract_measurements("ES.GetStatus", {
            "total_pv_energy": 123,
            "bat_soc": 70,
            "bat_power": None,
        })

        self.assertEqual([(item.key, item.value, item.unit) for item in measurements], [
            ("battery.soc_system", 70.0, "%"),
            ("solar.energy_total", 1.23, "kWh"),
        ])

    def test_reads_only_documented_pv_input_fields(self) -> None:
        measurements = extract_measurements("PV.GetStatus", {
            "pv1_power": 50,
            "pv2_voltage": 24,
            "pv1_state": 1,
            "unknown": 4,
        })

        self.assertEqual({item.key for item in measurements}, {
            "solar.pv1_power",
            "solar.pv2_voltage",
        })

    def test_classifies_measured_grid_and_backup_flows(self) -> None:
        self.assertEqual(classify_power_flows(120, 0), "Grid flow active")
        self.assertEqual(classify_power_flows(0, 250), "Backup flow active")
        self.assertEqual(classify_power_flows(120, 250), "Grid + backup flows")
        self.assertEqual(classify_power_flows(0, 0), "No active power flow")
        self.assertEqual(classify_power_flows(
            None, None), "Power flow unavailable")

    def test_estimates_runtime_from_remaining_capacity_or_soc(self) -> None:
        self.assertEqual(estimate_battery_runtime_hours(
            500, remaining_capacity_wh=1500), 3)
        self.assertEqual(
            estimate_battery_runtime_hours(
                500, rated_capacity_wh=4000, soc_percent=50),
            4,
        )
        self.assertIsNone(estimate_battery_runtime_hours(
            0, remaining_capacity_wh=1500))
        self.assertIsNone(estimate_battery_runtime_hours(500))


if __name__ == "__main__":
    unittest.main()
