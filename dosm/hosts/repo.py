from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from dosm.models import Credential, Host, HostTag, Tag

SUPPORTED_PROTOCOLS = ("ssh", "rdp", "vnc")


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
        .options(selectinload(Host.tags), selectinload(Host.credential))
        .order_by(Host.name)
    )
    return list(db.execute(stmt).scalars())


def get_host(db: Session, host_id: int) -> Host | None:
    return db.get(Host, host_id)


def list_credentials(db: Session) -> list[Credential]:
    return list(db.execute(select(Credential).order_by(Credential.name)).scalars())


def list_tags(db: Session) -> list[Tag]:
    return list(db.execute(select(Tag).order_by(Tag.name)).scalars())


def create_host(
    db: Session,
    *,
    name: str,
    hostname: str,
    port: int,
    protocol: str,
    description: str | None,
    credential_id: int | None,
    tags_csv: str,
    source_module: str | None = None,
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Unsupported protocol: {protocol!r}")
    host = Host(
        name=name,
        hostname=hostname,
        port=port,
        protocol=protocol,
        description=description or None,
        credential_id=credential_id,
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
    tags_csv: str,
) -> Host:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Unsupported protocol: {protocol!r}")
    host.name = name
    host.hostname = hostname
    host.port = port
    host.protocol = protocol
    host.description = description or None
    host.credential_id = credential_id
    # Replace tag set.
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
