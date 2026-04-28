from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dosm.recording.journal import JournalWriter, RecordingOptions


@dataclass
class ActiveRecording:
    recording_id: int
    user_id: int
    slug: str
    options: RecordingOptions
    tmp_path: Path
    started_at: datetime
    writer: JournalWriter


_registry: dict[int, ActiveRecording] = {}
_lock = threading.Lock()


def get_active(user_id: int) -> ActiveRecording | None:
    with _lock:
        return _registry.get(user_id)


def set_active(user_id: int, rec: ActiveRecording) -> None:
    with _lock:
        _registry[user_id] = rec


def clear_active(user_id: int) -> ActiveRecording | None:
    with _lock:
        return _registry.pop(user_id, None)


def all_active() -> list[ActiveRecording]:
    with _lock:
        return list(_registry.values())
