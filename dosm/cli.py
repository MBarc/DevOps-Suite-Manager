from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from dosm import __version__
from dosm.auth.passwords import hash_password
from dosm.bootstrap import initialize_home
from dosm.config import load_config
from dosm.db import create_all, init_engine, session_scope
from dosm.docs_index.indexer import get_index_status, reindex
from dosm.guacamole.auth_json import KEY_BYTES, load_secret_key
from dosm.models import Credential, User
from dosm.secrets import SecretNotFound, get_backend

app = typer.Typer(help="DevOps Operations Suite Manager.", no_args_is_help=True, add_completion=False)
db_app = typer.Typer(help="Database admin commands.", no_args_is_help=True)
user_app = typer.Typer(help="Local user management.", no_args_is_help=True)
secret_app = typer.Typer(help="Manage secrets via the configured backend.", no_args_is_help=True)
cred_app = typer.Typer(help="Manage credential records (references into the secrets backend).", no_args_is_help=True)
docs_app = typer.Typer(help="Documentation index commands.", no_args_is_help=True)
guac_app = typer.Typer(help="Guacamole integration helpers.", no_args_is_help=True)
pipelines_app = typer.Typer(help="Pipeline runner commands.", no_args_is_help=True)
folder_app = typer.Typer(help="Manage doc vault folders (taxonomy).", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(secret_app, name="secret")
app.add_typer(cred_app, name="credential")
app.add_typer(docs_app, name="docs")
app.add_typer(guac_app, name="guacamole")
app.add_typer(pipelines_app, name="pipelines")
app.add_typer(folder_app, name="folder")

console = Console()


def _load() -> None:
    """Load config + init DB engine so CLI subcommands can use session_scope."""
    cfg = load_config()
    init_engine(cfg)


# ---- top-level ------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the installed DOSM version."""
    console.print(f"dosm {__version__}")


@app.command()
def init(
    home: Path = typer.Argument(..., help="Path to create as $DOSM_HOME."),
    force: bool = typer.Option(False, "--force", help="Overwrite config.yaml and README."),
) -> None:
    """Create a new DOSM_HOME directory with the standard layout and default config."""
    created = initialize_home(home, force=force)
    home_resolved = home.resolve()
    if created:
        console.print(f"[green]Initialized[/green] {home_resolved}")
        for p in created:
            rel = p.relative_to(home_resolved) if p != home_resolved else p
            console.print(f"  + {rel}")
    else:
        console.print(f"[yellow]Nothing to do[/yellow] at {home_resolved} (already initialized)")
    console.print(
        f"\nNext:\n  export DOSM_HOME={home_resolved}\n  dosm db init\n  dosm user create admin\n  dosm serve"
    )


@app.command()
def serve(
    home: Path | None = typer.Option(None, "--home", help="Override $DOSM_HOME."),
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)."),
) -> None:
    """Start the DOSM web app."""
    if home is not None:
        os.environ["DOSM_HOME"] = str(home.expanduser().resolve())
    cfg = load_config()
    bind_host = host or cfg.server.host
    bind_port = port or cfg.server.port
    console.print(f"[green]Starting DOSM[/green] on http://{bind_host}:{bind_port}")
    console.print(f"  DOSM_HOME = {cfg.home}")
    uvicorn.run("dosm.main:create_app", factory=True, host=bind_host, port=bind_port, reload=reload)


# ---- db -------------------------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Create all tables in SQLite. Safe to re-run."""
    cfg = load_config()
    create_all(cfg)
    console.print(f"[green]Schema ready[/green] at {cfg.db_path}")


# ---- user -----------------------------------------------------------------


