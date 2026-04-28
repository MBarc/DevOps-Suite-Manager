"""SSH jump-host plumbing.

JumpTunnelManager keeps one persistent SSH connection per jump host alive in
the DOSM process. Each jump connection multiplexes many local-port forwards
to many target hosts, so opening N concurrent Guacamole sessions through one
jump uses one TCP/auth pair to the jump and N independent channels.
"""
from dosm.jumps.connections import build_jump_chain, connect_through_chain
from dosm.jumps.tunnels import (
    JumpAuthError,
    JumpTunnelManager,
    JumpUnreachableError,
    TargetAuthError,
    TargetUnreachableError,
    TunnelLease,
    gc_loop,
    get_tunnel_manager,
    probe_forward,
    verify_ssh_credentials,
)

__all__ = [
    "JumpAuthError",
    "JumpTunnelManager",
    "JumpUnreachableError",
    "TargetAuthError",
    "TargetUnreachableError",
    "TunnelLease",
    "build_jump_chain",
    "connect_through_chain",
    "gc_loop",
    "get_tunnel_manager",
    "probe_forward",
    "verify_ssh_credentials",
]
