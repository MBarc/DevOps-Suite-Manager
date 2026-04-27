# DOSM — Roadmap, design notes, and known limits

This is the running record of where we've been, where we're going, and the
design choices that shape how to extend the project. It's the context that
doesn't fit in commit messages.

---

## Phase log (shipped)

Each row corresponds to one focused commit on
`claude/devops-suite-llm-design-ZJ2aU`. The commit message is the detailed
changelog; this is the one-line summary.

| Phase | Commit       | Summary |
|-------|--------------|---------|
| 1     | `ea177d8`    | Scaffold: FastAPI + Typer CLI + `$DOSM_HOME` bootstrap. |
| 2     | `a78ddd3`    | SQLite + bcrypt auth + secrets backend (Local Fernet, Vault) + Hosts inventory. |
| 3     | `8d663a3`    | Module loader contract + bundled `system_info` module. |
| 4     | `89e7626`    | In-app terminals (admin-only), xterm.js + PTY bridge, asciinema recording, resource panel. |
| 5     | `b5b2a41`    | Local docs index (md/txt/pdf), fastembed embeddings, RAG search with citations + LIKE fallback. |
| —     | `9b3416a`    | UI polish: sidebar shell + design tokens. |
| 6     | `982593a`    | LLM chat (Ollama + RAG + citations) + terminal Run-as. |
| 7     | `75cd742`    | Agent mode: plan cards (Approve/Edit/Reject) + `ssh_exec` action with tiered allow-list. |
| 8     | `9c918e5`    | Apache Guacamole integration: signed JSON envelope, docker-compose stack, iframe wrapper. |
| 8b    | `39723e1`    | Per-host metrics sources for the resource panel: `LocalSource`, `SSHSource` (Linux). |
| 8c    | `d03abcc`    | `WinRMSource` for the resource panel on RDP/Windows hosts. |
| 9     | `f44c65f`    | Jump-host chains + credential profiles UI + WAL + idempotent migrations. |
| 10    | `1e60b94`    | Settings page + DevOps CLI catalog (15 tools, toggle into Terminals). |
| 11    | `21e35ae`    | Pipeline runner core + GitHub Actions adapter + `run_pipeline` agent action. |
| 12+13 | pending      | Monitoring integrations (Dynatrace / Datadog / ServiceNow adapters, host-check page, fleet coverage matrix, 60s cache) + Certificate inventory (Windows cert stores + Linux PEM/DER walk, expiry coloring, 5-min cache). |

## Open backlog (recommended order)

| Phase | Title | Why this order |
|-------|-------|----------------|
| **10.5** | **Tests + CI** | Smoke tests aren't a regression net. As the surface grows, refactoring becomes scary. Strong recommendation: do this before any more features. Pytest around `agent.actions`, `secrets`, `jumps`, `pipelines.repo`, route handlers — even ~50 tests would protect a lot. Add lint + type-check + smoke run on push. |
| 11b | Pipeline background poller | Runs currently update only on manual refresh. A small async task that polls non-terminal runs every N seconds finishes the v1 pipeline experience. |
| 11c | Azure DevOps adapter | Different auth (PAT or service principal), different status model (queued/inProgress/completed + result). Plug-in shape already proven. |
| 11d | Octopus Deploy adapter | REST API + API key. |
| 11e | Ansible/AWX adapter | AWX REST. |
| 11f | Terraform Cloud adapter | TFC REST + workspace runs. |
| ~~12~~ | ~~Monitoring integrations~~ | Shipped — see phase log above. |
| ~~12b~~ | ~~Dynatrace adapter~~ | Shipped — see phase log above. |
| ~~12c~~ | ~~Datadog adapter~~ | Shipped — see phase log above. |
| ~~12d~~ | ~~ServiceNow adapter~~ | Shipped — see phase log above. |
| ~~13~~ | ~~Certificate inventory~~ | Shipped — see phase log above. |
| 14 | Organization graph | Departments + AD sync (`ldap3`). List view + tree view (D3). Description text feeds the docs index so the agent can answer "who do I talk to about X". |
| 15 | Documentation vault & importer | Extends `docs_index/` with PDF/Word→markdown import (mammoth for Word, pypdf for PDF), in-UI markdown editor, per-application taxonomy. |
| 8d | Session recordings browser | Last because it's most useful once there's actually history to browse — the user explicitly asked for this to be near the end. |

