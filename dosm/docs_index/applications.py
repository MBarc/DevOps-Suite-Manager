"""CRUD repo for Folder taxonomy records."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.models import Folder, Tenant


def _default_tenant_id(db: Session) -> int:
    """Resolve the Default tenant id. System-managed doc folders (the CLI
    reference folder, frontmatter-derived folders from the indexer) live in the
    Default tenant - the docs filesystem is shared in the current multi-tenancy
    phase."""
    return int(db.execute(select(Tenant.id).where(Tenant.slug == "default")).scalar_one())


def list_folders(db: Session) -> list[Folder]:
    return list(db.execute(select(Folder).order_by(Folder.name)).scalars())


def get_folder(db: Session, folder_id: int) -> Folder | None:
    return db.get(Folder, folder_id)


def get_folder_by_slug(db: Session, slug: str) -> Folder | None:
    return db.execute(
        select(Folder).where(Folder.slug == slug)
    ).scalar_one_or_none()


def create_folder(
    db: Session, *, name: str, slug: str, description: str | None,
    tenant_id: int | None = None,
) -> Folder:
    """Create a doc taxonomy folder. ``tenant_id`` defaults to the Default
    tenant for system/CLI callers; web callers pass the acting tenant."""
    if tenant_id is None:
        tenant_id = _default_tenant_id(db)
    folder = Folder(name=name.strip(), slug=slug.strip(),
                    description=description or None, tenant_id=tenant_id)
    db.add(folder)
    db.flush()
    return folder


def update_folder(
    db: Session, folder: Folder, *, name: str, description: str | None
) -> Folder:
    folder.name = name.strip()
    folder.description = description or None
    db.flush()
    return folder


def delete_folder(db: Session, folder: Folder) -> None:
    db.delete(folder)
    db.flush()


def doc_count(db: Session, folder_id: int) -> int:
    from sqlalchemy import func

    from dosm.models import Document
    return db.execute(
        select(func.count(Document.id)).where(Document.folder_id == folder_id)
    ).scalar_one()
