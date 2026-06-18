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
| 3     | `8d663a3`    | ~~Module loader contract + bundled `system_info` module.~~ Retired — modules system was only ever consumed by the example `system_info` plug-in; integrations live in core under `dosm/monitoring/adapters/`, `dosm/pipelines/`, `dosm/metrics/`. |
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
| 11b   | (pending)    | Pipeline background poller: age-based cadence (5s→300s), asyncio.gather with bounded concurrency, run abandonment after configurable hours, auto-refresh meta tag on run detail page, `dosm pipelines poll` CLI debug command. |
| 12+13 | `2b1d1f9`    | Monitoring integrations (Dynatrace / Datadog / ServiceNow adapters, host-check page, fleet coverage matrix, 60s cache) + Certificate inventory (Windows cert stores + Linux PEM/DER walk, expiry coloring, 5-min cache). |
| 12d   | (shipped)    | Prometheus adapter: host-presence check via `up{instance=~"^hostname(:.+)?$"}`, bearer/basic/no-auth, plugs into coverage matrix. |
| 9b    | (shipped)    | RD Gateway support: RDP→RDP jump chains via Microsoft Remote Desktop Gateway. Auto-derived from protocol pairing (RDP target + RDP jumpbox = RD Gateway path). `Credential.domain` added for Windows domain auth. guacd handles the hop natively via `gateway-*` Guacamole params — no DOSM tunnel needed. |
| 12e   | (shipped)    | ServiceNow extended monitoring detail: discovery source/timestamp, monitoring relationships (cmdb_rel_ci), metric collection (metric_instance with ITOM Visibility fallback), thresholds note, multiple CMDB match surfacing. |
| 14    | (shipped)    | Organisation directory: AD-backed via WinRM jumpbox + PowerShell `ActiveDirectory` cmdlets (no direct LDAP). Empty-state configure flow, mock adapter for dev/test, manager-chain hierarchy inference, per-member manager capture. Unified directory list (departments + people) with hosts-style search bar (field selector + clear), pan/zoom D3 tree, disabled accounts shown with strikethrough + tooltip. Each dept's roster is written to `docs/org/{slug}.md` so the agent can answer "who do I talk to about X". |
| 17    | (shipped)    | Typed pipeline inputs: schema rows gain `type` (string/boolean/number/choice) + options/default/required/description. Row-based schema editor (add/remove rows, options field auto-disables for non-choice). Run form renders text / number / checkbox / `<select>` per type, server-side validates required + choice membership. Per-adapter wire coercion: GitHub stringifies all (booleans → `"true"/"false"`); Octopus stringifies `FormValues` the same way; ADO splits `var.`-prefixed inputs into `variables` (string-coerced) vs `templateParameters` (native types preserved); AWX passes through native (Ansible vars are typed); TFC adapter unchanged but run form shows an amber banner explaining inputs aren't sent (variables live on the workspace). Legacy `key=value` textarea kept as fallback for pipelines without a declared schema. 21 new tests (15 unit + 6 integration). |
| 22    | (this commit)| **Pipeline payloads** (branch `feature/pipeline-payloads`, off the RBAC branch). Named, reusable input-value sets per pipeline, so an executor picks a predefined payload instead of re-typing the run form. New `PipelinePayload` table (`pipeline_id` CASCADE, `name` unique-per-pipeline, `description`, `values_json` = same shape as `PipelineRun.inputs`, `created_by_id`, `visibility`). **Visibility mirrors credentials**: shared (any executor) or private (creator + admins), enforced by `dosm/pipelines/payload_access.py` (list, run-page picker, edit/delete routes 404-not-403). **Permissions**: select/run and manage (create/redefine/rename/copy/delete) are all **operator+** (matches the pipeline run gate); since others can't see a private payload, in practice only its owner+admins manage it. **Run UX**: a payload `<select>` on the pipeline page pre-fills the typed input form (editable before Run); a Payloads section offers Use / Edit / Copy / Rename / Delete. **Drift**: `validate_payload_values()` checks stored values against the *current* `inputs_schema`; mismatches are flagged "needs update" and block the run. Schemaless pipelines store `{"__raw__": text}` and pre-fill the textarea. `dosm pipelines payload list/show/add/rename/copy/rm`. `AuditLog` on every mutation + the selected payload recorded in the run audit. 8 new tests (drift validation, name-conflict, copy-name derivation, web CRUD, invalid-choice rejection, viewer-forbidden, private-hidden-from-others). |
| 21d   | `161a0a8`    | **Require group membership (deny unmapped Okta logins)** (branch `feature/rbac-okta-ad`). Security default: an Okta user who is in **none** of the mapped groups is now **denied** rather than granted a baseline role. `RbacConfig.default_role` defaults to `"none"` (deny); `map_groups_to_role` returns `None` when no mapped group matches and the default isn't a real role; the callback then refuses to provision/sign in, renders the login page with "not a member of any group granted DOSM access", and audit-logs `auth.login.okta.denied` (no user row created). The Settings → Access control "Unmapped users" selector gains a **No access (require group membership)** option (the default). Local break-glass accounts are unaffected (explicitly provisioned). `dosm rbac show-mapping` shows "no access" when deny. 4 new tests (deny mapping + model default, end-to-end callback denial with no provisioning, settings accepts `none`). |
| 21c   | `640ae2c`    | **RBAC admin UI — group→role editor + export** (branch `feature/rbac-okta-ad`). A new **Access control** tab on the Settings page (admin-only, so the break-glass admin + any admin manage it) to edit `rbac.group_role_map` and `default_role` without hand-editing `config.yaml`. Add/update a group→role (upsert; inline role `<select>` auto-submits), remove a mapping, set the default role. Persisted via `update_config_yaml` + live `cfg.rbac` mutation (no restart), each change `AuditLog`'d (`settings.rbac.*`). **Export** the full mapping as JSON (`{default_role, groups:[{group,role}]}`) or CSV (`group,role` rows + a trailing default-role row) via download buttons → `/settings/rbac/export.json` / `.csv`. 7 new tests (add/update/delete, invalid-role 400, default-role save, page render, JSON+CSV export shape, admin-only gating). |
| 21b   | `d0fdb25`    | **Okta SSO + AD-group→role mapping** (branch `feature/rbac-okta-ad`). Authentication via Okta OIDC; authorization from the ID token's `groups` claim (Okta federates AD — no live AD round-trip). `OktaConfig` + `RbacConfig` in `config.py` (`group_role_map`, highest-role-wins, `default_role`); client secret in the secrets backend (`okta/client_secret`), never YAML. `dosm/auth/okta.py` splits **pure** logic (group→role mapping, claim extraction, JIT provisioning, ID-token validation against a supplied JWKS) from **network** helpers (discovery, token exchange, JWKS fetch) so the security-critical paths are unit-testable offline with a self-signed token. `GET /auth/okta/login` (state+nonce+PKCE S256) → `GET /auth/okta/callback` (validate state, exchange code, verify ID-token signature/iss/aud/exp/nonce, map groups→role, JIT-upsert keyed on `okta_sub`, set `session["user_id"]`). Role is recomputed from the claim on **every** login, so AD group changes apply at next sign-in. SSO users get an unverifiable sentinel password hash (`!okta`) so `User.password_hash` stays NOT NULL without a SQLite ALTER. New `User` cols: `okta_sub`/`email`/`display_name`/`auth_provider`/`last_login`. Break-glass local login preserved; login page shows a "Sign in with Okta" button when enabled. `dosm okta test` (discovery+JWKS+secret check) + `dosm rbac show-mapping`. `authlib` dep added. 9 new tests (group mapping, ID-token validation incl. bad nonce/audience, end-to-end callback provisioning, role-recompute-on-login, local login unaffected, routes 404 when disabled). |
| 21    | `27c790f`    | **RBAC core** (branch `feature/rbac-okta-ad`). Single ranked role ladder `viewer<operator<admin` in `dosm/auth/deps.py` (`require_role` factory + `user_has_role` predicate for WS) replacing the `_require_admin` body copy-pasted into 5 modules + 2 WS handlers. Capability matrix preserves today's policy (terminals/files/settings/cert-sources/org stay admin) and closes the gaps where hosts + credentials + pipelines + agent + guacamole mutations were login-only (now operator). **Private vs shared credentials**: `Credential.owner_id` + `visibility` (idempotent column-adds); `dosm/credentials/access.py` is the single visibility predicate, applied to the list, the host-form picker, the detail/edit/delete routes (404 not 403 to avoid leaking existence), and a use-time guard at guacamole connect (blocks connecting through someone else's private credential, incl. jump hops). **Per-user private data**: chats/agent history already scoped by `Conversation.user_id`; recordings are admin-only via terminals; new `User.prefs_json` + `dosm/auth/prefs.py`, wired to remember the hosts-list kind filter. `dosm user set-role` (the missing role-mutation path) + role validation on create. Nav/buttons hide write actions from viewers. 8 new tests (role matrix, private-cred visibility, conversation ownership, break-glass login); 152 pass. **Okta SSO + AD-group→role mapping is the next commit (Phase 21b).** |
| 18    | (pending)    | File transfer (FTP / explicit FTPS / SFTP), jump-aware. `dosm/ftp/` with a `FileTransferBackend` ABC + `FtpBackend` (hand-rolled blocking FTP/FTPS client) and `SftpBackend` (asyncssh-native). Jumped FTP routes every socket through an `asyncssh` SOCKS5 listener leased from `JumpTunnelManager` (`acquire_socks`) — control + every passive data port tunnel through one proxy, so dynamic PASV ports need no per-transfer forwards. FTPS is explicit AUTH TLS with control-session reuse on the PROT P data channel (the thing `ftplib` can't do). File transfer is a **host capability**, not a host protocol: hosts gain `ft_method` (sftp/ftp/ftps) + `ft_port` + optional `ft_credential` override (idempotent column-add migration), set in a "File transfer" section on the host form — so an SSH box exposes SFTP without a duplicate inventory entry. Admin-only web file browser (breadcrumbs, list/upload/download/mkdir/rename/delete, drag-drop), reachable via a **Files** sidebar page (host picker), a Files button on the hosts list, and the host detail card; `dosm ftp ls/get/put/rm` CLI; `AuditLog` on every mutation + download. **Host-to-host copy/move**: `transfer_between_hosts` stages a file server-side (retrieve from source backend → store to dest backend, optional source delete = move) — each side traverses its own jump chain via `get_file_backend`; "Copy / move to another host" file action + picker modal, `/copy` + `/targets` routes, `dosm ftp cp [--move]`. Also fixed a latent `dosm.jumps` ↔ `dosm.hosts` import cycle (cold `import dosm.jumps` was broken) by lazy-importing `resolve_jump_chain` in `connections.build_jump_chain`. 12 new tests (in-process pyftpdlib FTPS + asyncssh SFTP/jump, no Docker). |

## Open backlog (recommended order)

| Phase | Title | Why this order |
|-------|-------|----------------|
| ~~9b~~ | ~~RD Gateway~~ | Shipped — see phase log above. |
| ~~10.5~~ | ~~Tests + CI~~ | Shipped. 89 pytest tests across auth, hosts, docs, agent, secrets, and unit utils. GitHub Actions CI on every push. Also caught and fixed two pre-existing bugs: `vault.py` used undefined `app_dir` (every doc save would crash), and `markdown.py` passed conflicting `link_rel`+`rel` to nh3 (preview/view 500s). |
| ~~11b~~ | ~~Pipeline background poller~~ | Shipped — see phase log above. |
| ~~11c~~ | ~~Azure DevOps adapter~~ | Shipped. |
| ~~11d~~ | ~~Octopus Deploy adapter~~ | Shipped. |
| ~~11e~~ | ~~Ansible/AWX adapter~~ | Shipped. |
| ~~11f~~ | ~~Terraform Cloud adapter~~ | Shipped. |
| ~~12~~ | ~~Monitoring integrations~~ | Shipped — see phase log above. |
| ~~12b~~ | ~~Dynatrace adapter~~ | Shipped — see phase log above. |
| ~~12c~~ | ~~Datadog adapter~~ | Shipped — see phase log above. |
| ~~12d~~ | ~~ServiceNow adapter~~ | Shipped — see phase log above. |
| ~~13~~ | ~~Certificate inventory~~ | Shipped — see phase log above. |
| ~~8d~~ | ~~Session recordings browser~~ | Shipped. |
| ~~14~~ | ~~Organisation directory~~ | Shipped — see phase log above. Pivoted from direct `ldap3` to WinRM-via-jumpbox so DOSM-in-Docker doesn't need to be domain-joined. |
| 16 | AI Agent enhancements | Expand the agent beyond `ssh_exec` + `run_pipeline`. Planned actions: `query_monitoring` (read live alerts), `search_docs` (RAG lookup without full chat), `cert_check` (on-demand cert status for a host), `host_metrics` (pull current CPU/mem/disk). Improve plan card UX once validated against a real Ollama model: streaming rationale, multi-step plan previews, conversation-level approval history. |
| ~~15~~ | ~~Documentation vault & importer~~ | Shipped. Application taxonomy model, YAML frontmatter, in-UI markdown editor with live preview, .docx/.pdf/.md/.txt importer (mammoth + pypdf), rendered markdown view, stale-edit conflict detection, `dosm docs new/import` + `dosm application` CLI commands. |
| 17.5 | Auto-introspect pipeline definitions | Eliminate the duplication between provider-side workflow definitions and DOSM's `inputs_schema`. Per-adapter introspectors that fetch the upstream definition and pre-populate the schema editor on pipeline create/edit, with overrides preserved on re-introspection. **GitHub:** `GET /repos/.../contents/.github/workflows/{file}` → base64-decode → parse YAML → map `on.workflow_dispatch.inputs` (type/options/default/required/description) onto schema rows. **AWX:** `GET /api/v2/job_templates/{id}/survey_spec/` → map survey questions (text/textarea → string, password → string secret, integer/float → number, multiplechoice → choice). **ADO:** `GET /{org}/{project}/_apis/pipelines/{id}` (definition YAML) → parse `parameters:` block. **Octopus:** `GET /api/{space}/projects/{id}/deploymentprocess` + `/variables` to surface prompted variables. **TFC:** out (no introspectable run-time inputs). UI: an "Introspect from provider" button next to each schema editor that fetches and replaces, with a confirm if rows already exist. |

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

### Phase 14 — Organisation directory: AD via jumpbox

The original plan was to bind LDAP directly from DOSM. The blocker: DOSM
runs in Docker, containers aren't domain-joined, and we didn't want to
require a stored bind password. We pivoted to a Windows jumpbox sidecar
pattern:

- **Adapter contract** — `AdDirectorySource` ABC in
  `dosm/directory/adapters/__init__.py` with `test_connection`,
  `resolve_group`, `resolve_user`, `sync_group`. Implementations:
  - `WinRMJumpboxSource` — opens a `winrm.Session` to a configured host,
    runs PowerShell `ActiveDirectory` cmdlets, returns parsed JSON. One
    round trip per group sync; per-member manager DNs resolved in the same
    script via a hashtable lookup pass.
  - `MockSource` — fixture-backed Acme org chart for dev/tests so the UI
    can be exercised without a real domain.
- **Config** — single global `directory.ad_jumpbox_host_id` in
  `config.yaml`; the bind identity is whatever credential profile is
  attached to that host. `directory.adapter = "mock"` toggles the mock for
  testing.
- **Hierarchy inference** — at sync time, the manager chain (capped at 20
  hops) is walked against `Department.manager_dn`; the first match wins
  and becomes `parent_id`. Auto-derived only — no manual override.
- **Members** — a separate `department_members` table with
  `(department_id, user_dn)` unique. Disabled AD accounts cached with
  `enabled=false` and rendered with strikethrough + "Account disabled"
  tooltip rather than hidden, so historical association is preserved.

**When to add a new directory adapter**: drop a class implementing the ABC
into `dosm/directory/adapters/`, register it in the factory at
`dosm/directory/adapters/__init__.py:get_directory_source`. Match the
pattern of monitoring/pipeline adapters elsewhere.

### Phase 18 — File transfer through a jump box

The hard part of FTP-through-a-jump isn't the jump; it's that FTP opens a
**second, dynamically-negotiated data connection** per transfer. Tunnelling
only port 21 makes the control channel work but every `LIST`/`RETR` hangs,
because passive mode hands back a fresh port that was never forwarded.

The solution — **route every FTP socket through an `asyncssh` SOCKS5 proxy**
opened over the jump (`JumpTunnelManager.acquire_socks`). A SOCKS proxy
forwards whatever host:port a connection asks for, on demand, so the control
connection *and* each ephemeral passive data port tunnel through one listener,
shared across sessions and GC-reaped like the existing port forwards. The
client prefers EPSV and, for PASV, ignores the server-advertised IP (often a
wrong NAT address) in favour of the control host.

FTPS forced two more decisions:
- The client is **hand-rolled and blocking** (`dosm/ftp/ftp_client.py`), run
  in a thread executor. Explicit FTPS needs the data connection to **reuse the
  control connection's TLS session** (strict servers: vsftpd
  `require_ssl_reuse=YES`). `ftplib.FTP_TLS` does not do this and asyncio's
  `start_tls` can't pass a session; blocking `ssl.wrap_socket(session=...)`
  can. That one capability is why the client exists.
