"""Read-only UDP JSON-RPC client for the Marstek Device Open API."""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
import time
from typing import Any

from src.metrics import Measurement, ModelProfile, extract_measurements


class MarstekApiError(RuntimeError):
    pass


class UdpMarstekClient:
    def __init__(self, port: int, timeout: float = 1.5) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("UDP port must be between 1 and 65535")
        self.port = port
        self.timeout = timeout
        self._request_ids = itertools.count(1)
        self.mode: str | None = None

    async def query(self, address: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = next(self._request_ids)
        request = {"id": request_id, "method": method, "params": params}
        return await asyncio.to_thread(self._query_sync, address, request_id, request)

    def _query_sync(self, address: str, request_id: int, request: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
            client_socket.settimeout(self.timeout)
            client_socket.sendto(payload, (address, self.port))
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"No response from Marstek device at {address}")
                client_socket.settimeout(remaining)
                try:
                    response_payload, sender = client_socket.recvfrom(8192)
                except TimeoutError as error:
                    raise TimeoutError(
                        f"No response from Marstek device at {address}") from error
                if sender[0] != address:
                    continue
                try:
                    response = json.loads(response_payload)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                if isinstance(response.get("error"), dict):
                    error = response["error"]
                    raise MarstekApiError(
                        str(error.get("message", "Device returned an API error")))
                result = response.get("result")
                if not isinstance(result, dict):
                    raise MarstekApiError(
                        "Device response is missing a result object")
                return result

    async def read_snapshot(self, address: str, profile: ModelProfile) -> list[Measurement]:
        measurements: list[Measurement] = []
        last_error: Exception | None = None
        for method in profile.telemetry_methods:
            # Some firmwares ignore individual methods; keep whatever answers.
            try:
                result = await self.query(address, method, {"id": 0})
            except (TimeoutError, MarstekApiError) as error:
                last_error = error
                continue
            measurements.extend(extract_measurements(method, result))
            if method == "ES.GetMode" and isinstance(result.get("mode"), str):
                self.mode = result["mode"]
        if not measurements and last_error is not None:
            raise last_error
        return measurements
