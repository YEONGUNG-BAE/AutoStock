# Paper-Day Operator Dry-Run Rehearsal — offline command-flow walkthrough (no live KIS)

> **Offline / docs-only rehearsal.** This page exists to let a human Operator
> rehearse the *shape* of the next paper-day run — the variables, command order,
> and artifact paths in `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md` — **without
> executing any live command.** It reduces copy/paste and ordering mistakes before
> the real market session. It is reading and finger-tracing only.

## Hard prohibitions for this rehearsal

This rehearsal is **offline and docs-only**. While walking it:

```text
do not run live KIS
do not use network
do not run startup smoke
do not run the attended paper-day pilot
do not use secrets
do not print config contents
do not run live orders
do not activate runtime
```

Cursor/Claude must never execute the live command excerpts on this page. They are
shown only so the Operator can read and compare them. Live KIS is **Operator-only**,
during a regular KR market session, from the Operator's own shell.

## Synthetic example variables (placeholders, not a real run)

These are **placeholder values for rehearsal only**. They are not a real run's
date/label/duration. On the real Monday, the Operator picks fresh values per
`docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md`.

```bash
SESSION_DATE="<YYYY-MM-DD>"
RUN_LABEL="<operator-selected-label>"
DURATION_SECONDS="<bounded-duration>"
RUN_DIR="runtime/paper-day/$SESSION_DATE/$RUN_LABEL"
```

`RUN_DIR` is derived from `SESSION_DATE` and `RUN_LABEL`; every later command must
reuse that **same** `RUN_DIR`. Trace it by eye through each step below.

## Step-by-step finger-trace (read, do not execute)

### 1. Readiness checker — runs BEFORE the live command

The offline, network-free, read-only readiness checker must be run **first**, and
it must consume the same Operator-selected variables:

```bash
PYTHONPATH=src uv run python ops/check_next_paper_day_readiness.py \
  --session-date "$SESSION_DATE" \
  --run-label "$RUN_LABEL" \
  --duration-seconds "$DURATION_SECONDS" \
  --run-dir "$RUN_DIR" \
  --config config/config.toml \
  --json
```

Check by inspection: it uses `SESSION_DATE`, `RUN_LABEL`, `DURATION_SECONDS`, and
`RUN_DIR`, and it appears **above** the live run command in step 2. Do not proceed
to step 2 on a nonzero exit.

### 2. Live run command — Operator-only, DO NOT EXECUTE here

> ⚠️ **DO NOT EXECUTE THIS BLOCK IN CURSOR/CLAUDE.** This is the live KIS command.
> It is Operator-only, run during a regular KR market session from the Operator's
> own shell. It is reproduced here for path/variable comparison only. Cursor/Claude
> must never run it, and must never run any `--live-kis` command.

```bash
# OPERATOR-ONLY — live KIS — do not execute in Cursor/Claude
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
```

Check by inspection:
- it uses the **same** `RUN_DIR` as step 1;
- `--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"` is present (the
  validator-read envelope is produced by the flag, not the redirect);
- the shell redirect goes to `"$RUN_DIR/stdout-envelope.shell.json"` (the
  belt-and-suspenders console capture).

### 3. Validator + report — must read the captured envelope

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json

PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"
```

Check by inspection: both commands pass
`--envelope "$RUN_DIR/stdout-envelope.json"` — the same file written by the flag in
step 2. Without it the verdict cannot be PASS.

### 4. Reviewer handoff

Hand the `RUN_DIR` artifacts to the Reviewer following
`docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md`. The reproducible on-disk verdict
comes from the validator; keep any operator-attested terminal values separate and
labeled.

## Inspection checklist (rehearsal pass criteria)

Confirm all of the following by eye before you trust the live flow:

```text
[ ] readiness checker command appears BEFORE the live run command
[ ] readiness checker uses SESSION_DATE / RUN_LABEL / DURATION_SECONDS / RUN_DIR
[ ] live run command uses the same RUN_DIR
[ ] --stdout-envelope-out "$RUN_DIR/stdout-envelope.json" is present
[ ] shell redirect goes to "$RUN_DIR/stdout-envelope.shell.json"
[ ] validator and report both use --envelope "$RUN_DIR/stdout-envelope.json"
[ ] Reviewer handoff points to docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md
[ ] current status points to docs/PAPER_DAY_CURRENT_STATUS.md
```

## Monday morning final manual checks

On the real session day, in order:

```text
1. Read docs/PAPER_DAY_CURRENT_STATUS.md first (single go/no-go entry point).
2. Choose fresh SESSION_DATE / RUN_LABEL / DURATION_SECONDS / RUN_DIR
   (do not reuse a rehearsal placeholder or any prior run's values).
3. Run the offline readiness checker (step 1 above) and confirm a zero exit.
4. Operator confirms a regular KR market session and session_state=OPEN
   (the offline checker cannot verify live session state).
5. Only then may the Operator run the live command (step 2) — Operator-only,
   from the Operator's own shell, never from Cursor/Claude.
```

The full authoritative run sheet is `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md`. This
page only rehearses its shape; it never replaces it and never executes it.
