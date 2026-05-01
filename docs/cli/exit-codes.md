---
folder: dosm-cli
title: Exit codes
---

# Exit codes

DOSM CLI commands follow a simple convention:

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | A user-facing failure: missing record, conflict (e.g. duplicate name), invalid input, or an integration the command requires (AD jumpbox, secrets backend, file path) returned an error. The command prints a red error line before exiting. |

Unhandled exceptions propagate as a Python traceback with exit code `1` from
the interpreter — those are bugs; please file them. The CLI does not currently
distinguish between exit codes for different failure classes.

Per-command exit-code tables in `_generated/<group>.md` list the specific
conditions each command treats as `1`.
