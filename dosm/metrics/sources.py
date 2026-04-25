"""Pluggable metrics sources for the resource panel.

The same panel UI is reused for the DOSM host (Terminals page) and remote
hosts (Guacamole connect page). Each source returns the same dict shape as
``system_info.snapshot_dict`` so the frontend doesn't care where the data
came from.
"""
from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from dosm.config import Config
from dosm.models import Credential, Host
from dosm.modules.builtin.system_info.snapshot import snapshot_dict
from dosm.secrets import SecretNotFound, get_backend


class MetricsError(RuntimeError):
    pass


class MetricsUnreachable(MetricsError):
    pass


class MetricsSource(ABC):
    """One source produces snapshots until the WebSocket is closed."""

    label: str
    scope: str  # "local" | "remote"

    @abstractmethod
    async def snapshot(self) -> dict: ...

    async def aclose(self) -> None:
        """Override if the source holds resources between snapshots (open
        SSH connection, etc.)."""


# ---- Local (DOSM host) ---------------------------------------------------


class LocalSource(MetricsSource):
    label = "DOSM host"
    scope = "local"

    async def snapshot(self) -> dict:
        loop = asyncio.get_running_loop()
        d = await loop.run_in_executor(None, snapshot_dict)
        d["_scope"] = self.scope
        d["_label"] = self.label
        return d


# ---- SSH (remote Linux host) ---------------------------------------------


_SNAPSHOT_SCRIPT = r"""set -e
echo '=hostname'
hostname
echo '=os'
( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" ) || uname -sr
echo '=uptime'
cat /proc/uptime 2>/dev/null || awk 'BEGIN{print 0,0}'
echo '=load'
cat /proc/loadavg 2>/dev/null || echo '0 0 0'
echo '=cpus'
( grep -c ^processor /proc/cpuinfo 2>/dev/null ) || nproc 2>/dev/null || echo 1
echo '=cpu_stat'
head -1 /proc/stat 2>/dev/null || echo 'cpu 0 0 0 0 0 0 0 0 0 0'
echo '=mem'
cat /proc/meminfo 2>/dev/null || echo ''
echo '=disk'
df -B1 -PT 2>/dev/null | tail -n +2 || true
echo '=end'
"""


def _to_gb(b: int) -> float:
    return round(b / (1024**3), 2)


