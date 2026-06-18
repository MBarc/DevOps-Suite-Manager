# prose: credential

## When to use

A *credential profile* is a named pointer that ties together: a kind
(`ssh_password`, `ssh_key`, `rdp_password`, `api_token`), an optional
username, and a `--secret-ref` path into the secrets backend. Hosts and
integrations refer to credentials by name; the actual secret stays in
the backend and is fetched on demand.

The typical flow is `dosm secret set …` first (so the backend has the
value), then `dosm credential add …` to register the profile.

## Examples

```bash
# Seed the secret, then register the credential
dosm secret set ssh/prod/admin
dosm credential add prod-admin --kind ssh_password \
    --username admin --secret-ref ssh/prod/admin

# SSH key auth - secret-ref points at the key material
dosm secret set ssh/keys/ops-bot
dosm credential add ops-bot --kind ssh_key \
    --username opsbot --secret-ref ssh/keys/ops-bot

# Verify
dosm credential list
```

## Gotchas

- `dosm credential add` does not validate the kind - pass one of the
  documented values exactly. A typo creates a credential that no host
  can use.
- The CLI does not yet expose `delete`, `update`, or `rename`. Use the
  web UI under **Credentials** for those operations; the CLI is
  add/list-only on purpose (delete is an audited destructive action).
- The `--secret-ref` value is the same string you used with
  `dosm secret set`. If you rotate by writing to a new path, update the
  credential to point at the new path and delete the old secret.
