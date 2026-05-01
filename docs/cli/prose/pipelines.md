# prose: pipelines

## When to use

`dosm pipelines poll` runs one tick of the background pipeline poller
synchronously and prints stats. Use it to smoke-test the poller wiring
without leaving `dosm serve` running, or to nudge state forward in
a headless context after triggering a pipeline.

In normal operation the poller runs inside `dosm serve` on a timer
(see Phase 11b in `docs/ROADMAP.md`).

## Examples

```bash
# Single tick
dosm pipelines poll
# polled=3 transitioned=1 abandoned=0 errors=0
```

## Gotchas

- `dosm pipelines poll` does **not** trigger pipelines — it only updates
  the recorded status of runs that are already in flight by hitting the
  provider API (e.g. GitHub Actions). To launch a run, use the web UI
  or the upcoming `dosm pipelines run` command.
- Errors during a poll are counted but not raised; check `errors=N` in
  the output and look at server logs for details.
