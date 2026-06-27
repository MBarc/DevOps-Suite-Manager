"""Verbose connection-error surfacing for the Connect path.

Covers the server-side preflight layer added so a failed SSH/RDP/VNC connect
shows the host's own wording in DOSM (locked account, refused port, no route)
rather than a flattened generic. The guacd-iframe scrape (Layer 2) is JS and
exercised by the live smoke, not here.
"""
from __future__ import annotations

import asyncio

import pytest

from dosm.jumps.tunnels import (
    TargetAuthError,
    TargetUnreachableError,
    _ssh_failure_detail,
    preflight_direct,
    ssh_auth_probe,
    tcp_probe,
)
from dosm.models import Host
from dosm.network.executor import _classify_ssh_error, _ssh_raw_detail


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_failure_detail_includes_server_banner():
    exc = ConnectionResetError("reset")
    detail = _ssh_failure_detail(exc, ["Account locked due to too many failures"])
    assert "Account locked due to too many failures" in detail
    assert detail.startswith(": ")


def test_failure_detail_prefers_reason_and_dedups():
    class _Disc(Exception):
        reason = "Access denied for this account"

    # banner + reason + str, de-duplicated, no empty fragments
    detail = _ssh_failure_detail(_Disc("Access denied for this account"), [])
    assert detail.count("Access denied for this account") == 1


def test_failure_detail_empty_when_nothing_useful():
    assert _ssh_failure_detail(Exception(""), []) == ""


def test_executor_raw_detail_truncates_and_uses_reason():
    class _Disc(Exception):
        reason = "x" * 500

    assert _ssh_raw_detail(_Disc("ignored")) == " - " + "x" * 160


def test_classify_ssh_error_appends_refused_detail():
    msg = _classify_ssh_error(ConnectionRefusedError("Connect call failed"), "db01")
    assert "db01" in msg
    assert "Connect call failed" in msg  # raw cause preserved, not dropped


# ── probes against a closed port (deterministic, no live server) ──────────────

CLOSED = ("127.0.0.1", 1)  # nothing listens on TCP/1 on loopback


def test_tcp_probe_refused_names_target_and_reason():
    with pytest.raises(TargetUnreachableError) as ei:
        asyncio.run(
            tcp_probe(
                connect_host=CLOSED[0],
                connect_port=CLOSED[1],
                target_host="winbox",
                target_port=3389,
                timeout=3.0,
            )
        )
    assert "winbox:3389" in str(ei.value)


def test_ssh_auth_probe_refused_is_unreachable_with_detail():
    with pytest.raises(TargetUnreachableError) as ei:
        asyncio.run(
            ssh_auth_probe(
                connect_host=CLOSED[0],
                connect_port=CLOSED[1],
                username="root",
                password="hunter2",
                private_key=None,
                target_host="linbox",
                target_port=22,
                timeout=3.0,
            )
        )
    assert "linbox:22" in str(ei.value)


def test_ssh_auth_probe_noop_without_credentials():
    # No password and no key → nothing to verify, must not raise (guacd prompts).
    asyncio.run(
        ssh_auth_probe(
            connect_host=CLOSED[0],
            connect_port=CLOSED[1],
            username="root",
            password=None,
            private_key=None,
            target_host="linbox",
            target_port=22,
            timeout=3.0,
        )
    )


def test_preflight_direct_rdp_probes_reachability(test_config):
    # RDP host with no credential → TCP probe path; closed port → unreachable.
    host = Host(name="winbox", hostname=CLOSED[0], port=CLOSED[1], protocol="rdp")
    with pytest.raises(TargetUnreachableError):
        asyncio.run(preflight_direct(test_config, host, timeout=3.0))