@user_app.command("create")
def user_create(
    username: str = typer.Argument(...),
    role: str = typer.Option("admin", "--role", help="admin | operator | viewer"),
    password: str | None = typer.Option(
        None, "--password", help="Password (will prompt if omitted).", show_default=False
    ),
) -> None:
    """Create a local user. First user created should be admin."""
    _load()
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    with session_scope() as s:
        existing = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if existing is not None:
            console.print(f"[red]User {username!r} already exists.[/red]")
            raise typer.Exit(1)
        s.add(User(username=username, password_hash=hash_password(password), role=role))
    console.print(f"[green]Created user[/green] {username} (role={role})")


@user_app.command("list")
def user_list() -> None:
    _load()
    with session_scope() as s:
        rows = [
            (u.id, u.username, u.role, u.is_active, u.created_at)
            for u in s.execute(select(User).order_by(User.username)).scalars().all()
        ]
    table = Table("ID", "Username", "Role", "Active", "Created")
    for uid, uname, role, active, created in rows:
        table.add_row(str(uid), uname, role, "yes" if active else "no", created.isoformat(timespec="seconds"))
    console.print(table)


@user_app.command("passwd")
def user_passwd(username: str = typer.Argument(...)) -> None:
    """Reset a user's password."""
    _load()
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    with session_scope() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None:
            console.print(f"[red]No such user: {username}[/red]")
            raise typer.Exit(1)
        u.password_hash = hash_password(password)
    console.print(f"[green]Password updated[/green] for {username}")


# ---- secret ---------------------------------------------------------------


@secret_app.command("set")
def secret_set(
    path: str = typer.Argument(..., help="e.g. ssh/prod/admin"),
    value: str | None = typer.Option(None, "--value", help="Value (will prompt if omitted)."),
) -> None:
    _load()
    if value is None:
        value = typer.prompt("Value", hide_input=True, confirmation_prompt=True)
    get_backend().set_str(path, value)
    console.print(f"[green]Wrote[/green] {path}")


@secret_app.command("get")
def secret_get(path: str = typer.Argument(...)) -> None:
    _load()
    try:
        console.print(get_backend().get_str(path))
    except SecretNotFound:
        console.print(f"[red]Not found: {path}[/red]")
        raise typer.Exit(1)


@secret_app.command("list")
def secret_list(prefix: str = typer.Argument("")) -> None:
    _load()
    for path in get_backend().list(prefix):
        console.print(path)


@secret_app.command("delete")
def secret_delete(path: str = typer.Argument(...)) -> None:
    _load()
    try:
        get_backend().delete(path)
    except SecretNotFound:
        console.print(f"[red]Not found: {path}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Deleted[/green] {path}")


# ---- credential -----------------------------------------------------------


@cred_app.command("add")
def credential_add(
    name: str = typer.Argument(..., help="Unique friendly name, e.g. 'prod-admin'"),
    kind: str = typer.Option(..., "--kind", help="ssh_password | ssh_key | rdp_password | api_token"),
    username: str | None = typer.Option(None, "--username"),
    secret_ref: str = typer.Option(..., "--secret-ref", help="Path in the secrets backend."),
) -> None:
    _load()
    with session_scope() as s:
        if s.execute(select(Credential).where(Credential.name == name)).scalar_one_or_none():
            console.print(f"[red]Credential {name!r} already exists.[/red]")
            raise typer.Exit(1)
        s.add(Credential(name=name, kind=kind, username=username, secret_ref=secret_ref))
    console.print(f"[green]Created credential[/green] {name}")


@cred_app.command("list")
def credential_list() -> None:
    _load()
    with session_scope() as s:
        rows = [
            (c.id, c.name, c.kind, c.username, c.secret_ref)
            for c in s.execute(select(Credential).order_by(Credential.name)).scalars().all()
        ]
    table = Table("ID", "Name", "Kind", "Username", "Secret ref")
    for cid, name, kind, username, secret_ref in rows:
        table.add_row(str(cid), name, kind, username or "", secret_ref)
    console.print(table)


# ---- docs -----------------------------------------------------------------


