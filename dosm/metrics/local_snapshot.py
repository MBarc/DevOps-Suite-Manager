"""Point-in-time snapshot of the box DOSM itself runs on.

Used by ``LocalSource`` (and only by it) to feed the resource panel for
the DOSM host. Was previously the heart of the ``system_info`` module;
moved into core when modules were retired since this is the lone
consumer.
"""
from __future__ import annotations

import platform
import socket
import time
from dataclasses import asdict, dataclass

import psutil


@dataclass
class DiskUsage:
    mountpoint: str
    device: str
    fstype: str
    total_gb: float
    used_gb: float
    percent: float


@dataclass
class Snapshot:
    hostname: str
    os: str
    os_release: str
    python: str
    uptime_seconds: int
    cpu_count_logical: int
    cpu_percent: float
    load_avg_1m: float | None
    memory_total_gb: float
    memory_used_gb: float
    memory_percent: float
    disks: list[DiskUsage]


def _to_gb(b: int) -> float:
    return round(b / (1024**3), 2)


def collect_snapshot() -> Snapshot:
    vm = psutil.virtual_memory()
    # getloadavg is POSIX-only; fall back to None on Windows.
    try:
        load1 = round(psutil.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load1 = None

    disks: list[DiskUsage] = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        disks.append(
            DiskUsage(
                mountpoint=part.mountpoint,
                device=part.device,
                fstype=part.fstype,
                total_gb=_to_gb(u.total),
                used_gb=_to_gb(u.used),
                percent=u.percent,
            )
        )

    return Snapshot(
        hostname=socket.gethostname(),
        os=platform.system(),
        os_release=platform.release(),
        python=platform.python_version(),
        uptime_seconds=int(time.time() - psutil.boot_time()),
        cpu_count_logical=psutil.cpu_count(logical=True) or 0,
        cpu_percent=psutil.cpu_percent(interval=0.2),
        load_avg_1m=load1,
        memory_total_gb=_to_gb(vm.total),
        memory_used_gb=_to_gb(vm.used),
        memory_percent=vm.percent,
        disks=disks,
    )


def snapshot_dict() -> dict:
    s = collect_snapshot()
    d = asdict(s)
    d["disks"] = [asdict(x) for x in s.disks]
    return d
