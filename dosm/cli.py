from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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
from dosm.models import (
    DEFAULT_TENANT_SLUG,
    AuditLog,
    Credential,
    Host,
    Tenant,
    User,
)
from dosm.secrets import SecretNotFound, get_backend

app = typer.Typer(help="DevOps Operations Suite Manager.", no_args_is_help=True, add_completion=False)
db_app = typer.Typer(help="Database admin commands.", no_args_is_help=True)
tenant_app = typer.Typer(help="Tenant (workspace) management.", no_args_is_help=True)
user_app = typer.Typer(help="Local user management.", no_args_is_help=True)
secret_app = typer.Typer(help="Manage secrets via the configured backend.", no_args_is_help=True)
cred_app = typer.Typer(help="Manage credential records (references into the secrets backend).", no_args_is_help=True)
hosts_app = typer.Typer(help="Manage host inventory entries.", no_args_is_help=True)
docs_app = typer.Typer(help="Documentation index commands.", no_args_is_help=True)
guac_app = typer.Typer(help="Guacamole integration helpers.", no_args_is_help=True)
pipelines_app = typer.Typer(help="Pipeline runner commands.", no_args_is_help=True)
payload_app = typer.Typer(help="Saved pipeline input payloads.", no_args_is_help=True)
folder_app = typer.Typer(help="Manage doc vault folders (taxonomy).", no_args_is_help=True)
org_app = typer.Typer(help="Organisation directory (AD-backed) commands.", no_args_is_help=True)
ftp_app = typer.Typer(help="File transfer (FTP / FTPS / SFTP), jump-aware.", no_args_is_help=True)
okta_app = typer.Typer(help="Okta SSO helpers.", no_args_is_help=True)
rbac_app = typer.Typer(help="Role-based access control helpers.", no_args_is_help=True)
audit_app = typer.Typer(help="Audit log queries.", no_args_is_help=True)
confluence_app = typer.Typer(help="Confluence space listeners.", no_args_is_help=True)
applications_app = typer.Typer(
    help="Host organisation: application -> environment -> unit tree.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(tenant_app, name="tenant")
app.add_typer(user_app, name="user")
app.add_typer(secret_app, name="secret")
app.add_typer(cred_app, name="credential")
app.add_typer(hosts_app, name="hosts")
app.add_typer(docs_app, name="docs")
app.add_typer(guac_app, name="guacamole")
app.add_typer(pipelines_app, name="pipelines")
pipelines_app.add_typer(payload_app, name="payload")
app.add_typer(folder_app, name="folder")
app.add_typer(org_app, name="org")
app.add_typer(ftp_app, name="ftp")
app.add_typer(okta_app, name="okta")
app.add_typer(rbac_app, name="rbac")
app.add_typer(audit_app, name="audit")
app.add_typer(confluence_app, name="confluence")
app.add_typer(applications_app, name="application")

console = Console()


def _load() -> None:
    """Load config + init DB engine so CLI subcommands can use session_scope."""
    cfg = load_config()
    init_engine(cfg)


# ---- tenant resolution ----------------------------------------------------
#
# The CLI has no web session, so resource commands carry an explicit tenant.
# These helpers turn a ``--tenant SLUG`` option into a tenant id.


def _resolve_tenant(s, slug: str | None) -> int:
    """Return the tenant id for ``slug`` (defaults to the Default tenant when
    ``slug`` is None). Exits with a clear error if the tenant does not exist."""
    wanted = (slug or DEFAULT_TENANT_SLUG).strip()
    tenant = s.execute(
        select(Tenant).where(Tenant.slug == wanted)
    ).scalar_one_or_none()
    if tenant is None:
        console.print(
            f"[red]No tenant with slug {wanted!r}.[/red] "
            f"List tenants with: dosm tenant list"
        )
        raise typer.Exit(1)
    return tenant.id


def _resolve_tenant_scope(s, slug: str | None) -> int | None:
    """Like ``_resolve_tenant`` but ``--tenant all`` yields None (every tenant,
    the platform all-tenants read view). Used by list/read commands that may
    legitimately span tenants."""
    if slug is not None and slug.strip().lower() == "all":
        return None
    return _resolve_tenant(s, slug)


def _slugify_tenant(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "tenant"


def _parse_when(value: str) -> datetime:
    """Parse an audit time bound: the literal ``now`` or an ISO-8601 date /
    datetime (a bare date is treated as 00:00). Naive values are assumed UTC."""
    v = (value or "").strip()
    if v.lower() == "now":
        return datetime.now(UTC)
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Reusable Typer option for the active tenant on resource commands.
_TENANT_OPT = typer.Option(
    None, "--tenant", help="Tenant slug (default: the Default tenant)."
)
_TENANT_SCOPE_OPT = typer.Option(
    None,
    "--tenant",
    help="Tenant slug, or 'all' for every tenant (default: the Default tenant).",
)


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


# ---- tenant ---------------------------------------------------------------


@tenant_app.command("list")
def tenant_list() -> None:
    """List all tenants (workspaces)."""
    _load()
    with session_scope() as s:
        rows = [
            (t.id, t.name, t.slug, "yes" if t.is_active else "no",
             t.created_at.isoformat(timespec="seconds"))
            for t in s.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
        ]
    table = Table("ID", "Name", "Slug", "Active", "Created")
    for tid, name, slug, active, created in rows:
        table.add_row(str(tid), name, slug, active, created)
    console.print(table)


@tenant_app.command("create")
def tenant_create(
    name: str = typer.Argument(..., help="Tenant display name."),
    slug: str | None = typer.Option(None, "--slug", help="URL slug (auto-derived if omitted)."),
    description: str | None = typer.Option(None, "--description"),
) -> None:
    """Create a new tenant. Audit-logged."""
    _load()
    final_slug = (slug or _slugify_tenant(name)).strip()
    with session_scope() as s:
        if s.execute(select(Tenant).where(Tenant.slug == final_slug)).scalar_one_or_none():
            console.print(f"[red]A tenant with slug {final_slug!r} already exists.[/red]")
            raise typer.Exit(1)
        if s.execute(select(Tenant).where(Tenant.name == name)).scalar_one_or_none():
            console.print(f"[red]A tenant named {name!r} already exists.[/red]")
            raise typer.Exit(1)
        tenant = Tenant(name=name, slug=final_slug, description=description or None)
        s.add(tenant)
        s.flush()
        s.add(AuditLog(tenant_id=tenant.id, actor_id=None, action="tenant.create",
                       target=f"tenant:{tenant.id}", details=f"cli name={name} slug={final_slug}"))
    console.print(f"[green]Created tenant[/green] {name!r} (slug={final_slug!r})")


@tenant_app.command("rename")
def tenant_rename(
    slug: str = typer.Argument(..., help="Slug of the tenant to rename."),
    new_name: str = typer.Argument(..., help="New display name."),
) -> None:
    """Rename a tenant (slug is immutable). Audit-logged."""
    _load()
    with session_scope() as s:
        tenant = s.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()
        if tenant is None:
            console.print(f"[red]No tenant with slug {slug!r}.[/red]")
            raise typer.Exit(1)
        clash = s.execute(
            select(Tenant).where(Tenant.name == new_name, Tenant.id != tenant.id)
        ).scalar_one_or_none()
        if clash is not None:
            console.print(f"[red]Another tenant is already named {new_name!r}.[/red]")
            raise typer.Exit(1)
        old = tenant.name
        tenant.name = new_name
        s.add(AuditLog(tenant_id=tenant.id, actor_id=None, action="tenant.rename",
                       target=f"tenant:{tenant.id}", details=f"cli {old} -> {new_name}"))
    console.print(f"[green]Renamed tenant[/green] {slug}: {old} -> {new_name}")


# ---- user -----------------------------------------------------------------


@user_app.command("create")
def user_create(
    username: str = typer.Argument(...),
    role: str = typer.Option("tenant_admin", "--role", help="tenant_admin | operator | viewer"),
    password: str | None = typer.Option(
        None, "--password", help="Password (will prompt if omitted).", show_default=False
    ),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant slug the user belongs to (default: the Default tenant)."
    ),
    platform_admin: bool = typer.Option(
        False, "--platform-admin",
        help="Create a tenant-less platform admin (role=platform_admin, no tenant).",
    ),
) -> None:
    """Create a local user. First user created should be admin.

    Regular users belong to one tenant (``--tenant``, defaulting to Default).
    ``--platform-admin`` creates a tenant-less user who can act across tenants.
    """
    _load()
    if platform_admin:
        role = "platform_admin"
    elif role not in ("tenant_admin", "operator", "viewer"):
        console.print(f"[red]Invalid role {role!r}. Use tenant_admin | operator | viewer.[/red]")
        raise typer.Exit(1)
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    with session_scope() as s:
        existing = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if existing is not None:
            console.print(f"[red]User {username!r} already exists.[/red]")
            raise typer.Exit(1)
        if platform_admin:
            tenant_id: int | None = None
        else:
            tenant_id = _resolve_tenant(s, tenant)
        s.add(User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
        ))
    where = "platform-wide (no tenant)" if platform_admin else f"tenant={tenant or DEFAULT_TENANT_SLUG}"
    console.print(f"[green]Created user[/green] {username} (role={role}, {where})")


