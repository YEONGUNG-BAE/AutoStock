# Paper-Day Readiness Checker — NOT_READY troubleshooting (secret-safe, offline)

> **Read this when `ops/check_next_paper_day_readiness.py` prints `NOT_READY`
> and/or exits nonzero before a live command.** This guide explains, per hard
> check, what the failure means and the safe action to take — without ever
> leaking `config/config.toml` contents or any credential value. It does **not**
> tell you to run live KIS, and it does **not** tell you to bypass the checker.

## What the readiness checker is (and is not)

The readiness checker is **offline, network-free, and read-only**. It never opens
a network connection, never imports a live KIS source/client path, never runs
startup smoke or an attended pilot, never mutates/creates/deletes any file, and
**never reads or prints `config/config.toml` contents or any secret value**. For
the four KIS environment variables it inspects only *metadata* — presence,
length, strip-cleanliness, placeholder status — never the value itself. It shells
out to `git` only for read-only queries.

It **must be run before any live command**, every session, from the Operator's
own shell:

```bash
PYTHONPATH=src uv run python ops/check_next_paper_day_readiness.py \
  --session-date "$SESSION_DATE" \
  --run-label "$RUN_LABEL" \
  --duration-seconds "$DURATION_SECONDS" \
  --run-dir "$RUN_DIR" \
  --config config/config.toml \
  --json
```

The verdict prints `READY` (exit `0`) only when every hard check passes, or
`NOT_READY` (exit nonzero) with a `hard_failures` list naming the checks that
failed. The checker is advisory about *shape*; it cannot and does not verify live
market-session state — the Operator must still confirm a regular KR market
session with `session_state=OPEN` at run time.

## Do not bypass

```text
A nonzero readiness checker exit means: DO NOT run the live command.
Do not edit, comment out, or skip the checker to make it pass.
Do not proceed until either:
  - the NOT_READY cause is genuinely resolved and a re-run exits 0, or
  - the Operator deliberately classifies it as an intentional NO_GO and stops.
```

A `NOT_READY` is a stop signal, not a warning to click through. If you cannot
resolve a hard failure safely, the correct outcome is **NO_GO** (do not run),
not a forced run.

## Hard-check reference

Each row is a check name as it appears in `hard_failures` / the `checks` list.
"Safe action" never involves printing a secret or bypassing the checker.

