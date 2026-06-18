# DOSM - DevOps Operations Suite Manager

A self-hosted, modular operations console for managing on-prem
infrastructure - Service Fabric clusters, Dynatrace ActiveGates, SAS
Linux servers, generic SSH/RDP/VNC hosts, and whatever else you bolt on
via modules. Browser SSH/RDP through an embedded Guacamole stack, an
LLM grounded in your local documentation, and an agent mode where every
action is a plan card you Approve / Edit / Reject before it runs.

Local-first by design. SQLite for state, Ollama for the LLM, fastembed
for embeddings, Guacamole for browser sessions - nothing here requires
outbound traffic.

> Status: actively developed. See [`docs/ROADMAP.md`](docs/ROADMAP.md)
> for the phase-by-phase log, open backlog with rationale, design
> decisions, and known limitations.

---

## What's in the box

| Area | What you get |
|------|--------------|
| **Hosts inventory** | CRUD over hosts (SSH/RDP/VNC), free-form tags, named credential profiles backed by your secrets store, jump-host chains with cycle/protocol validation. |
| **Credential profiles** | Named variables (e.g. *"Service Fabric Japan Model"*) tied to a secret in the configured backend. Set the secret inline from the UI or via CLI. |
| **Browser sessions** | Apache Guacamole integration via a signed JSON envelope. SSH, RDP, VNC. Session recording on by default. Resource panel attached, showing live metrics for the actual remote host. |
| **In-app terminals** | Local PowerShell / cmd / bash launched inside the DOSM UI (admin-only) with xterm.js + a PTY bridge. Asciinema recording. *Run as another user* via `sudo` / `runas` wrappers. |
| **Documentation index** | Drop markdown / text / PDF into `$DOSM_HOME/docs/`; DOSM chunks, embeds with the on-CPU `bge-small-en-v1.5` ONNX model, and serves search with snippet citations. Falls back to keyword search if the embedder can't initialize. |
| **LLM chat** | RAG-grounded chat against your docs index, streamed via SSE, with inline citations to the source files. |
| **Agent mode** | The model proposes actions as `<plan>` blocks; each becomes a plan card the operator approves before execution. First action: `ssh_exec` (with a tiered allow-list and elevated-confirmation flow). Second: `run_pipeline`. |
| **Pipeline runner** | Register CI/CD pipelines, trigger them with inputs, watch status. v1 supports GitHub Actions; the adapter contract is provider-agnostic. |
| **Modules** | First-party `system_info` ships in the box. Drop additional modules into `$DOSM_HOME/modules/` with a `module.yaml` manifest; each can mount routes, register agent actions, and constrain itself by OS. |
| **Settings & CLI catalog** | Toggle which DevOps CLIs (Azure / AWS / gcloud / git / gh / Terraform / kubectl / Helm / Docker / Ansible / sfctl / pwsh / cmd / bash) appear as quick-launch terminals. |
| **Pluggable secrets** | `LocalEncryptedBackend` (Fernet, blobs in the app DB) or `VaultBackend` (HashiCorp KV v2). Same interface; selected in `config.yaml`. |
| **Audit log** | Every state-changing operation lands a row - auth, host CRUD, credential changes, agent plan card lifecycle, pipeline runs, terminal sessions, Guacamole connects. |

---

## Quick start

Requirements: **Python 3.11+**. Linux, macOS, or Windows.

```bash
# 1. Clone and install
git clone https://github.com/MBarc/DevOps-Operations-Suite-Manager
cd DevOps-Operations-Suite-Manager
git checkout claude/devops-suite-llm-design-ZJ2aU

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .

# 2. Bootstrap a DOSM_HOME (config + folder layout)
dosm init ./.dosm-home

# 3. Initialize the SQLite schema and create the first admin user
export DOSM_HOME=$(pwd)/.dosm-home   # Windows: set DOSM_HOME=...
dosm db init
dosm user create admin               # prompts for password

# 4. Run the server
dosm serve
```

Open <http://127.0.0.1:8765> and sign in.

---

## `$DOSM_HOME` layout

The single root directory holding all state, config, secrets, and user
content. Bind-mount this in production; back it up like any other
ops-critical directory.

```
$DOSM_HOME/
  config.yaml                       Main app config
  config/
    secrets.key                     Local-backend Fernet key (auto-generated)
    session.key                     Cookie signing secret (auto-generated)
    guacamole.key                   Guacamole auth-json shared secret (optional)
  docs/                             Indexed into the docs search + RAG chat
    drafts/                         (excluded from the index by default)
  scripts/                          Browseable scripts the agent can propose
  modules/                          User-installed modules (module.yaml each)
  resources/                        Anything else
  data/
    app.db                          SQLite (state, audit log, plan cards, etc.)
    index/                          Vector index workspace
    action_log/                     Audit-trail extras
    terminal_recordings/            Asciinema .cast files
    guacamole_recordings/           Mounted into the Guacamole container
  logs/
```

---

## CLI reference

`dosm --help` lists all commands. The most common:

```bash
# bootstrap & service
dosm init <path>                    # create a new DOSM_HOME
dosm serve [--host H --port N]      # run the web app
dosm version

# database
dosm db init                        # create tables (idempotent)

# users
dosm user create <name> [--role admin|operator|viewer]
dosm user list
dosm user passwd <name>

# secrets backend
dosm secret set <path>              # writes through the configured backend
dosm secret get <path>
dosm secret list [<prefix>]
dosm secret delete <path>

# credential profiles (DB rows that reference secret paths)
dosm credential add <name> --kind ssh_password --username svc --secret-ref ssh/prod/svc
dosm credential list

# documentation index
dosm docs reindex [--force]
dosm docs status

# modules
dosm module list

# Guacamole
dosm guacamole keygen               # generates the auth-json shared secret
```

---

## Optional: Ollama for chat / agent mode

Chat and agent modes call out to an Ollama HTTP endpoint. Without one,
both pages still render and the SSE stream surfaces a clean
"Ollama unreachable" error - the rest of the app keeps working.

```bash
# Run Ollama locally
ollama serve

# Pull a CPU-friendly tool-using model (the default in config.yaml)
ollama pull qwen2.5:7b-instruct
```

In `$DOSM_HOME/config.yaml`:

```yaml
llm:
  provider: ollama
  base_url: http://127.0.0.1:11434
  model: qwen2.5:7b-instruct
  embedding_model: bge-small-en-v1.5
```

Restart DOSM. The Chat sidebar entry now produces real responses; Agent
mode emits real `<plan>` blocks that become plan cards.

---

## Optional: Guacamole stack for browser SSH / RDP / VNC

DOSM signs short-lived JSON connection envelopes that Guacamole's
`guacamole-auth-json` extension consumes - your hosts and credentials
stay in DOSM, Guacamole is a dumb HTML5 renderer.

```bash
# 1. Generate the shared 128-bit secret (writes to $DOSM_HOME/config/guacamole.key)
dosm guacamole keygen

# 2. Copy the env template and fill in the secret + a Postgres password
cp .env.example .env
# edit .env: GUACAMOLE_JSON_SECRET_KEY = the hex from `dosm guacamole keygen`

# 3. Generate the Guacamole DB schema (one-time)
mkdir -p guacamole/initdb
docker run --rm guacamole/guacamole:1.5.5 /opt/guacamole/bin/initdb.sh --postgres \
  > guacamole/initdb/001-initdb.sql

# 4. Bring the stack up
docker compose up -d

# 5. Enable in DOSM (config.yaml)
guacamole:
  enabled: true
  base_url: "http://127.0.0.1:8080/guacamole"
```

Restart DOSM. Each Host detail page now has a *Connect via Guacamole*
button. Sessions through a jump host are tunneled through DOSM via a
shared, multiplexed SSH connection (`JumpTunnelManager`) so concurrent
sessions to many targets behind one jump don't fight over auth.

---

## Configuration overview

`config.yaml` has the following top-level sections (all optional -
defaults shown via `dosm init` are sensible):

| Section | What it controls |
|---------|------------------|
| `server` | host, port |
| `auth` | session cookie name, max age, secret file |
| `secrets` | `backend: local` or `vault`, key file or Vault address/mount/prefix |
| `llm` | Ollama base URL + model + embedding model |
| `docs_index` | chunk size, overlap, include/exclude globs, embedder choice, auto-index on startup |
| `terminals` | enabled, auto-detect, record by default, custom shells |
| `metrics` | poll interval, WinRM port / transport / timeout |
| `guacamole` | enabled, base URL, secret key file, recording dir, DOSM-reachable host, tunnel bind host |
| `ssh_command_policy` | allow-list of safe commands for the agent's `ssh_exec` action |
| `cli_tools` | `{tool_id: bool}` map populated by the Settings page |
| `enabled_modules` | module names to load on startup |

---

## Architecture at a glance

- **FastAPI + Jinja2 + HTMX-style server-rendered pages.** No SPA
  framework; the UI is HTML the server sends back, with a small amount of
  JS for xterm.js, SSE, and resource-panel WebSockets.
- **SQLAlchemy 2 + SQLite** with WAL, `synchronous=NORMAL`, 30s timeout,
  and `foreign_keys=ON`. Idempotent column-add migrations on startup.
- **Pluggable interfaces**: `SecretsBackend`, `MetricsSource`,
  `PipelineAdapter`, module `register(app, cfg)`. Each documented in its
  package.
- **`JumpTunnelManager`** keeps one persistent SSH connection per jump
  host alive in-process; many targets behind one jump multiplex over it.
- **Plan cards** (`PlanCard` model) gate every agent action. Tier
  (`safe`/`elevated`) classified at approve time; elevated requires
  typing the host name to confirm.
- **Audit log** rows accompany every state mutation in the same DB
  session as the change itself.

For a deeper tour and the design choices behind these picks, see
[`docs/ROADMAP.md`](docs/ROADMAP.md). For the complete CLI reference,
see [`docs/cli/`](docs/cli/) - every administrative action is
scriptable from the CLI, and the same reference is auto-installed
into the docs vault on `dosm init` so the agent can retrieve it. For
an AI assistant working in this repo, [`CLAUDE.md`](CLAUDE.md) is the
orientation file.

---

## License

MIT.
