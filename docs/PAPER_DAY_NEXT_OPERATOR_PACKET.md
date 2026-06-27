# Paper-Day Next Operator Packet — CURRENT reusable run sheet (1-Day Attended Paper Diagnostic)

> **CURRENT next-session packet.** This is the reusable run sheet for the next
> regular KR market session. It supersedes the historical
> `docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md` (2026-06-22) and
> `docs/PAPER_DAY_MONDAY_EXECUTION_CHECKLIST.md` (2026-06-23) — those carry frozen
> dates and must not be run as-is. This packet carries **no baked-in date**: the
> Operator fills in every run-specific value before running.

The 1-day attended paper diagnostic has already been performed: pilot-3 on
2026-06-26 reached a clean operator-observed terminal **PASS**, and the H0STASP0
62-field live quote parser fix is **verified** on disk. Pilot-3's formal
reproducible verdict remains `NEEDS_REVIEW` only because `stdout-envelope.json`
was not captured that day; that capture gap is now closed at the tooling level
(`--stdout-envelope-out`, hardening commit `7d70ba0`, locally verified). See
`docs/PAPER_DAY_PILOT_EVIDENCE_LOG.md`.

**Any future live run is for envelope/runbook validation only — not for parser
verification, which is already complete.** This packet is consumed by a human
Operator during a regular KR market session. Cursor/Claude never executes any step
here: no `--live-kis`, no actual KIS network call, no live order, no daemon, no
activation, no automatic restart, no commit. Live KIS is **Operator-only**.

For phone/AnyDesk copy-paste operation, use
`docs/PAPER_DAY_MOBILE_REMOTE_OPERATOR_PACKET.md`.

## Operator-selected run variables

This packet has no default date, label, duration, RUN_DIR, or commit. Before
anything else, the Operator chooses all five for **this** run:

```text
SESSION_DATE       a regular KR market day (YYYY-MM-DD), chosen for this run
RUN_LABEL          a fresh label unique within that day (e.g. day-1, run-a)
DURATION_SECONDS   an explicit bounded duration (seconds) that fits entirely
                   inside the regular session — not settled by any doc, set per run
RUN_DIR            a fresh directory: runtime/paper-day/$SESSION_DATE/$RUN_LABEL
HEAD               the current reviewed commit (do not run a stale/unreviewed HEAD)
```

Export them once in the shell that will run the pilot (do not reuse a prior run's
values, and do not reuse any earlier RUN_DIR such as `startup-4` or a pilot-3 path):

```bash
SESSION_DATE="<OPERATOR_SELECTED_YYYY_MM_DD>"
RUN_LABEL="<OPERATOR_SELECTED_LABEL>"
DURATION_SECONDS="<OPERATOR_SELECTED_BOUNDED_SECONDS>"
RUN_DIR="runtime/paper-day/$SESSION_DATE/$RUN_LABEL"
mkdir -p "$RUN_DIR"
```

`DURATION_SECONDS` is **not** settled by existing docs. The Operator must set an
explicit bounded duration that fits entirely inside the regular session before
running. Do not hardcode a final duration here.

## Preconditions

```text
- KIS startup-only smoke PASS already on record (startup-4: PASS/startup_only/kis_live).
- HEAD must be the current reviewed commit.
- git status clean except ignored config/runtime.
- git ls-files runtime is empty (no tracked runtime artifacts).
- config/config.toml is gitignored and present locally.
- KIS env values set in the SAME shell that runs the command.
- No secret values are printed to the terminal or any artifact.
- Regular KR market session only (do not run pre-open, post-close, or weekend).
- If a full pilot sees session_state != OPEN, treat it as
  NO_GO/invalid_session_window and do not retry live KIS from Cursor/Claude.
```

Confirm repo state and that HEAD is the reviewed commit before anything else:

```bash
git rev-parse HEAD
git status --short
git ls-files runtime
PYTHONPATH=src uv run pytest tests/test_attended_paper_day.py
```

Stop if there are unexpected dirty tracked files, any tracked runtime file, or a
targeted pytest failure.

## Env/config check

Never print the values. Confirm only length, strip-cleanliness, and that no
placeholder is present.

