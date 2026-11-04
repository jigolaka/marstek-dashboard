"""Local SQLite persistence for confirmed devices and telemetry history."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.discovery import DeviceScanResult
from src.metrics import Measurement

DEFAULT_POLL_SECONDS = 30
DEFAULT_RETENTION_DAYS = 90


class DashboardStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    firmware INTEGER,
                    wifi_name TEXT,
                    designation TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY,
                    device_key TEXT NOT NULL REFERENCES devices(device_key) ON DELETE CASCADE,
                    metric_key TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    method TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS measurements_device_metric_time
                    ON measurements(device_key, metric_key, recorded_at);
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0
                );
            """)

    @staticmethod
    def identity_key(result: DeviceScanResult) -> str:
        if result.identity is None:
            raise ValueError(
                "A device must be verified before it can be saved")
        identity = result.identity
        mac = identity.wifi_mac or identity.ble_mac
        if not mac:
            raise ValueError(
                "Verified device response did not include a MAC address")
        return mac.replace(":", "").replace("-", "").lower()

    def save_device(self, result: DeviceScanResult, designation: str = "") -> str:
        if result.identity is None:
            raise ValueError(
                "A device must be verified before it can be saved")
        identity = result.identity
        device_key = self.identity_key(result)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO devices (
                    device_key, model, source, ip_address, firmware, wifi_name, designation, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_key) DO UPDATE SET
                    model = excluded.model,
                    source = excluded.source,
                    ip_address = excluded.ip_address,
                    firmware = excluded.firmware,
                    wifi_name = excluded.wifi_name,
                    designation = CASE WHEN excluded.designation = ''
                        THEN devices.designation ELSE excluded.designation END,
                    last_seen = excluded.last_seen
            """, (
                device_key,
                identity.device,
                identity.source,
                identity.ip,
                identity.version,
                identity.wifi_name,
                designation.strip(),
                now,
            ))
        return device_key

    def set_designation(self, device_key: str, designation: str) -> None:
        value = designation.strip()
        if not value:
            raise ValueError("Enter a device designation")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE devices SET designation = ? WHERE device_key = ?",
                (value, device_key),
            )
            if cursor.rowcount == 0:
                raise KeyError(device_key)

    def list_devices(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM devices ORDER BY designation, model, device_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_measurements(
        self,
        device_key: str,
        measurements: list[Measurement],
        recorded_at: datetime | None = None,
    ) -> None:
        timestamp = (recorded_at or datetime.now(timezone.utc)
                     ).astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.executemany("""
                INSERT INTO measurements (device_key, metric_key, value, unit, method, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (device_key, item.key, item.value,
                 item.unit, item.method, timestamp)
                for item in measurements
            ])
            connection.execute(
                "UPDATE devices SET last_seen = ? WHERE device_key = ?",
                (timestamp, device_key),
            )

    def measurements_since(
        self,
        device_key: str,
        since: datetime,
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT metric_key, value, unit, method, recorded_at
                FROM measurements
                WHERE device_key = ? AND recorded_at >= ?
                ORDER BY recorded_at, metric_key
            """, (device_key, since.astimezone(timezone.utc).isoformat())).fetchall()
        return [dict(row) for row in rows]

    def latest_measurements(self, device_key: str) -> dict[str, dict[str, object]]:
        latest: dict[str, dict[str, object]] = {}
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT metric_key, value, unit, recorded_at
                FROM measurements
                WHERE device_key = ?
                ORDER BY recorded_at DESC
            """, (device_key,)).fetchall()
        for row in rows:
            latest.setdefault(row["metric_key"], dict(row))
        return latest

    def get_setting(self, name: str, default: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE name = ?", (name,)
            ).fetchone()
        try:
            return int(row["value"]) if row is not None else default
        except (TypeError, ValueError):
            return default

    def get_bool_setting(self, name: str, default: bool = False) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return bool(default)
        value = str(row["value"]).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", ""}:
            return False
        try:
            return bool(int(value))
        except (TypeError, ValueError):
            return bool(default)

    def set_setting(self, name: str, value: int) -> None:
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO settings (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
            """, (name, str(value)))

    def get_text_setting(self, name: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE name = ?", (name,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_text_setting(self, name: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO settings (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
            """, (name, value))

    def set_bool_setting(self, name: str, value: bool) -> None:
        self.set_setting(name, int(bool(value)))

    def add_notification(self, source: str, level: str, message: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO notifications (source, level, message, created_at) VALUES (?, ?, ?, ?)",
                (source, level, message, _utc_now()),
            )
            return int(cursor.lastrowid)

    def list_notifications(self, limit: int = 100, after_id: int = 0) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notifications WHERE id > ? ORDER BY id DESC LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def unread_notification_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM notifications WHERE is_read = 0").fetchone()[0])

    def latest_notification_id(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0])

    def mark_notifications_read(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")

    def clear_notifications(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM notifications")

    def reset_factory_state(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM measurements")
            connection.execute("DELETE FROM devices")
            connection.execute("DELETE FROM settings")

    def purge_expired(self, retention_days: int, now: datetime | None = None) -> int:
        if retention_days < 1:
            raise ValueError("Retention must be at least one day")
        cutoff = ((now or datetime.now(timezone.utc)) -
                  timedelta(days=retention_days))
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM measurements WHERE recorded_at < ?",
                (cutoff.astimezone(timezone.utc).isoformat(),),
            )
            return cursor.rowcount


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