@docs_app.command("new")
def docs_new(
    title: str = typer.Argument(..., help="Document title."),
    app_slug: str = typer.Option("_unfiled", "--app", help="Folder slug."),
) -> None:
    """Scaffold a new vault doc and open it in $EDITOR."""
    import subprocess
    import sys

    cfg = load_config()
    init_engine(cfg)
    from dosm.docs_index.vault import find_unique_slug, save_doc, slugify, UNFILED_SLUG

    slug = find_unique_slug(cfg.docs_dir / app_slug, slugify(title))
    # Write initial file so the editor has something to open.
    saved = save_doc(cfg, folder_slug=app_slug, doc_slug=slug, title=title, body_md=f"# {title}\n\n", author="cli")
    editor = (
        __import__("os").environ.get("EDITOR")
        or __import__("os").environ.get("VISUAL")
        or ("notepad.exe" if sys.platform == "win32" else "vi")
    )
    subprocess.call([editor, str(saved)])
    console.print(f"[green]Saved[/green] {saved.relative_to(cfg.docs_dir)}")
    console.print("Run [bold]dosm docs reindex[/bold] to update the search index.")


@docs_app.command("import")
def docs_import(
    source: str = typer.Argument(..., help="Path to .docx, .pdf, .md, or .txt file."),
    app_slug: str = typer.Option("_unfiled", "--app", help="Folder slug."),
    title: str | None = typer.Option(None, "--title", help="Override title (defaults to filename)."),
) -> None:
    """Convert and import a document into the vault."""
    from pathlib import Path as _Path

    cfg = load_config()
    init_engine(cfg)
    from dosm.docs_index import vault

    src = _Path(source).expanduser().resolve()
    if not src.exists():
        console.print(f"[red]File not found:[/red] {src}")
        raise typer.Exit(1)

    suffix = src.suffix.lower()
    raw = src.read_bytes()
    doc_title = title or src.stem

    try:
        if suffix == ".docx":
            body_md, warnings = vault.import_docx(raw)
            if warnings:
                console.print(f"[yellow]Warnings:[/yellow] {warnings}")
        elif suffix == ".pdf":
            body_md = vault.import_pdf(raw)
        elif suffix in {".md", ".markdown", ".txt"}:
            body_md = raw.decode("utf-8", errors="replace")
        else:
            console.print(f"[red]Unsupported file type:[/red] {suffix}")
            raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Conversion failed:[/red] {e}")
        raise typer.Exit(1)

    slug = vault.find_unique_slug(cfg.docs_dir / app_slug, vault.slugify(doc_title))
    saved = vault.save_doc(cfg, folder_slug=app_slug, doc_slug=slug, title=doc_title, body_md=body_md, author="cli")
    console.print(f"[green]Imported[/green] {saved.relative_to(cfg.docs_dir)}")
    console.print("Run [bold]dosm docs reindex[/bold] to update the search index.")


@docs_app.command("reindex")
def docs_reindex(
    force: bool = typer.Option(False, "--force", help="Re-embed every file even if unchanged."),
) -> None:
    """Scan $DOSM_HOME/docs, chunk + embed, update the index. Runs synchronously."""
    _load()
    cfg = load_config()
    console.print(f"[green]Reindexing[/green] {cfg.docs_dir} (force={force})")
    stats = reindex(cfg, force=force)
    console.print(
        f"done · {stats.indexed} indexed · {stats.skipped_unchanged} unchanged · "
        f"{stats.errors} errors · embedder={stats.embedder_name}"
    )
    if stats.last_error:
        console.print(f"[red]Last error:[/red] {stats.last_error}")


@docs_app.command("status")
def docs_status() -> None:
    _load()
    s = get_index_status()
    console.print(f"running: {s.running}")
    console.print(f"embedder: {s.embedder_name}")
    console.print(
        f"files={s.total_files} processed={s.processed} indexed={s.indexed} "
        f"unchanged={s.skipped_unchanged} errors={s.errors}"
    )
    if s.started_at:
        console.print(f"started_at: {s.started_at.isoformat(timespec='seconds')}")
    if s.finished_at:
        console.print(f"finished_at: {s.finished_at.isoformat(timespec='seconds')}")
    if s.last_error:
        console.print(f"[red]last_error:[/red] {s.last_error}")


