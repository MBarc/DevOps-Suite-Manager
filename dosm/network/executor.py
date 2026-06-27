"""Connectivity check execution - run port reachability tests from a source host.

Linux sources: SSH in (jump-chain aware), run bash /dev/tcp or nc.
Windows sources: WinRM, run Test-NetConnection via PowerShell.

Jump box routing:
  Linux jump to Linux source  : SSH chain (asyncssh tunnel)
  Linux jump to Windows source: caller forwards WinRM port via JumpTunnelManager,
                               then calls winrm_group_check_sync with the local port
  Windows jump to Windows src : winrm_invoke_group_check_sync (Invoke-Command)
  Windows jump to Linux source: unsupported - Windows has no native SSH client by
                               default; enable OpenSSH on the jump box and set its
                               protocol to 'ssh' in the inventory

Error messages name which hop failed and why, so the operator immediately knows
whether the problem is DOSM to jumpbox, jumpbox to target, or credentials.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from sqlalchemy.orm import Session

from dosm.config import Config
from dosm.jumps.connections import HopCreds, build_jump_chain
from dosm.models import Host

log = logging.getLogger(__name__)

_SAFE_ADDR = re.compile(r"^[a-zA-Z0-9.\-_:]+$")
_CONNECT_TIMEOUT = 10.0   # seconds to establish SSH/WinRM connection
_CHECK_TIMEOUT = 8.0      # seconds per individual port check command


def _validate_address(addr: str) -> str:
    if not _SAFE_ADDR.match(addr):
        raise ValueError(f"unsafe destination address: {addr!r}")
    return addr


# ── SSH error classifier ──────────────────────────────────────────────────────

def _ssh_raw_detail(exc: BaseException) -> str:
    """`- <reason>` suffix carrying the server/OS-supplied cause, if any.

    asyncssh disconnect errors expose the server's stated reason on ``.reason``;
    everything else falls back to the exception text. Truncated and returned
    empty when there's nothing beyond the bare type name.
    """
    reason = getattr(exc, "reason", None)
    text = str(reason).strip() if reason else str(exc).strip()
    return f" - {text[:160]}" if text else ""


def _classify_ssh_error(exc: BaseException, hostname: str) -> str:
    import errno as _errno

    name = type(exc).__name__
    msg = str(exc).lower()
    detail = _ssh_raw_detail(exc)

    if "permissiondenied" in name.lower() or "permission denied" in msg:
        return f"SSH authentication failed on {hostname!r} - check credentials{detail}"

    if isinstance(exc, ConnectionRefusedError) or "connection refused" in msg:
        return f"SSH not enabled on {hostname!r} - port refused the connection{detail}"

    if isinstance(exc, OSError):
        if exc.errno in (_errno.ENETUNREACH, _errno.EHOSTUNREACH, _errno.ENONET):
            return f"Host {hostname!r} is unreachable - no route to host{detail}"
        if exc.errno == _errno.ECONNRESET:
            return f"SSH connection reset by {hostname!r}{detail}"

    if "disconnecterror" in name.lower() or "connection lost" in name.lower():
        return f"SSH connection to {hostname!r} was dropped unexpectedly{detail}"

    if "timeout" in name.lower() or "timed out" in msg or isinstance(exc, asyncio.TimeoutError):
        return (
            f"SSH connection to {hostname!r} timed out - "
            "the host may be unreachable or SSH may not be enabled"
        )

    if "hostkey" in name.lower():
        return f"SSH host-key verification failed for {hostname!r}{detail}"

    return f"SSH error on {hostname!r}: {name}: {str(exc)[:120]}"


# ── WinRM error classifier ────────────────────────────────────────────────────

def _classify_winrm_error(exc: BaseException, hostname: str) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()

    if "connectionerror" in name.lower() or "max retries" in msg or "connection refused" in msg:
        return (
            f"WinRM not reachable on {hostname!r}:5985 - "
            "WinRM may not be enabled (run: winrm quickconfig)"
        )

    if "timeout" in name.lower() or "timed out" in msg:
        return (
            f"WinRM connection to {hostname!r} timed out - "
            "the host may be unreachable or WinRM may not be enabled"
        )

    if (
        "invalidcredentials" in name.lower()
        or "401" in msg
        or "unauthorized" in msg
        or "access denied" in msg
        or "logon failure" in msg
    ):
        return f"WinRM authentication failed on {hostname!r} - check username/password"

    if "transport" in name.lower() and "error" in name.lower():
        return f"WinRM transport error on {hostname!r}: {str(exc)[:120]}"

    return f"WinRM error on {hostname!r}: {name}: {str(exc)[:120]}"


# ── SSH chain connect with per-hop error context ──────────────────────────────

def _hop_connect_kwargs(hop: HopCreds, *, tunnel=None) -> dict:
    import asyncssh  # type: ignore
    kwargs: dict = {
        "host": hop.hostname,
        "port": hop.port,
        "username": hop.username,
        "known_hosts": None,
    }
    if hop.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(hop.private_key)]
    if hop.password:
        kwargs["password"] = hop.password
    if tunnel is not None:
        kwargs["tunnel"] = tunnel
    return kwargs


class _HopConnectError(Exception):
    def __init__(self, message: str, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


async def _async_connect(hop: HopCreds, *, tunnel=None):
    import asyncssh  # type: ignore
    return await asyncssh.connect(**_hop_connect_kwargs(hop, tunnel=tunnel))


async def _connect_tracked(jump_hops: list[HopCreds], target: HopCreds) -> object:
    """Connect through the SSH jump chain with per-hop error context.

    Raises _HopConnectError with a message that names exactly which hop failed
    and why - distinguishing DOSM to jumpbox failures from jumpbox to target failures.

    A Windows hop (protocol='rdp') in the SSH chain raises immediately with a
    clear message directing the operator to enable OpenSSH on that host.
    """
    prev = None
    prev_hop: HopCreds | None = None

    for hop in jump_hops:
        # Windows hosts speak WinRM, not SSH. A Windows machine can be used as
        # a jump box only if OpenSSH is installed and its protocol is set to 'ssh'.
        if hop.protocol == "rdp":
            context = (
                f"via jump box {prev_hop.name!r}" if prev_hop
                else "directly from DOSM"
            )
            raise _HopConnectError(
                f"Jump box {hop.name!r} ({hop.hostname}) is a Windows host (protocol=rdp). "
                f"SSH tunnel traversal requires OpenSSH to be enabled on this jump box - "
                f"connect {context} and run: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0. "
                f"Then set this host's protocol to 'ssh' in the inventory.",
                phase="windows_jump_no_ssh",
            )

        try:
            prev = await asyncio.wait_for(
                _async_connect(hop, tunnel=prev),
                timeout=_CONNECT_TIMEOUT,
            )
        except _HopConnectError:
            raise
        except Exception as exc:
            if prev_hop is None:
                raise _HopConnectError(
                    _classify_ssh_error(exc, hop.hostname),
                    phase="jump_box",
                ) from exc
            else:
                raise _HopConnectError(
                    f"Connected to jump box {prev_hop.name!r} but it could not reach "
                    f"next hop {hop.name!r} ({hop.hostname}): "
                    + _classify_ssh_error(exc, hop.hostname),
                    phase="jump_to_jump",
                ) from exc
        prev_hop = hop

    try:
        return await asyncio.wait_for(
            _async_connect(target, tunnel=prev),
            timeout=_CONNECT_TIMEOUT,
        )
    except _HopConnectError:
        raise
    except Exception as exc:
        if prev_hop is None:
            raise _HopConnectError(
                _classify_ssh_error(exc, target.hostname),
                phase="direct",
            ) from exc
        else:
            raise _HopConnectError(
                f"Connected to jump box {prev_hop.name!r} but it could not reach "
                f"target {target.name!r} ({target.hostname}): "
                + _classify_ssh_error(exc, target.hostname),
                phase="jump_to_target",
            ) from exc


# ── SSH group check ───────────────────────────────────────────────────────────

async def _ssh_check_one(conn, dst_address: str, port: int) -> tuple[bool, int | None, str | None]:
    if port == 0:
        cmd = (
            f"S=$(date +%s%3N 2>/dev/null || echo 0); "
            f"ping -c 1 -W 3 '{dst_address}' >/dev/null 2>&1 && "
            f"E=$(date +%s%3N 2>/dev/null || echo 0) && echo \"OK $((E-S))\" || echo FAIL"
        )
        timeout_msg = f"ping timed out reaching {dst_address}"
        err_prefix = "ping error"
    else:
        cmd = (
            f"S=$(date +%s%3N 2>/dev/null || echo 0); "
            f"(bash -c '</dev/tcp/{dst_address}/{port}' 2>/dev/null || "
            f"nc -zw5 '{dst_address}' {port} 2>/dev/null) && "
            f"E=$(date +%s%3N 2>/dev/null || echo 0) && echo \"OK $((E-S))\" || echo FAIL"
        )
        timeout_msg = f"check timed out reaching {dst_address}:{port}"
        err_prefix = "check error"

    wall_start = time.monotonic()
    try:
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=_CHECK_TIMEOUT)
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        stdout = (result.stdout or "").strip()
        if stdout.startswith("OK"):
            parts = stdout.split()
            try:
                latency = int(parts[1])
                if latency <= 0 or latency > 30_000:
                    latency = wall_ms
            except (IndexError, ValueError):
                latency = wall_ms
            return True, latency, None
        return False, None, None
    except TimeoutError:
        return False, None, timeout_msg
    except Exception as exc:
        return False, None, f"{err_prefix}: {type(exc).__name__}: {str(exc)[:120]}"


async def ssh_group_check(
    jump_hops: list[HopCreds],
    target: HopCreds,
    checks: list[tuple[str, int]],
    *,
    on_result=None,
) -> list[tuple[bool, int | None, str | None]]:
    """Open one SSH connection to target via the jump chain, run all checks.

    on_result: optional async callable(idx, reachable, latency_ms, error_msg)
    called immediately after each check so callers can commit results
    incrementally rather than waiting for the full batch.
    """
    conn = None
    try:
        conn = await _connect_tracked(jump_hops, target)
        results = []
        for i, (dst_address, port) in enumerate(checks):
            result = await _ssh_check_one(conn, dst_address, port)
            results.append(result)
            if on_result is not None:
                await on_result(i, *result)
        return results
    except _HopConnectError as exc:
        return [(False, None, str(exc))] * len(checks)
    except Exception as exc:
        return [(False, None, _classify_ssh_error(exc, target.hostname))] * len(checks)
    finally:
        if conn is not None:
            conn.close()


# ── WinRM direct group check ──────────────────────────────────────────────────

def winrm_group_check_sync(
    hostname: str,
    username: str,
    password: str,
    checks: list[tuple[str, int]],
    *,
    winrm_port: int = 5985,
    on_result_sync=None,
) -> list[tuple[bool, int | None, str | None]]:
    """Run Test-NetConnection checks from a Windows source via WinRM.

    winrm_port can be overridden when the caller has forwarded WinRM through
    an SSH tunnel (e.g. Linux jump box to Windows source).
    """
    try:
        import winrm  # type: ignore
    except ImportError:
        return [(False, None, "pywinrm not installed - cannot check Windows hosts via WinRM")] * len(checks)

    connect_host = hostname if winrm_port == 5985 else "127.0.0.1"
    try:
        session = winrm.Session(
            f"http://{connect_host}:{winrm_port}/wsman",
            auth=(username, password),
            transport="ntlm",
            operation_timeout_sec=_CHECK_TIMEOUT,
            read_timeout_sec=_CONNECT_TIMEOUT,
        )
    except Exception as exc:
        err = _classify_winrm_error(exc, hostname)
        return [(False, None, err)] * len(checks)

    results: list[tuple[bool, int | None, str | None]] = []
    for dst_address, port in checks:
        try:
            if port == 0:
                script = (
                    f"$sw=[System.Diagnostics.Stopwatch]::StartNew();"
                    f"$r=Test-Connection -ComputerName '{dst_address}' -Count 1 -Quiet;"
                    f"$sw.Stop();"
                    f"if($r){{\"OK $([int]$sw.ElapsedMilliseconds)\"}}else{{\"FAIL\"}}"
                )
            else:
                script = (
                    f"$sw=[System.Diagnostics.Stopwatch]::StartNew();"
                    f"$r=Test-NetConnection -ComputerName '{dst_address}' -Port {port}"
                    f" -WarningAction SilentlyContinue;"
                    f"$sw.Stop();"
                    f"if($r.TcpTestSucceeded){{\"OK $([int]$sw.ElapsedMilliseconds)\"}}else{{\"FAIL\"}}"
                )
            res = session.run_ps(script)
            stdout = (res.std_out or b"").decode("utf-8", errors="replace").strip()
            if stdout.startswith("OK"):
                parts = stdout.split()
                try:
                    latency = int(parts[1])
                except (IndexError, ValueError):
                    latency = None
                result: tuple[bool, int | None, str | None] = (True, latency, None)
            else:
                result = (False, None, None)
        except Exception as exc:
            result = (False, None, _classify_winrm_error(exc, hostname))

        results.append(result)
        if on_result_sync is not None:
            on_result_sync(len(results) - 1, *result)

    return results


# ── WinRM Invoke-Command check (Windows jump box to Windows source) ────────────

def winrm_invoke_group_check_sync(
    jump_hostname: str,
    jump_username: str,
    jump_password: str,
    target_hostname: str,
    target_username: str,
    target_password: str,
    checks: list[tuple[str, int]],
    *,
    on_result_sync=None,
) -> list[tuple[bool, int | None, str | None]]:
    """Run checks from a Windows source via a Windows jump box using Invoke-Command.

    Connects to the Windows jump box via WinRM/NTLM, then uses Invoke-Command
    with CredSSP to hop to the Windows source and run Test-NetConnection there.

    Requires CredSSP to be enabled on the jump box:
      Enable-WSManCredSSP -Role Server -Force   (on the jump box)
      Enable-WSManCredSSP -Role Client -DelegateComputer '*' -Force  (on DOSM host, if applicable)

    If CredSSP is not configured the error message says so explicitly.
    """
    try:
        import winrm  # type: ignore
    except ImportError:
        return [(False, None, "pywinrm not installed - cannot check Windows hosts via WinRM")] * len(checks)

    try:
        jump_session = winrm.Session(
            f"http://{jump_hostname}:5985/wsman",
            auth=(jump_username, jump_password),
            transport="ntlm",
            operation_timeout_sec=_CHECK_TIMEOUT,
            read_timeout_sec=_CONNECT_TIMEOUT,
        )
    except Exception as exc:
        err = (
            f"Windows jump box {jump_hostname!r}: "
            + _classify_winrm_error(exc, jump_hostname)
        )
        return [(False, None, err)] * len(checks)

    results: list[tuple[bool, int | None, str | None]] = []
    for dst_address, port in checks:
        try:
            # Build PSCredential for the target inline (no plain-text in env vars)
            if port == 0:
                inner_check = (
                    f"$sw=[System.Diagnostics.Stopwatch]::StartNew(); "
                    f"$r=Test-Connection -ComputerName '{dst_address}' -Count 1 -Quiet; "
                    f"$sw.Stop(); "
                    f"if($r){{\"OK $([int]$sw.ElapsedMilliseconds)\"}}else{{\"FAIL\"}}"
                )
            else:
                inner_check = (
                    f"$sw=[System.Diagnostics.Stopwatch]::StartNew(); "
                    f"$r=Test-NetConnection -ComputerName '{dst_address}' -Port {port} -WarningAction SilentlyContinue; "
                    f"$sw.Stop(); "
                    f"if($r.TcpTestSucceeded){{\"OK $([int]$sw.ElapsedMilliseconds)\"}}else{{\"FAIL\"}}"
                )
            script = (
                f"$secpwd = ConvertTo-SecureString '{target_password}' -AsPlainText -Force; "
                f"$cred = New-Object System.Management.Automation.PSCredential('{target_username}', $secpwd); "
                f"try {{ "
                f"  $res = Invoke-Command -ComputerName '{target_hostname}' -Credential $cred "
                f"    -Authentication CredSSP -ScriptBlock {{ "
                f"      {inner_check} "
                f"    }}; "
                f"  $res "
                f"}} catch [System.Management.Automation.Remoting.PSRemotingTransportException] {{ "
                f"  if($_.Exception.Message -match 'CredSSP') {{ "
                f"    'CREDSSP_NOT_CONFIGURED' "
                f"  }} else {{ "
                f"    'INVOKE_FAILED: ' + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)) "
                f"  }} "
                f"}}"
            )
            res = jump_session.run_ps(script)
            stdout = (res.std_out or b"").decode("utf-8", errors="replace").strip()

            if stdout.startswith("OK"):
                parts = stdout.split()
                try:
                    latency = int(parts[1])
                except (IndexError, ValueError):
                    latency = None
                invoke_result: tuple[bool, int | None, str | None] = (True, latency, None)
            elif stdout == "FAIL":
                invoke_result = (False, None, None)
            elif stdout == "CREDSSP_NOT_CONFIGURED":
                invoke_result = (False, None, (
                    f"Windows jump box {jump_hostname!r} cannot reach "
                    f"target {target_hostname!r} via Invoke-Command: CredSSP is not configured. "
                    f"On the jump box run: Enable-WSManCredSSP -Role Server -Force"
                ))
            elif stdout.startswith("INVOKE_FAILED:"):
                invoke_result = (False, None, (
                    f"Windows jump box {jump_hostname!r}: Invoke-Command to "
                    f"{target_hostname!r} failed - {stdout[14:].strip()}"
                ))
            else:
                invoke_result = (False, None, (
                    f"Unexpected output from Windows jump box {jump_hostname!r}: {stdout[:120]!r}"
                ))
        except Exception as exc:
            invoke_result = (False, None, (
                f"Windows jump box {jump_hostname!r}: "
                + _classify_winrm_error(exc, jump_hostname)
            ))

        results.append(invoke_result)
        if on_result_sync is not None:
            on_result_sync(len(results) - 1, *invoke_result)

    return results


# ── Local (in-process) group check ───────────────────────────────────────────

def _local_tcp_check_sync(dst_address: str, port: int) -> tuple[bool, int | None, str | None]:
    import socket
    try:
        start = time.monotonic()
        with socket.create_connection((dst_address, port), timeout=_CHECK_TIMEOUT):
            latency_ms = int((time.monotonic() - start) * 1000)
            return True, latency_ms, None
    except TimeoutError:
        return False, None, f"connection to {dst_address}:{port} timed out"
    except ConnectionRefusedError:
        return False, None, None
    except OSError as exc:
        return False, None, f"socket error reaching {dst_address}:{port}: {exc}"


def _local_ping_sync(dst_address: str) -> tuple[bool, int | None, str | None]:
    import subprocess
    try:
        start = time.monotonic()
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "3", dst_address],
            capture_output=True, timeout=10,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        if r.returncode == 0:
            return True, latency_ms, None
        return False, None, None
    except subprocess.TimeoutExpired:
        return False, None, f"ping timed out reaching {dst_address}"
    except FileNotFoundError:
        return False, None, "ping not available in this environment"
    except Exception as exc:
        return False, None, f"ping error: {type(exc).__name__}: {str(exc)[:120]}"


async def local_group_check(
    checks: list[tuple[str, int]],
    *,
    on_result=None,
) -> list[tuple[bool, int | None, str | None]]:
    """Run checks directly from the DOSM process - no SSH or WinRM needed.

    on_result: optional async callable(idx, reachable, latency_ms, error_msg)
    """
    loop = asyncio.get_running_loop()
    results = []
    for i, (dst_address, port) in enumerate(checks):
        if port == 0:
            result = await loop.run_in_executor(None, _local_ping_sync, dst_address)
        else:
            result = await loop.run_in_executor(None, _local_tcp_check_sync, dst_address, port)
        results.append(result)
        if on_result is not None:
            await on_result(i, *result)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

async def quick_check(
    cfg: Config,
    db: Session,
    source_host: Host,
    dst_address: str,
    port: int,
) -> tuple[bool | None, int | None, str | None]:
    """Single ad-hoc port check - used by the Port Checker page."""
    try:
        _validate_address(dst_address)
    except ValueError as exc:
        return False, None, str(exc)

    if source_host.protocol == "local":
        results = await local_group_check([(dst_address, port)])
        return results[0]

    jump_hops, target = build_jump_chain(db, cfg, source_host)

    if source_host.protocol == "rdp":
        return await _quick_check_windows(jump_hops, target, dst_address, port)
    else:
        results = await ssh_group_check(jump_hops, target, [(dst_address, port)])
        return results[0]


async def _quick_check_windows(
    jump_hops: list[HopCreds],
    target: HopCreds,
    dst_address: str,
    port: int,
) -> tuple[bool | None, int | None, str | None]:
    """Route a Windows source check through the appropriate jump path."""
    if target.password is None:
        return False, None, "WinRM requires a password credential (SSH key not supported for Windows)"

    if not jump_hops:
        # Direct - no jump box
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, winrm_group_check_sync,
            target.hostname, target.username, target.password, [(dst_address, port)],
        )
        return results[0]

    last_hop = jump_hops[-1]

    if last_hop.protocol == "rdp":
        # Windows jump box to Windows source: use Invoke-Command
        if last_hop.password is None:
            return False, None, (
                f"Windows jump box {last_hop.name!r} requires a password credential for WinRM"
            )
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, winrm_invoke_group_check_sync,
            last_hop.hostname, last_hop.username, last_hop.password,
            target.hostname, target.username, target.password,
            [(dst_address, port)],
        )
        return results[0]

    if any(h.protocol == "rdp" for h in jump_hops):
        return False, None, (
            "Mixed jump chain with a Windows jump box before a Linux one is not supported. "
            "Reorder the chain so Linux jump boxes precede Windows ones, "
            "or enable OpenSSH on the Windows jump box and set its protocol to 'ssh'."
        )

    # All jump hops are SSH (Linux) to would forward the WinRM port through the
    # tunnel, but JumpTunnelManager.acquire needs a DB session + Host object;
    # the scanner handles that path directly for bulk scans. For the
    # quick_check single-shot path, fall back to a clear message.
    return False, None, (
        "Port Checker for a Windows source behind a Linux jump box requires "
        "the scanner path (use Network Map instead of Port Checker for this host)."
    )
