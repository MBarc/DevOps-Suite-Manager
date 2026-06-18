# prose: top-level

## When to use

`dosm init` is the very first command on a new machine - it creates
`$DOSM_HOME` with the standard subdirectory layout and seeds a default
`config.yaml`. After that, the typical bootstrap is `dosm db init`, then
`dosm user create admin`, then `dosm serve`.

`dosm serve` is the long-running process. Everything administrative is also
exposed as a CLI subcommand so headless setup and scripts can run without
the web UI.

## Examples

```bash
# Fresh install
dosm init ~/.dosm-home
export DOSM_HOME=~/.dosm-home
dosm db init
dosm user create admin --role admin
dosm serve

# Override host/port for one-off binding
dosm serve --host 0.0.0.0 --port 9000

# Dev loop with auto-reload
dosm serve --reload
```

## Gotchas

- `dosm init --force` rewrites `config.yaml` and `README.md` to the
  shipped defaults. It does **not** touch `data/`, `docs/`, or `config/`
  secrets - those are preserved. Use it after upgrading DOSM if you want a
  clean default config to compare against.
- `dosm serve --reload` is for dev only - it watches the package source
  and restarts on every change. Don't use it in production; long-running
  state (jump tunnels, embedder cache) is process-local and gets dropped.
- The `DOSM_HOME` env var must be set for any subcommand other than
  `init`, `version`, and `serve --home <path>`. Set it once per shell or
  put it in your shell profile.
