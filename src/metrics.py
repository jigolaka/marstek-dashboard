"""Model capability and read-only measurement definitions from Open API Rev 3.1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelProfile:
    family: str
    telemetry_methods: tuple[str, ...]
    supports_pv_inputs: bool


@dataclass(frozen=True)
class Measurement:
    key: str
    value: float
    unit: str
    method: str


class UnsupportedModelError(ValueError):
    pass


def normalize_backup_power(value: float | None) -> float | None:
    if value is not None and 32768 <= value <= 65535 and float(value).is_integer():
        return value - 65536
    return value


def resolve_battery_power(
    battery_power: float | None,
    grid_power: float | None,
    backup_power: float | None,
) -> tuple[float | None, bool]:
    backup_power = normalize_backup_power(backup_power)
    if battery_power is not None and abs(battery_power) > 1:
        return battery_power, False
    if battery_power is None and backup_power is not None and grid_power is not None and grid_power < -1:
        return -grid_power - max(0.0, backup_power), True
    if backup_power is not None and backup_power > 1:
        return -backup_power, False
    if battery_power is None and backup_power is not None and grid_power is not None:
        if abs(grid_power) <= 1:
            return 0.0, False
    return battery_power, False


def classify_power_flows(grid_power_w: float | None, backup_power_w: float | None) -> str:
    backup_power_w = normalize_backup_power(backup_power_w)
    grid_active = grid_power_w is not None and abs(grid_power_w) > 1
    backup_active = backup_power_w is not None and backup_power_w > 1
    if grid_active and backup_active:
        return "Grid + backup flows"
    if backup_active:
        return "Backup flow active"
    if grid_active:
        return "Grid flow active"
    if grid_power_w is None and backup_power_w is None:
        return "Power flow unavailable"
    return "No active power flow"


def estimate_battery_runtime_hours(
    discharge_power_w: float | None,
    remaining_capacity_wh: float | None = None,
    rated_capacity_wh: float | None = None,
    soc_percent: float | None = None,
) -> float | None:
    if discharge_power_w is None or abs(discharge_power_w) <= 1:
        return None
    if remaining_capacity_wh is not None and remaining_capacity_wh > 0:
        available_energy_wh = remaining_capacity_wh
    elif (
        rated_capacity_wh is not None
        and rated_capacity_wh > 0
        and soc_percent is not None
        and 0 <= soc_percent <= 100
    ):
        available_energy_wh = rated_capacity_wh * soc_percent / 100
    else:
        return None
    if available_energy_wh <= 0:
        return 0.0
    return available_energy_wh / abs(discharge_power_w)


_BASE_METHODS = (
    "Bat.GetStatus",
    "ES.GetStatus",
    "ES.GetMode",
    "EM.GetStatus",
)


def model_profile(device: str) -> ModelProfile:
    normalized = re.sub(r"[^a-z0-9]", "", device.casefold())
    if "venusemini" in normalized:
        return ModelProfile("Venus E mini", _BASE_METHODS, False)
    if normalized.startswith(("venusa", "venusd")):
        return ModelProfile("Venus A/D", (*_BASE_METHODS, "PV.GetStatus"), True)
    if normalized.startswith(("venusc", "venuse")):
        return ModelProfile("Venus C/E", _BASE_METHODS, False)
    raise UnsupportedModelError(
        f"The Open API reference does not define telemetry for {device!r}")


def extract_measurements(method: str, result: dict[str, Any]) -> list[Measurement]:
    fields = _METHOD_FIELDS.get(method, ())
    measurements: list[Measurement] = []
    for field, key, unit, multiplier in fields:
        value = _number(result.get(field))
        if key in {"offgrid.power", "mode.offgrid_power"}:
            value = normalize_backup_power(value)
        if value is not None:
            measurements.append(Measurement(
                key, value * multiplier, unit, method))

    if method == "PV.GetStatus":
        for field, raw_value in result.items():
            if not re.fullmatch(r"pv[1-4]_(?:power|voltage|current)", field):
                continue
            value = _number(raw_value)
            if value is None:
                continue
            suffix = field.rsplit("_", 1)[1]
            unit = {"power": "W", "voltage": "V", "current": "A"}[suffix]
            measurements.append(Measurement(
                f"solar.{field}", value, unit, method))
    return measurements


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_METHOD_FIELDS: dict[str, tuple[tuple[str, str, str, float], ...]] = {
    "Bat.GetStatus": (
        ("soc", "battery.soc", "%", 1.0),
        ("bat_temp", "battery.temperature", "°C", 1.0),
        ("bat_capacity", "battery.remaining_capacity", "Wh", 1.0),
        ("rated_capacity", "battery.rated_capacity", "Wh", 1.0),
    ),
    "ES.GetStatus": (
        ("bat_soc", "battery.soc_system", "%", 1.0),
        ("bat_cap", "battery.capacity_system", "Wh", 1.0),
        ("pv_power", "solar.power", "W", 1.0),
        ("ongrid_power", "grid.power", "W", 1.0),
        ("offgrid_power", "offgrid.power", "W", 1.0),
        ("bat_power", "battery.power", "W", 1.0),
        ("total_pv_energy", "solar.energy_total", "kWh", 0.01),
        ("total_grid_output_energy", "grid.output_energy_total", "Wh", 1.0),
        ("total_grid_input_energy", "grid.input_energy_total", "Wh", 1.0),
        ("total_load_energy", "load.energy_total", "Wh", 1.0),
    ),
    "ES.GetMode": (
        ("ongrid_power", "mode.grid_power", "W", 1.0),
        ("offgrid_power", "mode.offgrid_power", "W", 1.0),
        ("bat_soc", "mode.battery.soc", "%", 1.0),
        ("a_power", "mode.phase_a_power", "W", 1.0),
        ("b_power", "mode.phase_b_power", "W", 1.0),
        ("c_power", "mode.phase_c_power", "W", 1.0),
        ("total_power", "mode.total_power", "W", 1.0),
        ("input_energy", "mode.input_energy", "Wh", 0.1),
        ("output_energy", "mode.output_energy", "Wh", 0.1),
    ),
    "EM.GetStatus": (
        ("a_power", "meter.phase_a_power", "W", 1.0),
        ("b_power", "meter.phase_b_power", "W", 1.0),
        ("c_power", "meter.phase_c_power", "W", 1.0),
        ("total_power", "meter.total_power", "W", 1.0),
        ("input_energy", "meter.input_energy", "Wh", 0.1),
        ("output_energy", "meter.output_energy", "Wh", 0.1),
    ),
}
