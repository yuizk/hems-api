from __future__ import annotations

from datetime import datetime, timezone
from hems_runtime import RuntimeUnavailable
from hems_snapshot import SnapshotCoordinator, SnapshotStore


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def test_snapshot_freshness_partial_failure_and_floor_separation():
    clock = FakeClock()
    store = SnapshotStore(
        monotonic=clock,
        utcnow=lambda: datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
    )
    store.record_success("aircon:1", {"floor": 1, "power": "ON"})
    store.record_success("aircon:2", {"floor": 2, "power": "OFF"})

    clock.value = 189.9
    status, body = store.response("aircon:1")
    assert status == 200
    assert body["floor"] == 1
    assert body["snapshot"] == {
        "observed_at": "2026-08-27T00:00:00Z",
        "age_seconds": 89.9,
        "stale": False,
        "last_error": None,
    }

    store.record_failure("aircon:2", "refresh_failed")
    failed_status, failed_body = store.response("aircon:2")
    assert failed_status == 503
    assert failed_body["error"] == "HEMS snapshot unavailable"
    assert failed_body["snapshot"]["last_error"] == "refresh_failed"
    assert "floor" not in failed_body

    clock.value = 190.0
    stale_status, stale_body = store.response("aircon:1")
    assert stale_status == 503
    assert stale_body["snapshot"]["age_seconds"] == 90.0
    assert stale_body["snapshot"]["stale"] is True


def test_age_rounding_does_not_make_just_under_90_seconds_stale():
    clock = FakeClock()
    store = SnapshotStore(monotonic=clock)
    store.record_success("aircon:1", {"floor": 1, "power": "ON"})
    clock.value = 189.9996
    status, body = store.response("aircon:1")
    assert status == 200
    assert body["snapshot"]["age_seconds"] == 89.999


def test_unobserved_snapshot_is_503_without_age():
    store = SnapshotStore()
    status, body = store.response("security")
    assert status == 503
    assert body["snapshot"] == {
        "observed_at": None,
        "age_seconds": None,
        "stale": True,
        "last_error": "not_observed",
    }


def test_targeted_refresh_remains_pending_until_success():
    class Runtime:
        def __init__(self):
            self.results = [RuntimeUnavailable("cooldown"), (200, {"floor": 2, "power": "ON"})]

        def prewarm(self):
            return True

        def execute_refresh(self, operation, payload, *, timeout_seconds):
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    coordinator = SnapshotCoordinator(Runtime(), SnapshotStore(), retry_seconds=0.01)
    coordinator.request_targeted({"aircon:2"})
    assert coordinator.refresh_one_targeted() is False
    assert coordinator.pending_keys == {"aircon:2"}
    assert coordinator.refresh_one_targeted() is True
    assert coordinator.pending_keys == set()


def test_control_admission_deferral_keeps_last_success_fresh():
    class Runtime:
        def execute_refresh(self, operation, payload, *, timeout_seconds):
            raise RuntimeUnavailable("worker busy")

    store = SnapshotStore()
    store.record_success("security", {"lock": "LOCKED", "shutter": "CLOSED"})
    coordinator = SnapshotCoordinator(Runtime(), store)
    coordinator.request_targeted({"security"})

    assert coordinator.refresh_one_targeted() is False
    assert coordinator.pending_keys == {"security"}
    status, body = store.response("security")
    assert status == 200
    assert body["snapshot"]["last_error"] is None
