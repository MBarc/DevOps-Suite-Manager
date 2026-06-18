# prose: user

## When to use

DOSM uses local accounts only - there is no SSO. Create an `admin` user
first; only admins can open in-app terminals or approve agent plan cards.
Use `operator` for day-to-day operators who need pipeline/host access but
should not get terminal/agent privileges, and `viewer` for read-only.

## Examples

```bash
# Initial admin
dosm user create admin --role admin

# Add an operator (will prompt for password)
dosm user create alice --role operator

# Non-interactive password (e.g. from a secrets manager)
dosm user create bot --role viewer --password "$BOT_PASSWORD"

# List everyone
dosm user list

# Reset a password
dosm user passwd alice
```

## Gotchas

- `dosm user create` does **not** disable an existing user - it errors
  out if the username already exists. To recover from a forgotten password,
  use `dosm user passwd <username>`.
- Passing `--password` on the command line leaks the password into shell
  history and `ps`. Prefer the interactive prompt or read from an env
  var that you scrub afterward.
- The first user you create should be an admin; without one, the web UI
  is unusable.
