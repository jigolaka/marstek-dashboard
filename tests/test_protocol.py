import ipaddress
import json
import unittest

from src.protocol import (
    DeviceIdentity,
    build_get_device_request,
    parse_device_identity,
    parse_discovery_hint,
)


class ProtocolTests(unittest.TestCase):
    def test_builds_documented_get_device_broadcast_request(self) -> None:
        request = json.loads(build_get_device_request("0"))

        self.assertEqual(request, {
            "id": 0,
            "method": "Marstek.GetDevice",
            "params": {"ble_mac": "0"},
        })

    def test_parses_documented_discovery_parse_error_as_hint(self) -> None:
        payload = json.dumps({
            "id": 0,
            "src": "Venus-24215ee580e7",
            "error": {"code": -32700, "message": "Parse error", "data": 402},
        }).encode()

        hint = parse_discovery_hint(payload, "192.168.1.11")

        self.assertEqual(hint.model_hint, "Venus")
        self.assertEqual(hint.mac_hint, "24215ee580e7")

    def test_parses_model_source_with_spaces_and_version_dots(self) -> None:
        payload = json.dumps({
            "id": 0,
            "src": "VenusE 3.0-f8d8113a4587",
            "error": {"code": -32700, "message": "Parse error", "data": 402},
        }).encode()

        hint = parse_discovery_hint(payload, "192.168.1.117")

        self.assertEqual(hint.model_hint, "VenusE 3.0")
        self.assertEqual(hint.mac_hint, "f8d8113a4587")

    def test_rejects_other_errors_as_discovery_hints(self) -> None:
        payload = json.dumps({
            "src": "Venus-24215ee580e7",
            "error": {"code": -32601, "data": 402},
        }).encode()

        self.assertIsNone(parse_discovery_hint(payload, "192.168.1.11"))

    def test_parses_exact_identity_only_from_valid_subnet_response(self) -> None:
        payload = json.dumps({
            "id": 7,
            "src": "VenusE-24215edb178f",
            "result": {
                "device": "VenusE",
                "ver": 111,
                "ble_mac": "24215edb178f",
                "wifi_mac": "24215edb178f",
                "wifi_name": "HOME",
                "ip": "192.168.1.11",
            },
        }).encode()

        identity = parse_device_identity(
            payload,
            "192.168.1.11",
            expected_id=7,
            allowed_network=ipaddress.ip_network("192.168.1.0/24"),
        )

        self.assertEqual(identity, DeviceIdentity(
            device="VenusE",
            version=111,
            ble_mac="24215edb178f",
            wifi_mac="24215edb178f",
            wifi_name="HOME",
            ip="192.168.1.11",
            source="VenusE-24215edb178f",
        ))

    def test_rejects_identity_outside_selected_subnet(self) -> None:
        payload = json.dumps({
            "id": 7,
            "src": "VenusE-24215edb178f",
            "result": {"device": "VenusE", "ip": "192.168.2.11"},
        }).encode()

        identity = parse_device_identity(
            payload,
            "192.168.2.11",
            expected_id=7,
            allowed_network=ipaddress.ip_network("192.168.1.0/24"),
        )

        self.assertIsNone(identity)


if __name__ == "__main__":
    unittest.main()
