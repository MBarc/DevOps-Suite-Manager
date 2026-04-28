from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.guacamole.auth_json import AuthJsonCodec, build_connection_id, load_secret_key
from dosm.guacamole.builder import GuacamoleBuildError, _resolve_credential, build_connection
from dosm.guacamole.client import (
    GuacamoleClientError,
    GuacamoleUnreachable,
    fetch_session_token,
)
from dosm.hosts.repo import resolve_jump_chain
from dosm.jumps import (
    JumpAuthError,
    JumpUnreachableError,
    TargetAuthError,
    TargetUnreachableError,
    get_tunnel_manager,
    probe_forward,
    verify_ssh_credentials,
)
from dosm.models import AuditLog, Host, User
from dosm.recording import events as rec_events

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


@router.get("/hosts/{host_id}/connect", response_class=HTMLResponse, include_in_schema=False)
async def host_connect(
    host_id: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    cfg = request.app.state.config
    gc = cfg.guacamole

    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(404)

    if not gc.enabled:
        return _templates(request).TemplateResponse(
            request,
            "guacamole/connect.html",
            {
                "host": host,
                "iframe_url": None,
                "error": (
                    "Guacamole is not enabled. Set guacamole.enabled: true in config.yaml "
                    "and point guacamole.base_url at your Guacamole instance."
                ),
                "user": user,
                "jump_chain": [],
                "tunnel_endpoint": None,
                "tunnel_session_id": None,
                "rdgw_host_name": None,
            },
        )

    chain = resolve_jump_chain(db, host)

    error: str | None = None
    iframe_url: str | None = None
    tunnel_lease = None
    tunnel_session_id: str | None = None
    rdgw_host_name: str | None = None
    endpoint_override: tuple[str, int] | None = None

    try:
        if host.jump_host_id is not None:
            jump_host = chain[-1]  # direct/innermost hop
            if host.protocol == "rdp" and jump_host.protocol == "rdp":
                # RD Gateway path — guacd speaks RDP to the gateway, which
                # relays to the target. No DOSM tunnel needed.
                connection = build_connection(cfg, host, gateway_host=jump_host)
                rdgw_host_name = jump_host.name
            else:
                # SSH local-port-forward path.
                non_ssh = [h for h in chain if h.protocol != "ssh"]
                if non_ssh:
                    names = ", ".join(f"{h.name!r} ({h.protocol})" for h in non_ssh)
                    raise GuacamoleBuildError(
                        f"jump chain has non-SSH hops: {names}. DOSM tunnels via SSH "
                        f"local-port-forward; RDP-gateway-style chaining requires an "
                        f"RDP jumpbox and an RDP target."
                    )
                try:
                    tunnel_lease = await get_tunnel_manager().acquire(
                        db, cfg, host, bind_host=gc.tunnel_bind_host
                    )
                except JumpUnreachableError as e:
                    raise GuacamoleBuildError(str(e)) from e
                except JumpAuthError as e:
                    raise GuacamoleBuildError(str(e)) from e
                except Exception as e:
                    raise GuacamoleBuildError(
                        f"failed to open jump tunnel: {e}"
                    ) from e
                if tunnel_lease is not None:
                    try:
                        await probe_forward(tunnel_lease)
                    except TargetUnreachableError as e:
                        try:
                            await tunnel_lease.release()
                        except Exception:
                            pass
                        raise GuacamoleBuildError(str(e)) from e
                    if host.protocol == "ssh":
                        username, password, ssh_key, _ = _resolve_credential(cfg, host.credential)
                        try:
                            await verify_ssh_credentials(
                                bind_port=tunnel_lease.bind_port,
                                bind_host=tunnel_lease.bind_host,
                                username=username,
                                password=password,
                                private_key=ssh_key,
                                target_host=tunnel_lease.target_host,
                                target_port=tunnel_lease.target_port,
                            )
                        except (TargetAuthError, TargetUnreachableError) as e:
                            try:
                                await tunnel_lease.release()
                            except Exception:
                                pass
                            raise GuacamoleBuildError(str(e)) from e
                    endpoint_override = (gc.dosm_reachable_host, tunnel_lease.bind_port)
                connection = build_connection(cfg, host, endpoint_override=endpoint_override)
        else:
            connection = build_connection(cfg, host)
        codec = AuthJsonCodec(load_secret_key(cfg.home / gc.secret_key_file))
        payload = {
            "username": user.username,
            "expires": int(time.time() + gc.session_ttl_seconds) * 1000,
            "connections": {
                connection.name: {
                    "protocol": connection.protocol,
                    "parameters": connection.parameters,
                }
            },
        }
        encoded = codec.encode(payload)
        try:
            token = await fetch_session_token(gc.base_url, encoded)
        except GuacamoleUnreachable as e:
            error = str(e)
        except GuacamoleClientError as e:
            error = str(e)
        else:
            cid = build_connection_id(connection.name, source="json")
            public = (gc.public_url or gc.base_url).rstrip("/")
            iframe_url = f"{public}/#/client/{cid}?token={token}"
    except GuacamoleBuildError as e:
        error = str(e)

    # If we built a tunnel but the token exchange failed, release it now.
    # Otherwise hand the lease to the session registry so the browser
    # `pagehide` beacon (or the TTL backstop) can release it.
    if iframe_url is None and tunnel_lease is not None:
        try:
            await tunnel_lease.release()
        except Exception:
            pass
    elif iframe_url is not None and tunnel_lease is not None:
        tunnel_session_id = await get_tunnel_manager().register_session(
            tunnel_lease, ttl_seconds=gc.session_ttl_seconds
        )

    if iframe_url:
        cred_user = host.credential.username if host.credential else None
        rec_events.record_host_open(user.id, host.name, host.protocol, cred_user)

    db.add(
        AuditLog(
            actor_id=user.id,
            action="host.connect" if iframe_url else "host.connect.fail",
            target=f"host:{host.id}",
            details=(
                f"protocol={host.protocol}"
                + (f" jumps={len(chain)}" if chain else "")
                + (f" via_rdgw={rdgw_host_name}" if iframe_url and rdgw_host_name else "")
                + (f" via_tunnel={tunnel_lease.bind_host}:{tunnel_lease.bind_port}"
                   if iframe_url and tunnel_lease else "")
                + (f" error={error[:120]}" if error else "")
            ),
            ip=request.client.host if request.client else None,
        )
    )

    return _templates(request).TemplateResponse(
        request,
        "guacamole/connect.html",
        {
            "host": host,
            "iframe_url": iframe_url,
            "error": error,
            "user": user,
            "jump_chain": chain,
            "tunnel_endpoint": (
                f"{tunnel_lease.bind_host}:{tunnel_lease.bind_port}" if tunnel_lease else None
            ),
            "tunnel_session_id": tunnel_session_id,
            "rdgw_host_name": rdgw_host_name,
        },
    )


@router.post("/hosts/{host_id}/disconnect/{sid}", include_in_schema=False)
async def host_disconnect(
    host_id: int,
    sid: str,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
) -> Response:
    """Release a tunnel lease registered for a Guacamole session.

    Called by the browser via ``navigator.sendBeacon`` on tab close /
    navigation. Idempotent — releasing an unknown sid is a no-op (returns
    204) so a TTL-backstop release that already fired doesn't surface as
    an error if the beacon also arrives.
    """
    released = await get_tunnel_manager().release_session(sid)
    if released:
        host = db.get(Host, host_id)
        if host:
            rec_events.record_host_close(user.id, host.name)
        db.add(
            AuditLog(
                actor_id=user.id,
                action="host.disconnect",
                target=f"host:{host_id}",
                details=f"sid={sid[:8]}…",
                ip=request.client.host if request.client else None,
            )
        )
        db.commit()
    return Response(status_code=204)
