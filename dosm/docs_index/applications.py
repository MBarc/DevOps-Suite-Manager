"""CRUD repo for Folder taxonomy records."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from dosm.models import Folder


def list_folders(db: Session) -> list[Folder]:
    return list(db.execute(select(Folder).order_by(Folder.name)).scalars())


def get_folder(db: Session, folder_id: int) -> Folder | None:
    return db.get(Folder, folder_id)


def get_folder_by_slug(db: Session, slug: str) -> Folder | None:
    return db.execute(
        select(Folder).where(Folder.slug == slug)
    ).scalar_one_or_none()


def create_folder(
    db: Session, *, name: str, slug: str, description: str | None
) -> Folder:
    folder = Folder(name=name.strip(), slug=slug.strip(), description=description or None)
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
