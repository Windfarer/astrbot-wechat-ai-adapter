from __future__ import annotations

from polling_state import PollingStateStore, message_fingerprint


def test_message_fingerprint_prefers_message_id() -> None:
    message = {"message_id": "abc123", "content": "hello"}

    fingerprint = message_fingerprint(message, "session-1")

    assert fingerprint == "id:abc123"


def test_message_fingerprint_falls_back_to_hash() -> None:
    message = {"sender_name": "alice", "timestamp": 1234567890, "content": "hello"}

    fingerprint = message_fingerprint(message, "session-1")

    assert fingerprint.startswith("hash:")


def test_polling_state_store_round_trip(tmp_path) -> None:
    store = PollingStateStore(str(tmp_path / "state.json"), max_fingerprints_per_session=2)
    store.mark_seen("session-1", "one")
    store.mark_seen("session-1", "two")
    store.mark_seen("session-1", "three")
    store.save()

    reloaded = PollingStateStore(str(tmp_path / "state.json"), max_fingerprints_per_session=2)
    reloaded.load()

    assert reloaded.has_seen("session-1", "two")
    assert reloaded.has_seen("session-1", "three")
    assert not reloaded.has_seen("session-1", "one")
