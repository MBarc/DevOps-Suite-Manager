"""Read-only query tools the agent can call without plan-card approval.

Each QuerySpec has an async runner that returns a QueryResult.  The LLM
emits <query>{"tool": "...", "args": {...}}</query> blocks; the stream
handler executes them, injects the results, and lets the LLM continue.
No human approval is required - these are read-only.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class QueryResult:
    ok: bool
    summary: str
    data: str | None = None
    error: str | None = None

    def to_llm_text(self) -> str:
        if not self.ok:
            return f"ERROR: {self.error or self.summary}"
        if self.data is None:
            return self.summary
        return self.data


_OPENAI_TYPE: dict[str, str] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "secret": "string",
    "textarea": "string",
    "object": "object",
}


@dataclass
class QuerySpec:
    name: str
    description: str
    args_schema: list[dict]
    runner: Callable[..., Awaitable[QueryResult]]

    def to_openai_schema(self) -> dict:
        properties = {
            a["name"]: {
                "type": _OPENAI_TYPE.get(a["type"], "string"),
                "description": a.get("description", ""),
            }
            for a in self.args_schema
        }
        required = [a["name"] for a in self.args_schema if a.get("required")]
        schema: dict = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


_QUERY_REGISTRY: dict[str, QuerySpec] = {}


def register_query(spec: QuerySpec) -> None:
    _QUERY_REGISTRY[spec.name] = spec


def list_queries() -> list[QuerySpec]:
    return list(_QUERY_REGISTRY.values())


def get_query(name: str) -> QuerySpec | None:
    return _QUERY_REGISTRY.get(name)


def query_tools() -> list[dict]:
    """Return all registered query tools as OpenAI-compatible tool schemas."""
    return [spec.to_openai_schema() for spec in list_queries()]


# ---------------------------------------------------------------------------
# list_hosts
# ---------------------------------------------------------------------------

async def _list_hosts_runner(cfg, args: dict) -> QueryResult:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from dosm.db import session_scope
    from dosm.models import Host

    filt = (args.get("filter") or "").lower().strip()
    with session_scope() as s:
        hosts = list(
            s.execute(
                select(Host).options(selectinload(Host.tags)).order_by(Host.name)
            ).scalars()
        )
        if filt:
            hosts = [
                h for h in hosts
                if filt in h.name.lower()
                or filt in h.hostname.lower()
                or filt in (h.protocol or "").lower()
                or any(filt in (t.name or "").lower() for t in h.tags)
            ]
        if not hosts:
            return QueryResult(ok=True, summary="No hosts found", data="No hosts found.")
        total = len(hosts)
        lines = [
            f"{h.name} ({h.hostname}:{h.port} {h.protocol})"
            + (" [jumpbox]" if h.is_jumpbox else "")
            + (f" tags={','.join(t.name for t in h.tags)}" if h.tags else "")
            for h in hosts[:60]
        ]
    suffix = f"\n(showing first 60 of {total})" if total > 60 else ""
    return QueryResult(ok=True, summary=f"{total} host(s)", data="\n".join(lines) + suffix)


LIST_HOSTS = QuerySpec(
    name="list_hosts",
    description=(
        "Read the host inventory from the database. "
        "Returns name, address, protocol, and tags. "
        "No SSH or RDP connections are made - this is a pure database read."
    ),
    args_schema=[
        {"name": "filter", "type": "string", "required": False, "description": "Substring to filter by name/hostname/protocol/tag."},
    ],
    runner=_list_hosts_runner,
)
register_query(LIST_HOSTS)


# ---------------------------------------------------------------------------
# host_metrics
# ---------------------------------------------------------------------------

async def _host_metrics_runner(cfg, args: dict) -> QueryResult:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from dosm.db import session_scope
    from dosm.metrics.sources import MetricsError, make_source_for_host
    from dosm.models import Host

    host_name = (args.get("host") or "").strip()
    if not host_name:
        return QueryResult(ok=False, summary="host argument is required", error="host argument is required")

    with session_scope() as s:
        host = s.execute(
            select(Host)
            .where(Host.name == host_name)
            .options(selectinload(Host.credential))
        ).scalar_one_or_none()
        if host is None:
            return QueryResult(ok=False, summary=f"host {host_name!r} not found", error=f"No host named {host_name!r}")
        try:
            source = await make_source_for_host(cfg, host)
            snap = await source.snapshot()
            await source.aclose()
        except MetricsError as e:
            return QueryResult(ok=False, summary=f"Metrics unavailable: {e}", error=str(e))
        except Exception as e:
            return QueryResult(ok=False, summary=f"Metrics error: {type(e).__name__}: {e}", error=str(e))

    lines = []
    cpu = snap.get("cpu_percent")
    if cpu is not None:
        lines.append(f"CPU: {cpu}%")
    mem_used = snap.get("memory_used_gb")
    mem_total = snap.get("memory_total_gb")
    mem_pct = snap.get("memory_percent")
    if mem_used is not None and mem_total is not None:
        lines.append(f"Memory: {mem_used}GB / {mem_total}GB ({mem_pct}%)")
    for disk in (snap.get("disks") or [])[:4]:
        lines.append(f"Disk {disk.get('mountpoint','?')}: {disk.get('used_gb')}GB / {disk.get('total_gb')}GB ({disk.get('percent')}%)")
    load = snap.get("load_avg_1m")
    if load is not None:
        lines.append(f"Load (1m): {load}")
    if not lines:
        lines.append(str(snap))
    return QueryResult(ok=True, summary=f"Metrics for {host_name}", data="\n".join(lines))


HOST_METRICS = QuerySpec(
    name="host_metrics",
    description="Fetch current CPU, memory, and disk usage for a host via SSH or WinRM.",
    args_schema=[
        {"name": "host", "type": "string", "required": True, "description": "Exact host name from the inventory."},
    ],
    runner=_host_metrics_runner,
)
register_query(HOST_METRICS)


# ---------------------------------------------------------------------------
# query_monitoring
# ---------------------------------------------------------------------------

async def _query_monitoring_runner(cfg, args: dict) -> QueryResult:
    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.models import MonitoringSource
    from dosm.monitoring.adapters import make_adapter
    from dosm.secrets import get_backend

    hostname = (args.get("host") or "").strip()

    with session_scope() as s:
        sources = list(
            s.execute(select(MonitoringSource).where(MonitoringSource.enabled.is_(True))).scalars()
        )
        if not sources:
            return QueryResult(ok=True, summary="No monitoring sources configured", data="No enabled monitoring sources configured.")

        if not hostname:
            lines = [f"{src.name} ({src.tool}): enabled" for src in sources]
            return QueryResult(ok=True, summary=f"{len(sources)} source(s)", data="\n".join(lines))

        backend = get_backend(cfg)
        results: list[str] = []
        for source in sources:
            try:
                token = backend.get_str(source.token_secret) if source.token_secret else ""
            except Exception:
                token = ""
            try:
                token2 = backend.get_str(source.token2_secret) if source.token2_secret else ""
            except Exception:
                token2 = ""

            adapter = make_adapter(source, token, token2)
            if adapter is None:
                continue
            try:
                result = await adapter.check_host(hostname)
                if result.error:
                    results.append(f"{result.source_name}: error={result.error}")
                elif result.found:
                    results.append(
                        f"{result.source_name}: FOUND entity={result.entity_name or 'N/A'}"
                        + (f" url={result.entity_url}" if result.entity_url else "")
                    )
                else:
                    results.append(f"{result.source_name}: not found in {result.tool}")
            except Exception as e:
                results.append(f"{source.name}: error={type(e).__name__}: {e}")

    if not results:
        return QueryResult(ok=True, summary="No adapters available", data="No monitoring adapters could run.")
    return QueryResult(ok=True, summary=f"{len(results)} source(s) checked", data="\n".join(results))


QUERY_MONITORING = QuerySpec(
    name="query_monitoring",
    description="Check monitoring status for a host across all enabled sources (Dynatrace, Datadog, etc.). Without 'host', lists configured sources.",
    args_schema=[
        {"name": "host", "type": "string", "required": False, "description": "Hostname to check. Omit to list all sources."},
    ],
    runner=_query_monitoring_runner,
)
register_query(QUERY_MONITORING)


# ---------------------------------------------------------------------------
# cert_check
# ---------------------------------------------------------------------------

async def _cert_check_runner(cfg, args: dict) -> QueryResult:
    from dosm.certs.routes import peek_cached

    host_filter = (args.get("host") or "").lower().strip()
    try:
        expires_days = int(args["expires_within_days"])
    except (KeyError, TypeError, ValueError):
        expires_days = None

    cached = peek_cached()
    if cached is None:
        return QueryResult(
            ok=True,
            summary="No cert data cached",
            data="Certificate data not yet loaded. Visit /certs in the UI to fetch from monitoring sources.",
        )

    certs, _ = cached

    if host_filter:
        certs = [
            c for c in certs
            if host_filter in c.subject_cn.lower()
            or host_filter in c.endpoint.lower()
            or host_filter in c.subject.lower()
        ]
    if expires_days is not None:
        certs = [c for c in certs if c.days_remaining <= expires_days]

    if not certs:
        return QueryResult(ok=True, summary="No matching certificates", data="No matching certificates found.")

    lines = [
        f"{c.subject_cn}: {c.status} ({c.days_remaining}d remaining)"
        f" issuer={c.issuer_cn} source={c.source_name}"
        for c in certs[:50]
    ]
    suffix = f"\n(showing first 50 of {len(certs)})" if len(certs) > 50 else ""
    return QueryResult(ok=True, summary=f"{len(certs)} cert(s)", data="\n".join(lines) + suffix)


CERT_CHECK = QuerySpec(
    name="cert_check",
    description="Check certificate status. Filter by host substring and/or expiry window.",
    args_schema=[
        {"name": "host", "type": "string", "required": False, "description": "Substring to filter by CN/source."},
        {"name": "expires_within_days", "type": "number", "required": False, "description": "Only show certs expiring within N days."},
    ],
    runner=_cert_check_runner,
)
register_query(CERT_CHECK)


# ---------------------------------------------------------------------------
# list_pipelines
# ---------------------------------------------------------------------------

async def _list_pipelines_runner(cfg, args: dict) -> QueryResult:
    from dosm.db import session_scope
    from dosm.pipelines.repo import list_pipelines

    with session_scope() as s:
        pipelines = list_pipelines(s)
        if not pipelines:
            return QueryResult(ok=True, summary="No pipelines", data="No pipelines configured.")
        lines = [
            f"{p.name} ({p.provider})" + (f": {p.description[:80]}" if p.description else "")
            for p in pipelines
        ]
    return QueryResult(ok=True, summary=f"{len(lines)} pipeline(s)", data="\n".join(lines))


LIST_PIPELINES = QuerySpec(
    name="list_pipelines",
    description="List all configured CI/CD pipelines with their provider and description.",
    args_schema=[],
    runner=_list_pipelines_runner,
)
register_query(LIST_PIPELINES)


# ---------------------------------------------------------------------------
# list_pipeline_runs
# ---------------------------------------------------------------------------

async def _list_pipeline_runs_runner(cfg, args: dict) -> QueryResult:
    from dosm.db import session_scope
    from dosm.pipelines.repo import get_pipeline_by_name, list_pipelines, list_runs

    name = (args.get("name") or "").strip()
    try:
        limit = min(int(args.get("limit") or 10), 25)
    except (TypeError, ValueError):
        limit = 10

    with session_scope() as s:
        if name:
            pipeline = get_pipeline_by_name(s, name)
            if pipeline is None:
                return QueryResult(ok=False, summary=f"Pipeline {name!r} not found", error=f"No pipeline named {name!r}")
            runs = list_runs(s, pipeline.id, limit=limit)
            lines = [
                f"run#{r.id}: {r.status}"
                f" triggered={r.triggered_at.isoformat(timespec='seconds')}"
                + (f" url={r.html_url}" if r.html_url else "")
                + (f" error={r.error[:60]}" if r.error else "")
                for r in runs
            ]
            if not lines:
                return QueryResult(ok=True, summary=f"No runs for {name!r}", data=f"No runs yet for pipeline {name!r}.")
            return QueryResult(ok=True, summary=f"{len(lines)} run(s) for {name!r}", data="\n".join(lines))
        else:
            pipelines = list_pipelines(s)
            lines = []
            for p in pipelines[:15]:
                runs = list_runs(s, p.id, limit=1)
                last = runs[0] if runs else None
                lines.append(
                    f"{p.name}: last_run={last.status if last else 'never'}"
                    + (f" at {last.triggered_at.isoformat(timespec='seconds')}" if last else "")
                )
            if not lines:
                return QueryResult(ok=True, summary="No pipelines", data="No pipelines configured.")
            return QueryResult(ok=True, summary=f"{len(lines)} pipeline(s)", data="\n".join(lines))


LIST_PIPELINE_RUNS = QuerySpec(
    name="list_pipeline_runs",
    description="List recent pipeline run history. With 'name', shows runs for that pipeline. Without, shows last run per pipeline.",
    args_schema=[
        {"name": "name", "type": "string", "required": False, "description": "Pipeline name. Omit for overview."},
        {"name": "limit", "type": "number", "required": False, "description": "Max runs to return (default 10, max 25)."},
    ],
    runner=_list_pipeline_runs_runner,
)
register_query(LIST_PIPELINE_RUNS)


# ---------------------------------------------------------------------------
# find_person
# ---------------------------------------------------------------------------

async def _find_person_runner(cfg, args: dict) -> QueryResult:
    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.models import Department, DepartmentMember

    query = (args.get("name_or_dept") or "").strip().lower()
    if not query:
        return QueryResult(ok=False, summary="name_or_dept is required", error="name_or_dept argument is required")

    with session_scope() as s:
        depts = list(s.execute(select(Department)).scalars())
        dept_matches = [
            d for d in depts
            if query in d.name.lower() or query in (d.slug or "").lower()
            or query in (d.manager_name or "").lower()
        ]

        members = list(s.execute(select(DepartmentMember)).scalars())
        member_matches = [
            m for m in members
            if query in m.display_name.lower()
            or query in (m.email or "").lower()
            or query in (m.title or "").lower()
        ]

        dept_id_to_name = {d.id: d.name for d in depts}

        lines: list[str] = []
        for d in dept_matches[:10]:
            lines.append(
                f"Dept: {d.name}"
                + (f" | manager: {d.manager_name}" if d.manager_name else "")
                + (f" | email: {d.manager_email}" if d.manager_email else "")
                + f" | sync: {d.sync_status}"
            )
        for m in member_matches[:20]:
            dept_name = dept_id_to_name.get(m.department_id, "?")
            lines.append(
                f"Person: {m.display_name}"
                + (f" | {m.title}" if m.title else "")
                + (f" | {m.email}" if m.email else "")
                + f" | dept: {dept_name}"
                + (f" | manager: {m.manager_name}" if m.manager_name else "")
                + ("" if m.enabled else " [DISABLED]")
            )

    if not lines:
        return QueryResult(ok=True, summary="No matches", data=f"No departments or people matching {args.get('name_or_dept')!r}.")
    return QueryResult(ok=True, summary=f"{len(lines)} match(es)", data="\n".join(lines))


FIND_PERSON = QuerySpec(
    name="find_person",
    description="Search the org directory for a person by name/email/title, or a department by name.",
    args_schema=[
        {"name": "name_or_dept", "type": "string", "required": True, "description": "Name, email, title, or department name to search."},
    ],
    runner=_find_person_runner,
)
register_query(FIND_PERSON)


# ---------------------------------------------------------------------------
# list_credentials
# ---------------------------------------------------------------------------

async def _list_credentials_runner(cfg, args: dict) -> QueryResult:
    from sqlalchemy import select

    from dosm.db import session_scope
    from dosm.models import Credential

    with session_scope() as s:
        creds = list(s.execute(select(Credential).order_by(Credential.name)).scalars())
        if not creds:
            return QueryResult(ok=True, summary="No credentials", data="No credential profiles configured.")
        lines = [
            f"{c.name} (kind={c.kind}"
            + (f", user={c.username}" if c.username else "")
            + (f", domain={c.domain}" if c.domain else "")
            + ")"
            for c in creds
        ]
    return QueryResult(ok=True, summary=f"{len(lines)} credential(s)", data="\n".join(lines))


LIST_CREDENTIALS = QuerySpec(
    name="list_credentials",
    description="Read credential profile names and metadata from the database. No secret values are returned. No connections are made.",
    args_schema=[],
    runner=_list_credentials_runner,
)
register_query(LIST_CREDENTIALS)


# ---------------------------------------------------------------------------
# search_docs
# ---------------------------------------------------------------------------

async def _search_docs_runner(cfg, args: dict) -> QueryResult:
    from dosm.db import session_scope
    from dosm.docs_index.search import search as _search

    query = (args.get("query") or "").strip()
    if not query:
        return QueryResult(ok=False, summary="query is required", error="query argument is required")
    try:
        k = min(int(args.get("k") or 5), 10)
    except (TypeError, ValueError):
        k = 5

    with session_scope() as s:
        hits = _search(s, cfg, query, limit=k, exclude_org=False)

    if not hits:
        return QueryResult(ok=True, summary="No matching documents", data=f"No documents matched {query!r}.")
    lines = [
        f"[{i + 1}] {h.rel_path} (score {h.score:.3f}): {h.snippet[:300]}"
        for i, h in enumerate(hits)
    ]
    return QueryResult(ok=True, summary=f"{len(hits)} doc hit(s)", data="\n".join(lines))


SEARCH_DOCS = QuerySpec(
    name="search_docs",
    description="Search the documentation index for relevant content. Returns scored snippets.",
    args_schema=[
        {"name": "query", "type": "string", "required": True, "description": "Search query."},
        {"name": "k", "type": "number", "required": False, "description": "Max results (default 5, max 10)."},
    ],
    runner=_search_docs_runner,
)
register_query(SEARCH_DOCS)


# ---------------------------------------------------------------------------
# list_docs
# ---------------------------------------------------------------------------

async def _list_docs_runner(cfg, args: dict) -> QueryResult:
    import os

    folder = (args.get("folder") or "").strip().strip("/")
    base = cfg.docs_dir.resolve()
    target = (base / folder).resolve() if folder else base

    if base not in target.parents and target != base:
        return QueryResult(ok=False, summary="folder outside docs vault", error="invalid path")
    if not target.is_dir():
        return QueryResult(ok=True, summary=f"folder not found: {folder}", data=f"No folder {folder!r} in the docs vault.")

    entries: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel_root = os.path.relpath(root, base)
        for f in sorted(files):
            if f.endswith((".md", ".markdown", ".txt", ".pdf")):
                rel = os.path.join(rel_root, f).replace("\\", "/")
                if rel.startswith("./"):
                    rel = rel[2:]
                entries.append(rel)
        if len(entries) >= 100:
            break

    if not entries:
        return QueryResult(ok=True, summary="No documents", data="No documents found in that folder.")
    suffix = "\n(truncated at 100)" if len(entries) >= 100 else ""
    return QueryResult(ok=True, summary=f"{len(entries)} file(s)", data="\n".join(entries) + suffix)


LIST_DOCS = QuerySpec(
    name="list_docs",
    description="List files in the documentation vault. Use to discover available runbooks before calling read_doc.",
    args_schema=[
        {"name": "folder", "type": "string", "required": False, "description": "Sub-folder to list (e.g. 'services'). Omit for root."},
    ],
    runner=_list_docs_runner,
)
register_query(LIST_DOCS)


# ---------------------------------------------------------------------------
# read_doc
# ---------------------------------------------------------------------------

async def _read_doc_runner(cfg, args: dict) -> QueryResult:
    path = (args.get("path") or "").strip()
    if not path:
        return QueryResult(ok=False, summary="path argument is required", error="path argument is required")

    base = cfg.docs_dir.resolve()
    full = (base / path).resolve()

    if base not in full.parents and full != base:
        return QueryResult(ok=False, summary="path outside docs vault", error="invalid path")
    if not full.is_file():
        return QueryResult(ok=False, summary=f"not found: {path}", error=f"No file at docs/{path}")

    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return QueryResult(ok=False, summary=f"read error: {e}", error=str(e))

    truncated = text[:20000]
    suffix = f"\n\n[truncated - {len(text)} chars total, showing first 20000]" if len(text) > 20000 else ""
    return QueryResult(ok=True, summary=f"read {path} ({len(text)} chars)", data=truncated + suffix)


READ_DOC = QuerySpec(
    name="read_doc",
    description=(
        "Read a full document from the docs vault by relative path. "
        "Use after search_docs or list_docs identifies the right file. "
        "Returns up to 20,000 characters of content."
    ),
    args_schema=[
        {"name": "path", "type": "string", "required": True, "description": "Relative path from docs root, e.g. 'services/prometheus.md'."},
    ],
    runner=_read_doc_runner,
)
register_query(READ_DOC)


# ---------------------------------------------------------------------------
# cli_help
# ---------------------------------------------------------------------------

async def _cli_help_runner(cfg, args: dict) -> QueryResult:
    """Return structured help for a `dosm` subcommand.

    Use this before proposing any plan card whose action invokes the dosm
    CLI - verifies the command exists, flags are spelled correctly, and
    required arguments are present. Cheaper than searching the docs and
    immune to RAG ranking noise.
    """
    import click

    from dosm.cli import app as _app

    command = (args.get("command") or "").strip()
    if not command:
        return QueryResult(ok=False, summary="command is required", error="command argument is required")

    parts = command.split()
    if parts and parts[0] == "dosm":
        parts = parts[1:]

    import typer as _typer
    root = _typer.main.get_command(_app)
    node: click.Command = root
    walked: list[str] = []
    for token in parts:
        if not isinstance(node, click.Group):
            return QueryResult(
                ok=False,
                summary=f"{' '.join(walked)!r} is not a group",
                error=f"{' '.join(walked) or 'dosm'!r} has no subcommands; cannot resolve {token!r}",
            )
        sub = node.commands.get(token)
        if sub is None:
            available = ", ".join(sorted(node.commands)) or "(none)"
            return QueryResult(
                ok=False,
                summary=f"unknown command: dosm {' '.join(walked + [token])}",
                error=f"unknown command at {' '.join(walked) or 'dosm'!r}: {token!r}. Available: {available}",
            )
        node = sub
        walked.append(token)

    lines: list[str] = []
    full = " ".join(["dosm", *walked])

    if isinstance(node, click.Group):
        # Listing a group - show its children.
        lines.append(f"{full} - {(node.help or '').strip() or '(group)'}")
        lines.append("")
        lines.append("Subcommands:")
        for name in sorted(node.commands):
            child = node.commands[name]
            short = (child.short_help or child.help or "").strip().splitlines()[0] if (child.short_help or child.help) else ""
            lines.append(f"  {name} - {short}" if short else f"  {name}")
        return QueryResult(ok=True, summary=f"group {full!r}", data="\n".join(lines))

    # Concrete command.
    desc = (node.help or node.short_help or "").strip()
    lines.append(f"{full}")
    if desc:
        lines.append("")
        lines.append(desc)

    args_rows: list[str] = []
    opts_rows: list[str] = []
    for p in node.params:
        if p.name == "help":
            continue
        type_name = getattr(p.type, "name", None) or p.type.__class__.__name__
        if isinstance(p, click.Argument):
            req = "required" if p.required else "optional"
            args_rows.append(f"  <{p.name.upper()}> ({type_name}, {req})")
        else:
            flags = ",".join(getattr(p, "opts", []) or [])
            default = "" if p.default in (None, ()) else f" default={p.default!r}"
            help_text = (getattr(p, "help", None) or "").strip()
            opts_rows.append(f"  {flags} ({type_name}){default}" + (f" - {help_text}" if help_text else ""))

    if args_rows:
        lines.append("")
        lines.append("Arguments:")
        lines.extend(args_rows)
    if opts_rows:
        lines.append("")
        lines.append("Options:")
        lines.extend(opts_rows)

    synopsis_parts = ["dosm", *walked]
    if opts_rows:
        synopsis_parts.append("[OPTIONS]")
    for p in node.params:
        if isinstance(p, click.Argument):
            token = f"<{p.name.upper()}>"
            synopsis_parts.append(token if p.required else f"[{token}]")
    lines.append("")
    lines.append("Synopsis: " + " ".join(synopsis_parts))

    return QueryResult(ok=True, summary=f"help for {full!r}", data="\n".join(lines))


CLI_HELP = QuerySpec(
    name="cli_help",
    description=(
        "Look up exact synopsis, arguments, and options for a `dosm` CLI command. "
        "Call this before proposing any plan card that invokes the dosm CLI to "
        "verify spelling and required flags. Pass the command path with or without "
        "the leading 'dosm' (e.g. 'secret set' or 'dosm secret set'). "
        "Pass a group name (e.g. 'docs') to list its subcommands."
    ),
    args_schema=[
        {"name": "command", "type": "string", "required": True, "description": "Command path, e.g. 'secret set' or 'org sync'."},
    ],
    runner=_cli_help_runner,
)
register_query(CLI_HELP)
