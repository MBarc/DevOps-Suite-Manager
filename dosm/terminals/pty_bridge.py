from __future__ import annotations

import os
import platform
import signal
from abc import ABC, abstractmethod


class PtyBridge(ABC):
    """Abstract PTY wrapping a local shell process.

    Concrete impls: PosixPty (stdlib pty + fork/execvp), WinPty (pywinpty).
    """

    @abstractmethod
    def read(self, max_bytes: int = 4096) -> bytes: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def resize(self, rows: int, cols: int) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def alive(self) -> bool: ...


class PosixPty(PtyBridge):
    def __init__(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        rows: int = 24,
        cols: int = 80,
    ):
        import fcntl
        import pty
        import struct
        import termios

        self._termios = termios
        self._fcntl = fcntl
        self._struct = struct

        pid, fd = pty.fork()
        if pid == 0:  # child - pty.fork() already called setsid() for us.
            try:
                if cwd:
                    try:
                        os.chdir(cwd)
                    except OSError:
                        pass
                merged = os.environ.copy()
                merged.setdefault("TERM", "xterm-256color")
                merged.setdefault("COLORTERM", "truecolor")
                if env:
                    merged.update(env)
                os.execvpe(argv[0], argv, merged)
            except FileNotFoundError:
                os._exit(127)
            except Exception:
                os._exit(1)
            os._exit(1)  # unreachable; belt-and-braces if execvpe returns
        # parent
        self._pid = pid
        self._fd = fd
        self._alive = True
        self.resize(rows, cols)

    def read(self, max_bytes: int = 4096) -> bytes:
        try:
            data = os.read(self._fd, max_bytes)
        except OSError:
            self._alive = False
            return b""
        if not data:
            self._alive = False
        return data

    def write(self, data: bytes) -> None:
        if not self._alive:
            return
        try:
            os.write(self._fd, data)
        except OSError:
            self._alive = False

    def resize(self, rows: int, cols: int) -> None:
        try:
            packed = self._struct.pack("HHHH", rows, cols, 0, 0)
            self._fcntl.ioctl(self._fd, self._termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    @property
    def alive(self) -> bool:
        if not self._alive:
            return False
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._alive = False
            return False
        if pid == self._pid:
            self._alive = False
        return self._alive

    def close(self) -> None:
        if not self._alive:
            try:
                os.close(self._fd)
            except OSError:
                pass
            return
        try:
            os.kill(self._pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._alive = False


class WinPty(PtyBridge):  # pragma: no cover - exercised on Windows hosts only
    def __init__(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        rows: int = 24,
        cols: int = 80,
    ):
        import winpty  # type: ignore

        merged = os.environ.copy()
        merged.setdefault("TERM", "xterm-256color")
        if env:
            merged.update(env)
        self._pty = winpty.PTY(cols, rows)
        appname = argv[0]
        cmdline = " ".join(argv)
        env_block = "\0".join(f"{k}={v}" for k, v in merged.items()) + "\0"
        self._pty.spawn(
            appname=appname, cmdline=cmdline, cwd=cwd, env=env_block
        )
        self._alive = True

    def read(self, max_bytes: int = 4096) -> bytes:
        try:
            data = self._pty.read(max_bytes, blocking=False)
        except Exception:
            self._alive = False
            return b""
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        return data

    def write(self, data: bytes) -> None:
        try:
            self._pty.write(data.decode("utf-8", errors="replace"))
        except Exception:
            self._alive = False

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._pty.set_size(cols, rows)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        if not self._alive:
            return False
        try:
            self._alive = self._pty.isalive()
        except Exception:
            self._alive = False
        return self._alive

    def close(self) -> None:
        try:
            del self._pty
        except Exception:
            pass
        self._alive = False


def open_pty(
    argv: list[str],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    rows: int = 24,
    cols: int = 80,
) -> PtyBridge:
    if platform.system() == "Windows":
        return WinPty(argv, env=env, cwd=cwd, rows=rows, cols=cols)
    return PosixPty(argv, env=env, cwd=cwd, rows=rows, cols=cols)
