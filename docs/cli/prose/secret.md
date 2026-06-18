# prose: secret

## When to use

`dosm secret` reads and writes through the configured secrets backend
(`local` Fernet-encrypted file or HashiCorp `vault`). Use it to seed
credential values that `dosm credential` records will reference.

The path you pass (`ssh/prod/admin`) is the **logical key** in the
backend - for the local backend it becomes a row in the encrypted
file; for Vault it becomes a path under the configured mount/prefix.

## Examples

```bash
# Set a value (prompts for the secret with confirmation)
dosm secret set ssh/prod/admin

# Set non-interactively (avoid this in shells with history)
dosm secret set api/github/token --value "$GH_TOKEN"

# Read it back to verify
dosm secret get ssh/prod/admin

# List everything under a prefix
dosm secret list ssh/

# Remove
dosm secret delete ssh/old/admin
```

## Gotchas

- `dosm secret get` prints the cleartext value to stdout. Don't run it
  in a screen-share or in a terminal whose output is being recorded.
- Deleting a secret that's still referenced by a `Credential` row leaves
  the credential pointing at a dead reference - the next operation that
  needs the secret will fail loudly. Run `dosm credential list` first
  to see what's in use.
- For the local backend, the encryption key file is at
  `$DOSM_HOME/config/secrets.key`. Lose that file and every secret is
  unrecoverable. Back it up out-of-band.
- For the Vault backend, the env var named in
  `secrets.vault_token_env` (default `VAULT_TOKEN`) must be set when the
  CLI runs. There is no fallback to other Vault auth methods yet.
