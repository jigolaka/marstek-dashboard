"""Documented Marstek Device Open API Rev 3.1 message handling."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any

DEVICE_SOURCE_PATTERN = re.compile(
    r"^(?P<model>[A-Za-z0-9 .-]+)-(?P<mac>[0-9a-fA-F]{12})$")
DISCOVERY_PARSE_ERROR = -32700
DISCOVERY_PARSE_DATA = 402


@dataclass(frozen=True)
class DiscoveryHint:
    """A device hint from the API's documented LAN broadcast response."""

    source: str
    sender_ip: str
    model_hint: str
    mac_hint: str


@dataclass(frozen=True)
class DeviceIdentity:
    """Identity returned by a successful Marstek.GetDevice response."""

    device: str
    version: int | None
    ble_mac: str | None
    wifi_mac: str | None
    wifi_name: str | None
    ip: str
    source: str


def build_get_device_request(ble_mac: str, request_id: int = 0) -> bytes:
    """Build the documented JSON request for Marstek.GetDevice."""
    if not ble_mac:
        raise ValueError("ble_mac must not be empty")
    if request_id < 0:
        raise ValueError("request_id must be non-negative")
    request = {
        "id": request_id,
        "method": "Marstek.GetDevice",
        "params": {"ble_mac": ble_mac},
    }
    return json.dumps(request, separators=(",", ":")).encode("utf-8")


def parse_discovery_hint(payload: bytes, sender_ip: str) -> DiscoveryHint | None:
    """Parse the documented broadcast signature without claiming exact identity."""
    try:
        response = json.loads(payload)
        ipaddress.ip_address(sender_ip)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(response, dict) or not isinstance(response.get("src"), str):
        return None
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    if error.get("code") != DISCOVERY_PARSE_ERROR or error.get("data") != DISCOVERY_PARSE_DATA:
        return None

    match = DEVICE_SOURCE_PATTERN.fullmatch(response["src"])
    if match is None:
        return None
    return DiscoveryHint(
        source=response["src"],
        sender_ip=sender_ip,
        model_hint=match.group("model").strip(),
        mac_hint=match.group("mac").lower(),
    )


def parse_device_identity(
    payload: bytes,
    sender_ip: str,
    *,
    expected_id: int,
    allowed_network: ipaddress.IPv4Network,
) -> DeviceIdentity | None:
    """Validate an exact GetDevice result against its sender and selected subnet."""
    try:
        response = json.loads(payload)
        sender = ipaddress.ip_address(sender_ip)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(sender, ipaddress.IPv4Address) or sender not in allowed_network:
        return None
    if not isinstance(response, dict) or response.get("id") != expected_id:
        return None

    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("device"), str):
        return None
    source = response.get("src")
    reported_ip = result.get("ip")
    if not isinstance(source, str) or not isinstance(reported_ip, str):
        return None
    try:
        normalized_reported_ip = ipaddress.ip_address(reported_ip)
    except ValueError:
        return None
    if normalized_reported_ip != sender:
        return None

    version = result.get("ver")
    return DeviceIdentity(
        device=result["device"],
        version=version if isinstance(version, int) else None,
        ble_mac=_optional_string(result.get("ble_mac")),
        wifi_mac=_optional_string(result.get("wifi_mac")),
        wifi_name=_optional_string(result.get("wifi_name")),
        ip=str(sender),
        source=source,
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
