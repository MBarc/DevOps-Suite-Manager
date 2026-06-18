from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from dosm.models import Credential, Host, HostTag, Tag

SUPPORTED_PROTOCOLS = ("ssh", "rdp", "vnc")
# File-transfer methods a host can additionally expose (a capability, not the
# host's primary protocol). None/"" = file transfer not configured.
FILE_TRANSFER_METHODS = ("sftp", "ftp", "ftps")
JUMP_CHAIN_MAX_DEPTH = 5


class HostValidationError(ValueError):
    pass


def _normalize_tags(raw: str) -> list[str]:
    return sorted({t.strip() for t in raw.split(",") if t.strip()})


def get_or_create_tag(db: Session, name: str) -> Tag:
    tag = db.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
    if tag is None:
        tag = Tag(name=name)
        db.add(tag)
        db.flush()
    return tag


def list_hosts(db: Session, *, kind: str | None = None, tag: str | None = None) -> list[Host]:
    """List hosts, optionally filtered by role and/or tag."""
    stmt = (
        select(Host)
        .options(
            selectinload(Host.tags),
            selectinload(Host.credential),
            selectinload(Host.jump_host),
        )
        .order_by(Host.name)
    )
    if kind == "jumpboxes":
        stmt = stmt.where(Host.is_jumpbox.is_(True))
    elif kind == "servers":
        stmt = stmt.where(Host.is_jumpbox.is_(False))
    if tag:
        stmt = stmt.where(
            Host.id.in_(
                select(HostTag.host_id)
                .join(Tag, HostTag.tag_id == Tag.id)
                .where(Tag.name == tag)
            )
        )
    return list(db.execute(stmt).scalars())


def count_by_kind(db: Session) -> tuple[int, int]:
    """Return (servers_count, jumpboxes_count)."""
    rows = db.execute(select(Host.is_jumpbox)).scalars().all()
    jumpboxes = sum(1 for v in rows if v)
    servers = len(rows) - jumpboxes
    return servers, jumpboxes


def get_host(db: Session, host_id: int) -> Host | None:
    return db.get(Host, host_id)


def list_credentials(db: Session) -> list[Credential]:
    return list(db.execute(select(Credential).order_by(Credential.name)).scalars())


def list_tags(db: Session) -> list[Tag]:
    stmt = (
        select(Tag)
        .where(Tag.id.in_(select(HostTag.tag_id)))
        .order_by(Tag.name)
    )
    return list(db.execute(stmt).scalars())


def list_jump_candidates(db: Session, exclude_host_id: int | None = None) -> list[Host]:
    """Hosts eligible to act as a jump box: flagged is_jumpbox, not the host
    itself. Protocol isn't filtered - DOSM's tunnel mechanism currently only
    works with SSH hops, but the inventory accepts RDP/VNC jumpboxes for
    operators who model their environment that way (the connect route
    surfaces a clear error if the chain can't be tunneled)."""
    stmt = (
        select(Host)
        .where(Host.is_jumpbox.is_(True))
        .order_by(Host.name)
    )
    if exclude_host_id is not None:
        stmt = stmt.where(Host.id != exclude_host_id)
    return list(db.execute(stmt).scalars())


def _validate_jump(db: Session, host_id: int | None, jump_host_id: int | None) -> None:
    """Reject self-reference, non-SSH jump hosts, and cycles."""
    if jump_host_id is None:
        return
    if host_id is not None and jump_host_id == host_id:
        raise HostValidationError("a host cannot be its own jump host")
    seen: set[int] = set()
    if host_id is not None:
        seen.add(host_id)
    cur_id: int | None = jump_host_id
    depth = 0
    while cur_id is not None:
        if depth > JUMP_CHAIN_MAX_DEPTH:
            raise HostValidationError(
                f"jump chain exceeds max depth {JUMP_CHAIN_MAX_DEPTH}"
            )
        if cur_id in seen:
            raise HostValidationError("jump chain forms a cycle")
        seen.add(cur_id)
        node = db.get(Host, cur_id)
        if node is None:
            raise HostValidationError(f"jump host {cur_id} not found")
        if not node.is_jumpbox:
            raise HostValidationError(
                f"host {node.name!r} is not flagged as a jumpbox"
            )
        cur_id = node.jump_host_id
        depth += 1


