"""Background scan runner.

Each scan is an asyncio task. Results are written to network_scan_results
row-by-row as they complete. The route layer can poll /network/map/{id}/status
for live progress.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from dosm.config import Config
from dosm.db import session_scope
from dosm.jumps.connections import HopCreds, build_jump_chain
from dosm.models import Host, NetworkScan, NetworkScanResult
from dosm.network.executor import (
    local_group_check,
    ssh_group_check,
    winrm_group_check_sync,
    winrm_invoke_group_check_sync,
)

log = logging.getLogger(__name__)

_active_tasks: dict[int, asyncio.Task] = {}
_active_sources: dict[int, set[str]] = {}   # scan_id to source names currently in flight
_last_check: dict[int, str] = {}            # scan_id to most recent "src to dst:port ✓/✗"


def get_scan_activity(scan_id: int) -> dict:
    return {
        "active": sorted(_active_sources.get(scan_id, set())),
        "last": _last_check.get(scan_id, ""),
    }


def start_scan(scan_id: int, cfg: Config) -> None:
    """Create and register a background asyncio task for the scan."""
    task = asyncio.create_task(_run(scan_id, cfg))
    _active_tasks[scan_id] = task
    task.add_done_callback(lambda _: _active_tasks.pop(scan_id, None))


def is_running(scan_id: int) -> bool:
    return scan_id in _active_tasks


async def _run(scan_id: int, cfg: Config) -> None:
    log.info("network scan %d: started", scan_id)
    with session_scope() as db:
        scan = db.get(NetworkScan, scan_id)
        if scan is None:
            return
        scan.status = "running"

    try:
        await _execute_scan(scan_id, cfg)
        with session_scope() as db:
            scan = db.get(NetworkScan, scan_id)
            if scan:
                scan.status = "completed"
                scan.completed_at = datetime.now(UTC)
        log.info("network scan %d: completed", scan_id)
    except Exception:
        log.exception("network scan %d: failed", scan_id)
        with session_scope() as db:
            scan = db.get(NetworkScan, scan_id)
            if scan:
                scan.status = "failed"
    finally:
        _last_check.pop(scan_id, None)


async def _execute_scan(scan_id: int, cfg: Config) -> None:
    """Group results by source host and run checks with bounded concurrency."""
    src_groups: dict[int | str, list[int]] = defaultdict(list)
    with session_scope() as db:
        rows = db.execute(
            select(NetworkScanResult).where(NetworkScanResult.scan_id == scan_id)
        ).scalars().all()
        for r in rows:
            if r.src_host_id is not None:
                src_groups[r.src_host_id].append(r.id)
            elif r.src_label == "DOSM Server":
                src_groups["__local__"].append(r.id)

    sem = asyncio.Semaphore(5)

    _active_sources[scan_id] = set()

    async def _bounded(src_key: int | str, result_ids: list[int]) -> None:
        async with sem:
            if src_key == "__local__":
                await _check_local_source(scan_id, result_ids)
            else:
                await _check_source(scan_id, src_key, result_ids, cfg)

    await asyncio.gather(
        *(_bounded(key, rids) for key, rids in src_groups.items()),
        return_exceptions=True,
    )

    _active_sources.pop(scan_id, None)
    # _last_check is kept alive so the next poll can display the final result.
    # It is cleaned up in _run() once the scan task exits.


async def _check_local_source(scan_id: int, result_ids: list[int]) -> None:
    """Run checks directly from the DOSM process - no credentials needed."""
    source_name = "DOSM Server"
    _active_sources.setdefault(scan_id, set()).add(source_name)
    _last_check[scan_id] = "DOSM Server to checking…"
    try:
        check_params: list[tuple[int, str, int]] = []
        with session_scope() as db:
            for rid in result_ids:
                r = db.get(NetworkScanResult, rid)
                if r:
                    check_params.append((rid, r.dst_address, r.port))

        checks = [(addr, port) for _, addr, port in check_params]
        committed: set[int] = set()

        async def commit_result(
            i: int,
            reachable: bool,
            latency_ms: int | None,
            error_msg: str | None,
        ) -> None:
            committed.add(i)
            rid, addr, port = check_params[i]
            with session_scope() as db:
                r = db.get(NetworkScanResult, rid)
                if r:
                    r.reachable = reachable
                    r.latency_ms = latency_ms
                    r.error_msg = error_msg
                    r.checked_at = datetime.now(UTC)
                    icon = "✓" if reachable else ("✗" if reachable is False else "?")
                    _last_check[scan_id] = (
                        f"{source_name} to {r.dst_label or addr}:{port} {icon}"
                    )

        raw_results = await local_group_check(checks, on_result=commit_result)

        now = datetime.now(UTC)
        for i, ((rid, addr, port), (reachable, latency_ms, error_msg)) in enumerate(
            zip(check_params, raw_results)
        ):
            if i in committed:
                continue
            with session_scope() as db:
                r = db.get(NetworkScanResult, rid)
                if r:
                    r.reachable = reachable
                    r.latency_ms = latency_ms
                    r.error_msg = error_msg
                    r.checked_at = now
                    icon = "✓" if reachable else ("✗" if reachable is False else "?")
                    _last_check[scan_id] = (
                        f"{source_name} to {r.dst_label or addr}:{port} {icon}"
                    )
    finally:
        _active_sources.get(scan_id, set()).discard(source_name)


async def _check_source(scan_id: int, src_host_id: int, result_ids: list[int], cfg: Config) -> None:
    """Open one connection to the source host and run all its checks."""
    # Load host and resolve credentials in a short session
    with session_scope() as db:
        source = db.get(Host, src_host_id)
        if source is None:
            _mark_all(result_ids, False, "source host not found")
            _last_check[scan_id] = "source host not found"
            return
        is_local = source.protocol == "local"
        is_windows = source.protocol == "rdp"
        source_name = source.name
        jump_hops = target = None
        if not is_local:
            try:
                jump_hops, target = build_jump_chain(db, cfg, source)
            except Exception as exc:
                _mark_all(result_ids, False, f"credential error: {exc}")
                _last_check[scan_id] = f"{source.name}: credential error"
                return

    _active_sources.setdefault(scan_id, set()).add(source_name)
    _last_check[scan_id] = f"{source_name} to {'checking' if is_local else 'connecting'}…"
    try:
        # Load the check parameters for each result
        check_params: list[tuple[int, str, int]] = []
        with session_scope() as db:
            for rid in result_ids:
                r = db.get(NetworkScanResult, rid)
                if r:
                    check_params.append((rid, r.dst_address, r.port))

        checks = [(addr, port) for _, addr, port in check_params]

        committed: set[int] = set()

        def commit_result_sync(
            i: int,
            reachable: bool,
            latency_ms: int | None,
            error_msg: str | None,
        ) -> None:
            committed.add(i)
            rid, addr, port = check_params[i]
            with session_scope() as db:
                r = db.get(NetworkScanResult, rid)
                if r:
                    r.reachable = reachable
                    r.latency_ms = latency_ms
                    r.error_msg = error_msg
                    r.checked_at = datetime.now(UTC)
                    icon = "✓" if reachable else ("✗" if reachable is False else "?")
                    _last_check[scan_id] = (
                        f"{source_name} to {r.dst_label or addr}:{port} {icon}"
                    )

        async def commit_result(
            i: int,
            reachable: bool,
            latency_ms: int | None,
            error_msg: str | None,
        ) -> None:
            commit_result_sync(i, reachable, latency_ms, error_msg)

        if is_local:
            raw_results = await local_group_check(checks, on_result=commit_result)
        elif is_windows:
            raw_results = await _check_windows_source(
                src_host_id, jump_hops, target, checks, cfg,
                on_result_sync=commit_result_sync,
            )
        else:
            raw_results = await ssh_group_check(
                jump_hops, target, checks, on_result=commit_result
            )

        # Write any results the callbacks never saw (connection-level failure)
        now = datetime.now(UTC)
        for i, ((rid, addr, port), (reachable, latency_ms, error_msg)) in enumerate(
            zip(check_params, raw_results)
        ):
            if i in committed:
                continue
            with session_scope() as db:
                r = db.get(NetworkScanResult, rid)
                if r:
                    r.reachable = reachable
                    r.latency_ms = latency_ms
                    r.error_msg = error_msg
                    r.checked_at = now
                    icon = "✓" if reachable else ("✗" if reachable is False else "?")
                    _last_check[scan_id] = (
                        f"{source_name} to {r.dst_label or addr}:{port} {icon}"
                    )
    finally:
        _active_sources.get(scan_id, set()).discard(source_name)


async def _check_windows_source(
    src_host_id: int,
    jump_hops: list[HopCreds],
    target: HopCreds,
    checks: list[tuple[str, int]],
    cfg: Config,
    *,
    on_result_sync=None,
) -> list[tuple[bool, int | None, str | None]]:
    """Route a Windows source check through the appropriate jump path.

    No hops to direct WinRM to target
    Last hop = rdp to Windows jump box via Invoke-Command + CredSSP
    All hops = ssh to Linux jump(s): forward WinRM port via JumpTunnelManager
    Mixed chain to unsupported, clear error
    """
    if target.password is None:
        err = "WinRM requires a password credential (SSH key not supported for Windows)"
        return [(False, None, err)] * len(checks)

    loop = asyncio.get_running_loop()

    if not jump_hops:
        return await loop.run_in_executor(
            None,
            functools.partial(
                winrm_group_check_sync,
                target.hostname, target.username, target.password, checks,
                on_result_sync=on_result_sync,
            ),
        )

    last_hop = jump_hops[-1]

    if last_hop.protocol == "rdp":
        # Windows jump to Windows source via Invoke-Command + CredSSP
        if last_hop.password is None:
            return [(False, None,
                f"Windows jump box {last_hop.name!r} requires a password credential for WinRM"
            )] * len(checks)
        return await loop.run_in_executor(
            None,
            functools.partial(
                winrm_invoke_group_check_sync,
                last_hop.hostname, last_hop.username, last_hop.password,
                target.hostname, target.username, target.password,
                checks,
                on_result_sync=on_result_sync,
            ),
        )

    if any(h.protocol == "rdp" for h in jump_hops):
        # A Windows hop appears before a Linux one - not supported
        return [(False, None,
            "Mixed jump chain with a Windows jump box before a Linux one is not supported. "
            "Reorder so Linux jump boxes precede Windows ones, or enable OpenSSH on the "
            "Windows jump box and set its protocol to 'ssh'."
        )] * len(checks)

    # All jump hops are SSH (Linux) to forward WinRM port through the tunnel
    from dosm.jumps import get_tunnel_manager
    tunnel_manager = get_tunnel_manager()

    try:
        with session_scope() as db:
            source_host = db.get(Host, src_host_id)
            if source_host is None:
                return [(False, None, "source host not found during tunnel setup")] * len(checks)
            lease = await tunnel_manager.acquire(db, cfg, source_host, target_port=5985)
    except Exception as exc:
        return [(False, None,
            f"Failed to establish SSH tunnel to Windows source {target.name!r}: {exc}"
        )] * len(checks)

    if lease is None:
        # No jump hops according to tunnel manager - fall back to direct
        return await loop.run_in_executor(
            None,
            functools.partial(
                winrm_group_check_sync,
                target.hostname, target.username, target.password, checks,
                on_result_sync=on_result_sync,
            ),
        )

    try:
        return await loop.run_in_executor(
            None,
            functools.partial(
                winrm_group_check_sync,
                target.hostname, target.username, target.password, checks,
                winrm_port=lease.bind_port,
                on_result_sync=on_result_sync,
            ),
        )
    finally:
        await lease.release()


def _mark_all(result_ids: list[int], reachable: bool, error: str) -> None:
    now = datetime.now(UTC)
    for rid in result_ids:
        with session_scope() as db:
            r = db.get(NetworkScanResult, rid)
            if r:
                r.reachable = reachable
                r.error_msg = error
                r.checked_at = now
