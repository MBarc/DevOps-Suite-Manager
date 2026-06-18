"""Ping behaviour for RDP-target-behind-RDP-jumpbox (RD Gateway) topology.

Regression: clicking Ping on such a host used to return the alarming
"jump chain has non-SSH hops … ping requires an SSH-tunnelable chain", which
reads like a connectivity failure. It's a feature mismatch — that path isn't
SSH-tunnelled. Ping now validates the RD Gateway's reachability instead and
explains the limitation.
"""
from __future__ import annotations

from dosm.models import Host


def _make_rdp_via_gateway(session_factory, *, gw_host="127.0.0.1", gw_port=1):
    """Create an RDP target behind an RDP jumpbox. The gateway points at a
    closed local port so the probe fails fast and deterministically."""
    with session_factory() as s:
        gw = Host(name="rdgw", hostname=gw_host, port=gw_port, protocol="rdp", is_jumpbox=True)
        s.add(gw)
        s.flush()
        target = Host(name="winbox", hostname="10.0.0.50", port=3389,
                      protocol="rdp", jump_host_id=gw.id)
        s.add(target)
        s.commit()
        return target.id


def test_ping_rdp_jumpbox_is_not_reported_as_ssh_failure(auth_client, session_factory):
    tid = _make_rdp_via_gateway(session_factory)
    d = auth_client.post(f"/hosts/{tid}/ping", follow_redirects=False).json()

    # It targets the RD Gateway, not the generic SSH-tunnel path.
    assert d["via"] == "rdgateway"
    # The old, misleading message must be gone.
    assert "non-SSH hops" not in d["message"]
    assert "SSH-tunnelable chain" not in d["message"]
    # The message explains the RD Gateway semantics and points at Connect.
    assert "RD Gateway" in d["message"]
    assert "Connect" in d["message"]


def test_ping_rdp_jumpbox_unreachable_gateway_fails_cleanly(auth_client, session_factory):
    # Gateway at a closed port → genuine unreachable, reported as such.
    tid = _make_rdp_via_gateway(session_factory, gw_port=1)
    d = auth_client.post(f"/hosts/{tid}/ping", follow_redirects=False).json()
    assert d["via"] == "rdgateway"
    assert d["ok"] is False
    assert "unreachable" in d["message"]
