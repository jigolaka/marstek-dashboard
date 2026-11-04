# Marstek Device Open API Matrix

Source: user-provided `MarstekDeviceOpenApi.pdf`, Rev 3.1. This matrix records only fields and capabilities stated in that reference.

| Device family     | Documented components                                            | Dashboard telemetry                                            |
| ----------------- | ---------------------------------------------------------------- | -------------------------------------------------------------- |
| Venus C / Venus E | Marstek, WiFi, Bluetooth, Battery, ES, EM; SYS from firmware 150 | Battery status, ES status/mode, energy meter status            |
| Venus D / Venus A | Venus C/E components plus PV; SYS from firmware 150              | Battery status, ES status/mode, energy meter status, PV inputs |
| Venus E mini      | Marstek, WiFi, Bluetooth, Battery, ES, EM; subset of SYS         | Battery status, ES status/mode, energy meter status            |

## Discovery and transport

- LAN API uses JSON over UDP. The documented default port is `30000`; users may configure the port in the Marstek app. The PDF recommends ports `49152` through `65535` when choosing a port.
- The documented discovery request is `Marstek.GetDevice` with `params.ble_mac` set to `"0"`, broadcast on the local network.
- The PDF's broadcast example identifies a device by a `src` value like `Venus-24215ee580e7` alongside error code `-32700` and data `402`. This is treated only as a candidate hint.
- A successful `Marstek.GetDevice` result supplies `device`, `ver`, `ble_mac`, `wifi_mac`, `wifi_name`, and `ip`; exact model classification uses the returned `device` value.
- The API must be enabled for the device in the Marstek app. API calls are kept on the selected local interface and the initial dashboard is read-only.

## Telemetry fields and normalization

- `Bat.GetStatus`: `soc` (%), `bat_temp` (°C), `bat_capacity` (Wh), `rated_capacity` (Wh).
- `ES.GetStatus`: battery SOC/capacity, PV/grid/off-grid/battery power (W), total PV energy (0.01 kWh units), and cumulative grid/load energy (Wh).
- `ES.GetMode`: mode power and phase values in W; cumulative input/output values normalized from 0.1 Wh.
- `EM.GetStatus`: phase/total power in W; cumulative input/output values normalized from 0.1 Wh.
- `PV.GetStatus` is restricted to Venus A/D by the device support table. Documented `pv1`-`pv4` power, voltage, current, and state fields are read when present; state fields are not plotted as numeric measurements.

No control/configuration methods are enabled by the dashboard. Any API response omissions, nulls, unsupported methods, or unknown model strings remain unavailable/unsupported rather than being inferred.