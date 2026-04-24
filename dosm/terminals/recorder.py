from __future__ import annotations

import json
import os
import time
from io import TextIOWrapper
from pathlib import Path


class AsciinemaRecorder:
    """Write an asciinema cast v2 file as terminal output streams in.

    Format: https://docs.asciinema.org/manual/asciicast/v2/
    """

    def __init__(
        self,
        path: Path,
        *,
        cols: int,
        rows: int,
        command: str,
        title: str | None = None,
        env: dict[str, str] | None = None,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._fh: TextIOWrapper | None = open(path, "w", encoding="utf-8", newline="\n")
        header = {
            "version": 2,
            "width": cols,
            "height": rows,
            "timestamp": int(time.time()),
            "command": command,
            "env": {
                k: v
                for k, v in (env or {}).items()
                if k in {"SHELL", "TERM"}
            },
        }
        if title:
            header["title"] = title
        self._fh.write(json.dumps(header) + "\n")
        self._fh.flush()
        self._t0 = time.monotonic()

    @property
    def path(self) -> Path:
        return self._path

    def record_output(self, data: bytes) -> None:
        self._append("o", data)

    def record_input(self, data: bytes) -> None:
        # asciinema supports "i" events but most players ignore them; they are
        # still useful for auditing exactly what the user typed.
        self._append("i", data)

    def resize(self, rows: int, cols: int) -> None:
        if self._fh is None:
            return
        elapsed = round(time.monotonic() - self._t0, 6)
        self._fh.write(json.dumps([elapsed, "r", f"{cols}x{rows}"]) + "\n")
        self._fh.flush()

    def _append(self, kind: str, data: bytes) -> None:
        if self._fh is None:
            return
        text = data.decode("utf-8", errors="replace")
        elapsed = round(time.monotonic() - self._t0, 6)
        self._fh.write(json.dumps([elapsed, kind, text]) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.close()
        finally:
            self._fh = None


def recording_path(root: Path, session_id: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe = session_id.replace(os.sep, "-").replace("/", "-")
    return root / f"{ts}-{safe}.cast"
