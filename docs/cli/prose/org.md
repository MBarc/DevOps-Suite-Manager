# prose: org

## When to use

The `org` commands talk to the Organisation Directory (Phase 14) - an
Active Directory–backed view of departments and people, fetched via
PowerShell run on a configured AD jumpbox over WinRM.

Use `dosm org test-ad` first to verify the jumpbox is reachable and the
AD module is loaded. Then use `dosm org sync <slug>` to pull a single
department's members on demand (the web UI also exposes this). Use
`dosm org tree` and `dosm org find` to query the cached snapshot
without hitting AD.

## Examples

```bash
# Check the jumpbox + AD cmdlets
dosm org test-ad

# Sync one department by slug (configured under /org/configure)
dosm org sync platform-eng

# Show the cached member list
dosm org members platform-eng

# ASCII org chart
dosm org tree

# Search across all cached people
dosm org find "alice"
dosm org find "@example.com"
dosm org find "Senior Engineer"
```

## Gotchas

- `dosm org test-ad` and `dosm org sync` make a live WinRM connection.
  They will hang for several seconds waiting on a network timeout if the
  jumpbox is unreachable; budget for that in scripts.
- `dosm org members`, `tree`, and `find` are pure cache reads - they
  succeed even when the jumpbox is offline, but the data may be stale.
  Look at the `Department.last_synced_at` column (in the DB) or trigger
  a sync before relying on member lists.
- The slug argument is the URL fragment, not the human name. Find it
  via the web UI under `/org/<slug>` or by listing departments in the
  DB.
- `dosm org sync` does not yet do a full directory walk - sync each
  department individually. A bulk-sync subcommand is planned.
