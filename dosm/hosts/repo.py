from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from dosm.models import Credential, Host, HostTag, Tag

SUPPORTED_PROTOCOLS = ("ssh", "rdp", "vnc")
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


def list_hosts(db: Session) -> list[Host]:
    stmt = (
        select(Host)
        .options(
            selectinload(Host.tags),
            selectinload(Host.credential),
            selectinload(Host.jump_host),
        )
        .order_by(Host.name)
    )
    return list(db.execute(stmt).scalars())


def get_host(db: Session, host_id: int) -> Host | None:
    return db.get(Host, host_id)


def list_credentials(db: Session) -> list[Credential]:
    return list(db.execute(select(Credential).order_by(Credential.name)).scalars())


def list_tags(db: Session) -> list[Tag]:
    return list(db.execute(select(Tag).order_by(Tag.name)).scalars())


def list_jump_candidates(db: Session, exclude_host_id: int | None = None) -> list[Host]:
    """Hosts eligible to act as a jump box: SSH protocol, not the host itself."""
    stmt = select(Host).where(Host.protocol == "ssh").order_by(Host.name)
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
        if node.protocol != "ssh":
            raise HostValidationError(
                f"jump host {node.name!r} must be SSH protocol (was {node.protocol})"
            )
        cur_id = node.jump_host_id
        depth += 1


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
    source_module: str | None = None,
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise HostValidationError(f"Unsupported protocol: {protocol!r}")
    _validate_jump(db, host_id=None, jump_host_id=jump_host_id)
    host = Host(
        name=name,
        hostname=hostname,
        port=port,
        protocol=protocol,
        description=description or None,
        credential_id=credential_id,
        jump_host_id=jump_host_id,
        source_module=source_module,
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
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise HostValidationError(f"Unsupported protocol: {protocol!r}")
    _validate_jump(db, host_id=host.id, jump_host_id=jump_host_id)
    host.name = name
    host.hostname = hostname
    host.port = port
    host.protocol = protocol
    host.description = description or None
    host.credential_id = credential_id
    host.jump_host_id = jump_host_id
    db.query(HostTag).filter(HostTag.host_id == host.id).delete()
    for tag_name in _normalize_tags(tags_csv):
        tag = get_or_create_tag(db, tag_name)
        db.add(HostTag(host_id=host.id, tag_id=tag.id))
    db.flush()
    return host


def delete_host(db: Session, host: Host) -> None:
    db.query(HostTag).filter(HostTag.host_id == host.id).delete()
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