## Design notes

### Phase 12 — Monitoring integrations

A new **Monitoring** section in the sidebar (between Pipelines and Settings).
The core idea is a read-only health dashboard: pull current alert/incident
state from whichever tools the operator has configured, surface it in one
place, and let the agent answer questions about it.

**Adapter contract** — same plug-in shape as pipeline adapters:
- `MonitoringAdapter` ABC in `dosm/monitoring/adapters/base.py`
- Required method: `fetch_alerts() -> list[Alert]`
- `Alert` is a dataclass: `id, title, severity, status, source, url, ts`
- Each adapter reads its config (base URL, token) from the secrets backend
  so credentials are never stored in plain text

**Adapters planned:**
- `DynatraceAdapter` — Problems API v2 (`/api/v2/problems`). Auth: `Api-Token` header.
- `DatadogAdapter` — Monitors API (`/api/v1/monitor`). Auth: `DD-API-KEY` + `DD-APPLICATION-KEY` headers.
- `ServiceNowAdapter` — Incidents table API (`/api/now/table/incident`). Auth: basic or OAuth bearer.

**UI:**
- Dashboard tab: severity-banded alert list across all enabled sources, auto-refresh every 60 s
- Per-source config tab: enable/disable toggle, base URL, API key fields (stored via secrets backend)
- Alert detail side-panel with a link out to the native tool

**Agent integration:**
- Expose a `query_monitoring` action so the agent can answer "are there any
  P1 alerts right now?" by calling `fetch_alerts()` across enabled adapters
- Result is read-only; no write actions to monitoring tools (out of scope)

**Dependencies to add:** `httpx` is already present. No new deps needed for
Dynatrace or Datadog. ServiceNow OAuth may need `authlib` if basic auth
isn't sufficient in the target environment.

## Design decisions worth preserving

### Local-first by default

SQLite for state, Ollama for the LLM, fastembed for embeddings, Guacamole
for browser sessions. Nothing requires outbound traffic. This matches the
target audience (on-prem ops teams in air-gapped or restricted networks).
Don't introduce a SaaS dependency without an offline fallback.

### Plan cards, not auto-execution

The agent never executes anything autonomously. Every action gets a
`PlanCard` row that the operator approves. Tiered classification (`safe` if
the command matches the allow-list glob, `elevated` if not) and an
elevated-tier path that requires typing the host name to confirm.

When you add new agent actions:
1. Register via `dosm.agent.actions.register_action(ActionSpec(...))`
2. Provide a `classify(args)` that returns `"safe"` or `"elevated"`
3. Make the runner side-effect-free if it raises — wrap in try/except in
   `actions.py`, return an `ActionResult(ok=False, summary=...)`
4. Result message gets appended to the conversation as an assistant turn so
   the LLM can react in the next turn.

### Pluggable secrets backend

`SecretsBackend` ABC with `LocalEncryptedBackend` (Fernet, blobs in app.db)
and `VaultBackend` (hvac, KV v2). When you reach for the secrets backend
inside a request, **commit the request session before** opening the
backend's session — SQLite is single-writer and the request would otherwise
deadlock with itself (we hit this in Phase 9).

### Jump tunnel pooling

`dosm.jumps.tunnels.JumpTunnelManager` keeps one persistent SSH connection
per jump host alive in the DOSM process. Each forward is leased; many
targets behind the same jump multiplex over one auth. Solves the
"concurrent sessions through one jump kicks each other out" problem.

When you add a feature that needs a remote SSH session through a jumped
host, prefer `connect_through_chain(jump_hops, target)` from
`dosm.jumps.connections` — it does the right thing for a one-shot, and the
manager handles long-lived multi-target cases.

### Modular integrations

`dosm/modules/builtin/` for first-party (currently just `system_info`).
`$DOSM_HOME/modules/` for user-installed. Each module has a `module.yaml`
with name, version, OS constraints, capabilities, optional Python deps. The
loader filters by OS, imports the package, calls `register(app, cfg)`.