@user_app.command("list")
def user_list() -> None:
    _load()
    with session_scope() as s:
        tenant_slugs = {
            t.id: t.slug for t in s.execute(select(Tenant)).scalars().all()
        }
        rows = [
            (u.id, u.username, u.role,
             tenant_slugs.get(u.tenant_id, "-") if u.tenant_id is not None else "(platform)",
             u.is_active, u.created_at)
            for u in s.execute(select(User).order_by(User.username)).scalars().all()
        ]
    table = Table("ID", "Username", "Role", "Tenant", "Active", "Created")
    for uid, uname, role, tslug, active, created in rows:
        table.add_row(str(uid), uname, role, tslug, "yes" if active else "no", created.isoformat(timespec="seconds"))
    console.print(table)


@user_app.command("set-role")
def user_set_role(
    username: str = typer.Argument(...),
    role: str = typer.Argument(..., help="tenant_admin | operator | viewer"),
    lock: bool | None = typer.Option(
        None, "--lock/--unlock",
        help="Pin this role so Okta group changes won't overwrite it (per-user override).",
    ),
) -> None:
    """Change a user's role. The only path to change a role after creation.

    ``--lock`` pins the role: subsequent SSO logins keep it regardless of the
    user's group claims (the per-user permission override). ``--unlock`` lets
    group-derived roles take over again at next login."""
    _load()
    if role not in ("tenant_admin", "operator", "viewer"):
        console.print(f"[red]Invalid role {role!r}. Use tenant_admin | operator | viewer.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None:
            console.print(f"[red]No such user: {username}[/red]")
            raise typer.Exit(1)
        old = u.role
        u.role = role
        if lock is not None:
            u.role_locked = lock
        s.add(
            AuditLog(
                tenant_id=u.tenant_id,
                actor_id=u.id,
                action="user.set_role",
                target=f"user:{u.id}",
                details=f"{old} -> {role}"
                + ("" if lock is None else f" lock={lock}") + " (via CLI)",
            )
        )
    lock_note = "" if lock is None else f" (locked={lock})"
    console.print(f"[green]Role updated[/green] {username}: {old} -> {role}{lock_note}")


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
    """Print the AD/Okta group -> (tenant, role) mapping from the DB."""
    _load()
    from dosm.models import GroupMapping, Tenant
    cfg = load_config()
    with session_scope() as s:
        names = {t.id: t.name for t in s.execute(select(Tenant)).scalars()}
        rows = list(s.execute(
            select(GroupMapping).order_by(GroupMapping.group_name)
        ).scalars())
        table = Table("Group (from Okta claim)", "Tenant", "DOSM role")
        for m in rows:
            table.add_row(m.group_name, names.get(m.tenant_id, "?"), m.role)
    if not rows:
        console.print("[yellow]No group mappings configured.[/yellow]")
    else:
        console.print(table)
    if cfg.rbac.default_role in ("tenant_admin", "operator", "viewer"):
        console.print(
            f"Unmapped users get: [cyan]{cfg.rbac.default_role}[/cyan] (Default tenant)"
        )
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
    kind: str = typer.Option(..., "--kind", help="login | ssh_key | pat | azure_sp | aws_keys | gcp_sa"),
    username: str | None = typer.Option(None, "--username"),
    password: str | None = typer.Option(
        None, "--password", help="Secret value (password / token / key) to store in the secrets backend."
    ),
    secret_ref: str | None = typer.Option(
        None, "--secret-ref", help="Secrets-backend path (auto-generated from the name if omitted)."
    ),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Create a credential profile. With --password the secret value is stored in
    the secrets backend; otherwise only the row + secret_ref path are recorded."""
    import re as _re

    _load()
    cfg = load_config()
    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        if s.execute(
            select(Credential).where(Credential.name == name, Credential.tenant_id == tid)
        ).scalar_one_or_none():
            console.print(f"[red]Credential {name!r} already exists in this tenant.[/red]")
            raise typer.Exit(1)
        tenant_obj = s.get(Tenant, tid)
        tslug = tenant_obj.slug if tenant_obj else str(tid)
        ref = (secret_ref or "").strip() or (
            f"t/{tslug}/credentials/{_re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')}"
        )
        s.add(Credential(tenant_id=tid, name=name, kind=kind, username=username, secret_ref=ref))
        s.add(AuditLog(tenant_id=tid, actor_id=None, action="credential.create",
                       target=f"credential:{name}", details=f"cli kind={kind}"))
    # Store the secret value AFTER the row commits (SQLite single-writer ordering).
    if password:
        from dosm.secrets import get_backend
        get_backend(cfg).set_str(ref, password)
        console.print(f"[green]Created credential[/green] {name} (secret stored at {ref})")
    else:
        console.print(
            f"[green]Created credential[/green] {name} "
            f"(no secret stored; set one later, secret_ref={ref})"
        )


@cred_app.command("list")
def credential_list(tenant: str | None = _TENANT_SCOPE_OPT) -> None:
    _load()
    from dosm.hosts import repo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        rows = [
            (c.id, c.name, c.kind, c.username, c.secret_ref)
            for c in repo.list_credentials(s, tid)
        ]
    table = Table("ID", "Name", "Kind", "Username", "Secret ref")
    for cid, name, kind, username, secret_ref in rows:
        table.add_row(str(cid), name, kind, username or "", secret_ref)
    console.print(table)


# ---- hosts ----------------------------------------------------------------


def _get_host_by_name(s, name: str, tid: int | None = None) -> Host:
    """Resolve a host by its inventory name (unique per tenant), or exit with
    an error. Scoped to tenant ``tid`` when given (None = any tenant)."""
    stmt = select(Host).where(Host.name == name)
    if tid is not None:
        stmt = stmt.where(Host.tenant_id == tid)
    host = s.execute(stmt).scalar_one_or_none()
    if host is None:
        console.print(f"[red]No host named {name!r}.[/red]")
        raise typer.Exit(1)
    return host


@hosts_app.command("add")
def host_add(
    name: str = typer.Argument(..., help="Inventory name (unique per tenant)."),
    hostname: str = typer.Option(..., "--hostname", help="DNS name or IP, e.g. herupa.local"),
    protocol: str = typer.Option("ssh", "--protocol", help="ssh | rdp | vnc"),
    port: int | None = typer.Option(None, "--port", help="Defaults: ssh 22, rdp 3389, vnc 5900."),
    credential: str | None = typer.Option(None, "--credential", help="Credential profile name to attach."),
    org_unit: str | None = typer.Option(None, "--org-unit", help="Org node id or 'App/Env/Unit' path."),
    ft_method: str | None = typer.Option(None, "--ft-method", help="sftp | ftp | ftps (enables file transfer)."),
    ft_port: int | None = typer.Option(None, "--ft-port"),
    ft_credential: str | None = typer.Option(
        None, "--ft-credential", help="Credential name for file transfer (defaults to the host credential)."
    ),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Add a host to the inventory, optionally with a credential, org placement,
    and file-transfer settings. Audit-logged."""
    _load()
    from dosm.hosts import repo

    default_ports = {"ssh": 22, "rdp": 3389, "vnc": 5900}
    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)

        def _cred_id(cname: str | None) -> int | None:
            if not cname:
                return None
            c = s.execute(
                select(Credential).where(Credential.name == cname, Credential.tenant_id == tid)
            ).scalar_one_or_none()
            if c is None:
                console.print(f"[red]No credential named {cname!r} in this tenant.[/red]")
                raise typer.Exit(1)
            return c.id

        org_id: int | None = None
        if org_unit and org_unit.strip().lower() != "none":
            u = _resolve_org_unit(s, org_unit, tid)
            if u is None:
                console.print(f"[red]Org node not found:[/red] {org_unit!r}")
                raise typer.Exit(1)
            org_id = u.id
        try:
            h = repo.create_host(
                s, tenant_id=tid, name=name, hostname=hostname,
                port=port or default_ports.get(protocol, 22), protocol=protocol,
                description=None, credential_id=_cred_id(credential),
                jump_host_id=None, tags_csv="",
                ft_method=ft_method, ft_port=ft_port,
                ft_credential_id=_cred_id(ft_credential), org_unit_id=org_id,
            )
        except repo.HostValidationError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=tid, actor_id=None, action="host.create",
                       target=f"host:{h.id}", details=f"cli {protocol} {hostname}"))
        hid = h.id
    console.print(f"[green]Created host[/green] {name} (id={hid})")


