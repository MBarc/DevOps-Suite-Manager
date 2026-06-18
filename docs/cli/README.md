---
folder: dosm-cli
title: DOSM CLI Reference
---

# DOSM CLI Reference

Complete reference for the `dosm` command-line interface. Every administrative
action exposed in the web UI is also reachable from the CLI; the CLI is the
canonical interface for scripting and automation.

The pages under [`_generated/`](_generated/) are produced by
[`scripts/gen_cli_docs.py`](../../scripts/gen_cli_docs.py) and combine:

- **Synopsis, arguments, options, exit codes** - extracted directly from
  `dosm/cli.py` via Typer/Click introspection. Cannot drift.
- **When to use, examples, gotchas** - hand-written prose from
  [`prose/`](prose/) that the generator splices in.

CI fails the build if the generated files are out of sync with `dosm/cli.py`.

## Command groups

| Group | Page | Purpose |
| --- | --- | --- |
| top-level | [top-level.md](_generated/top-level.md) | `dosm version`, `dosm init`, `dosm serve` |
| `db` | [db.md](_generated/db.md) | Database admin |
| `user` | [user.md](_generated/user.md) | Local user accounts |
| `secret` | [secret.md](_generated/secret.md) | Read/write secrets via the configured backend |
| `credential` | [credential.md](_generated/credential.md) | Credential profiles (named pointers into the secrets backend) |
| `docs` | [docs.md](_generated/docs.md) | Doc vault: scaffold, import, reindex, install CLI reference |
| `guacamole` | [guacamole.md](_generated/guacamole.md) | Guacamole integration helpers |
| `pipelines` | [pipelines.md](_generated/pipelines.md) | Pipeline runner |
| `folder` | [folder.md](_generated/folder.md) | Doc vault folder taxonomy |
| `org` | [org.md](_generated/org.md) | Organisation directory (AD-backed) |

See also: [exit-codes.md](exit-codes.md).

## For agents

The same pages are bundled inside the `dosm` package and installed into the
user's docs vault on `dosm init` (or via `dosm docs install-cli-reference`).
The agent retrieves them through the standard RAG pipeline. For structured
lookups (exact flags / exit codes), the agent has a `cli_help` query tool -
see [`dosm/agent/queries.py`](../../dosm/agent/queries.py).

## Regenerating

```bash
python scripts/gen_cli_docs.py
git diff docs/cli/_generated/   # review changes
```

If you add or change a CLI command in `dosm/cli.py`, also update the
matching `docs/cli/prose/<group>.md` if the change affects "When to use",
examples, or gotchas. CI will fail the build if you commit a `cli.py` change
without re-running the generator.
