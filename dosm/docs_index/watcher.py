"""Filesystem watcher for $DOSM_HOME/docs/.

Triggers reindex_async whenever docs are created, modified, or deleted
outside of DOSM's own vault UI - e.g. when files are dropped in via
Explorer, rsync, or an external editor.

Uses watchdog's OS-native backends (ReadDirectoryChangesW on Windows,
inotify on Linux, FSEvents on macOS). A 2-second debounce prevents
editor autosave storms from triggering redundant reindexes.

Falls back silently if watchdog is not installed.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dosm.config import Config

_observer = None
_observer_lock = threading.Lock()

# SMB sources can't use OS filesystem events (inotify/ReadDirectoryChangesW
# don't fire for a network share), so they are watched by a periodic polling
# reindex instead. The size+mtime fast-path keeps each poll cheap.
_poll_thread: threading.Thread | None = None
_poll_stop = threading.Event()

_WATCHED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}


def _start_polling(cfg: Config) -> None:
    """Background thread that reindexes an SMB source on a fixed cadence."""
    global _poll_thread
    _poll_stop.clear()
    interval = max(5.0, float(cfg.docs_index.smb.poll_interval_seconds))

    def _loop() -> None:
        from dosm.docs_index.indexer import reindex_async

        while not _poll_stop.wait(interval):
            reindex_async(cfg, force=False)

    t = threading.Thread(target=_loop, daemon=True, name="docs-smb-poller")
    t.start()
    _poll_thread = t


def start_watcher(cfg: Config) -> None:
    """Start the background docs watcher. No-op if already running or watchdog missing.

    Local sources use watchdog's OS-native events; SMB sources use polling.
    """
    global _observer
    with _observer_lock:
        if _observer is not None or (_poll_thread is not None and _poll_thread.is_alive()):
            return
        if cfg.docs_index.source == "smb":
            _start_polling(cfg)
            return
        if not cfg.docs_dir.exists():
            return
        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer

            class _Handler(FileSystemEventHandler):
                def __init__(self) -> None:
                    self._lock = threading.Lock()
                    self._timer: threading.Timer | None = None

                def _schedule(self) -> None:
                    with self._lock:
                        if self._timer is not None:
                            self._timer.cancel()
                        self._timer = threading.Timer(2.0, self._fire)
                        self._timer.daemon = True
                        self._timer.start()

                def _fire(self) -> None:
                    with self._lock:
                        self._timer = None
                    from dosm.docs_index.indexer import reindex_async
                    reindex_async(cfg, force=False)

                def on_any_event(self, event: FileSystemEvent) -> None:
                    if event.is_directory:
                        return
                    path = str(getattr(event, "src_path", ""))
                    ext = path[path.rfind("."):].lower() if "." in path else ""
                    if ext in _WATCHED_EXTENSIONS:
                        self._schedule()

            obs = Observer()
            obs.schedule(_Handler(), str(cfg.docs_dir), recursive=True)
            obs.daemon = True
            obs.start()
            _observer = obs
        except ImportError:
            pass  # watchdog not installed - watcher disabled, vault UI reindex still works
        except Exception:
            pass


def stop_watcher() -> None:
    global _observer, _poll_thread
    with _observer_lock:
        if _observer is not None:
            try:
                _observer.stop()
                _observer.join(timeout=2.0)
            except Exception:
                pass
            _observer = None
        if _poll_thread is not None:
            _poll_stop.set()
            try:
                _poll_thread.join(timeout=2.0)
            except Exception:
                pass
            _poll_thread = None