def _validate_ft(method: str | None) -> str | None:
    method = (method or "").strip().lower() or None
    if method is not None and method not in FILE_TRANSFER_METHODS:
        raise HostValidationError(f"Unsupported file-transfer method: {method!r}")
    return method


def create_host(
    db: Session,
    *,
    name: str,
    hostname: str,
    port: int,
    protocol: str,
    description: str | None,
    credential_id: int | None,
    jump_host_id: int | None,
    tags_csv: str,
    is_jumpbox: bool = False,
    source_module: str | None = None,
    ft_method: str | None = None,
    ft_port: int | None = None,
    ft_credential_id: int | None = None,
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise HostValidationError(f"Unsupported protocol: {protocol!r}")
    ft_method = _validate_ft(ft_method)
    if ft_method is None:
        ft_port = None
        ft_credential_id = None
    if is_jumpbox:
        jump_host_id = None  # jumpboxes connect directly - no chained jumps
    _validate_jump(db, host_id=None, jump_host_id=jump_host_id)
    host = Host(
        name=name,
        hostname=hostname,
        port=port,
        protocol=protocol,
        description=description or None,
        credential_id=credential_id,
        jump_host_id=jump_host_id,
        is_jumpbox=is_jumpbox,
        source_module=source_module,
        ft_method=ft_method,
        ft_port=ft_port,
        ft_credential_id=ft_credential_id,
    )
    db.add(host)
    db.flush()
    for tag_name in _normalize_tags(tags_csv):
        tag = get_or_create_tag(db, tag_name)
        db.add(HostTag(host_id=host.id, tag_id=tag.id))
    db.flush()
    return host


def update_host(
    db: Session,
    host: Host,
    *,
    name: str,
    hostname: str,
    port: int,
    protocol: str,
    description: str | None,
    credential_id: int | None,
    jump_host_id: int | None,
    tags_csv: str,
    is_jumpbox: bool = False,
    ft_method: str | None = None,
    ft_port: int | None = None,
    ft_credential_id: int | None = None,
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise HostValidationError(f"Unsupported protocol: {protocol!r}")
    ft_method = _validate_ft(ft_method)
    if ft_method is None:
        ft_port = None
        ft_credential_id = None
    if host.is_jumpbox and not is_jumpbox:
        in_use = db.execute(
            select(Host.id).where(Host.jump_host_id == host.id).limit(1)
        ).scalar_one_or_none()
        if in_use is not None:
            raise HostValidationError(
                "cannot unflag - this host is currently used as a jump host by another host"
            )
    if is_jumpbox:
        jump_host_id = None  # jumpboxes connect directly - no chained jumps
    _validate_jump(db, host_id=host.id, jump_host_id=jump_host_id)
    host.name = name
    host.hostname = hostname
    host.port = port
    host.protocol = protocol
    host.description = description or None
    host.credential_id = credential_id
    host.jump_host_id = jump_host_id
    host.is_jumpbox = is_jumpbox
    host.ft_method = ft_method
    host.ft_port = ft_port
    host.ft_credential_id = ft_credential_id
    db.query(HostTag).filter(HostTag.host_id == host.id).delete()
    for tag_name in _normalize_tags(tags_csv):
        tag = get_or_create_tag(db, tag_name)
        db.add(HostTag(host_id=host.id, tag_id=tag.id))
    db.flush()
    return host


def delete_host(db: Session, host: Host) -> None:
    db.delete(host)
    db.flush()


def resolve_jump_chain(db: Session, host: Host) -> list[Host]:
    """Return the chain [outermost_jump, ..., direct_jump] for a host (empty
    if no jump). Validation has already rejected cycles, so this is safe."""
    chain: list[Host] = []
    cur: Host | None = host.jump_host
    while cur is not None:
        chain.append(cur)
        cur = cur.jump_host
    chain.reverse()
    return chain
