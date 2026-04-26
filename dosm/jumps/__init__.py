"""SSH jump-host plumbing.

JumpTunnelManager keeps one persistent SSH connection per jump host alive in
the DOSM process. Each jump connection multiplexes many local-port forwards
to many target hosts, so opening N concurrent Guacamole sessions through one
jump uses one TCP/auth pair to the jump and N independent channels.
"""
from dosm.jumps.connections import build_jump_chain, connect_through_chain
from dosm.jumps.tunnels import JumpTunnelManager, TunnelLease, get_tunnel_manager

__all__ = [
    "JumpTunnelManager",
    "TunnelLease",
    "build_jump_chain",
    "connect_through_chain",
    "get_tunnel_manager",
]
