# prose: guacamole

## When to use

Only relevant if you've enabled the Guacamole integration
(`guacamole.enabled: true` in `config.yaml`) for browser SSH/RDP/VNC.

`dosm guacamole keygen` generates the 128-bit shared secret that
DOSM signs auth tokens with and that Guacamole's
`guacamole-auth-json` plugin verifies. Run it once during initial
Guacamole setup, then paste the printed hex into Guacamole's
`guacamole.properties`.

## Examples

```bash
# Generate the key (errors if one already exists)
dosm guacamole keygen

# Force overwrite (will invalidate every existing token)
dosm guacamole keygen --force
```

## Gotchas

- Re-running with `--force` invalidates every previously-issued
  Guacamole session token. Only do it during planned maintenance.
- The hex value printed must be pasted **exactly** into Guacamole's
  `guacamole.properties` as `json-secret-key: <hex>`. A trailing
  newline or extra space will silently break auth and produce
  unhelpful 403s.
