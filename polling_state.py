from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def message_fingerprint(message: dict[str, Any], session_key: str) -> str:
    message_id = message.get("message_id") or message.get("id")
    if message_id:
        return f"id:{message_id}"

    digest_source = {
        "session_key": session_key,
        "sender": message.get("sender") or message.get("sender_name") or message.get("talker"),
        "timestamp": message.get("timestamp") or message.get("create_time"),
        "content": message.get("content") or message.get("text") or message.get("message"),
        "type": message.get("type") or message.get("msg_type"),
    }
    normalized = json.dumps(digest_source, ensure_ascii=False, sort_keys=True)
    return f"hash:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


@dataclass(slots=True)
class SessionState:
    fingerprints: list[str] = field(default_factory=list)


class PollingStateStore:
    def __init__(self, path: str, max_fingerprints_per_session: int = 200) -> None:
        self.path = Path(path)
        self.max_fingerprints_per_session = max_fingerprints_per_session
        self._sessions: dict[str, SessionState] = {}

    def load(self) -> None:
        if not self.path.exists():
            self._sessions = {}
            return

        data = json.loads(self.path.read_text(encoding="utf-8"))
        sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        loaded: dict[str, SessionState] = {}
        for key, value in sessions.items():
            if not isinstance(value, dict):
                continue
            fingerprints = value.get("fingerprints", [])
            if not isinstance(fingerprints, list):
                continue
            loaded[key] = SessionState(fingerprints=[str(item) for item in fingerprints[-self.max_fingerprints_per_session :]])
        self._sessions = loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": {
                key: {"fingerprints": value.fingerprints[-self.max_fingerprints_per_session :]}
                for key, value in self._sessions.items()
            }
        }
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            temp_path = Path(handle.name)
        temp_path.replace(self.path)

    def has_seen(self, session_key: str, fingerprint: str) -> bool:
        state = self._sessions.get(session_key)
        if state is None:
            return False
        return fingerprint in state.fingerprints

    def mark_seen(self, session_key: str, fingerprint: str) -> None:
        state = self._sessions.setdefault(session_key, SessionState())
        if fingerprint in state.fingerprints:
            return
        state.fingerprints.append(fingerprint)
        if len(state.fingerprints) > self.max_fingerprints_per_session:
            state.fingerprints = state.fingerprints[-self.max_fingerprints_per_session :]
