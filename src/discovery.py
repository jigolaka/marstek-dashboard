"""Bounded local-network discovery using the documented Marstek UDP API."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

import psutil

from src.protocol import (
    DeviceIdentity,
    DiscoveryHint,
    build_get_device_request,
    parse_device_identity,
    parse_discovery_hint,
)

DEFAULT_UDP_PORT = 30000
MAX_NETWORK_ADDRESSES = 256


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network

    @property
    def label(self) -> str:
        return f"{self.name}  {self.address} / {self.network.prefixlen}"


@dataclass(frozen=True)
class DeviceScanResult:
    hint: DiscoveryHint
    identity: DeviceIdentity | None


def list_local_interfaces() -> list[NetworkInterface]:
    interfaces: list[NetworkInterface] = []
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family != socket.AF_INET or not address.address or not address.netmask:
                continue
            try:
                local_address = ipaddress.IPv4Address(address.address)
                network = ipaddress.IPv4Network(
                    f"{address.address}/{address.netmask}", strict=False)
            except ipaddress.AddressValueError:
                continue
            except ipaddress.NetmaskValueError:
                continue
            if not local_address.is_private or local_address.is_loopback or local_address.is_link_local:
                continue
            if network.num_addresses > MAX_NETWORK_ADDRESSES:
                continue
            interfaces.append(NetworkInterface(name, local_address, network))
    return sorted(interfaces, key=lambda item: (item.name, int(item.address)))


def validate_scan_target(interface: NetworkInterface, port: int) -> None:
    if not interface.address.is_private or interface.address not in interface.network:
        raise ValueError("Select an interface on a private local network")
    if interface.network.num_addresses > MAX_NETWORK_ADDRESSES:
        raise ValueError(
            "The selected subnet is too large; select a subnet with at most 256 addresses")
    if not 1 <= port <= 65535:
        raise ValueError("UDP port must be between 1 and 65535")


async def scan_for_marstek_devices(
    interface: NetworkInterface,
    *,
    port: int = DEFAULT_UDP_PORT,
    discovery_timeout: float = 1.5,
    response_timeout: float = 1.0,
) -> list[DeviceScanResult]:
    """Broadcast GetDevice on one confirmed subnet and verify each response unicast."""
    validate_scan_target(interface, port)
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((str(interface.address), 0))
    sock.setblocking(False)

    hints: dict[tuple[str, str], DiscoveryHint] = {}
    verified: dict[str, DeviceScanResult] = {}
    try:
        request = build_get_device_request("0", request_id=0)
        await loop.sock_sendto(sock, request, (str(interface.network.broadcast_address), port))
        deadline = loop.time() + discovery_timeout
        while loop.time() < deadline:
            try:
                payload, sender = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096),
                    timeout=max(0.01, deadline - loop.time()),
                )
            except TimeoutError:
                break
            sender_ip = sender[0]
            if ipaddress.ip_address(sender_ip) not in interface.network:
                continue
            hint = parse_discovery_hint(payload, sender_ip)
            if hint is not None:
                hints[(sender_ip, hint.mac_hint)] = hint
                continue
            identity = parse_device_identity(
                payload,
                sender_ip,
                expected_id=0,
                allowed_network=interface.network,
            )
            if identity is not None:
                key = (
                    identity.wifi_mac or identity.ble_mac or identity.source).lower()
                verified[key] = DeviceScanResult(
                    DiscoveryHint(identity.source, sender_ip,
                                  identity.device, key),
                    identity,
                )

        for request_id, hint in enumerate(hints.values(), start=1):
            await loop.sock_sendto(
                sock,
                build_get_device_request(hint.mac_hint, request_id=request_id),
                (hint.sender_ip, port),
            )
            deadline = loop.time() + response_timeout
            while loop.time() < deadline:
                try:
                    payload, sender = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, 4096),
                        timeout=max(0.01, deadline - loop.time()),
                    )
                except TimeoutError:
                    break
                identity = parse_device_identity(
                    payload,
                    sender[0],
                    expected_id=request_id,
                    allowed_network=interface.network,
                )
                if identity is not None:
                    key = (
                        identity.wifi_mac or identity.ble_mac or identity.source).lower()
                    verified[key] = DeviceScanResult(hint, identity)
                    break
    finally:
        sock.close()

    results = list(verified.values())
    results.extend(DeviceScanResult(hint, None) for key, hint in hints.items()
                   if not any(result.hint.sender_ip == key[0] and result.hint.mac_hint == key[1]
                              for result in results))
    return results