- A clean TLS `close_notify` (`SSLSocket.unwrap()`) is required on the data
  socket or a `tls_data_required` server discards uploads as aborted.

**Backends sit behind a `FileTransferBackend` ABC** so the web browser, CLI,
and audit logging are backend-agnostic. `SftpBackend` is asyncssh-native and
tunnels through the chain for free via `connect_through_chain`. **Active mode
is unsupported** (server-connect-back is unroutable through a jump) — passive
only, documented, not built.

**File transfer is a host capability, not a host protocol.** A host keeps its
primary protocol (ssh/rdp/vnc for Guacamole) and *additionally* carries
`ft_method` / `ft_port` / `ft_credential_id` (override; falls back to the host
credential). `service.get_file_backend` selects the backend from `ft_method`
and resolves the effective port + credential (`resolve_ft_target`). This avoids
forcing a duplicate inventory entry just to SFTP into an existing SSH box. The
Files sidebar page lists hosts where `ft_method` is set.

**Adding a backend**: implement the ABC in `dosm/ftp/`, register it in
`dosm/ftp/service.py:get_file_backend`. Same shape as the monitoring/pipeline
adapter factories.

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
- **Auth: Okta OIDC SSO + local break-glass** (Phase 21/21b, branch
  `feature/rbac-okta-ad`; full reference in `docs/rbac-okta.md`). Authorization is the `viewer<operator<admin` role
  ladder, sourced from Okta's `groups` claim mapped via `rbac.group_role_map`.
  Still no MFA on the *local* path (rely on Okta for that). The Okta OIDC flow
  is exercised offline with a self-signed token but **not yet validated against
  a real Okta tenant** — confirm discovery, the groups claim, and redirect-URI
  registration against a live org before relying on it.
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
- **FTPS strict session-reuse is unproven against a real server.** The
  Phase 18 client *performs* data-channel TLS session reuse, but the test
  server (pyftpdlib) doesn't *enforce* `require_ssl_reuse=YES`. Confirm
  against a real vsftpd-behind-a-jump target when one is available. FTP/FTPS
  active mode is intentionally unsupported (unroutable through a jump).
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
- Apply minor UI polish where it doesn't change behavior
- Fix bugs the smoke tests reveal
