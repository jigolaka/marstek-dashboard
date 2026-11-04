"""Compact NiceGUI battery widget with background polling and local persistence."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import sys
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nicegui import app, background_tasks, ui
import psutil
from screeninfo import get_monitors

from src import __version__
from src.api import UdpMarstekClient
from src.discovery import (
    DEFAULT_UDP_PORT,
    DeviceScanResult,
    list_local_interfaces,
    scan_for_marstek_devices,
)
from src.metrics import (
    UnsupportedModelError,
    model_profile,
    normalize_backup_power,
    resolve_battery_power,
)
from src.storage import DEFAULT_POLL_SECONDS, DEFAULT_RETENTION_DAYS, DashboardStore

logger = logging.getLogger(__name__)
def _default_db_path() -> str:
    """Installed builds keep data per user; a source checkout keeps ./data."""
    if not getattr(sys, "frozen", False):
        return "data/marstek.sqlite3"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return str(base / "Marstek Dashboard" / "marstek.sqlite3")


STORE = DashboardStore(os.getenv("MARSTEK_DB_PATH", _default_db_path()))
ACTIVE_DEVICE = "active_device"
BATTERY_MODE = "battery_mode"
LOW_SOC, CRITICAL_SOC, HOT_C, COLD_C = 20, 10, 45, 0
IDLE_WATTS = 1
CHART_BUCKETS = 120
KIND_COLORS = {"Discharging": "#f18477", "Charging": "#73e2a7", "Standby": "#8ecafc"}
DEFAULT_STATS_MINUTES = 60
WINDOW_TITLE = "Marstek Dashboard"


def _own_window_handle() -> int:
    """Top-level window of this app's native child process, not another running copy."""
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    pids = {child.pid for child in psutil.Process().children(recursive=True)}
    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _: int) -> bool:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            if title.value == WINDOW_TITLE:
                found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    return found[0] if found else 0


def _window_frame_style(style: int, compact: bool, resizable: bool) -> int:
    caption, system_menu, minimize_box = 0x00C00000, 0x00080000, 0x00020000
    thick_frame, maximize_box = 0x00040000, 0x00010000
    if compact:
        return style & ~(caption | system_menu | minimize_box | thick_frame | maximize_box)
    style |= caption | system_menu | minimize_box
    return style | thick_frame | maximize_box if resizable else style & ~(thick_frame | maximize_box)


