"""Thread-safe HEMS status snapshots and background refresh coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
import time
from typing import Any, Callable

from hems_runtime import RuntimeUnavailable


STALE_SECONDS = 90.0


@dataclass
class _Snapshot:
    body: dict[str, Any] | None = None
    observed_at: str | None = None
    observed_monotonic: float | None = None
    last_error: str = "not_observed"


class SnapshotStore:
    """Keep the three public snapshots independent and never expose stale bodies."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._items = {
            "aircon:1": _Snapshot(),
            "aircon:2": _Snapshot(),
            "security": _Snapshot(),
        }

    @staticmethod
    def valid_body(key: str, body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        if key == "aircon:1":
            return body.get("floor") == 1
        if key == "aircon:2":
            return body.get("floor") == 2
        return isinstance(body.get("lock"), str) and isinstance(body.get("shutter"), str)

    def record_success(self, key: str, body: dict[str, Any]) -> None:
        now = self._monotonic()
        observed_at = self._utcnow().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            self._items[key] = _Snapshot(
                body=dict(body),
                observed_at=observed_at,
                observed_monotonic=now,
                last_error="",
            )

    def record_failure(self, key: str, error: str) -> None:
        with self._lock:
            self._items[key].last_error = error

    def response(self, key: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            item = self._items[key]
            body = None if item.body is None else dict(item.body)
            observed_at = item.observed_at
            observed_monotonic = item.observed_monotonic
            last_error = item.last_error
        raw_age = None if observed_monotonic is None else max(0.0, self._monotonic() - observed_monotonic)
        age = None if raw_age is None else math.floor(raw_age * 1000) / 1000
        fresh = body is not None and not last_error and raw_age is not None and raw_age < STALE_SECONDS
        metadata = {
            "observed_at": observed_at,
            "age_seconds": age,
            "stale": not fresh,
            "last_error": None if fresh else (last_error or "stale"),
        }
        if not fresh:
            return 503, {"error": "HEMS snapshot unavailable", "snapshot": metadata}
        body["snapshot"] = metadata
        return 200, body


REFRESH_TARGETS = {
    "aircon:1": ("status", {"floor": 1}),
    "aircon:2": ("status", {"floor": 2}),
    "security": ("security_status", {}),
}


class SnapshotCoordinator:
    """Refresh snapshots serially while allowing controls to take priority."""

    def __init__(
        self,
        runtime,
        store: SnapshotStore,
        *,
        cadence_seconds: float = 60.0,
        retry_seconds: float = 5.0,
        read_timeout_seconds: float = 6.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._cadence_seconds = cadence_seconds
        self._retry_seconds = retry_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: set[str] = set()
        self._thread: threading.Thread | None = None

    @property
    def pending_keys(self) -> set[str]:
        with self._lock:
            return set(self._pending)

    def request_targeted(self, keys: set[str]) -> None:
        unknown = keys - REFRESH_TARGETS.keys()
        if unknown:
            raise ValueError(f"unknown snapshot target: {sorted(unknown)}")
        with self._lock:
            self._pending.update(keys)
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="hems-snapshot-refresh", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 8.0) -> bool:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def refresh_one_targeted(self) -> bool:
        with self._lock:
            key = next((item for item in REFRESH_TARGETS if item in self._pending), None)
        if key is None:
            return True
        success = self._refresh(key)
        if success:
            with self._lock:
                self._pending.discard(key)
        return success

    def _run(self) -> None:
        self._runtime.prewarm()
        periodic: list[str] = []
        next_cycle = self._monotonic()
        retry_at = 0.0
        while not self._stop.is_set():
            now = self._monotonic()
            pending = self.pending_keys
            if pending:
                if now >= retry_at:
                    if self.refresh_one_targeted():
                        retry_at = 0.0
                    else:
                        retry_at = self._monotonic() + self._retry_seconds
                    continue
                timeout = max(0.0, retry_at - now)
            elif periodic:
                self._refresh(periodic.pop(0))
                continue
            elif now >= next_cycle:
                periodic = list(REFRESH_TARGETS)
                next_cycle = now + self._cadence_seconds
                continue
            else:
                timeout = max(0.0, next_cycle - now)
            self._wake.wait(timeout)
            self._wake.clear()

    def _refresh(self, key: str) -> bool:
        operation, payload = REFRESH_TARGETS[key]
        try:
            status, body = self._runtime.execute_refresh(
                operation,
                dict(payload),
                timeout_seconds=self._read_timeout_seconds,
            )
        except RuntimeUnavailable as error:
            if str(error) not in {"worker busy", "control waiting"}:
                self._store.record_failure(key, "runtime_unavailable")
            return False
        except Exception:
            self._store.record_failure(key, "refresh_failed")
            return False
        if status != 200 or not self._store.valid_body(key, body):
            self._store.record_failure(key, "refresh_failed")
            return False
        self._store.record_success(key, body)
        return True
