# DevOps Operations Suite Manager (DOSM)

A self-hosted, modular operations console for managing on-prem infrastructure —
Service Fabric, Dynatrace ActiveGates, SAS Linux servers, and whatever else
you bolt on via modules — with an embedded, CPU-friendly LLM that reads your
local documentation and can run in either chat or agent (propose-and-approve)
mode.

> Status: early scaffold. Phase 1 ships the app skeleton, `$DOSM_HOME`
> bootstrap, config loader, and a minimal dashboard. LLM, modules, and agent
> mode land in later phases.

## Design at a glance

- **Cross-platform app** (Windows + Linux). Individual modules may declare OS
  constraints — e.g. the Service Fabric module requires Windows/pwsh.
- **Modular integrations.** Each integration (Service Fabric, Dynatrace, SAS,
  ...) is a module dropped into `$DOSM_HOME/modules/` with its own
  `module.yaml`, routes, and agent-callable actions.
- **Local-first LLM.** Ollama by default (CPU-friendly model like
  `qwen2.5:7b-instruct`), with optional GPU. Retrieval is grounded in your
  local `docs/` directory — no outbound traffic required.
- **Two interaction modes.**
  - *LLM mode*: RAG chat with citations, read-only.
  - *Agent mode*: every action is a plan card (summary, command preview,
    expected effect, rollback). User approves / edits / rejects before
    execution. Approved actions feed an audit log that can be drafted into new
    runbooks.
- **Pluggable secrets.** Local age-encrypted file for solo setups; HashiCorp
  Vault for shared deployments. Same interface.
- **SQLite** for state (with `sqlite-vec` for embeddings) — one file under
  `$DOSM_HOME/data/`.

## `$DOSM_HOME` layout

```
$DOSM_HOME/
  config.yaml
  config/        secrets key, per-host config
  docs/          your documentation (indexed into the LLM)
    drafts/      LLM-authored runbook drafts pending review
  scripts/       scripts the agent can propose to run
  modules/       installed integration modules
  resources/     anything else
  data/
    app.db       SQLite (state + vector index)
    action_log/  audit trail of approved agent actions
  logs/
```

## Quick start (Phase 1)

```bash
# 1. Install in editable mode
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .

# 2. Create your DOSM_HOME
dosm init ./.dosm-home

# 3. Start the app
export DOSM_HOME=$(pwd)/.dosm-home   # Windows: set DOSM_HOME=...
dosm serve
```

Then open <http://127.0.0.1:8765>.

## Continue from another machine

The complete planning context, phase-by-phase log, open backlog with
recommended ordering, design rationale, and known limitations live in
the repo so they travel with the code:

- **[`CLAUDE.md`](CLAUDE.md)** — concise orientation any AI assistant
  should read first.
- **[`docs/ROADMAP.md`](docs/ROADMAP.md)** — full phase log, open backlog
  (with rationale), preserved design decisions, and known limits.

To pick up on a different machine:

```bash
git clone <repo-url>
cd DevOps-Operations-Suite-Manager
git checkout claude/devops-suite-llm-design-ZJ2aU

python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .

dosm init ./.dosm-home
export DOSM_HOME=$(pwd)/.dosm-home
dosm db init
dosm user create admin       # prompts for password
dosm serve                   # then open http://127.0.0.1:8765
```

Read `docs/ROADMAP.md` for what's done, what's queued, and the design
choices that shape the next move.

## Optional: Guacamole stack for browser SSH/RDP/VNC

DOSM signs short-lived JSON connection envelopes that Guacamole's
`guacamole-auth-json` extension consumes — your hosts and credentials stay in
DOSM, Guacamole is a dumb renderer.

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

# 5. Enable the integration in DOSM
# In $DOSM_HOME/config.yaml set:
#   guacamole:
#     enabled: true
#     base_url: "http://127.0.0.1:8080/guacamole"
# Restart DOSM. Each Host detail page now has a "Connect via Guacamole" button.
```

## Roadmap

1. ✅ Phase 1 — scaffold, bootstrap, minimal dashboard
2. ⏭ Phase 2 — SQLite + auth + secrets backend (local + Vault)
3. ⏭ Phase 3 — module loader contract + trivial example module
4. ⏭ Phase 4 — docs ingestion + RAG search
5. ⏭ Phase 5 — Ollama wiring + LLM chat mode
6. ⏭ Phase 6 — Agent mode: plan cards with Approve / Edit / Reject
7. ⏭ Phase 7 — Script runner with approval
8. ⏭ Phase 8 — Service Fabric module (PowerShell, Windows)
9. ⏭ Phase 9 — Observer: draft runbooks from approved action logs