```bash
PYTHONPATH=src uv run python - <<'PY'
import os
for k in ["KIS_LIVE_APP_KEY", "KIS_LIVE_APP_SECRET", "KIS_LIVE_ACCOUNT", "KIS_WS_READONLY_CONFIRM"]:
    v = os.environ.get(k, "")
    print(k, "len=", len(v), "strip_same=", v == v.strip(), "placeholder=", v in {"...", "YOUR_KEY", "YOUR_SECRET", "PLACEHOLDER"})
PY
```

All four must be present (`len > 0`), `strip_same=True`, and `placeholder=False`.

**Quote contamination gotcha.** If `APP_KEY` or `APP_SECRET` length unexpectedly
carries two extra characters, suspect copied quote characters around the value.
Never print the value. Re-export from the KIS portal using plain shell quotes. A
`source_approval_failed` with all env vars present is the signature of this
contamination.

Also confirm the websocket read-only config is enabled (no secret printed):

```bash
PYTHONPATH=src uv run python - <<'PY'
from config.settings import load_settings
ws = load_settings("config/config.toml").broker.kis_ws_read_only
print("enabled=", ws.enabled)
print("approval_base_url_set=", bool(ws.approval_base_url))
print("websocket_url_set=", bool(ws.websocket_url))
PY
```

## Market-session timing

```text
Run only during a regular KR market session (session_state=OPEN) on SESSION_DATE.
Do not run pre-open, after close, or on a weekend.
Do not run as a daemon.
Do not auto-restart.
Run once, attended, bounded duration. Watch the terminal for the whole run.
```

## Next-session readiness check (offline, run before the live command)

Before the live run, run the offline readiness checker. It is **offline,
network-free, and read-only**: it never opens a network connection, never imports
a live KIS source/client path, never runs startup smoke or an attended pilot, and
never reads or prints `config/config.toml` contents or any secret value. It
inspects only env-var *metadata* (present / length / strip_same / placeholder) and
read-only `git` queries, and confirms the Operator-selected run variables.

```bash
PYTHONPATH=src uv run python ops/check_next_paper_day_readiness.py \
  --session-date "$SESSION_DATE" \
  --run-label "$RUN_LABEL" \
  --duration-seconds "$DURATION_SECONDS" \
  --run-dir "$RUN_DIR" \
  --config config/config.toml \
  --json
```

It checks: repo HEAD is readable, `git status --short` is clean, `git ls-files
runtime` is empty, the config path exists (contents never read) and
`config/config.toml` stays untracked/gitignored, the four required env vars
(`KIS_LIVE_APP_KEY`, `KIS_LIVE_APP_SECRET`, `KIS_LIVE_ACCOUNT`,
`KIS_WS_READONLY_CONFIRM`) are present/strip-clean/non-placeholder, `SESSION_DATE`
parses as `YYYY-MM-DD`, `RUN_LABEL` is a safe path component, `DURATION_SECONDS` is
a positive integer, `RUN_DIR` equals `runtime/paper-day/$SESSION_DATE/$RUN_LABEL`,
and `RUN_DIR` holds no stale `summary.json`/`evidence.jsonl`/`stdout-envelope.json`/`db`.
The checker exits `0` only when every hard check passes; do not proceed to the live
run on a nonzero exit. It also reminds you that a **regular KR market session with
session_state=OPEN must still be confirmed by the Operator at run time** — the
offline checker cannot verify live session state.

## Run command

Use the fresh Operator-selected `RUN_DIR`. Do not reuse `startup-4` or any prior
path.

```bash
PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml \
  --session-date "$SESSION_DATE" \
  --symbol 005930 \
  --duration-seconds "$DURATION_SECONDS" \
  --evidence-out "$RUN_DIR/evidence.jsonl" \
  --summary-out "$RUN_DIR/summary.json" \
  --db-dir "$RUN_DIR/db" \
  --stdout-envelope-out "$RUN_DIR/stdout-envelope.json" \
  --confirm-attended-paper \
  --live-kis \
  --json > "$RUN_DIR/stdout-envelope.shell.json"

PILOT_EXIT=$?
cat "$RUN_DIR/stdout-envelope.json"
echo "PILOT_EXIT=$PILOT_EXIT"
```

