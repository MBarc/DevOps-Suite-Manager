"""Build Guacamole connection parameter dicts per protocol.

Output shape matches what the auth-json extension expects under
`connections.<name>.parameters`. Credentials are pulled from the secrets
backend and inlined; the blob is short-lived (TTL from config).
"""
from __future__ import annotations

from dataclasses import dataclass

from dosm.config import Config, GuacamoleConfig
from dosm.models import Credential, Host
from dosm.secrets import SecretNotFound, get_backend


class GuacamoleBuildError(RuntimeError):
    pass


@dataclass
class BuiltConnection:
    name: str
    protocol: str
    parameters: dict


def _recording_params(gc: GuacamoleConfig, host_name: str) -> dict:
    if not gc.record_sessions:
        return {}
    return {
        # The Guacamole webapp container is expected to mount the host
        # recordings dir at /recordings (see docker-compose.yml).
        "recording-path": "/recordings",
        "recording-name": f"{host_name}-${{GUAC_USERNAME}}-${{HISTORY_UUID}}",
        "create-recording-path": "true",
    }


def _common_for_ssh(host: Host, params: dict) -> dict:
    params.update(
        {
            "hostname": host.hostname,
            "port": str(host.port or 22),
            "color-scheme": "gray-black",
            "font-size": "12",
            "font-name": "monospace",
        }
    )
    return params


def _common_for_rdp(host: Host, params: dict) -> dict:
    params.update(
        {
            "hostname": host.hostname,
            "port": str(host.port or 3389),
            "security": "any",
            "ignore-cert": "true",
            "resize-method": "display-update",
            "enable-wallpaper": "false",
            "enable-theming": "false",
            "color-depth": "24",
        }
    )
    return params


def _common_for_vnc(host: Host, params: dict) -> dict:
    params.update(
        {
            "hostname": host.hostname,
            "port": str(host.port or 5900),
            "color-depth": "24",
        }
    )
    return params


def _resolve_credential(cfg: Config, cred: Credential | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (username, password, ssh_private_key, domain) for the given credential."""
    if cred is None:
        return None, None, None, None
    try:
        secret_text = get_backend(cfg).get_str(cred.secret_ref)
    except SecretNotFound as e:
        raise GuacamoleBuildError(
            f"credential {cred.name!r} secret_ref {cred.secret_ref!r} missing"
        ) from e
    if cred.kind == "ssh_key":
        return cred.username, None, secret_text, None
    # login to username + password; pat to token used as password
    return cred.username, secret_text, None, cred.domain


def build_connection(
    cfg: Config,
    host: Host,
    *,
    endpoint_override: tuple[str, int] | None = None,
    gateway_host: Host | None = None,
) -> BuiltConnection:
    """Build a Guacamole connection blob for ``host``.

    ``endpoint_override`` (host, port) replaces the target address — used by
    the SSH-tunnel path so Guacamole connects to DOSM's local port forward.

    ``gateway_host`` is an RDP jumpbox reached via RD Gateway. When supplied
    (and target is RDP), the gateway's hostname/port/credentials are added as
    ``gateway-*`` params and guacd handles the hop directly. This is mutually
    exclusive with ``endpoint_override``.
    """
    if not host.protocol or host.protocol not in {"ssh", "rdp", "vnc"}:
        raise GuacamoleBuildError(f"unsupported protocol: {host.protocol!r}")
    username, password, ssh_key, domain = _resolve_credential(cfg, host.credential)
    params: dict = {}
    if host.protocol == "ssh":
        if username:
            params["username"] = username
        if password:
            params["password"] = password
        if ssh_key:
            params["private-key"] = ssh_key
        _common_for_ssh(host, params)
    elif host.protocol == "rdp":
        if username:
            params["username"] = username
        if password:
            params["password"] = password
        if domain:
            params["domain"] = domain
        _common_for_rdp(host, params)
        if gateway_host is not None:
            gw_username, gw_password, _, gw_domain = _resolve_credential(cfg, gateway_host.credential)
            params["gateway-hostname"] = gateway_host.hostname
            params["gateway-port"] = str(gateway_host.port or 443)
            if gw_username:
                params["gateway-username"] = gw_username
            if gw_password:
                params["gateway-password"] = gw_password
            if gw_domain:
                params["gateway-domain"] = gw_domain
    else:  # vnc
        if password:
            params["password"] = password
        _common_for_vnc(host, params)
    if endpoint_override is not None:
        ep_host, ep_port = endpoint_override
        params["hostname"] = ep_host
        params["port"] = str(ep_port)
    params.update(_recording_params(cfg.guacamole, host.name))
    return BuiltConnection(name=host.name, protocol=host.protocol, parameters=params)