def _set_window_resizable(allowed: bool) -> None:
    """pywebview only fixes resizability at creation, so toggle the Win32 frame style live."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = _own_window_handle()
    if not hwnd or STORE.get_text_setting("window_mode", "windowed") != "windowed":
        return
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    style = _window_frame_style(user32.GetWindowLongW(hwnd, -16),
                                STORE.get_bool_setting("compact_mode", False), allowed)
    user32.SetWindowLongW(hwnd, -16, style)
    # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x37)


def _set_native_icons() -> None:
    if sys.platform != "win32":
        return
    hwnd = _own_window_handle()
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    dpi = user32.GetDpiForWindow(hwnd) or 96
    icon_path = str(Path(__file__).resolve().parent / "static" / "logo.ico")
    for kind, metric in ((0, 49), (1, 11)):
        size = user32.GetSystemMetricsForDpi(metric, dpi)
        icon = user32.LoadImageW(None, icon_path, 1, size, size, 0x10)
        if icon:
            user32.SendMessageW(hwnd, 0x0080, kind, icon)


def _monitor_dimensions() -> tuple[int, int]:
    try:
        monitors = get_monitors()
        monitor = next((item for item in monitors if item.is_primary), monitors[0])
        return monitor.width, monitor.height
    except Exception:
        logger.warning("Could not detect monitor dimensions; using a 360 px widget")
        return 1920, 1080


MONITOR_WIDTH, MONITOR_HEIGHT = _monitor_dimensions()
DEFAULT_WINDOW_SIZE = (min(MONITOR_WIDTH, 600), min(MONITOR_HEIGHT, 680))
COMPACT_SIZE = (min(MONITOR_WIDTH, 420), 120)
COMPACT_FIT_JS = """(() => {
  const px = (el, prop) => parseFloat(getComputedStyle(el)[prop]) || 0;
  const content = document.querySelector('.page-content');
  const header = document.querySelector('.header-row');
  const outer = document.querySelector('.nicegui-content');
  const page = document.querySelector('.q-page');
  const width = header.getBoundingClientRect().right + px(content, 'paddingRight')
    + px(outer, 'paddingRight') + px(page, 'paddingRight');
  const height = content.getBoundingClientRect().bottom + px(outer, 'paddingBottom') + px(page, 'paddingBottom');
  return [Math.ceil(width), Math.ceil(height), innerWidth, innerHeight];
})()"""

STYLES = """
<style>
    html, body, #app { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body, .q-dialog__backdrop { background: #000; color: #fff; font-family: "Segoe UI", sans-serif; }
    .q-page { width: 100%; height: 100dvh; min-height: 0 !important; box-sizing: border-box; overflow: auto; padding: 8px; }
    .page-content { min-width: 300px; }
    .page-content:not(.compact) { container-type: inline-size; }
    .page-content:not(.compact) .header-row {
        display: grid; grid-template-columns: 32px minmax(0, 1fr) max-content;
        grid-template-areas: "logo metadata controls";
    }
    .header-logo { grid-area: logo; }
    .header-metadata {
        grid-area: metadata; min-width: 0; overflow: auto; background: #141414;
        border-radius: 8px; padding: 8px 12px; box-sizing: border-box;
    }
    .header-controls { grid-area: controls; }
    .metadata-table { width: 100%; min-width: max-content; border-collapse: collapse; font-size: 12px; }
    .metadata-table td { text-align: center; white-space: nowrap; padding: 0 6px; border-right: 1px solid #333; }
    .metadata-table td:last-child { border-right: 0; }
    @container (max-width: 520px) {
        .page-content:not(.compact) .header-row {
            grid-template-columns: 1fr max-content; grid-template-areas: "logo controls" "metadata metadata";
        }
    }
    .page-content.compact { flex-direction: row !important; align-items: center; flex-wrap: nowrap; min-width: 0; }
    .page-content.compact .header-row { order: 2; width: auto !important; flex-shrink: 0; }
    .page-content.compact .header-logo, .page-content.compact .hide-compact, .page-content.compact .app-version { display: none; }
    .page-content.compact .view-box { order: 1; flex: 0 0 auto; width: auto !important; }
    .page-content.compact .view-box { cursor: move; user-select: none; }
    .nicegui-content:has(.page-content.compact) { padding: 2px 8px; }
    .q-dialog .q-card { max-width: 95vw !important; max-height: 95dvh; overflow: auto; }
    .q-dialog .q-card > * { min-width: 260px; flex-shrink: 0; }
    * { scrollbar-width: thin; scrollbar-color: #8a8a8a #141414; }
    .q-select__dropdown-icon { transition: none !important; }
    .muted { color: #9a9a9a; }
    .big { font-size: 48px; font-weight: 700; line-height: 1; }
    .card { background: #141414; border-radius: 12px; padding: 12px; width: 100%; }
    .draw-wrap { width: 100%; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box;
                 background: #141414; border-radius: 12px; padding: 12px; }
    .draw-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .draw-chart { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .spark-box { height: 64px; }
    .spark { display: block; width: 100%; height: 100%; }
    .legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .nicegui-content:has(.page-content:not(.compact)) { min-height: 100%; box-sizing: border-box; }
    .page-content:not(.compact), .page-content:not(.compact) .view-box,
    .page-content:not(.compact) .draw-wrap { flex: 1 0 auto; }
    .page-content:not(.compact) .draw-chart { flex: 1 1 0; }
    .page-content:not(.compact) .spark-box { flex: 1 0 64px; position: relative; }
    .page-content:not(.compact) .spark { position: absolute; inset: 0; }
    @media (max-height: 460px) {
        .draw-wrap { flex-direction: row; background: none; padding: 0; gap: 8px; }
        .draw-main, .draw-chart { flex: 1 1 0; background: #141414; border-radius: 12px;
                                  padding: 8px 12px; box-sizing: border-box; }
        .spark-box { flex: 1; min-height: 48px; height: auto; }
        .big { font-size: 40px; }
        .card { padding: 8px 12px; }
        .q-page { padding: 4px 8px; }
    }
    .q-card { background: #141414; }
    .settings-card { max-height: 90dvh; overflow: auto; }
    .q-btn.bg-primary { background: #fff !important; color: #000 !important; }
    .q-btn .q-icon, .q-btn__content { color: #000 !important; }
    .state-discharging { color: #f18477; }
    .state-standby { color: #8ecafc; }
    .state-offline { color: #9a9a9a; }
    .state-charging { color: #73e2a7; }
    .warn-critical { color: #f18477; }
    .warn-warning { color: #f6c86d; }
</style>
"""
LOGO_URL = app.add_static_file(
    local_file=Path(__file__).resolve().parent / "static" / "logo.png", url_path="/static/logo.png")

ALERT_TOGGLES = {"Marstek": "alerts_marstek", "System": "alerts_system"}
OFFLINE_AFTER_FAILURES = 3
_poll_failures = 0
_poll_online: dict[str, bool] = {}


def _notify(source: str, level: str, message: str) -> None:
    """Store a notification unless its source (Marstek or System) is switched off."""
    if STORE.get_bool_setting(ALERT_TOGGLES[source], True):
        STORE.add_notification(source, level, message)


def _device_name(device: dict[str, object]) -> str:
    return str(device["designation"] or device["model"])


def _temperature_value(celsius: float, unit: str) -> float:
    return celsius * 9 / 5 + 32 if unit == "F" else celsius


def _temperature_celsius(value: float, unit: str) -> float:
    return (value - 32) * 5 / 9 if unit == "F" else value


def _format_temperature(celsius: float | None) -> str:
    if celsius is None:
        return ""
    unit = STORE.get_text_setting("temperature_unit", "C")
    return f"{_temperature_value(celsius, unit):.1f}".rstrip("0").rstrip(".") + f"°{unit}"


def _temperature_threshold() -> float:
    try:
        value = float(STORE.get_text_setting("alert_temperature_c", str(HOT_C)))
        return value if math.isfinite(value) else HOT_C
    except ValueError:
        return HOT_C


def _check_temperature_alert(device: dict[str, object], latest: dict[str, dict[str, object]]) -> None:
    temperature = _value(latest, "battery.temperature")
    if temperature is None:
        return
    threshold = _temperature_threshold()
    setting = f"alert_temperature_active:{device['device_key']}"
    active = STORE.get_bool_setting(setting, False)
    if temperature >= threshold and not active:
        STORE.set_bool_setting(setting, True)
        _notify("Marstek", "warning", f"Battery temperature {_format_temperature(temperature)} "
                f"(limit {_format_temperature(threshold)})")
    elif temperature <= threshold - 2 and active:
        STORE.set_bool_setting(setting, False)


def _check_battery_alerts(device: dict[str, object]) -> None:
    latest = STORE.latest_measurements(str(device["device_key"]))
    _check_temperature_alert(device, latest)
    soc = _value(latest, "battery.soc", "battery.soc_system")
    if soc is None:
        return
    # The higher of the alert level and the reserve wins, so reserve power is never eaten silently.
    threshold = max(STORE.get_setting("alert_low_pct", LOW_SOC),
                    STORE.get_setting("alert_reserve_pct", 0))
    low_active = STORE.get_bool_setting("alert_low_active", False)
    if soc <= threshold and not low_active:
        STORE.set_bool_setting("alert_low_active", True)
        _notify("Marstek", "critical" if soc <= CRITICAL_SOC else "warning",
                f"Battery low: {soc:g}% left (alert level {threshold}%)")
    elif soc > threshold + 2 and low_active:
        STORE.set_bool_setting("alert_low_active", False)

    full_active = STORE.get_bool_setting("alert_full_active", False)
    if soc >= 100 and not full_active:
        STORE.set_bool_setting("alert_full_active", True)
        if STORE.get_bool_setting("alert_full", True):
            _notify("Marstek", "success", "Battery fully charged (100%)")
    elif soc < 98 and full_active:
        STORE.set_bool_setting("alert_full_active", False)


def _active_device() -> dict[str, object] | None:
    devices = STORE.list_devices()
    key = STORE.get_text_setting(ACTIVE_DEVICE)
    return next((d for d in devices if d["device_key"] == key), devices[0] if devices else None)


async def _poll_active() -> None:
    global _poll_failures
    device = _active_device()
    if device is None:
        return
    client = UdpMarstekClient(STORE.get_setting("udp_port", DEFAULT_UDP_PORT))
    try:
        profile = model_profile(str(device["model"]))
        measurements = await client.read_snapshot(str(device["ip_address"]), profile)
        if not measurements:
            raise ValueError("No telemetry returned by device")
        STORE.record_measurements(str(device["device_key"]), measurements)
        if client.mode:
            STORE.set_text_setting(f"{BATTERY_MODE}:{device['device_key']}", client.mode)
    except (OSError, TimeoutError, UnsupportedModelError, ValueError) as error:
        _poll_online[str(device["device_key"])] = False
        logger.warning("Telemetry unavailable for %s: %s",
                       device["device_key"], error)
        _poll_failures += 1
        if _poll_failures == OFFLINE_AFTER_FAILURES:
            _notify("Marstek", "warning", f"{_device_name(device)} is not responding")
        return
    except Exception:
        _poll_online[str(device["device_key"])] = False
        logger.exception("Unexpected telemetry error")
        return
    _poll_online[str(device["device_key"])] = True
    if _poll_failures >= OFFLINE_AFTER_FAILURES:
        _notify("Marstek", "success", f"{_device_name(device)} is responding again")
    _poll_failures = 0
    _check_battery_alerts(device)


async def _collect_forever() -> None:
    _notify("System", "info", "Application started")
    while True:
        await _poll_active()
        STORE.purge_expired(DEFAULT_RETENTION_DAYS)
        await asyncio.sleep(STORE.get_setting("poll_seconds", DEFAULT_POLL_SECONDS))


app.on_startup(lambda: background_tasks.create(_collect_forever()))


def _value(latest: dict[str, dict[str, object]], *keys: str) -> float | None:
    for key in keys:
        if key in latest:
            return float(latest[key]["value"])
    return None


def _fmt_duration(hours: float) -> str:
    minutes = max(0, round(hours * 60))
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _format_power(watts: float | None) -> str:
    if watts is None:
        return "--"
    value = abs(watts)
    return f"{value / 1000:.2f} kW" if value >= 1000 else f"{value:,.0f} W"


def _format_capacity(remaining: float | None, rated: float | None) -> str:
    available = "--" if remaining is None else f"{remaining / 1000:.2f}"
    total = "--" if rated is None else f"{rated / 1000:.2f}"
    return f"{available} kWh / {total} kWh"


def _analyze(device: dict[str, object]) -> dict[str, object]:
    key = str(device["device_key"])
    latest = STORE.latest_measurements(key)
    soc = _value(latest, "battery.soc", "battery.soc_system", "mode.battery.soc")
    power, power_estimated = resolve_battery_power(
        _value(latest, "battery.power"),
        _value(latest, "mode.grid_power", "grid.power"),
        _value(latest, "mode.offgrid_power", "offgrid.power"),
    )
    remaining = _value(latest, "battery.remaining_capacity")
    rated = _value(latest, "battery.rated_capacity")
    temp = _value(latest, "battery.temperature")
    if remaining is None and rated is not None and soc is not None:
        remaining = rated * soc / 100
    timestamps = [str(row["recorded_at"]) for row in latest.values() if row.get("recorded_at")]
    last_received = max(timestamps, default=str(device["last_seen"])) if latest else None
    stale_after = max(60, 3 * STORE.get_setting("poll_seconds", DEFAULT_POLL_SECONDS))
    offline = (not latest or _poll_online.get(key) is False or
               (last_received is not None and datetime.now(timezone.utc) -
                datetime.fromisoformat(last_received) > timedelta(seconds=stale_after)))
    if offline:
        power, power_estimated = None, False

    # Assumes battery power is positive while charging, negative while discharging.
    state = "Offline" if offline else "Standby" if power is not None else "No data"
    eta, hours, eta_hours = None, None, None
    if power is not None and power < -IDLE_WATTS:
        state = "Discharging"
        reserve = STORE.get_setting("alert_reserve_pct", 0)
        energy = remaining if remaining else (
            rated * soc / 100 if rated and soc is not None else None)
        if energy is not None:
            usable = max(0.0, energy - (rated or 0) * reserve / 100)
            hours = eta_hours = usable / abs(power)
            suffix = f" ({reserve}% reserve)" if reserve else ""
            eta = f"Empty in {_fmt_duration(hours)}{suffix}"
    elif power is not None and power > IDLE_WATTS:
        state = "Charging"
        if rated and soc is not None:
            eta_hours = rated * (100 - soc) / 100 / power
            eta = f"Full in {_fmt_duration(eta_hours)} (100%)"

    warnings: list[tuple[str, str]] = []
    if offline:
        warnings.append(("critical", "No recent data from device"))
    if soc is not None and soc <= CRITICAL_SOC:
        warnings.append(("critical", f"Battery critical ({soc:g}%)"))
    elif soc is not None and soc <= LOW_SOC:
        warnings.append(("warning", f"Battery low ({soc:g}%)"))
    if hours is not None and hours < 1:
        warnings.append(("critical", "Less than 1 hour of runtime left"))
    if temp is not None and (temp >= _temperature_threshold() or temp <= COLD_C):
        warnings.append(("warning", f"Battery temperature {_format_temperature(temp)}"))
    if state == "Charging" and soc is not None and soc >= 100:
        warnings.append(("warning", "Battery full"))

    stats_minutes = STORE.get_setting("stats_minutes", DEFAULT_STATS_MINUTES)
    now = datetime.now(timezone.utc)
    rows = STORE.measurements_since(
        key, now - timedelta(minutes=stats_minutes))
    by_time: dict[str, dict[str, float]] = {}
    for r in rows:
        by_time.setdefault(str(r["recorded_at"]), {})[str(r["metric_key"])] = float(r["value"])
    span_seconds = stats_minutes * 60
    window_start = now - timedelta(seconds=span_seconds)
    stats: list[float] = []
    buckets: dict[int, list[float]] = {}
    for stamp in sorted(by_time):
        sample = by_time[stamp]
        watts, _ = resolve_battery_power(
            sample.get("battery.power"),
            sample.get("mode.grid_power", sample.get("grid.power")),
            sample.get("mode.offgrid_power", sample.get("offgrid.power")),
        )
        if watts is None:
            continue
        stats.append(abs(watts))
        fraction = (datetime.fromisoformat(stamp) - window_start).total_seconds() / span_seconds
        buckets.setdefault(min(CHART_BUCKETS - 1, max(0, int(fraction * CHART_BUCKETS))), []).append(watts)
    series = []
    for index, values in sorted(buckets.items()):
        average = sum(values) / len(values)
        series.append(((index + 0.5) / CHART_BUCKETS, abs(average), _flow_kind(average)))
    return {"soc": soc, "power": power, "state": state, "eta": eta, "eta_hours": eta_hours,
                        "offline": offline, "last_received": last_received,
            "power_estimated": power_estimated,
            "grid_state": _grid_state(latest),
            "temperature": temp,
            "remaining": remaining, "rated": rated, "warnings": warnings,
            "series": series, "stats": stats, "stats_minutes": stats_minutes}


def _flow_kind(watts: float) -> str:
    """Battery power is positive while charging, negative while discharging."""
    if watts > IDLE_WATTS:
        return "Charging"
    return "Discharging" if watts < -IDLE_WATTS else "Standby"


def _sparkline(series: list[tuple[float, float, str]]) -> str:
    """x is the position within the statistics window (0-1); y is watts from 0 to the peak."""
    top = max(w for _, w, _ in series) * 1.1 or 1
    segments: list[list[tuple[float, float, str]]] = [[series[0]]]
    for previous, point in zip(series, series[1:]):
        if point[2] != previous[2]:
            segments.append([previous])  # shared point keeps the line unbroken
        segments[-1].append(point)
    shapes = []
    for segment in segments:
        if len(segment) < 2:
            continue
        color = KIND_COLORS[segment[-1][2]]
        points = " ".join(f"{x * 100:.2f},{38 - w / top * 34:.2f}" for x, w, _ in segment)
        shapes.append(
            f'<polygon points="{segment[0][0] * 100:.2f},40 {points} {segment[-1][0] * 100:.2f},40" '
            f'fill="{color}" fill-opacity="0.15"/>'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" '
            'vector-effect="non-scaling-stroke" stroke-linejoin="round"/>')
    return ('<svg class="spark" viewBox="0 0 100 40" preserveAspectRatio="none">'
            + "".join(shapes) + '</svg>')


def _compact_duration(hours: float) -> str:
    return f"{round(hours)} h" if hours >= 1 else f"{max(0, round(hours * 60))} min"


def _battery_mode(device: dict[str, object]) -> str:
    return STORE.get_text_setting(f"{BATTERY_MODE}:{device['device_key']}")


def _grid_state(latest: dict[str, dict[str, object]]) -> str:
    """On-grid or backup, from the ES.GetMode grid and off-grid power; empty when neither flows."""
    grid = _value(latest, "mode.grid_power", "grid.power")
    backup = normalize_backup_power(_value(latest, "mode.offgrid_power", "offgrid.power"))
    on_grid = grid is not None and abs(grid) > IDLE_WATTS
    on_backup = backup is not None and backup > IDLE_WATTS
    if on_grid and on_backup:
        return "On-grid + Backup"
    return "Backup" if on_backup else "On-grid" if on_grid else ""


def _render_compact(device: dict[str, object], data: dict[str, object]) -> None:
    soc, power, state = data["soc"], data["power"], str(data["state"])
    last_received = data.get("last_received", device["last_seen"])
    updated = datetime.fromisoformat(str(last_received)).astimezone() if last_received else None
    offline = data.get("offline", False)
    parts = [str(device["designation"] or device["model"]), _battery_mode(device),
             str(data["grid_state"]), _format_temperature(data["temperature"]),
             str(device["ip_address"])]
    with ui.column().classes("gap-0 w-full"):
        ui.label(" | ".join(part for part in parts if part)).classes("muted text-xs")
        with ui.element("div").classes("inline-grid").style("grid-template-columns: max-content"):
            with ui.row().classes("items-baseline gap-x-3 gap-y-0 flex-wrap"):
                ui.label("--" if soc is None else f"{soc:g}%").classes("text-xl font-bold")
                state_label = ui.label(state).classes("text-xl font-bold")
                if state == "Discharging":
                    state_label.classes(add="state-discharging")
                elif state == "Charging":
                    state_label.classes(add="state-charging")
                elif state == "Standby":
                    state_label.classes(add="state-standby")
                elif state == "Offline":
                    state_label.classes(add="state-offline")
                if offline:
                    ui.label(_format_capacity(data.get("remaining"), data.get("rated"))).classes(
                        "text-xl font-bold")
                elif power is not None:
                    ui.label(_format_power(power)).classes("text-xl font-bold")
                if not offline and data["eta_hours"] is not None:
                    ui.label(_compact_duration(float(data["eta_hours"]))).classes("text-xl font-bold")
            with ui.row().classes("items-center gap-2 no-wrap w-full"):
                ui.label(f"Updated {updated:%H:%M:%S}" if updated else "No readings stored").classes(
                    "muted text-xs whitespace-nowrap")
                if not offline:
                    ui.linear_progress(value=max(0, min(100, soc or 0)) / 100, show_value=False).props(
                        "rounded size=4px color=white track-color=grey-9"
                    ).classes("flex-1").style("width: 0; min-width: 24px")


@ui.page("/")
def widget_page() -> None:
    ui.add_head_html(STYLES)
    ui.colors(primary="#ffffff")
    ui.page_title(WINDOW_TITLE)
    ui.dark_mode(True)

    @ui.refreshable
    def view() -> None:
        device = _active_device()
        if device is None:
            header_metadata.set_visibility(False)
            with ui.column().classes("card gap-1"):
                ui.label("No device yet").classes("muted text-xs")
                ui.label("Open Settings > Scan to find one.").classes("text-sm")
            return
        data = _analyze(device)
        if STORE.get_bool_setting("compact_mode", False):
            _render_compact(device, data)
            return
        soc, power = data["soc"], data["power"]
        updated = datetime.fromisoformat(str(device["last_seen"])).astimezone()
        header_metadata.set_visibility(True)
        metadata_values = (
            str(device["designation"] or device["model"]), _battery_mode(device),
            str(data["grid_state"]), _format_temperature(data["temperature"]),
            str(device["ip_address"]))
        for cell, value in zip(metadata_cells, metadata_values):
            cell.text = value or "--"
        remaining, rated = data["remaining"], data["rated"]
        if remaining is None and rated is not None and soc is not None:
            remaining = rated * soc / 100
        with ui.row().classes("items-end gap-x-2 gap-y-0 flex-wrap w-full"):
            ui.label("--" if soc is None else f"{soc:g}%").classes("big")
            state_label = ui.label(str(data["state"])).classes("pb-1")
            if data["state"] == "Discharging":
                state_label.classes(add="state-discharging")
            elif data["state"] == "Charging":
                state_label.classes(add="state-charging")
            elif data["state"] == "Standby":
                state_label.classes(add="state-standby")
            elif data["state"] == "Offline":
                state_label.classes(add="state-offline")
            if data["offline"] or (remaining is not None and rated is not None):
                ui.label(_format_capacity(remaining, rated)).classes(
                    "muted pb-1 text-sm ml-auto")
        if data["offline"]:
            with ui.column().classes("card gap-1"):
                ui.label("No recent data from device").classes("muted text-sm")
                ui.label(f"Updated {datetime.fromisoformat(data['last_received']).astimezone():%H:%M:%S}"
                         if data["last_received"] else "No readings stored").classes("muted text-xs")
            return
        ui.linear_progress(value=(soc or 0) / 100, show_value=False).props(
            "rounded size=12px color=white track-color=grey-9")
        charging = data["state"] == "Charging"
        with ui.element("div").classes("draw-wrap"):
            with ui.element("div").classes("draw-main"):
                ui.label("Current supply" if charging else "Current draw").classes(
                    "muted text-xs")
                ui.label(_format_power(power)).classes(
                    "text-2xl font-bold")
                ui.label(str(data["eta"] or "No runtime estimate")).classes("text-sm")
                stats = data["stats"]
                if stats:
                    minutes = int(data["stats_minutes"])
                    span = f"{minutes // 60} h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} min"
                    ui.label(f"Last {span}").classes("muted text-xs mt-1")
                    with ui.grid(columns=3).classes("w-full gap-x-2 gap-y-0"):
                        for title in ("Avg", "Min", "Max"):
                            ui.label(title).classes("muted text-xs")
                        for number in (sum(stats) / len(stats), min(stats), max(stats)):
                            ui.label(_format_power(number)).classes("text-sm font-bold")
            with ui.element("div").classes("draw-chart"):
                series = data["series"]
                if len(series) < 2:
                    ui.label("Collecting data…").classes("muted text-xs")
                else:
                    ui.html(_sparkline(series), sanitize=False).classes("spark-box")
                    with ui.row().classes("gap-x-3 gap-y-0 flex-wrap"):
                        for kind in dict.fromkeys(item[2] for item in series):
                            with ui.row().classes("items-center gap-1 no-wrap"):
                                ui.element("span").classes("legend-dot").style(
                                    f"background: {KIND_COLORS[kind]}")
                                ui.label(kind).classes("muted text-xs")
                ui.label(f"Updated {updated:%H:%M:%S}").classes("muted text-xs")
        if data["warnings"]:
            with ui.column().classes("card gap-1"):
                for level, text in data["warnings"]:
                    ui.label(f"⚠ {text}").classes(f"warn-{level} text-sm")

    page_content = ui.column().classes("page-content w-full gap-2 p-1")
    if STORE.get_bool_setting("compact_mode", False):
        page_content.classes(add="compact")
    with page_content:
        with ui.row().classes("header-row w-full justify-between items-center gap-2 no-wrap"):
            ui.image(LOGO_URL).classes("header-logo w-8 h-8 shrink-0")
            with ui.element("div").classes("header-metadata muted hide-compact") as header_metadata:
                with ui.element("table").classes("metadata-table").props('aria-label="Device metadata"'):
                    with ui.element("tbody"), ui.element("tr"):
                        metadata_cells = []
                        for label in ("Model", "Mode", "Power delivery", "Temperature", "IP address"):
                            with ui.element("td").props(f'title="{label}"'):
                                metadata_cells.append(ui.label(""))
            with ui.row().classes("header-controls gap-1 no-wrap shrink-0"):
                toggle_button = ui.button(
                    icon="unfold_more" if STORE.get_bool_setting("compact_mode", False) else "unfold_less",
                    on_click=lambda: toggle_compact()).props("round dense")
                with ui.button(icon="notifications", on_click=lambda: open_notifications()).props(
                        "round dense").classes("hide-compact"):
                    unread_badge = ui.badge("0", color="red").props("floating")
                    unread_badge.set_visibility(False)
                ui.button(icon="settings", on_click=lambda: settings_dialog.open()).props(
                    "round dense").classes("hide-compact")
        with ui.column().classes("view-box w-full gap-2"):
            view()
        ui.label(f"v{__version__}").classes("app-version muted text-xs self-end")

    async def fit_compact_window() -> None:
        """Size the compact window to its content so no spare space is left at the edges."""
        native_window = getattr(app.native, "main_window", None)
        if (native_window is None or not STORE.get_bool_setting("compact_mode", False)
                or STORE.get_text_setting("window_mode", "windowed") != "windowed"):
            return
        need_w, need_h, inner_w, inner_h = await ui.run_javascript(COMPACT_FIT_JS)
        size = await native_window.get_size()
        if not size:
            return
        width = min(MONITOR_WIDTH, round(need_w + size[0] - inner_w))
        height = min(MONITOR_HEIGHT, round(need_h + size[1] - inner_h))
        if abs(width - size[0]) > 2 or abs(height - size[1]) > 2:
            native_window.resize(width, height)

    async def refresh_view() -> None:
        view.refresh()
        if STORE.get_bool_setting("compact_mode", False):
            await asyncio.sleep(0.3)
            await fit_compact_window()

    async def toggle_compact() -> None:
        compact = not STORE.get_bool_setting("compact_mode", False)
        native_window = getattr(app.native, "main_window", None)
        if native_window is not None and STORE.get_text_setting("window_mode", "windowed") == "windowed":
            if compact:
                size = await native_window.get_size()
                if size:
                    STORE.set_setting("window_width", int(size[0]))
                    STORE.set_setting("window_height", int(size[1]))
                native_window.resize(*COMPACT_SIZE)
            else:
                native_window.resize(
                    STORE.get_setting("window_width", DEFAULT_WINDOW_SIZE[0]),
                    STORE.get_setting("window_height", DEFAULT_WINDOW_SIZE[1]))
        STORE.set_bool_setting("compact_mode", compact)
        if native_window is not None:
            await native_window.get_size()
            _set_window_resizable(STORE.get_bool_setting("allow_resize", True))
        if compact:
            page_content.classes(add="compact")
        else:
            page_content.classes(remove="compact")
        toggle_button.props(f"icon={'unfold_more' if compact else 'unfold_less'}")
        view.refresh()
        if compact:
            await asyncio.sleep(0.5)
            await fit_compact_window()

    level_colors = {"info": "info", "success": "positive", "warning": "warning", "critical": "negative"}
    level_text = {"info": "", "success": "state-charging", "warning": "warn-warning",
                  "critical": "warn-critical"}
    popup_state = {"last_id": STORE.latest_notification_id()}

    def refresh_unread() -> None:
        count = STORE.unread_notification_count()
        unread_badge.text = str(count) if count < 100 else "99+"
        unread_badge.set_visibility(count > 0)

    def pop_out_new() -> None:
        for row in reversed(STORE.list_notifications(limit=20, after_id=popup_state["last_id"])):
            popup_state["last_id"] = int(row["id"])
            ui.notify(f"{row['source']}: {row['message']}",
                      type=level_colors.get(str(row["level"]), "info"),
                      position="bottom", timeout=6000, close_button=True)
        refresh_unread()

    with ui.dialog() as notifications_dialog, ui.card().classes("w-96"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Notifications").classes("font-bold")
            ui.button("Clear all", icon="delete_sweep",
                      on_click=lambda: (STORE.clear_notifications(), notification_list.refresh(),
                                        refresh_unread())).props("dense no-caps")

        @ui.refreshable
        def notification_list() -> None:
            rows = STORE.list_notifications(limit=100)
            if not rows:
                ui.label("No notifications yet").classes("muted text-sm")
            for row in rows:
                created = datetime.fromisoformat(str(row["created_at"])).astimezone()
                with ui.column().classes("w-full gap-0"):
                    with ui.row().classes("items-center gap-2"):
                        ui.badge(str(row["source"]), color="grey-8")
                        ui.label(f"{created:%Y-%m-%d %H:%M:%S}").classes("muted text-xs")
                    ui.label(str(row["message"])).classes(
                        f"text-sm {level_text.get(str(row['level']), '')}")
                ui.separator()

        notification_list()

    def open_notifications() -> None:
        STORE.mark_notifications_read()
        notification_list.refresh()
        refresh_unread()
        notifications_dialog.open()

    with ui.dialog() as settings_dialog, ui.card().classes("settings-card w-96"):
        with ui.tabs().props("dense align=left mobile-arrows outside-arrows").classes(
                "w-full") as settings_tabs:
            window_tab = ui.tab("Window", icon="desktop_windows")
            alerts_tab = ui.tab("Alerts", icon="notifications_active")
            polling_tab = ui.tab("Polling", icon="schedule")
            scan_tab = ui.tab("Scan", icon="radar")
        with ui.tab_panels(settings_tabs, value=window_tab).classes("w-full"):
            with ui.tab_panel(polling_tab).classes("px-0"):
                poll = ui.number("Poll interval (s)", value=STORE.get_setting(
                    "poll_seconds", DEFAULT_POLL_SECONDS), min=5, max=600, step=5).classes("w-full")

                def save_poll_settings() -> None:
                    seconds = int(poll.value or DEFAULT_POLL_SECONDS)
                    STORE.set_setting("poll_seconds", seconds)
                    _notify("System", "info", f"Poll interval set to {seconds} s")

                ui.button("Save polling", on_click=save_poll_settings).classes("w-full")

                stats_window = ui.number("Statistics update interval (min)", value=STORE.get_setting(
                    "stats_minutes", DEFAULT_STATS_MINUTES), min=5, max=1440, step=5).classes("w-full")

                def save_stats_settings() -> None:
                    minutes = int(stats_window.value or DEFAULT_STATS_MINUTES)
                    STORE.set_setting("stats_minutes", minutes)
                    _notify("System", "info", f"Statistics update interval set to {minutes} min")
                    view.refresh()

                ui.button("Save statistics", on_click=save_stats_settings).classes("w-full")

            with ui.tab_panel(window_tab).classes("px-0"):
                window_mode = ui.select(
                    {"windowed": "Windowed", "fullscreen": "Full screen"},
                    value=STORE.get_text_setting("window_mode", "windowed"),
                    label="Window mode",
                ).classes("w-full")
                with ui.row().classes("w-full gap-2"):
                    window_width = ui.number(
                        "Width (px)", value=STORE.get_setting("window_width", DEFAULT_WINDOW_SIZE[0]),
                        min=200, max=MONITOR_WIDTH, step=20,
                    ).classes("flex-1 min-w-0")
                    window_height = ui.number(
                        "Height (px)", value=STORE.get_setting("window_height", DEFAULT_WINDOW_SIZE[1]),
                        min=200, max=MONITOR_HEIGHT, step=20,
                    ).classes("flex-1 min-w-0")
                allow_resize = ui.checkbox(
                    "Allow resize",
                    value=STORE.get_bool_setting("allow_resize", True),
                )
                always_on_top = ui.checkbox(
                    "Always on top",
                    value=STORE.get_bool_setting("always_on_top", False),
                )

                def update_topmost_availability() -> None:
                    for option in (allow_resize, always_on_top):
                        if window_mode.value == "windowed":
                            option.enable()
                        else:
                            option.disable()

                window_mode.on_value_change(lambda _: update_topmost_availability())
                update_topmost_availability()

                async def save_window_settings() -> None:
                    width = min(MONITOR_WIDTH, max(200, int(window_width.value or DEFAULT_WINDOW_SIZE[0])))
                    height = min(MONITOR_HEIGHT, max(200, int(window_height.value or DEFAULT_WINDOW_SIZE[1])))
                    mode = str(window_mode.value)
                    native_window = getattr(app.native, "main_window", None)
                    if native_window is None:
                        _notify("System", "warning", "Window controls are only available in the desktop app")
                        return
                    previous_mode = STORE.get_text_setting("window_mode", "windowed")
                    if mode != previous_mode:
                        native_window.toggle_fullscreen()
                    if mode == "windowed":
                        native_window.restore()
                        if not STORE.get_bool_setting("compact_mode", False):
                            native_window.resize(width, height)
                        STORE.set_bool_setting("allow_resize", bool(allow_resize.value))
                        STORE.set_bool_setting("always_on_top", bool(always_on_top.value))
                        native_window.set_always_on_top(bool(always_on_top.value))
                        # The window process applies queued calls asynchronously and can reset the frame style.
                        await asyncio.sleep(1)
                        _set_window_resizable(bool(allow_resize.value))
                    else:
                        native_window.set_always_on_top(False)
                    STORE.set_setting("window_width", width)
                    STORE.set_setting("window_height", height)
                    STORE.set_text_setting("window_mode", mode)
                    settings_dialog.close()
                    _notify("System", "info", f"Window settings applied ({mode}, {width}x{height})")

                ui.button("Apply window", on_click=save_window_settings).classes("w-full")

            with ui.tab_panel(alerts_tab).classes("px-0 gap-2"):
                marstek_alerts = ui.checkbox(
                    "Receive Marstek alerts", value=STORE.get_bool_setting("alerts_marstek", True))
                system_alerts = ui.checkbox(
                    "Receive System notifications", value=STORE.get_bool_setting("alerts_system", True))
                low_pct = ui.number(
                    "Alert when battery reaches (%)", value=STORE.get_setting("alert_low_pct", LOW_SOC),
                    min=1, max=100, step=1).classes("w-full")
                reserve_pct = ui.number(
                    "Reserve power (%)", value=STORE.get_setting("alert_reserve_pct", 0),
                    min=0, max=100, step=1).classes("w-full")
                ui.label("The alert fires at the higher of the two values.").classes("muted text-xs")
                full_alert = ui.checkbox(
                    "Notify when fully charged", value=STORE.get_bool_setting("alert_full", True))
                temperature_unit = ui.select(
                    {"C": "Celsius (°C)", "F": "Fahrenheit (°F)"},
                    value=STORE.get_text_setting("temperature_unit", "C"),
                    label="Temperature unit",
                ).classes("w-full")
                selected_unit = {"value": str(temperature_unit.value)}
                temperature_limit = ui.number(
                    f"Alert at or above (°{temperature_unit.value})",
                    value=round(_temperature_value(_temperature_threshold(), selected_unit["value"]), 1),
                    min=-40, max=_temperature_value(85, selected_unit["value"]), step=1,
                ).classes("w-full")

                def change_temperature_unit() -> None:
                    previous = selected_unit["value"]
                    unit = str(temperature_unit.value)
                    value = temperature_limit.value
                    celsius = (_temperature_celsius(float(value), previous)
                               if value is not None else _temperature_threshold())
                    temperature_limit.max = _temperature_value(85, unit)
                    temperature_limit.label = f"Alert at or above (°{unit})"
                    temperature_limit.set_value(round(_temperature_value(celsius, unit), 1))
                    selected_unit["value"] = unit

                temperature_unit.on_value_change(lambda _: change_temperature_unit())

                def save_alert_settings() -> None:
                    value = temperature_limit.value
                    unit = str(temperature_unit.value)
                    if value is None or not math.isfinite(float(value)):
                        _notify("System", "warning", "Enter a valid temperature limit")
                        return
                    celsius = _temperature_celsius(float(value), unit)
                    if not -40 <= celsius <= 85:
                        _notify("System", "warning", "Temperature limit must be between -40°C and 85°C")
                        return
                    STORE.set_text_setting("temperature_unit", unit)
                    STORE.set_text_setting("alert_temperature_c", str(celsius))
                    STORE.set_setting("alert_low_pct", int(low_pct.value or LOW_SOC))
                    STORE.set_setting("alert_reserve_pct", int(reserve_pct.value or 0))
                    STORE.set_bool_setting("alert_full", bool(full_alert.value))
                    # Notify before the toggles apply so disabling still confirms once.
                    _notify("System", "info", "Alert settings saved")
                    STORE.set_bool_setting("alerts_marstek", bool(marstek_alerts.value))
                    STORE.set_bool_setting("alerts_system", bool(system_alerts.value))
                    view.refresh()

                ui.button("Save alerts", on_click=save_alert_settings).classes("w-full")

            with ui.tab_panel(scan_tab).classes("px-0 gap-2"):
                interfaces = {f"{i.address}/{i.network.prefixlen}": i for i in list_local_interfaces()}
                select = ui.select({k: i.label for k, i in interfaces.items()},
                                   value=next(iter(interfaces), None), label="Network").classes("w-full")
                port = ui.number("UDP port", value=STORE.get_setting(
                    "udp_port", DEFAULT_UDP_PORT), min=1, max=65535, step=1).classes("w-full")
                status = ui.label("").classes("muted text-sm")
                results = ui.column().classes("w-full gap-1")

                async def add(result: DeviceScanResult) -> None:
                    identity = result.identity
                    if identity is None:
                        return
                    try:
                        model_profile(identity.device)
                        key = STORE.save_device(result, identity.device)
                    except (UnsupportedModelError, ValueError) as error:
                        _notify("System", "warning", str(error))
                        return
                    STORE.set_text_setting(ACTIVE_DEVICE, key)
                    _notify("System", "success", f"Now monitoring {identity.device} at {identity.ip}")
                    settings_dialog.close()
                    await _poll_active()
                    view.refresh()

                async def scan() -> None:
                    if select.value not in interfaces:
                        _notify("System", "warning", "No private network available for scanning")
                        return
                    STORE.set_setting("udp_port", int(port.value))
                    status.text = "Scanning…"
                    try:
                        found = await scan_for_marstek_devices(
                            interfaces[select.value], port=int(port.value))
                    except (OSError, TimeoutError, ValueError) as error:
                        status.text = str(error)
                        _notify("System", "warning", f"Scan failed: {error}")
                        return
                    verified = [r for r in found if r.identity is not None]
                    status.text = f"{len(verified)} device(s) found" if verified else "No device responded"
                    _notify("System", "info", f"Scan finished: {status.text}")
                    results.clear()
                    with results:
                        for result in verified:
                            ui.button(f"Use {result.identity.device} · {result.identity.ip}",
                                      on_click=lambda r=result: add(r)).props(
                                "dense no-caps").classes("w-full")

                ui.button("Scan", icon="radar", on_click=scan).props(
                    "unelevated").classes("w-full")

    ui.timer(5, refresh_view)
    ui.timer(1, pop_out_new)
    async def initialize_native_window() -> None:
        native_window = getattr(app.native, "main_window", None)
        if native_window is None:
            return
        await native_window.get_size()
        _set_window_resizable(STORE.get_bool_setting("allow_resize", True))
        _set_native_icons()
        await fit_compact_window()

    ui.timer(0.8, initialize_native_window, once=True)
    refresh_unread()