`--stdout-envelope-out` makes the tool persist `stdout-envelope.json` under
`RUN_DIR` itself, after the run completes, removing the dependency on a manual
shell redirect that can be forgotten or mishandled (the 2026-06-26 pilot-3 gap).
The tool-written envelope is the full `--json` payload — the clean-exit clauses
(`summary_publication_outcome`, `cleanup_outcome`, `runtime_lock_*`) stay at top
level so the validator and report read them unchanged — plus a reserved
`_envelope_capture` block holding the process `exit_code`, `run_id`, the
summary/evidence/db paths, a **secret-sanitized** argv, a `captured_at` timestamp,
and the git HEAD. It carries no secrets, env values, raw KIS frames, URLs, app
keys/secrets, approval keys, account values, or tracebacks. The
`stdout-envelope.json` file is **produced by the flag, not by the shell redirect**;
the `--json > "$RUN_DIR/stdout-envelope.shell.json"` redirect is only a
**belt-and-suspenders console capture**. The tool-written `stdout-envelope.json` is
the file the validator reads.

Shell safety: create `RUN_DIR` with `mkdir -p` **before** the run so the redirect
target's parent exists, and use plain stdout redirection (`>`), not a `tee`
pipeline. Use stdout redirection so `$?` captures the Python process exit code
directly in **both bash and zsh** — the bash-only `${PIPESTATUS[0]}` is not safe
under macOS's default zsh (its analogue is `$pipestatus[1]`), and a pipeline's `$?`
reflects `tee`, not the Python process.

## Immediate stop conditions

Stop the run / do not retry blindly if any of these are observed:

```text
real-order adapter constructed
credential / raw-frame / raw-HTTP-response leak (any sensitive_data_present=true)
unexpected network route
journal uncertain
reconcile required
nonterminal journal stuck
ledger invariant failure
evidence write failure
resource close failure
activation_authorized = true
automatic_restart = true
```

## Post-run existence gate

First confirm every required artifact exists and the tree is clean. All six checks
must hold before the validator verdict can be trusted as PASS:

```bash
test -f "$RUN_DIR/summary.json"         && echo "summary.json OK"
test -f "$RUN_DIR/evidence.jsonl"       && echo "evidence.jsonl OK"
test -f "$RUN_DIR/stdout-envelope.json" && echo "stdout-envelope.json OK"
test -d "$RUN_DIR/db"                   && echo "db OK"
test -z "$(git status --short)"         && echo "git tree clean OK"
test -z "$(git ls-files runtime)"       && echo "no tracked runtime OK"
```

A missing `stdout-envelope.json` means the clean-exit clauses are not
disk-verifiable: the validator returns `NEEDS_REVIEW`
(`missing_from_persisted_summary`) and the run cannot be claimed PASS. Do not
hand-edit an envelope to backfill the fields — re-run capture from the correct run.

**Same-run envelope only.** The `stdout-envelope.json` you validate must come from
this same `RUN_DIR`/run as its `summary.json` and `evidence.jsonl`. Do not copy,
hand-edit, or reuse a `stdout-envelope.json` from another run (a prior pilot, a
different `RUN_DIR`, or a different symbol/date). The validator cross-checks the
envelope's identity against the summary — `run_id`, `session_date`, `symbol`, and
the reserved `_envelope_capture.run_id` — and a wrong-run envelope yields
`NEEDS_REVIEW` with `envelope_run_mismatch`; it can never be PASS.

```bash
cat "$RUN_DIR/summary.json"
cat "$RUN_DIR/evidence.jsonl"
cat "$RUN_DIR/stdout-envelope.json"
git status --short
git ls-files runtime
find "$RUN_DIR" -maxdepth 2 -type f | sort
```

## Offline validator + report

