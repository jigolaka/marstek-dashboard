import ipaddress
import unittest

from src.discovery import NetworkInterface, validate_scan_target


class DiscoveryValidationTests(unittest.TestCase):
    def test_accepts_private_local_subnet(self) -> None:
        interface = NetworkInterface(
            "Ethernet",
            ipaddress.ip_address("192.168.1.20"),
            ipaddress.ip_network("192.168.1.0/24"),
        )

        validate_scan_target(interface, 30000)

    def test_rejects_subnets_larger_than_256_addresses(self) -> None:
        interface = NetworkInterface(
            "Ethernet",
            ipaddress.ip_address("192.168.0.20"),
            ipaddress.ip_network("192.168.0.0/23"),
        )

        with self.assertRaises(ValueError):
            validate_scan_target(interface, 30000)

    def test_rejects_non_private_networks(self) -> None:
        interface = NetworkInterface(
            "WAN",
            ipaddress.ip_address("8.8.8.10"),
            ipaddress.ip_network("8.8.8.0/24"),
        )

        with self.assertRaises(ValueError):
            validate_scan_target(interface, 30000)

    def test_rejects_invalid_udp_ports(self) -> None:
        interface = NetworkInterface(
            "Ethernet",
            ipaddress.ip_address("192.168.1.20"),
            ipaddress.ip_network("192.168.1.0/24"),
        )

        with self.assertRaises(ValueError):
            validate_scan_target(interface, 65536)


if __name__ == "__main__":
    unittest.main()
