# prose: folder

## When to use

A *folder* is the doc-vault taxonomy — usually one folder per
application or service area (e.g. `fabric`, `dynatrace`, `runbooks`).
Documents live under a folder so the UI and the LLM can scope searches.

Use `dosm folder create` to register a new bucket before importing or
authoring docs into it. Use `dosm folder list` to see slugs and doc
counts.

## Examples

```bash
# List the taxonomy
dosm folder list

# Create a folder (slug derived from name)
dosm folder create "Service Fabric"

# Create with explicit slug + description
dosm folder create "Active Gates" --slug activegates \
    --description "Dynatrace AG runbooks and notes"

# Delete (interactive confirmation; attached docs become unfiled)
dosm folder delete activegates
```

## Gotchas

- `dosm folder delete` re-parents docs to `_unfiled` rather than deleting
  them. Files on disk are untouched — only the DB association changes.
  Run `dosm docs reindex` afterward to pick up the new locations.
- Deleting a folder is irreversible from the CLI (no undo). The
  interactive confirmation prompt is the only safety net — don't
  pipe `yes` into it.
- Folder slugs must be URL-safe; `dosm folder create` derives one from
  the name via the same `slugify` helper the doc vault uses.
