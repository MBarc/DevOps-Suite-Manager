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
from dosm.models import AuditLog, Credential, Host, User
from dosm.secrets import SecretNotFound, get_backend

app = typer.Typer(help="DevOps Operations Suite Manager.", no_args_is_help=True, add_completion=False)
db_app = typer.Typer(help="Database admin commands.", no_args_is_help=True)
user_app = typer.Typer(help="Local user management.", no_args_is_help=True)
secret_app = typer.Typer(help="Manage secrets via the configured backend.", no_args_is_help=True)
cred_app = typer.Typer(help="Manage credential records (references into the secrets backend).", no_args_is_help=True)
hosts_app = typer.Typer(help="Manage host inventory entries.", no_args_is_help=True)
docs_app = typer.Typer(help="Documentation index commands.", no_args_is_help=True)
guac_app = typer.Typer(help="Guacamole integration helpers.", no_args_is_help=True)
pipelines_app = typer.Typer(help="Pipeline runner commands.", no_args_is_help=True)
folder_app = typer.Typer(help="Manage doc vault folders (taxonomy).", no_args_is_help=True)
org_app = typer.Typer(help="Organisation directory (AD-backed) commands.", no_args_is_help=True)
ftp_app = typer.Typer(help="File transfer (FTP / FTPS / SFTP), jump-aware.", no_args_is_help=True)
okta_app = typer.Typer(help="Okta SSO helpers.", no_args_is_help=True)
rbac_app = typer.Typer(help="Role-based access control helpers.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(secret_app, name="secret")
app.add_typer(cred_app, name="credential")
app.add_typer(hosts_app, name="hosts")
app.add_typer(docs_app, name="docs")
app.add_typer(guac_app, name="guacamole")
app.add_typer(pipelines_app, name="pipelines")
app.add_typer(folder_app, name="folder")
app.add_typer(org_app, name="org")
app.add_typer(ftp_app, name="ftp")
app.add_typer(okta_app, name="okta")
app.add_typer(rbac_app, name="rbac")

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
    # Seed the DOSM-CLI folder so generated CLI reference docs land in
    # their own folder when the indexer runs, even if the user never
    # explicitly invokes `dosm docs install-cli-reference`.
    from dosm.docs_index.cli_reference import ensure_cli_folder

    with session_scope() as s:
        ensure_cli_folder(s)
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
    if role not in ("admin", "operator", "viewer"):
        console.print(f"[red]Invalid role {role!r}. Use admin | operator | viewer.[/red]")
        raise typer.Exit(1)
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


@user_app.command("set-role")
def user_set_role(
    username: str = typer.Argument(...),
    role: str = typer.Argument(..., help="admin | operator | viewer"),
) -> None:
    """Change a user's role. The only path to change a role after creation."""
    _load()
    if role not in ("admin", "operator", "viewer"):
        console.print(f"[red]Invalid role {role!r}. Use admin | operator | viewer.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None:
            console.print(f"[red]No such user: {username}[/red]")
            raise typer.Exit(1)
        old = u.role
        u.role = role
        s.add(
            AuditLog(
                actor_id=u.id,
                action="user.set_role",
                target=f"user:{u.id}",
                details=f"{old} -> {role} (via CLI)",
            )
        )
    console.print(f"[green]Role updated[/green] {username}: {old} -> {role}")


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


# ---- okta / rbac ----------------------------------------------------------


@okta_app.command("test")
def okta_test() -> None:
    """Validate Okta config: reach the issuer's discovery doc + JWKS."""
    import asyncio

    from dosm.auth import okta as okta_oidc

    cfg = load_config()
    init_engine(cfg)
    okta = cfg.okta
    if not okta.enabled:
        console.print("[yellow]Okta is disabled[/yellow] (okta.enabled: false)")
    if not okta.issuer or not okta.client_id:
        console.print("[red]Missing okta.issuer or okta.client_id in config.yaml[/red]")
        raise typer.Exit(1)

    async def _check() -> tuple[dict, dict]:
        meta = await okta_oidc.fetch_metadata(okta.issuer)
        jwks = await okta_oidc.fetch_jwks(meta["jwks_uri"])
        return meta, jwks

    try:
        meta, jwks = asyncio.run(_check())
    except Exception as e:
        console.print(f"[red]Okta discovery failed:[/red] {e}")
        raise typer.Exit(1)

    try:
        get_backend(cfg).get_str("okta/client_secret")
        secret_state = "[green]present[/green]"
    except SecretNotFound:
        secret_state = "[red]missing[/red] (set with: dosm secret set okta/client_secret)"

    console.print("[green]Okta reachable[/green]")
    console.print(f"  issuer:        {okta.issuer}")
    console.print(f"  authorize:     {meta.get('authorization_endpoint')}")
    console.print(f"  token:         {meta.get('token_endpoint')}")
    console.print(f"  jwks keys:     {len(jwks.get('keys', []))}")
    console.print(f"  client secret: {secret_state}")


@rbac_app.command("show-mapping")
def rbac_show_mapping() -> None:
    """Print the AD/Okta group -> DOSM role mapping."""
    cfg = load_config()
    rbac = cfg.rbac
    table = Table("Group (from Okta claim)", "DOSM role")
    for group, role in sorted(rbac.group_role_map.items()):
        table.add_row(group, role)
    if not rbac.group_role_map:
        console.print("[yellow]No group_role_map configured.[/yellow]")
    else:
        console.print(table)
    if rbac.default_role in ("admin", "operator", "viewer"):
        console.print(f"Unmapped users get: [cyan]{rbac.default_role}[/cyan]")
    else:
        console.print("Unmapped users get: [red]no access[/red] (group membership required)")


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


# ---- hosts ----------------------------------------------------------------


def _get_host_by_name(s, name: str) -> Host:
    """Resolve a host by its unique inventory name, or exit with an error."""
    host = s.execute(select(Host).where(Host.name == name)).scalar_one_or_none()
    if host is None:
        console.print(f"[red]No host named {name!r}.[/red]")
        raise typer.Exit(1)
    return host


@hosts_app.command("list")
def hosts_list() -> None:
    """List host inventory entries."""
    _load()
    from dosm.hosts import repo

    with session_scope() as s:
        rows = [
            (
                h.id,
                h.name,
                h.hostname,
                h.port,
                h.protocol,
                h.credential.name if h.credential else "",
                "yes" if h.is_jumpbox else "",
                h.jump_host.name if h.jump_host else "",
            )
            for h in repo.list_hosts(s)
        ]
    table = Table("ID", "Name", "Hostname", "Port", "Proto", "Credential", "Jumpbox", "Jump via")
    for hid, name, hostname, port, proto, cred, jb, via in rows:
        table.add_row(str(hid), name, hostname, str(port), proto, cred, jb, via)
    console.print(table)


@hosts_app.command("show")
def hosts_show(name: str = typer.Argument(..., help="Host name.")) -> None:
    """Show full details for one host."""
    _load()
    with session_scope() as s:
        h = _get_host_by_name(s, name)
        console.print(f"[bold]{h.name}[/bold] (id={h.id})")
        console.print(f"  hostname  : {h.hostname}")
        console.print(f"  port      : {h.port}")
        console.print(f"  protocol  : {h.protocol}")
        console.print(f"  credential: {h.credential.name if h.credential else '—'}")
        console.print(f"  jumpbox   : {'yes' if h.is_jumpbox else 'no'}")
        console.print(f"  jump via  : {h.jump_host.name if h.jump_host else '—'}")
        if h.ft_method:
            console.print(f"  file xfer : {h.ft_method} (port {h.ft_port or 'default'})")
        if h.description:
            console.print(f"  notes     : {h.description}")
        console.print(f"  updated   : {h.updated_at.isoformat(timespec='seconds')}")


@hosts_app.command("set-hostname")
def hosts_set_hostname(
    name: str = typer.Argument(..., help="Host name."),
    hostname: str = typer.Argument(..., help="New address: hostname, IP, or FQDN."),
) -> None:
    """Update a host's address (e.g. after a DHCP/IP change). Audit-logged."""
    _load()
    new = hostname.strip()
    if not new:
        console.print("[red]Hostname cannot be empty.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        h = _get_host_by_name(s, name)
        old = h.hostname
        if old == new:
            console.print(f"[yellow]No change[/yellow] — {name} already points at {new}.")
            return
        h.hostname = new
        s.add(
            AuditLog(
                actor_id=None,
                action="host.update",
                target=f"host:{h.id}",
                details=f"cli set-hostname {old} -> {new}",
            )
        )
    console.print(f"[green]Updated[/green] {name}: {old} → {new}")


@hosts_app.command("set-port")
def hosts_set_port(
    name: str = typer.Argument(..., help="Host name."),
    port: int = typer.Argument(..., help="New connection port (1-65535)."),
) -> None:
    """Update a host's connection port. Audit-logged."""
    _load()
    if not 1 <= port <= 65535:
        console.print("[red]Port must be between 1 and 65535.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        h = _get_host_by_name(s, name)
        old = h.port
        if old == port:
            console.print(f"[yellow]No change[/yellow] — {name} already uses port {port}.")
            return
        h.port = port
        s.add(
            AuditLog(
                actor_id=None,
                action="host.update",
                target=f"host:{h.id}",
                details=f"cli set-port {old} -> {port}",
            )
        )
    console.print(f"[green]Updated[/green] {name}: port {old} → {port}")


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
    from dosm.docs_index.vault import find_unique_slug, save_doc, slugify

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


@docs_app.command("install-cli-reference")
def docs_install_cli_reference(
    force: bool = typer.Option(
        False, "--force", help="Reinstall even if the version stamp matches."
    ),
) -> None:
    """Copy the bundled CLI reference into $DOSM_HOME/docs/_dosm-cli/.

    Also seeds the DOSM-CLI Folder row so the pages appear in their own
    folder rather than Unfiled. Run after upgrading DOSM to refresh the
    docs the agent retrieves via RAG. Auto-runs on `dosm init`. Files in
    `_dosm-cli/` are owned by DOSM — do not hand-edit them.
    """
    from dosm.docs_index.cli_reference import (
        ensure_cli_folder,
        install_cli_reference,
        is_current,
    )

    _load()
    cfg = load_config()
    if not force and is_current(cfg.docs_dir):
        console.print(f"[yellow]Already current[/yellow]: {cfg.docs_dir / '_dosm-cli'}")
        console.print("Use [bold]--force[/bold] to reinstall.")
        return
    count, target = install_cli_reference(cfg.docs_dir)
    with session_scope() as s:
        ensure_cli_folder(s)
    console.print(f"[green]Installed[/green] {count} file(s) to {target}")
    console.print("[green]Seeded[/green] DOSM-CLI folder")
    console.print("Run [bold]dosm docs reindex[/bold] to make them searchable now")
    console.print("(or just start [bold]dosm serve[/bold] — auto-index picks them up).")


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


# ---- org -----------------------------------------------------------------


@org_app.command("test-ad")
def org_test_ad() -> None:
    """Verify the configured AD jumpbox is reachable and AD cmdlets work."""
    cfg = load_config()
    init_engine(cfg)
    if cfg.directory.ad_jumpbox_host_id is None:
        console.print("[red]AD jumpbox not configured.[/red] Run /org/configure in the UI.")
        raise typer.Exit(code=1)
    from dosm.directory import get_directory_source

    try:
        domain = get_directory_source(cfg).test_connection()
    except Exception as e:
        console.print(f"[red]FAIL[/red] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/green] connected to AD domain {domain!r}")


@org_app.command("sync")
def org_sync(
    slug: str = typer.Argument(..., help="Department slug (URL fragment)."),
) -> None:
    """Run a one-shot sync of a single department from AD."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.directory.sync import sync_department
    from dosm.models import Department

    with session_scope() as db:
        dept = db.execute(_select(Department).where(Department.slug == slug)).scalar_one_or_none()
        if dept is None:
            console.print(f"[red]Department {slug!r} not found.[/red]")
            raise typer.Exit(code=1)
        try:
            summary = sync_department(db, cfg, dept, actor_id=None)
        except Exception as e:
            console.print(f"[red]Sync failed:[/red] {e}")
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] {slug}: +{summary['added']} -{summary['removed']} "
            f"(kept {summary['kept']}), parent_changed={summary['parent_changed']}"
        )


@org_app.command("members")
def org_members(slug: str = typer.Argument(..., help="Department slug.")) -> None:
    """Print the cached member list for a department."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.models import Department, DepartmentMember

    with session_scope() as db:
        dept = db.execute(_select(Department).where(Department.slug == slug)).scalar_one_or_none()
        if dept is None:
            console.print(f"[red]Department {slug!r} not found.[/red]")
            raise typer.Exit(code=1)
        members = list(
            db.execute(
                _select(DepartmentMember)
                .where(DepartmentMember.department_id == dept.id)
                .order_by(DepartmentMember.display_name)
            ).scalars()
        )
    if not members:
        console.print(f"{slug}: no cached members. Run [cyan]dosm org sync {slug}[/cyan].")
        return
    table = Table(title=f"{dept.name} ({len(members)} members)")
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Email")
    table.add_column("Status")
    for m in members:
        table.add_row(
            m.display_name,
            m.title or "—",
            m.email or "—",
            "[green]enabled[/green]" if m.enabled else "[red]disabled[/red]",
        )
    console.print(table)


@org_app.command("tree")
def org_tree() -> None:
    """Print an ASCII tree of the org chart from cached data."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.models import Department

    with session_scope() as db:
        depts = list(db.execute(_select(Department).order_by(Department.name)).scalars())
    by_id = {d.id: d for d in depts}
    children: dict[int | None, list[Department]] = {}
    for d in depts:
        children.setdefault(d.parent_id, []).append(d)
    roots = sorted(children.get(None, []), key=lambda d: d.name)
    if not roots:
        console.print("(no departments)")
        return

    def _walk(d: Department, prefix: str, last: bool) -> None:
        connector = "└─ " if last else "├─ "
        mgr = f" [{d.manager_name}]" if d.manager_name else ""
        console.print(f"{prefix}{connector}{d.name}{mgr}")
        kids = sorted(children.get(d.id, []), key=lambda x: x.name)
        for i, kid in enumerate(kids):
            _walk(kid, prefix + ("   " if last else "│  "), i == len(kids) - 1)

    for i, root in enumerate(roots):
        _walk(root, "", i == len(roots) - 1)
    _ = by_id  # kept for future "depth=" filtering; silences unused warning


@org_app.command("find")
def org_find(query: str = typer.Argument(..., help="Substring to match name/email/title.")) -> None:
    """Search cached people across all departments."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import or_
    from sqlalchemy import select as _select

    from dosm.models import Department, DepartmentMember

    like = f"%{query}%"
    with session_scope() as db:
        rows = db.execute(
            _select(DepartmentMember, Department)
            .join(Department, DepartmentMember.department_id == Department.id)
            .where(
                or_(
                    DepartmentMember.display_name.ilike(like),
                    DepartmentMember.email.ilike(like),
                    DepartmentMember.title.ilike(like),
                )
            )
            .order_by(DepartmentMember.display_name)
            .limit(50)
        ).all()
    if not rows:
        console.print(f"No people match {query!r}.")
        return
    table = Table()
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Email")
    table.add_column("Department")
    for m, d in rows:
        name = m.display_name if m.enabled else f"[strike]{m.display_name}[/strike]"
        table.add_row(name, m.title or "—", m.email or "—", d.name)
    console.print(table)


# ---- ftp / file transfer --------------------------------------------------
def _resolve_ft_host(s, name: str):
    """Resolve a host by name and assert file transfer is configured on it."""
    from dosm.ftp.service import host_has_file_transfer
    from dosm.models import Host

    host = s.execute(select(Host).where(Host.name == name)).scalar_one_or_none()
    if host is None:
        console.print(f"[red]No host named {name!r}.[/red]")
        raise typer.Exit(1)
    if not host_has_file_transfer(host):
        console.print(
            f"[red]Host {name!r} has no file transfer configured "
            f"(set a method on the host).[/red]"
        )
        raise typer.Exit(1)
    return host


@ftp_app.command("ls")
def ftp_ls(
    host: str = typer.Argument(..., help="Inventory host name (ftp/ftps/sftp)."),
    path: str = typer.Argument("", help="Directory, relative to the login home."),
) -> None:
    """List a remote directory."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        h = _resolve_ft_host(s, host)
        backend = get_file_backend(cfg, s, h)
        try:
            entries = asyncio.run(backend.list_dir(path))
        except FileTransferError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    table = Table("Type", "Name", "Size")
    for e in entries:
        table.add_row("dir" if e.is_dir else "file", e.name, "" if e.is_dir else str(e.size or 0))
    console.print(table)


@ftp_app.command("get")
def ftp_get(
    host: str = typer.Argument(..., help="Inventory host name."),
    remote: str = typer.Argument(..., help="Remote file path (home-relative)."),
    out: Path = typer.Option(None, "--out", "-o", help="Local destination (default: basename)."),
) -> None:
    """Download a remote file."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    dest = out or Path(remote.rsplit("/", 1)[-1] or "download")
    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        h = _resolve_ft_host(s, host)
        backend = get_file_backend(cfg, s, h)
        try:
            with open(dest, "wb") as fh:
                n = asyncio.run(backend.retrieve(remote, fh))
        except FileTransferError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]Downloaded[/green] {remote} to {dest} ({n} bytes)")


@ftp_app.command("put")
def ftp_put(
    host: str = typer.Argument(..., help="Inventory host name."),
    local: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local file to upload."),
    dest: str = typer.Option("", "--dest", "-d", help="Remote directory (home-relative)."),
) -> None:
    """Upload a local file."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    remote = f"{dest.strip('/')}/{local.name}" if dest.strip("/") else local.name
    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        h = _resolve_ft_host(s, host)
        backend = get_file_backend(cfg, s, h)
        try:
            with open(local, "rb") as fh:
                n = asyncio.run(backend.store(remote, fh))
        except FileTransferError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]Uploaded[/green] {local} to {remote} ({n} bytes)")


@ftp_app.command("rm")
def ftp_rm(
    host: str = typer.Argument(..., help="Inventory host name."),
    path: str = typer.Argument(..., help="Remote path to remove (home-relative)."),
    is_dir: bool = typer.Option(False, "--dir", help="Remove an (empty) directory instead of a file."),
) -> None:
    """Delete a remote file, or an empty directory with --dir."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        h = _resolve_ft_host(s, host)
        backend = get_file_backend(cfg, s, h)
        try:
            asyncio.run(backend.rmdir(path) if is_dir else backend.delete(path))
        except FileTransferError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]Removed[/green] {path}")


@ftp_app.command("cp")
def ftp_cp(
    src_host: str = typer.Argument(..., help="Source host name."),
    src_path: str = typer.Argument(..., help="Source file path (home-relative)."),
    dst_host: str = typer.Argument(..., help="Destination host name."),
    dest: str = typer.Option("", "--dest", "-d", help="Destination directory (home-relative)."),
    move: bool = typer.Option(False, "--move", "-m", help="Delete the source after copy (move)."),
) -> None:
    """Copy (or --move) a file from one host to another, server-side (jump-aware)."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import transfer_between_hosts

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        sh = _resolve_ft_host(s, src_host)
        dh = _resolve_ft_host(s, dst_host)
        basename = src_path.rsplit("/", 1)[-1]
        dst_path = f"{dest.strip('/')}/{basename}" if dest.strip("/") else basename
        try:
            n = asyncio.run(
                transfer_between_hosts(cfg, s, sh, src_path, dh, dst_path, move=move)
            )
        except FileTransferError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    verb = "Moved" if move else "Copied"
    console.print(
        f"[green]{verb}[/green] {src_host}:{src_path} to {dst_host}:{dst_path} ({n} bytes)"
    )


if __name__ == "__main__":
    app()
