# Project context for AI assistants

This file orients a new Claude Code (or any AI coding assistant) session on
this repository. Read it first.

## What this project is

DOSM (DevOps Operations Suite Manager) is a self-hosted Python web app for
managing on-prem infrastructure (Service Fabric, Dynatrace ActiveGates, SAS
Linux servers, generic SSH/RDP/VNC hosts). It bundles:

- a modular host inventory with credential profiles and jump-box chains
- in-app PowerShell/cmd/bash terminals (admin-only) with session recording
- Apache Guacamole integration for browser SSH/RDP/VNC sessions
- a local docs index with RAG search (fastembed + numpy cosine)
- an embedded LLM (Ollama) with two modes:
  - **LLM mode**: RAG chat grounded in the docs index, with citations
  - **Agent mode**: every action is a *plan card* the operator
    Approves / Edits / Rejects before execution. First action is
    `ssh_exec`, second is `run_pipeline`.
- pluggable secrets backend (local Fernet-encrypted, or HashiCorp Vault)
- pipeline runner (GitHub Actions today; Azure DevOps / Octopus / AWX /
  Terraform Cloud planned as adapters)
- per-host live metrics panel (Linux SSH source + Windows WinRM source)

The user is building this primarily for themselves; coworkers may use later.
Local-first, on-prem-friendly throughout: SQLite, Ollama, Guacamole all run
without outbound traffic.

## How to find things fast

- Phase-by-phase status, backlog, design decisions, known limitations →
  **`docs/ROADMAP.md`** (read this every new session)
- High-level architecture and the working CLI/web flow → `README.md`
- Each phase has its own commit with a detailed message - `git log --oneline`
  shows the chronology, `git show <hash>` gives the rationale

## Conventions in this repo

- **Phases ship as one focused commit** with a thorough message that captures
  the "why" - preserve this rhythm. The commit is also the changelog.
- **Smoke tests are inline bash blocks**, not pytest. End every meaningful
  change with a smoke that exercises both the happy path and at least one
  error path against the live server. There is no automated test suite yet.
- **`dosm` CLI** (`dosm/cli.py`) is the canonical admin interface - every
  user-facing action exposed in the UI should also be reachable from the CLI
  where it makes sense.
- **Audit log everything that mutates state.** Insert an `AuditLog` row in
  the same DB session as the change. Look at any existing route for the
  pattern.
- **Integrations live in core, organized by domain.** Monitoring adapters
  in `dosm/monitoring/adapters/`, pipeline adapters in `dosm/pipelines/`,
  metrics sources in `dosm/metrics/`. Each domain has its own ABC; new
  integrations slot in there. (The pluggable `dosm/modules/` system was
  retired - it had only ever shipped one example module.)
- **Don't push to `main`** without explicit user permission. The working
  branch is `claude/devops-suite-llm-design-ZJ2aU`.
- **Honor `dosm/migrations.py` for schema changes.** It's an idempotent
  column-add helper. Anything more complex (renames, FK changes) needs
  Alembic - call that out instead of working around it.

## Things to be careful about

- **The agent has not been validated against a live LLM.** Every code path
  is exercised in mock/error mode but real model output will misformat plan
  blocks, propose dangerous commands, hallucinate hosts. The plan card UX
  will need iteration once a real Ollama instance is wired up.
- **No automated tests.** Refactoring is risky. If you change the agent
  registry, secrets backend, or jump tunnel manager, smoke-test it against
  a live server before shipping.
- **Process-local state**: `JumpTunnelManager`, `_embedder` cache,
  ephemeral run-as registry. These don't survive a restart and don't share
  across instances. Don't accidentally introduce a feature that assumes
  cross-process state.
- **Auth is local-only.** Don't surface anything new under a non-admin role
  unless it's truly safe for an operator.
- **Do not delete `.dosm-home`** in any user environment. The smoke-test
  pattern of `rm -rf .dosm-home && dosm init …` is fine for the dev
  sandbox; in real use it would wipe their secrets, hosts, plan cards,
  pipeline history, and chat transcripts.

## Where the user is in the build

See `docs/ROADMAP.md` for the authoritative status. As of the last commit:
- Phases 1 through 11 are shipped (with sub-phases 8b, 8c).
- Open: 11b (background poller), 11c–f (more pipeline adapters), 13
  (certificate inventory), 14 (organization graph), 15 (documentation
  vault & importer), 8d (session recordings browser).
- The user's stated preference: do **tests + CI** (Phase 10.5) before
  loading more features on, then probably 11b. They've not committed yet -
  ask before assuming.