### Idempotent column migrations

`dosm/migrations.py::run_migrations(engine)` adds new columns at startup
without disturbing existing data. Lightweight by design — anything more
complex (renames, type changes, FK changes) needs Alembic. **Don't
work around the limit; introduce Alembic when you need it.** The right
trigger is your second column-rename or first FK change.

### Single-file SQLite + WAL

`dosm/db.py` enables WAL + `synchronous=NORMAL` + 30s timeout +
`foreign_keys=ON`. Keeps things responsive when the secrets backend writes
inside a request. If you ever need multi-writer concurrency, that's the
moment to switch to Postgres — don't try to engineer SQLite around it.

### One commit per phase

This makes git history the changelog. Ship a phase only when the smoke test
exercises both happy and error paths. Each commit message captures the
"why" (what's in it, what's deliberately left out, what was tested,
limitations). Preserve this.

## Known limitations

- **No automated tests / no CI.** Highest-priority debt. See Phase 10.5.
- **The agent has not been validated against a live LLM.** Real model
  output will misformat plan blocks, propose dangerous commands,
  hallucinate hosts. Plan card UX will need iteration once Ollama is
  pointed at a real model.
- **Auth is local username/password only.** No SSO, no MFA. For a tool
  that holds prod creds, this is the weakest link. Worth a phase
  eventually.
- **Process-local state** (`JumpTunnelManager`, `_embedder`,
  ephemeral run-as registry). Don't deploy more than one DOSM instance
  expecting them to share state.
- **Schema migrations** are column-add only. Renames / FK changes need
  Alembic — not yet added.
- **WinRMSource** is unverified against a real Windows host. Code paths
  are exercised in error/timeout cases but the success path is theoretical
  until someone points it at a working WinRM endpoint.
- **No deployment story for production.** docker-compose exists for
  Guacamole; there's no all-in-one DOSM container, no production hardening
  guide, no upgrade docs. Worth a phase once tests + CI are in.
- **Run-as supports sudo / runas wrappers** but not credential
  pass-through. For environments that need it (no sudoers, no saved
  `runas` creds), needs `pywin32 LogonUser` on Windows and `SUDO_ASKPASS`
  on Linux. Not yet built.
- **Documentation embedder requires a one-time HuggingFace download** for
  the bge-small-en-v1.5 ONNX model on first use. Air-gapped environments
  need to pre-fetch the model into `$HOME/.cache/fastembed/`. The
  `NoEmbedder` fallback degrades to LIKE search if the download fails.

## Picking up where we left off

1. `git clone` the repo, check out `claude/devops-suite-llm-design-ZJ2aU`
2. Read **this file** (you're here) and **`CLAUDE.md`**
3. `pip install -e .` in a Python 3.11+ venv
4. `dosm init ./.dosm-home`, set `DOSM_HOME`, `dosm db init`,
   `dosm user create admin`, `dosm serve`
5. Browse the running app at <http://127.0.0.1:8765> to ground yourself
6. Pick the next item from the backlog. **Recommendation: Phase 10.5
   (tests + CI) first** — it makes everything after safer.

## What I (the AI assistant) would not do without asking

- Push to `main` (working branch is the feature branch above)
- Refactor anything across phases (e.g. moving `secrets` into a different
  package, renaming `Credential` to `CredentialProfile`)
- Add a SaaS dependency without an offline fallback
- Skip the smoke-test step at the end of a phase
- Add tests at the end of a phase that don't actually run in CI (since
  there is no CI yet — they'd just be theater)
- Bypass the plan-card approval flow for any agent action
- Delete `$DOSM_HOME` (or any directory with user state) in a user
  environment

## What I would do without asking

- Add new agent actions following the existing `ActionSpec` pattern
- Add new pipeline adapters following the existing `PipelineAdapter`
  pattern
- Add new metrics sources following the existing `MetricsSource` pattern
- Add new bundled modules under `dosm/modules/builtin/`
- Apply minor UI polish where it doesn't change behavior
- Fix bugs the smoke tests reveal