def _parse_blocks(stdout: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        if line.startswith("=") and not line.startswith("=="):
            current = line[1:].strip()
            blocks[current] = []
            continue
        if current is None:
            continue
        blocks[current].append(line)
    return blocks


def _parse_meminfo(lines: list[str]) -> tuple[int, int]:
    """Returns (total_bytes, available_bytes). Falls back to (0, 0)."""
    fields: dict[str, int] = {}
    for ln in lines:
        m = re.match(r"^(\S+):\s+(\d+)(?:\s+(\S+))?", ln)
        if not m:
            continue
        name, val, unit = m.group(1), int(m.group(2)), (m.group(3) or "kB")
        scale = 1024 if unit.lower() == "kb" else 1
        fields[name] = val * scale
    total = fields.get("MemTotal", 0)
    avail = fields.get("MemAvailable") or (
        fields.get("MemFree", 0) + fields.get("Buffers", 0) + fields.get("Cached", 0)
    )
    return total, avail


@dataclass
class _CPUSample:
    total: int
    idle: int


def _parse_cpu_stat(line: str) -> _CPUSample | None:
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    nums = [int(x) for x in parts[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    return _CPUSample(total=total, idle=idle)


def _parse_disks(lines: list[str]) -> list[dict]:
    disks: list[dict] = []
    for ln in lines:
        # Filesystem Type 1B-blocks Used Available Capacity Mounted-on
        parts = ln.split(None, 6)
        if len(parts) < 7:
            continue
        device, fstype, total_s, used_s, _avail, percent_s, mount = parts
        if fstype in {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs"}:
            continue
        try:
            total = int(total_s)
            used = int(used_s)
            percent = float(percent_s.rstrip("%"))
        except ValueError:
            continue
        if total == 0:
            continue
        disks.append(
            {
                "mountpoint": mount,
                "device": device,
                "fstype": fstype,
                "total_gb": _to_gb(total),
                "used_gb": _to_gb(used),
                "percent": percent,
            }
        )
    return disks


def _parse_snapshot(stdout: str, prev_cpu: _CPUSample | None) -> tuple[dict, _CPUSample | None]:
    blocks = _parse_blocks(stdout)
    hostname = (blocks.get("hostname") or [""])[0].strip() or "remote"
    os_name = (blocks.get("os") or [""])[0].strip() or ""
    uptime_s = 0
    if blocks.get("uptime"):
        try:
            uptime_s = int(float(blocks["uptime"][0].split()[0]))
        except (ValueError, IndexError):
            pass
    load1 = None
    if blocks.get("load"):
        try:
            load1 = round(float(blocks["load"][0].split()[0]), 2)
        except (ValueError, IndexError):
            pass
    try:
        cpus = int((blocks.get("cpus") or ["1"])[0])
    except ValueError:
        cpus = 1
    cur = _parse_cpu_stat((blocks.get("cpu_stat") or ["cpu"])[0]) if blocks.get("cpu_stat") else None
    cpu_percent = 0.0
    if prev_cpu is not None and cur is not None:
        dt = cur.total - prev_cpu.total
        di = cur.idle - prev_cpu.idle
        if dt > 0:
            cpu_percent = round(max(0.0, min(100.0, (1.0 - di / dt) * 100.0)), 1)
    total_b, avail_b = _parse_meminfo(blocks.get("mem", []))
    used_b = max(0, total_b - avail_b)
    mem_percent = round((used_b / total_b * 100.0) if total_b else 0.0, 1)
    disks = _parse_disks(blocks.get("disk", []))
    return (
        {
            "hostname": hostname,
            "os": "Linux",
            "os_release": os_name or "",
            "python": "",
            "uptime_seconds": uptime_s,
            "cpu_count_logical": cpus,
            "cpu_percent": cpu_percent,
            "load_avg_1m": load1,
            "memory_total_gb": _to_gb(total_b),
            "memory_used_gb": _to_gb(used_b),
            "memory_percent": mem_percent,
            "disks": disks,
        },
        cur,
    )


class SSHSource(MetricsSource):
    """Polls a Linux host over SSH using stored host credentials.

    Keeps one open asyncssh connection across snapshots so each tick is just
    one ``run`` call. CPU% needs two samples; the first snapshot reports 0.0
    and subsequent ticks compute deltas.
    """

    scope = "remote"

    def __init__(
        self,
        host: Host,
        *,
        username: str | None,
        password: str | None,
        ssh_private_key: str | None,
    ):
        self._host = host
        self.label = host.name
        self._username = username or "root"
        self._password = password
        self._ssh_key = ssh_private_key
        self._conn = None
        self._prev_cpu: _CPUSample | None = None

    async def _ensure_conn(self):
        import asyncssh  # type: ignore

        if self._conn is not None:
            return self._conn
        kwargs: dict = {
            "host": self._host.hostname,
            "port": self._host.port,
            "username": self._username,
            "known_hosts": None,
        }
        if self._ssh_key:
            kwargs["client_keys"] = [asyncssh.import_private_key(self._ssh_key)]
        if self._password:
            kwargs["password"] = self._password
        try:
            self._conn = await asyncio.wait_for(asyncssh.connect(**kwargs), timeout=8.0)
        except asyncio.TimeoutError as e:
            raise MetricsUnreachable(f"timed out connecting to {self._host.name}") from e
        except Exception as e:
            raise MetricsUnreachable(f"{self._host.name}: {type(e).__name__}: {e}") from e
        return self._conn

    async def snapshot(self) -> dict:
        conn = await self._ensure_conn()
        try:
            res = await asyncio.wait_for(
                conn.run(_SNAPSHOT_SCRIPT, check=False), timeout=8.0
            )
        except (asyncio.TimeoutError, Exception) as e:
            await self.aclose()  # force reconnect on next tick
            raise MetricsUnreachable(f"{self._host.name}: snapshot failed: {e}") from e
        snap, self._prev_cpu = _parse_snapshot(str(res.stdout or ""), self._prev_cpu)
        snap["_scope"] = self.scope
        snap["_label"] = self.label
        return snap

    async def aclose(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
                await self._conn.wait_closed()
            except Exception:
                pass
            self._conn = None


# ---- Factory --------------------------------------------------------------


async def make_source_for_host(cfg: Config, host: Host) -> MetricsSource:
    """Pick the right source for a host based on its protocol + credential."""
    if host.protocol == "ssh":
        cred: Credential | None = host.credential
        username = cred.username if cred else None
        password: str | None = None
        ssh_key: str | None = None
        if cred is not None:
            try:
                secret_text = get_backend(cfg).get_str(cred.secret_ref)
            except SecretNotFound as e:
                raise MetricsError(
                    f"credential {cred.name!r} secret_ref {cred.secret_ref!r} missing"
                ) from e
            if cred.kind == "ssh_key":
                ssh_key = secret_text
            else:
                password = secret_text
        return SSHSource(host, username=username, password=password, ssh_private_key=ssh_key)
    raise MetricsError(
        f"no metrics source for protocol {host.protocol!r} yet — WinRM lands in 8c"
    )