@hosts_app.command("list")
def hosts_list(tenant: str | None = _TENANT_SCOPE_OPT) -> None:
    """List host inventory entries."""
    _load()
    from dosm.hosts import repo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
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
            for h in repo.list_hosts(s, tid=tid)
        ]
    table = Table("ID", "Name", "Hostname", "Port", "Proto", "Credential", "Jumpbox", "Jump via")
    for hid, name, hostname, port, proto, cred, jb, via in rows:
        table.add_row(str(hid), name, hostname, str(port), proto, cred, jb, via)
    console.print(table)


@hosts_app.command("show")
def hosts_show(
    name: str = typer.Argument(..., help="Host name."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Show full details for one host."""
    _load()
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _get_host_by_name(s, name, tid)
        console.print(f"[bold]{h.name}[/bold] (id={h.id})")
        console.print(f"  hostname  : {h.hostname}")
        console.print(f"  port      : {h.port}")
        console.print(f"  protocol  : {h.protocol}")
        console.print(f"  credential: {h.credential.name if h.credential else '-'}")
        console.print(f"  jumpbox   : {'yes' if h.is_jumpbox else 'no'}")
        console.print(f"  jump via  : {h.jump_host.name if h.jump_host else '-'}")
        if h.ft_method:
            console.print(f"  file xfer : {h.ft_method} (port {h.ft_port or 'default'})")
        if h.description:
            console.print(f"  notes     : {h.description}")
        console.print(f"  updated   : {h.updated_at.isoformat(timespec='seconds')}")


@hosts_app.command("set-hostname")
def hosts_set_hostname(
    name: str = typer.Argument(..., help="Host name."),
    hostname: str = typer.Argument(..., help="New address: hostname, IP, or FQDN."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Update a host's address (e.g. after a DHCP/IP change). Audit-logged."""
    _load()
    new = hostname.strip()
    if not new:
        console.print("[red]Hostname cannot be empty.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _get_host_by_name(s, name, tid)
        old = h.hostname
        if old == new:
            console.print(f"[yellow]No change[/yellow] - {name} already points at {new}.")
            return
        h.hostname = new
        s.add(
            AuditLog(
                tenant_id=h.tenant_id,
                actor_id=None,
                action="host.update",
                target=f"host:{h.id}",
                details=f"cli set-hostname {old} -> {new}",
            )
        )
    console.print(f"[green]Updated[/green] {name}: {old} -> {new}")


@hosts_app.command("set-port")
def hosts_set_port(
    name: str = typer.Argument(..., help="Host name."),
    port: int = typer.Argument(..., help="New connection port (1-65535)."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Update a host's connection port. Audit-logged."""
    _load()
    if not 1 <= port <= 65535:
        console.print("[red]Port must be between 1 and 65535.[/red]")
        raise typer.Exit(1)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _get_host_by_name(s, name, tid)
        old = h.port
        if old == port:
            console.print(f"[yellow]No change[/yellow] - {name} already uses port {port}.")
            return
        h.port = port
        s.add(
            AuditLog(
                tenant_id=h.tenant_id,
                actor_id=None,
                action="host.update",
                target=f"host:{h.id}",
                details=f"cli set-port {old} -> {port}",
            )
        )
    console.print(f"[green]Updated[/green] {name}: port {old} -> {port}")


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
    from dosm.docs_index.store import LocalDocsStore, make_docs_store
    from dosm.docs_index.vault import find_unique_slug, save_doc, slugify

    store = make_docs_store(cfg)
    slug = find_unique_slug(store, app_slug, slugify(title))
    # Write initial file so the editor has something to open.
    rel = save_doc(store, folder_slug=app_slug, doc_slug=slug, title=title, body_md=f"# {title}\n\n", author="cli")
    if isinstance(store, LocalDocsStore):
        saved = store.root / rel
        editor = (
            __import__("os").environ.get("EDITOR")
            or __import__("os").environ.get("VISUAL")
            or ("notepad.exe" if sys.platform == "win32" else "vi")
        )
        subprocess.call([editor, str(saved)])
    else:
        console.print(f"Saved to {store.label}; edit it from the Docs web UI (no local file to open).")
    console.print(f"[green]Saved[/green] {rel}")
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

    from dosm.docs_index.store import make_docs_store
    store = make_docs_store(cfg)
    slug = vault.find_unique_slug(store, app_slug, vault.slugify(doc_title))
    rel = vault.save_doc(store, folder_slug=app_slug, doc_slug=slug, title=doc_title, body_md=body_md, author="cli")
    console.print(f"[green]Imported[/green] {rel}")
    console.print("Run [bold]dosm docs reindex[/bold] to update the search index.")


@docs_app.command("reindex")
def docs_reindex(
    force: bool = typer.Option(False, "--force", help="Re-embed every file even if unchanged."),
) -> None:
    """Scan $DOSM_HOME/docs, chunk + embed, update the index. Runs synchronously."""
    _load()
    cfg = load_config()
    from dosm.docs_index.store import make_docs_store
    console.print(f"[green]Reindexing[/green] {make_docs_store(cfg).label} (force={force})")
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
    `_dosm-cli/` are owned by DOSM - do not hand-edit them.
    """
    from dosm.docs_index.cli_reference import (
        ensure_cli_folder,
        install_cli_reference,
        is_current,
    )
    from dosm.docs_index.store import make_docs_store

    _load()
    cfg = load_config()
    store = make_docs_store(cfg)
    if not force and is_current(store):
        console.print(f"[yellow]Already current[/yellow] on {store.label}")
        console.print("Use [bold]--force[/bold] to reinstall.")
        return
    count, target = install_cli_reference(store)
    with session_scope() as s:
        ensure_cli_folder(s)
    console.print(f"[green]Installed[/green] {count} file(s) to {target}")
    console.print("[green]Seeded[/green] DOSM-CLI folder")
    console.print("Run [bold]dosm docs reindex[/bold] to make them searchable now")
    console.print("(or just start [bold]dosm serve[/bold] - auto-index picks them up).")


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


@docs_app.command("test-source")
def docs_test_source() -> None:
    """Check that the configured docs source (local or SMB) is reachable."""
    _load()
    cfg = load_config()
    from dosm.docs_index.store import probe_source

    ok, message, sample = probe_source(cfg)
    console.print(f"source: [bold]{cfg.docs_index.source}[/bold]")
    if ok:
        console.print(f"[green]reachable[/green]: {message}")
        if sample:
            console.print("sample files:")
            for rel in sample:
                console.print(f"  {rel}")
    else:
        console.print(f"[red]unreachable[/red]: {message}")
        raise typer.Exit(1)


@docs_app.command("migrate-to-smb")
def docs_migrate_to_smb(
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be copied without writing."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite files already on the share."),
    no_reindex: bool = typer.Option(False, "--no-reindex", help="Skip the reindex after copying."),
) -> None:
    """Copy local docs ($DOSM_HOME/docs) to the configured SMB share.

    Configure and test the SMB source first (Settings → Docs source, or
    `dosm docs test-source`). Idempotent - existing files on the share are
    skipped unless --overwrite.
    """
    _load()
    cfg = load_config()
    from dosm.docs_index.store import (
        LocalDocsStore,
        last_store_error,
        make_docs_store,
        migrate_docs,
        store_fell_back,
    )

    if cfg.docs_index.source != "smb":
        console.print("[red]docs_index.source is not 'smb'[/red] - configure the SMB source first.")
        raise typer.Exit(1)
    dst = make_docs_store(cfg)
    if store_fell_back(cfg, dst):
        console.print(f"[red]SMB source unavailable:[/red] {last_store_error()}")
        raise typer.Exit(1)
    src = LocalDocsStore(cfg.docs_dir)
    if not src.exists():
        console.print("[yellow]No local docs to migrate.[/yellow]")
        return

    console.print(f"Migrating {src.label} -> {dst.label}{' [dry-run]' if dry_run else ''}")
    result = migrate_docs(src, dst, dry_run=dry_run, overwrite=overwrite)
    console.print(
        f"copied={len(result.copied)} skipped={len(result.skipped)} errors={len(result.errors)}"
    )
    for rel, err in result.errors[:20]:
        console.print(f"[red]error[/red] {rel}: {err}")

    if not dry_run and not no_reindex and not result.errors:
        console.print("[green]Reindexing[/green] from the SMB source (force)...")
        stats = reindex(cfg, force=True)
        console.print(f"done · {stats.indexed} indexed · {stats.errors} errors")
    if result.errors:
        raise typer.Exit(1)


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


# ---- applications (host organisation tree) --------------------------------


def _resolve_org_unit(s, ref: str, tid: int | None):
    """Resolve an org unit by integer id or by ``App/Env/Unit`` path within
    tenant ``tid`` (None = any tenant)."""
    from dosm.applications import repo as org_repo

    ref = (ref or "").strip()
    if ref.isdigit():
        return org_repo.get_unit(s, int(ref), tid)
    return org_repo.get_by_path(s, ref, tid)


@applications_app.command("tree")
def application_tree(tenant: str | None = _TENANT_SCOPE_OPT) -> None:
    """Print the application -> environment -> unit tree with host counts."""
    _load()
    from dosm.applications import repo as org_repo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        tree = org_repo.build_tree(s, tid)
        if not tree:
            console.print("[yellow]No applications defined yet.[/yellow] "
                          "Add one with: dosm application add NAME")
            return

        def emit(node: dict, depth: int) -> None:
            pad = "  " * depth
            u = node["unit"]
            console.print(
                f"{pad}[bold]{u.name}[/bold] "
                f"[dim]({u.tier}, id={u.id})[/dim] "
                f"- {node['total']} host{'' if node['total'] == 1 else 's'}"
            )
            for child in node["children"]:
                emit(child, depth + 1)

        for app_node in tree:
            emit(app_node, 0)


@applications_app.command("add")
def application_add(
    name: str = typer.Argument(..., help="Name of the new node."),
    tier: str | None = typer.Option(
        None, "--tier", help="application | environment | unit. Inferred from --parent if omitted."
    ),
    parent: str | None = typer.Option(
        None, "--parent", help="Parent node, by id or 'App/Env' path. Omit for an application."
    ),
    description: str | None = typer.Option(None, "--description"),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Add an application, environment, or unit. Audit-logged."""
    _load()
    from dosm.applications import repo as org_repo

    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        parent_id: int | None = None
        parent_unit = None
        if parent:
            parent_unit = _resolve_org_unit(s, parent, tid)
            if parent_unit is None:
                console.print(f"[red]Parent not found:[/red] {parent!r}")
                raise typer.Exit(1)
            parent_id = parent_unit.id
        # Infer tier from the parent when not given explicitly.
        if tier is None:
            if parent_unit is None:
                tier = "application"
            else:
                tier = org_repo.CHILD_TIER.get(parent_unit.tier)
                if tier is None:
                    console.print(f"[red]Cannot add a child under a {parent_unit.tier}.[/red]")
                    raise typer.Exit(1)
        try:
            u = org_repo.create_unit(
                s, tenant_id=tid, name=name, tier=tier, parent_id=parent_id, description=description
            )
        except org_repo.OrgValidationError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=tid, actor_id=None, action="orgunit.create", target=f"orgunit:{u.id}",
                       details=f"cli tier={u.tier} name={u.name} parent={u.parent_id}"))
        console.print(f"[green]Created[/green] {u.tier} {u.name!r} (id={u.id})")


@applications_app.command("rename")
def application_rename(
    ref: str = typer.Argument(..., help="Node id or 'App/Env/Unit' path."),
    name: str = typer.Argument(..., help="New name."),
    description: str | None = typer.Option(None, "--description"),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Rename a node (and optionally set its description). Audit-logged."""
    _load()
    from dosm.applications import repo as org_repo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        u = _resolve_org_unit(s, ref, tid)
        if u is None:
            console.print(f"[red]Not found:[/red] {ref!r}")
            raise typer.Exit(1)
        try:
            org_repo.update_unit(s, u, name=name, description=description)
        except org_repo.OrgValidationError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=u.tenant_id, actor_id=None, action="orgunit.update", target=f"orgunit:{u.id}",
                       details=f"cli name={u.name}"))
        console.print(f"[green]Renamed[/green] -> {u.name!r} (id={u.id})")


@applications_app.command("rm")
def application_rm(
    ref: str = typer.Argument(..., help="Node id or 'App/Env/Unit' path."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Delete a node and its subtree. Hosts become unassigned. Audit-logged."""
    _load()
    from dosm.applications import repo as org_repo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        u = _resolve_org_unit(s, ref, tid)
        if u is None:
            console.print(f"[red]Not found:[/red] {ref!r}")
            raise typer.Exit(1)
        descendants = len(org_repo.subtree_ids(s, u)) - 1
        label = f"{u.tier} {u.name!r}"
        unit_tid = u.tenant_id
        if not yes:
            extra = f" and {descendants} descendant node(s)" if descendants else ""
            if not typer.confirm(f"Delete {label}{extra}? Hosts will be unassigned."):
                raise typer.Exit(0)
        uid = u.id
        org_repo.delete_unit(s, u)
        s.add(AuditLog(tenant_id=unit_tid, actor_id=None, action="orgunit.delete", target=f"orgunit:{uid}",
                       details=f"cli {label} cascade={descendants}"))
        console.print(f"[green]Deleted[/green] {label}")


@applications_app.command("assign")
def application_assign(
    host: str = typer.Argument(..., help="Host name."),
    to: str | None = typer.Option(
        None, "--to", help="Org node id or 'App/Env/Unit' path. Omit or 'none' to unassign."
    ),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Assign (or unassign) a host's place in the organisation tree. Audit-logged."""
    _load()
    from dosm.applications import repo as org_repo

    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        h = _get_host_by_name(s, host, tid)
        target_id: int | None = None
        label = "unassigned"
        if to and to.strip().lower() != "none":
            u = _resolve_org_unit(s, to, tid)
            if u is None:
                console.print(f"[red]Org node not found:[/red] {to!r}")
                raise typer.Exit(1)
            target_id = u.id
            label = u.path_str
        try:
            org_repo.assign_host(s, h, target_id)
        except org_repo.OrgValidationError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=h.tenant_id, actor_id=None, action="host.update", target=f"host:{h.id}",
                       details=f"cli org-assign -> {label}"))
        console.print(f"[green]Assigned[/green] {h.name} -> {label}")


# ---- pipelines ------------------------------------------------------------


@pipelines_app.command("add")
def pipeline_add(
    name: str = typer.Argument(..., help="Unique pipeline name (per tenant)."),
    provider: str = typer.Option("github_actions", "--provider", help="Pipeline provider."),
    config_json: str | None = typer.Option(
        None, "--config",
        help='Provider config as JSON, e.g. {"owner":"acme","repo":"app","workflow":"ci.yml","ref":"main"}',
    ),
    description: str | None = typer.Option(None, "--description"),
    credential: str | None = typer.Option(None, "--credential", help="Credential profile name."),
    org_unit: str | None = typer.Option(None, "--org-unit", help="Org node id or 'App/Env/Unit' path."),
    visibility: str = typer.Option("shared", "--visibility", help="shared | private"),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Register a pipeline, optionally filed into the org tree. Audit-logged."""
    _load()
    from dosm.pipelines import repo as pipe_repo

    cfg_dict = json.loads(config_json) if config_json else {}
    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        cred_id: int | None = None
        if credential:
            c = s.execute(
                select(Credential).where(Credential.name == credential, Credential.tenant_id == tid)
            ).scalar_one_or_none()
            if c is None:
                console.print(f"[red]No credential named {credential!r} in this tenant.[/red]")
                raise typer.Exit(1)
            cred_id = c.id
        org_id: int | None = None
        if org_unit and org_unit.strip().lower() != "none":
            u = _resolve_org_unit(s, org_unit, tid)
            if u is None:
                console.print(f"[red]Org node not found:[/red] {org_unit!r}")
                raise typer.Exit(1)
            org_id = u.id
        try:
            p = pipe_repo.create_pipeline(
                s, tenant_id=tid, name=name, provider=provider,
                description=description, config=cfg_dict, inputs_schema=None,
                credential_id=cred_id, org_unit_id=org_id, owner_id=None,
                visibility=visibility,
            )
        except Exception as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=tid, actor_id=None, action="pipeline.create",
                       target=f"pipeline:{p.id}", details=f"cli provider={provider}"))
        pid = p.id
    console.print(f"[green]Created pipeline[/green] {name} (id={pid})")


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


def _resolve_pipeline_or_exit(s, pipeline: str, tid: int | None):
    from dosm.pipelines import repo as prepo

    p = prepo.get_pipeline_by_name(s, pipeline, tid)
    if p is None:
        console.print(f"[red]No pipeline named {pipeline!r}[/red]")
        raise typer.Exit(1)
    return p


def _resolve_payload_or_exit(s, pipeline_id: int, name: str):
    from sqlalchemy import select

    from dosm.models import PipelinePayload

    pl = s.execute(
        select(PipelinePayload).where(
            PipelinePayload.pipeline_id == pipeline_id, PipelinePayload.name == name
        )
    ).scalar_one_or_none()
    if pl is None:
        console.print(f"[red]No payload named {name!r} on that pipeline[/red]")
        raise typer.Exit(1)
    return pl


@payload_app.command("list")
def payload_list(
    pipeline: str = typer.Argument(..., help="Pipeline name."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """List saved payloads for a pipeline."""
    _load()
    from dosm.pipelines import repo as prepo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        rows = [(pl.name, pl.visibility, pl.description or "") for pl in prepo.list_payloads(s, p.id)]
    table = Table("Name", "Visibility", "Description")
    for name, vis, desc in rows:
        table.add_row(name, vis, desc)
    console.print(table)


@payload_app.command("show")
def payload_show(
    pipeline: str = typer.Argument(...),
    name: str = typer.Argument(...),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Print a payload's stored input values as JSON."""
    _load()
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        pl = _resolve_payload_or_exit(s, p.id, name)
        console.print_json(pl.values_json or "{}")


@payload_app.command("add")
def payload_add(
    pipeline: str = typer.Argument(...),
    name: str = typer.Argument(...),
    values_json: str = typer.Option("{}", "--json", help='Input values as a JSON object, e.g. \'{"env":"prod"}\''),
    description: str | None = typer.Option(None, "--description"),
    private: bool = typer.Option(False, "--private", help="Keep this payload to yourself."),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Create a saved payload from a JSON object of input values."""
    _load()
    from dosm.pipelines import repo as prepo
    from dosm.pipelines.inputs import normalize_schema, validate_payload_values

    try:
        values = json.loads(values_json)
        if not isinstance(values, dict):
            raise ValueError("--json must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        console.print(f"[red]Invalid --json:[/red] {e}")
        raise typer.Exit(1)

    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        schema = normalize_schema(json.loads(p.inputs_schema)) if p.inputs_schema else []
        problems = validate_payload_values(schema, values) if schema else []
        if problems:
            console.print("[red]Payload does not match the pipeline schema:[/red]")
            for prob in problems:
                console.print(f"  - {prob}")
            raise typer.Exit(1)
        try:
            pl = prepo.create_payload(
                s, pipeline_id=p.id, name=name, values=values,
                description=description, visibility="private" if private else "shared",
            )
        except (prepo.PayloadNameConflict, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        s.add(AuditLog(tenant_id=p.tenant_id, action="payload.create", target=f"pipeline:{p.id}",
                       details=f"payload={pl.id} name={pl.name!r} (via CLI)"))
    console.print(f"[green]Created payload[/green] {name!r} on {pipeline!r}")


@payload_app.command("rename")
def payload_rename(
    pipeline: str = typer.Argument(...),
    name: str = typer.Argument(..., help="Current name."),
    new_name: str = typer.Argument(..., help="New name."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Rename a payload."""
    _load()
    from dosm.pipelines import repo as prepo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        pl = _resolve_payload_or_exit(s, p.id, name)
        try:
            prepo.update_payload(s, pl, name=new_name)
        except (prepo.PayloadNameConflict, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]Renamed[/green] {name!r} -> {new_name!r}")


@payload_app.command("copy")
def payload_copy(
    pipeline: str = typer.Argument(...),
    name: str = typer.Argument(...),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Duplicate a payload under a derived name."""
    _load()
    from dosm.pipelines import repo as prepo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        pl = _resolve_payload_or_exit(s, p.id, name)
        new = prepo.copy_payload(s, pl)
        console.print(f"[green]Copied[/green] to {new.name!r}")


@payload_app.command("rm")
def payload_rm(
    pipeline: str = typer.Argument(...),
    name: str = typer.Argument(...),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Delete a payload."""
    _load()
    from dosm.pipelines import repo as prepo

    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        p = _resolve_pipeline_or_exit(s, pipeline, tid)
        pl = _resolve_payload_or_exit(s, p.id, name)
        prepo.delete_payload(s, pl)
    console.print(f"[green]Deleted payload[/green] {name!r}")


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
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Run a one-shot sync of a single department from AD."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.auth.tenancy import tenant_clause
    from dosm.directory.sync import sync_department
    from dosm.models import Department

    with session_scope() as db:
        tid = _resolve_tenant_scope(db, tenant)
        _stmt = _select(Department).where(Department.slug == slug)
        _c = tenant_clause(Department, tid)
        if _c is not None:
            _stmt = _stmt.where(_c)
        dept = db.execute(_stmt).scalar_one_or_none()
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
def org_members(
    slug: str = typer.Argument(..., help="Department slug."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Print the cached member list for a department."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.auth.tenancy import tenant_clause
    from dosm.models import Department, DepartmentMember

    with session_scope() as db:
        tid = _resolve_tenant_scope(db, tenant)
        _stmt = _select(Department).where(Department.slug == slug)
        _c = tenant_clause(Department, tid)
        if _c is not None:
            _stmt = _stmt.where(_c)
        dept = db.execute(_stmt).scalar_one_or_none()
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
            m.title or "-",
            m.email or "-",
            "[green]enabled[/green]" if m.enabled else "[red]disabled[/red]",
        )
    console.print(table)


@org_app.command("tree")
def org_tree(tenant: str | None = _TENANT_SCOPE_OPT) -> None:
    """Print an ASCII tree of the org chart from cached data."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import select as _select

    from dosm.auth.tenancy import tenant_clause
    from dosm.models import Department

    with session_scope() as db:
        tid = _resolve_tenant_scope(db, tenant)
        _stmt = _select(Department).order_by(Department.name)
        _c = tenant_clause(Department, tid)
        if _c is not None:
            _stmt = _stmt.where(_c)
        depts = list(db.execute(_stmt).scalars())
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
def org_find(
    query: str = typer.Argument(..., help="Substring to match name/email/title."),
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Search cached people across all departments."""
    cfg = load_config()
    init_engine(cfg)
    from sqlalchemy import or_
    from sqlalchemy import select as _select

    from dosm.auth.tenancy import tenant_clause
    from dosm.models import Department, DepartmentMember

    like = f"%{query}%"
    with session_scope() as db:
        tid = _resolve_tenant_scope(db, tenant)
        _stmt = (
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
        )
        _c = tenant_clause(Department, tid)
        if _c is not None:
            _stmt = _stmt.where(_c)
        rows = db.execute(_stmt).all()
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
        table.add_row(name, m.title or "-", m.email or "-", d.name)
    console.print(table)


# ---- ftp / file transfer --------------------------------------------------
def _resolve_ft_host(s, name: str, tid: int | None = None):
    """Resolve a host by name (scoped to tenant ``tid`` when given) and assert
    file transfer is configured on it."""
    from dosm.ftp.service import host_has_file_transfer
    from dosm.models import Host

    stmt = select(Host).where(Host.name == name)
    if tid is not None:
        stmt = stmt.where(Host.tenant_id == tid)
    host = s.execute(stmt).scalar_one_or_none()
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
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """List a remote directory."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _resolve_ft_host(s, host, tid)
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
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Download a remote file."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    dest = out or Path(remote.rsplit("/", 1)[-1] or "download")
    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _resolve_ft_host(s, host, tid)
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
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Upload a local file."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    remote = f"{dest.strip('/')}/{local.name}" if dest.strip("/") else local.name
    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _resolve_ft_host(s, host, tid)
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
    tenant: str | None = _TENANT_SCOPE_OPT,
) -> None:
    """Delete a remote file, or an empty directory with --dir."""
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import get_file_backend

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        h = _resolve_ft_host(s, host, tid)
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
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Copy (or --move) a file from one host to another, server-side (jump-aware).

    Both hosts must live in the same tenant (default: the Default tenant).
    """
    import asyncio

    from dosm.ftp.base import FileTransferError
    from dosm.ftp.service import transfer_between_hosts

    cfg = load_config()
    init_engine(cfg)
    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        sh = _resolve_ft_host(s, src_host, tid)
        dh = _resolve_ft_host(s, dst_host, tid)
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


# ---- audit ----


@audit_app.command("list")
def audit_list(
    start: str | None = typer.Option(
        None, "--start",
        help="Start of range: ISO 8601 date/datetime (e.g. 2026-06-01 or "
             "2026-06-01T09:00). Omit for no lower bound."),
    end: str = typer.Option(
        "now", "--end", help="End of range: ISO 8601, or the literal 'now' (default)."),
    action: str | None = typer.Option(
        None, "--action", help="Filter by exact action (e.g. host.connect)."),
    actor: str | None = typer.Option(
        None, "--user", help="Filter by actor username."),
    tenant: str | None = _TENANT_SCOPE_OPT,
    fmt: str = typer.Option(
        "table", "--format", help="Output format: table | csv | json."),
    limit: int = typer.Option(
        0, "--limit", help="Max rows, newest first (0 = no limit)."),
) -> None:
    """Pull audit-log entries within a time range, newest first.

    The end of the range may be the literal 'now'. Scope to one tenant with
    --tenant <slug>, or '--tenant all' for every tenant (the default is the
    Default tenant). Use --format csv|json to export."""
    import csv as _csv
    import sys

    from dosm.auth.tenancy import tenant_clause

    fmt = (fmt or "table").strip().lower()
    if fmt not in ("table", "csv", "json"):
        console.print(f"[red]Unknown --format {fmt!r}; use table, csv, or json.[/red]")
        raise typer.Exit(1)
    try:
        start_dt = _parse_when(start) if start else None
        end_dt = _parse_when(end)
    except ValueError as exc:
        console.print(f"[red]Bad date: {exc}[/red]")
        raise typer.Exit(1)
    if start_dt is not None and start_dt > end_dt:
        console.print("[red]--start is after --end.[/red]")
        raise typer.Exit(1)

    _load()
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        stmt = select(AuditLog).where(AuditLog.ts <= end_dt)
        if start_dt is not None:
            stmt = stmt.where(AuditLog.ts >= start_dt)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actor:
            u = s.execute(select(User).where(User.username == actor)).scalars().first()
            if u is None:
                console.print(f"[red]No such user {actor!r}.[/red]")
                raise typer.Exit(1)
            stmt = stmt.where(AuditLog.actor_id == u.id)
        clause = tenant_clause(AuditLog, tid)
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = stmt.order_by(AuditLog.ts.desc())
        if limit and limit > 0:
            stmt = stmt.limit(limit)
        rows = list(s.execute(stmt).scalars())
        user_names = {u.id: (u.display_name or u.username)
                      for u in s.execute(select(User)).scalars()}
        tenant_names = {t.id: t.name for t in s.execute(select(Tenant)).scalars()}
        records = [{
            "ts": a.ts.isoformat(),
            "action": a.action,
            "actor_id": a.actor_id,
            "actor": user_names.get(a.actor_id),
            "tenant_id": a.tenant_id,
            "tenant": tenant_names.get(a.tenant_id),
            "target": a.target,
            "details": a.details,
            "ip": a.ip,
        } for a in rows]

    if fmt == "json":
        print(json.dumps(records, indent=2, default=str))
        return
    if fmt == "csv":
        cols = ["ts", "action", "actor_id", "actor", "tenant_id", "tenant",
                "target", "details", "ip"]
        writer = _csv.DictWriter(sys.stdout, fieldnames=cols)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
        return
    if not records:
        console.print("[yellow]No audit events in range.[/yellow]")
        return
    table = Table("Time (UTC)", "Action", "User", "Tenant", "Target", "Details")
    for rec in records:
        table.add_row(
            rec["ts"], rec["action"],
            rec["actor"] or ("-" if rec["actor_id"] is None else f"user:{rec['actor_id']}"),
            rec["tenant"] or ("-" if rec["tenant_id"] is None else f"tenant:{rec['tenant_id']}"),
            rec["target"] or "", rec["details"] or "",
        )
    console.print(table)
    console.print(f"[dim]{len(records)} event(s).[/dim]")


# ---- confluence listeners -------------------------------------------------


@confluence_app.command("list")
def confluence_list(tenant: str | None = _TENANT_SCOPE_OPT) -> None:
    """List Confluence listeners."""
    from dosm.confluence import repo

    _load()
    with session_scope() as s:
        tid = _resolve_tenant_scope(s, tenant)
        rows = repo.list_listeners(s, tid)
        table = Table("ID", "Name", "Space", "Deployment", "Enabled", "Last sync", "Status")
        for li in rows:
            table.add_row(
                str(li.id), li.name, li.space_key, li.deployment,
                "yes" if li.enabled else "no",
                li.last_synced_at.strftime("%Y-%m-%d %H:%M") if li.last_synced_at else "-",
                li.last_status or "-",
            )
    console.print(table)


@confluence_app.command("add")
def confluence_add(
    name: str = typer.Option(..., "--name", help="Listener name."),
    deployment: str = typer.Option(..., "--deployment", help="cloud | server"),
    base_url: str = typer.Option(..., "--base-url", help="Confluence base URL."),
    space: str = typer.Option(..., "--space", help="Confluence space key."),
    credential: str = typer.Option(..., "--credential", help="Credential name (login or pat) in the tenant."),
    no_pages: bool = typer.Option(False, "--no-pages", help="Do not sync page bodies."),
    no_attachments: bool = typer.Option(False, "--no-attachments", help="Do not sync attachments."),
    tenant: str | None = _TENANT_OPT,
) -> None:
    """Add a Confluence listener for one space."""
    from dosm.confluence import DEPLOYMENTS
    from dosm.docs_index.vault import slugify
    from dosm.models import ConfluenceListener

    if deployment not in DEPLOYMENTS:
        console.print(f"[red]--deployment must be one of: {', '.join(DEPLOYMENTS)}[/red]")
        raise typer.Exit(1)
    _load()
    with session_scope() as s:
        tid = _resolve_tenant(s, tenant)
        cred = s.execute(
            select(Credential).where(Credential.tenant_id == tid, Credential.name == credential)
        ).scalar_one_or_none()
        if cred is None:
            console.print(f"[red]No credential named {credential!r} in that tenant.[/red]")
            raise typer.Exit(1)
        listener = ConfluenceListener(
            tenant_id=tid, name=name, deployment=deployment, base_url=base_url,
            space_key=space, slug=slugify(name), credential_id=cred.id,
            sync_pages=not no_pages, sync_attachments=not no_attachments, enabled=True,
        )
        s.add(listener)
        s.flush()
        s.add(AuditLog(
            tenant_id=tid, actor_id=None, action="settings.confluence.create",
            target=f"confluence_listener:{listener.id}",
            details=f"cli {deployment} {space} ({name})",
        ))
        new_id = listener.id
    console.print(f"[green]Added[/green] listener {new_id} for space {space}")


@confluence_app.command("rm")
def confluence_rm(
    listener_id: int = typer.Argument(..., help="Listener id."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Delete a Confluence listener (synced docs are left in place)."""
    from dosm.confluence import repo

    _load()
    with session_scope() as s:
        row = repo.get_listener(s, listener_id, None)
        if row is None:
            console.print("[red]No such listener.[/red]")
            raise typer.Exit(1)
        if not yes:
            typer.confirm(f"Delete listener {row.name!r} (space {row.space_key})?", abort=True)
        tid = row.tenant_id
        s.delete(row)
        s.add(AuditLog(
            tenant_id=tid, actor_id=None, action="settings.confluence.delete",
            target=f"confluence_listener:{listener_id}", details="cli",
        ))
    console.print(f"[green]Deleted[/green] listener {listener_id}")


@confluence_app.command("test")
def confluence_test(listener_id: int = typer.Argument(..., help="Listener id.")) -> None:
    """Probe a listener's Confluence connection."""
    import asyncio

    from dosm.confluence import make_confluence_client, repo

    _load()
    cfg = load_config()
    with session_scope() as s:
        row = repo.get_listener(s, listener_id, None)
        if row is None:
            console.print("[red]No such listener.[/red]")
            raise typer.Exit(1)
    client = make_confluence_client(cfg, row)  # row detached; attrs already loaded
    ok, message = asyncio.run(client.test_connection())
    if ok:
        console.print(f"[green]OK[/green] {message}")
    else:
        console.print(f"[red]FAIL[/red] {message}")
        raise typer.Exit(1)


@confluence_app.command("sync")
def confluence_sync(listener_id: int = typer.Argument(..., help="Listener id.")) -> None:
    """Sync a listener now (pull pages/attachments + reindex)."""
    import asyncio

    from dosm.confluence import repo
    from dosm.confluence.sync import sync_listener

    _load()
    cfg = load_config()
    with session_scope() as s:
        row = repo.get_listener(s, listener_id, None)
        if row is None:
            console.print("[red]No such listener.[/red]")
            raise typer.Exit(1)
        result = asyncio.run(sync_listener(cfg, row, s))
        s.add(AuditLog(
            tenant_id=row.tenant_id, actor_id=None, action="settings.confluence.sync",
            target=f"confluence_listener:{row.id}",
            details=(
                f"cli pages={result.pages_written} attachments={result.attachments_written} "
                f"deleted={result.deleted} errors={len(result.errors)}"
            ),
        ))
    console.print(
        f"[green]Synced[/green] pages={result.pages_written} "
        f"attachments={result.attachments_written} removed={result.deleted} "
        f"unchanged={result.unchanged}"
    )
    if result.errors:
        console.print(f"[yellow]{len(result.errors)} error(s):[/yellow] {result.errors[0]}")


if __name__ == "__main__":
    app()