| Check name | What a failure means | Safe action |
| --- | --- | --- |
| `repo_head_readable` | `git rev-parse HEAD` did not return a valid 40-char commit. Repo state is unreadable or you are not in the repo root. | `cd` to the repo root; confirm it is a git repo. Do not run live until HEAD reads as the current reviewed commit. |
| `git_status_clean` | `git status --short` reports dirty tracked entries. The detail lists `path + status code` only (no file contents, no secrets). `config/config.toml` is gitignored and will not appear. | Review each listed path. Do **not** delete blindly and do **not** commit runtime artifacts. Commit or stash *intended* reviewed changes; investigate anything unexpected before proceeding. |
| `runtime_untracked` | `git ls-files runtime` found tracked files under `runtime/`. Runtime artifacts must never be committed. | Stop and review. Do not run live with tracked runtime present. Untrack/remove from the index after review (do not commit runtime); confirm `git ls-files runtime` is empty before re-running the checker. |
| `config_exists` | The `--config` path (default `config/config.toml`) does not exist. Contents are never read — only existence is checked. | Restore the local config file at the expected path in the Operator shell. Do not print its contents. Do not commit it (it must stay gitignored). |
| `config_untracked` | `config/config.toml` is tracked by git; it must stay gitignored so secrets are never committed. | Stop. Remove it from git tracking (keep the local file) and confirm `.gitignore` still ignores it. Never print or commit its contents. |
| `env:KIS_LIVE_APP_KEY` | The app key env var is missing, empty, a placeholder, or has leading/trailing whitespace (`strip_same=false`). The value is never inspected or printed. | Fix in the Operator shell. See **Env var failures** below — inspect only presence/length/strip_same/placeholder, never the value. |
| `env:KIS_LIVE_APP_SECRET` | The app secret env var is missing/placeholder/whitespace-contaminated. Value never inspected/printed. | Fix in the Operator shell. See **Env var failures** below. |
| `env:KIS_LIVE_ACCOUNT` | The account env var is missing/placeholder/whitespace-contaminated. Value never inspected/printed. | Fix in the Operator shell. See **Env var failures** below. |
| `env:KIS_WS_READONLY_CONFIRM` | The websocket read-only confirm env var is missing/placeholder/whitespace-contaminated. Value never inspected/printed. | Fix in the Operator shell. See **Env var failures** below. |
| `session_date_valid` | `SESSION_DATE` does not parse as `YYYY-MM-DD`. | Re-export `SESSION_DATE` as a valid `YYYY-MM-DD` for a regular KR market day. |
| `run_label_valid` | `RUN_LABEL` is empty or not a safe path component (must match `[A-Za-z0-9._-]+`, and not `.`/`..`). | Re-export `RUN_LABEL` as a fresh, safe label unique within the day (e.g. `day-1`, `run-a`). |
| `duration_valid` | `DURATION_SECONDS` is not a positive integer. | Re-export `DURATION_SECONDS` as a positive integer that fits entirely inside the regular session. |
| `run_dir_matches` | `RUN_DIR` does not equal `runtime/paper-day/$SESSION_DATE/$RUN_LABEL`. | Re-derive `RUN_DIR="runtime/paper-day/$SESSION_DATE/$RUN_LABEL"` from the same variables; do not hand-set a divergent path. |
| `run_dir_no_stale_artifacts` | `RUN_DIR` already contains `summary.json`, `evidence.jsonl`, `stdout-envelope.json`, or `db` from a prior run. | Choose a **fresh** `RUN_DIR` (new `RUN_LABEL`). Do not delete a prior run's artifacts blindly and do not overwrite them — pick a new label and re-run the checker. |

## Env var failures (secret-safe)

When any `env:KIS_*` check fails:

```text
- DO NOT paste the value anywhere.
- DO NOT print the value (no echo, no log, no commit).
- ONLY inspect the non-secret metadata the checker reports:
    present / length / strip_same / placeholder
- Fix it in the Operator shell:
    * missing      -> export it (from the KIS portal) in the SAME shell that
                      will run the command
    * placeholder  -> replace the leftover template value (e.g. "...",
                      "YOUR_KEY", "YOUR_SECRET", "PLACEHOLDER") with the real one
    * strip_same=false -> re-export with no leading/trailing whitespace
- Quote-contamination gotcha: if APP_KEY/APP_SECRET length is two characters
  longer than expected, copied quote characters were likely included around the
  value. Never print the value — re-export using plain shell quotes.
```

All four KIS env vars must end up `present` (length > 0), `strip_same=true`, and
`placeholder=false` before the checker will pass.

## Dirty git / tracked runtime / stale RUN_DIR (do not delete blindly)

```text
- Do not delete files blindly to make git_status_clean / runtime_untracked pass.
- Do not commit runtime artifacts, ever.
- If git_status_clean fails: review each listed path; commit/stash only intended
  reviewed changes; investigate anything unexpected before proceeding.
- If runtime_untracked fails (tracked runtime exists): STOP and review before
  proceeding — confirm git ls-files runtime is empty after the fix.
- If run_dir_no_stale_artifacts fails: choose a FRESH RUN_DIR (new RUN_LABEL)
  rather than deleting or overwriting a prior run's artifacts.
```

## After fixing

Re-run the readiness checker with the same variables. Only a clean `READY` /
exit `0` clears the gate. If you cannot reach `READY` safely, the outcome is a
deliberate **NO_GO** — do not run the live command.

The authoritative run sheet is `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md`; the
single go/no-go entry point is `docs/PAPER_DAY_CURRENT_STATUS.md`; rehearse the
command flow offline with `docs/PAPER_DAY_OPERATOR_DRY_RUN_REHEARSAL.md`.