# ---- guacamole ------------------------------------------------------------


@guac_app.command("keygen")
def guac_keygen(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing key file."),
) -> None:
    """Generate the 128-bit shared secret used by guacamole-auth-json.

    Writes hex-encoded bytes to $DOSM_HOME/<guacamole.secret_key_file>. Paste
    the same hex value into Guacamole's `guacamole.properties` as
    `json-secret-key`.
    """
    cfg = load_config()
    path = cfg.home / cfg.guacamole.secret_key_file
    if path.exists() and not force:
        console.print(f"[yellow]Already exists[/yellow]: {path}")
        console.print(f"hex: {path.read_text().strip()}")
        raise typer.Exit(0)
    if path.exists():
        path.unlink()
    key = load_secret_key(path, create_if_missing=True)
    console.print(f"[green]Wrote[/green] {path}")
    console.print(f"hex ({KEY_BYTES} bytes): {key.hex()}")
    console.print(
        "\nPaste this hex value into Guacamole's guacamole.properties as:"
    )
    console.print(f"  json-secret-key: {key.hex()}")


# ---- folder ---------------------------------------------------------------


@folder_app.command("list")
def folder_list() -> None:
    """List all doc vault folders."""
    _load()
    from dosm.docs_index import applications as folder_repo

    with session_scope() as s:
        folders = folder_repo.list_folders(s)
        rows = [(f.id, f.name, f.slug, f.description or "", folder_repo.doc_count(s, f.id)) for f in folders]

    table = Table("ID", "Name", "Slug", "Description", "Docs")
    for fid, name, slug, desc, cnt in rows:
        table.add_row(str(fid), name, slug, desc, str(cnt))
    console.print(table)


@folder_app.command("create")
def folder_create(
    name: str = typer.Argument(..., help="Folder name."),
    slug: str | None = typer.Option(None, "--slug", help="URL slug (auto-derived if omitted)."),
    description: str | None = typer.Option(None, "--description"),
) -> None:
    """Create a new doc vault folder."""
    _load()
    from dosm.docs_index import applications as folder_repo
    from dosm.docs_index.vault import slugify

    final_slug = slug or slugify(name)
    with session_scope() as s:
        folder_repo.create_folder(s, name=name, slug=final_slug, description=description)
    console.print(f"[green]Created folder[/green] {name!r} (slug={final_slug!r})")


@folder_app.command("delete")
def folder_delete(slug: str = typer.Argument(...)) -> None:
    """Delete a folder. Attached docs become unfiled."""
    _load()
    from dosm.docs_index import applications as folder_repo

    with session_scope() as s:
        folder = folder_repo.get_folder_by_slug(s, slug)
        if folder is None:
            console.print(f"[red]No folder with slug {slug!r}[/red]")
            raise typer.Exit(1)
        confirm = typer.confirm(f"Delete {folder.name!r}? Attached docs will become unfiled.")
        if not confirm:
            raise typer.Exit(0)
        folder_repo.delete_folder(s, folder)
    console.print(f"[green]Deleted[/green] folder {slug!r}")


# ---- pipelines ------------------------------------------------------------


@pipelines_app.command("poll")
def pipelines_poll() -> None:
    """Run one background-poller tick synchronously and print stats.

    Useful for smoke-testing the poller without leaving `dosm serve` running.
    """
    import asyncio

    cfg = load_config()
    init_engine(cfg)
    from dosm.pipelines.poller import poll_tick

    stats = asyncio.run(poll_tick(cfg))
    console.print(
        f"polled={stats.polled} transitioned={stats.transitioned} "
        f"abandoned={stats.abandoned} errors={stats.errors}"
    )


if __name__ == "__main__":
    app()
