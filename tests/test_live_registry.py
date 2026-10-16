import pytest

from app.live.config import MAX_DELEGATIONS, MAX_SESSIONS
from app.live.errors import (
    LiveCallConflict,
    LiveCapacityExceeded,
    LiveProviderError,
    LiveSessionForbidden,
    LiveSessionUnavailable,
)
from app.live.registry import LiveRegistry


def test_ownership_expiry_duration_and_close():
    clock = [10.0]
    registry = LiveRegistry(lambda: clock[0])
    registry.add("opaque", "manager", 30)
    entry = registry.owned("opaque", "manager")
    with pytest.raises(LiveSessionForbidden):
        registry.close("opaque", "other")
    with pytest.raises(LiveSessionUnavailable):
        registry.owned("unknown", "manager")
    clock[0] = 39.0
    assert registry.duration(entry) == 29
    assert registry.owned("opaque", "manager") is entry
    clock[0] = 40.0
    with pytest.raises(LiveSessionUnavailable):
        registry.owned("opaque", "manager")
    assert registry._entries == {}
    registry.add("new", "manager", 30)
    registry.close("new", "manager")
    with pytest.raises(LiveSessionUnavailable):
        registry.owned("new", "manager")


def test_duplicate_provider_id_cannot_change_owner():
    registry = LiveRegistry()
    registry.add("id", "a", 600)
    with pytest.raises(LiveProviderError):
        registry.add("id", "b", 600)
    assert registry.owned("id", "a").subject == "a"


def test_per_subject_capacity_includes_pending_requests_and_recovers():
    registry = LiveRegistry()
    with registry.reserve("a"):
        with registry.reserve("a"):
            with pytest.raises(LiveCapacityExceeded), registry.reserve("a"):
                pytest.fail("capacity exceeded")
        registry.add("id", "a", 600)
    with registry.reserve("a"):
        registry.add("id2", "a", 600)
    with pytest.raises(LiveCapacityExceeded), registry.reserve("a"):
        pytest.fail("capacity exceeded")
    registry.close("id", "a")
    with registry.reserve("a"):
        pass
    assert registry._pending == {}


def test_global_capacity_including_pending():
    registry = LiveRegistry()
    for i in range(MAX_SESSIONS - 1):
        registry.add(str(i), str(i), 600)
    with registry.reserve("pending"):
        with pytest.raises(LiveCapacityExceeded), registry.reserve("another"):
            pytest.fail("capacity exceeded")
    registry.clear()
    assert registry._entries == {} and registry._pending == {}


def test_calls_are_serial_bounded_and_never_replayed_even_after_failure():
    registry = LiveRegistry()
    registry.add("id", "owner", 600)
    with pytest.raises(RuntimeError), registry.delegation("id", "owner", "failed") as entry:
        with pytest.raises(LiveCallConflict), registry.delegation("id", "owner", "other"):
            pytest.fail("concurrent execution")
        raise RuntimeError("simulated failure")
    assert not entry.busy
    with pytest.raises(LiveCallConflict), registry.delegation("id", "owner", "failed"):
        pytest.fail("replayed")
    for i in range(MAX_DELEGATIONS - 1):
        with registry.delegation("id", "owner", str(i)):
            pass
    with pytest.raises(LiveCapacityExceeded), registry.delegation("id", "owner", "excess"):
        pytest.fail("too many calls")
    assert set(entry.__slots__) == {"subject", "created_at", "expires_at", "calls", "busy"}
