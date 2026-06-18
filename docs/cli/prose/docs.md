# prose: docs

## When to use

The docs subcommands manage the user's documentation vault at
`$DOSM_HOME/docs/` - the content the embedded LLM searches and quotes
in LLM-mode chats. Use `dosm docs new` to scaffold a runbook, `dosm docs
import` to convert a `.docx` / `.pdf` / `.md` / `.txt` into the vault,
and `dosm docs reindex` after bulk changes outside the watcher (the
running server already auto-indexes saved files).

`dosm docs status` reports embedder name, file counts, and the last
error - handy when reindex appears to hang.

`dosm docs install-cli-reference` copies this CLI reference into the
vault under `docs/_dosm-cli/` so the agent can RAG it. It runs
automatically on `dosm init`; re-run it after upgrading DOSM to refresh
the bundled docs.

## Examples

```bash
# Author a new runbook in $EDITOR, then index it
dosm docs new "Restart prod app server" --app fabric

# Import a Word doc into the 'fabric' folder
dosm docs import ./fabric-runbook.docx --app fabric --title "Fabric Runbook"

# Force a full re-embed (e.g. after switching embedder model)
dosm docs reindex --force

# Refresh the bundled CLI reference after upgrading DOSM
dosm docs install-cli-reference
dosm docs reindex

# Watch progress
dosm docs status
```

## Gotchas

- `dosm docs reindex` runs **synchronously** in the foreground. On a
  large vault with `--force`, this can take many minutes. The same
  reindex runs in the background when `dosm serve` starts (see
  `docs_index.auto_index_on_startup`); use the CLI form for batch
  imports or troubleshooting.
- `dosm docs new` opens `$EDITOR` (or `notepad.exe` on Windows / `vi`
  elsewhere). If your editor exits before saving, the seeded `# Title`
  file is still committed to the vault. Edit again with `$EDITOR` or
  delete the file manually.
- `--app` takes a folder *slug*, not a name. If the folder doesn't
  exist yet, the file lands in `_unfiled` until you create the folder
  and move it. Use `dosm folder create` to create a folder by name and
  let DOSM derive the slug.
- `dosm docs install-cli-reference` overwrites any prior contents of
  `$DOSM_HOME/docs/_dosm-cli/`. Don't hand-edit files there - your
  changes will be wiped on the next install.