Run the offline validator (network-free, secret-free, read-only). Pass the
captured stdout envelope so the publication/lock clauses are checked:

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json
```

If `stdout-envelope.json` is missing, empty, malformed, or captured from the wrong
run, the validator must not infer the envelope-only fields. It returns
`NEEDS_REVIEW` (`missing_from_persisted_summary`) when the envelope is absent, or
`NEEDS_REVIEW`/`FAIL` per its existing rules when the supplied JSON is malformed or
contradicts the persisted summary. A **wrong-run** envelope — one whose `run_id`,
`session_date`, `symbol`, or `_envelope_capture.run_id` does not match the
summary — is blocked as `envelope_run_mismatch` and returns `NEEDS_REVIEW`; it can
never be PASS. Re-capture from the correct run before claiming PASS — do not copy,
reuse, or hand-edit `stdout-envelope.json`.

Then render the offline Reviewer report (same offline, network-free, secret-free,
read-only guarantee; the verdict is reused verbatim from the validator and never
recomputed). Pass the same captured stdout envelope:

```bash
PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"
```

Without `--envelope` the report's cleanup/publication/lock clauses are
`missing_from_persisted_summary` and the verdict cannot be PASS (it is reported
`NEEDS_REVIEW`). Include `$RUN_DIR/review-report.md` in the Reviewer handoff. See
`docs/PAPER_DAY_REVIEW_REPORT_TEMPLATE.md` for the section skeleton.

If the verdict is not PASS, classify the first failed stage using
`docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md`. The detailed PASS / NO_GO / FAIL
criteria and `stop_reason` taxonomies are authoritative in
`docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md` (retained as historical safety/runbook
reference) and `docs/PAPER_DAY_OPERATOR_RUNBOOK.md`. Do not retry blindly; isolate
the first failed stage first.

## Offline rehearsal (optional, before the live run)

To rehearse the validator/report/handoff flow before the session, walk the offline
synthetic rehearsal in `docs/PAPER_DAY_MONDAY_PREFLIGHT_REHEARSAL.md`. It is
**offline, synthetic, network-free, and not a live KIS run** — it touches only
fixtures and a throwaway working directory, never `config`, credentials, or the
network. The rehearsal fixtures live under
`tests/fixtures/paper_day_reports/<fixture_name>/` (e.g.
`tests/fixtures/paper_day_reports/pass_startup_like`). The rehearsal helper's
`--fixture` flag takes that **directory path**, not a bare fixture key.

## Safety prohibitions

This run is a bounded, attended paper diagnostic for **envelope/runbook validation
only**. It is not parser verification (already complete), and it is not activation.
The following are prohibited:

```text
no live orders
no activation
no daemon
no automatic restart
Operator-only live KIS (Cursor/Claude never runs --live-kis)
no raw frame / payload / field-value / URL / token / app key / approval key /
  account / traceback logging
```

The handoff must show:

```text
live order                         0
runtime activation                 0
activation_authorized              false
automatic_restart                  false
real_order_adapter_constructed     false
paper_only                         true
secret persisted                   0
raw HTTP response persisted        0
raw websocket frame persisted      0
traceback persisted                0
sensitive_data_present (any row)   false
tracked runtime                    0
daemon / auto-restart              none
```

## Handoff format

The Reviewer accepts this handoff using
`docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md`, which lists exactly what to verify
on the `RUN_DIR` artifacts (offline, secret-free, no raw frames). Report back to
the Reviewer with:

```text
## Run identity
RUN_DIR, SESSION_DATE, RUN_LABEL, symbol, DURATION_SECONDS, HEAD commit
## Outcome
PILOT_EXIT, outcome, stop_reason, source_kind
## Clean-exit clauses
summary_publication_outcome, cleanup_outcome, runtime_lock_fd_closed,
runtime_lock_absent_confirmed, runtime_lock_release_reason_code
## Paper-only counters
normalized_trades, normalized_quotes, health_*, trigger_evaluations,
publication_slot_outcomes, journal_committed, orders, fills, nonterminal_journal,
reason_counts
## Validator verdict
PASS / NO_GO / FAIL / NEEDS_REVIEW (+ pass_blockers / hard_fail / first_failure)
## Reviewer report
attach $RUN_DIR/review-report.md (rendered by ops/render_paper_day_report.py)
## Safety proof
(the Safety prohibitions block above, with observed values)
## Git/runtime hygiene
git status --short, git ls-files runtime
## Verdict
PASS / NO_GO / FAIL and the first failed stage if not PASS
(classify the first failed stage via docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md)
```

Do not commit. The Operator commits after Reviewer review.
