# Paper-Day Monday Execution Checklist — HISTORICAL (2026-06-22, pre-pilot)

> **HISTORICAL — do not use as the current entry point.** This is the run sheet
> that was prepared for the **2026-06-22** Monday diagnostic. The 1-day attended
> paper diagnostic has since been performed (pilot-3, 2026-06-26, clean operator
> terminal PASS; H0STASP0 parser fix verified). It is retained for its frozen
> safety criteria and step ordering. **Before any future market-session run it
> must be refreshed** with an Operator-selected session date, RUN_DIR, and
> duration during a regular KR market session. See
> `docs/PAPER_DAY_PILOT_EVIDENCE_LOG.md` for current state and
> `docs/PAPER_DAY_OPERATOR_RUNBOOK.md` for the live capture pattern.

This document froze and ordered everything prepared in RTM-7c.7–7c.10 (Operator
packet, offline validator, report generator, failure triage playbook, offline
rehearsal) into one in-session run sheet. Cursor/Claude never executes any step
here — it is consumed by a human Operator during a regular KR market session.

## Status

```text
KIS startup-only readiness: PASS
startup run: runtime/paper-day/2026-06-18/startup-4
1-day attended paper diagnostic: PERFORMED (pilot-3 2026-06-26, operator terminal PASS)
pilot-3 parser fix: VERIFIED (on-disk evidence)
pilot-3 formal reproducible verdict: NEEDS_REVIEW (stdout-envelope.json not captured)
this 2026-06-22 run sheet: HISTORICAL — refresh before any future run
live order: prohibited
runtime activation: prohibited
automatic restart: prohibited
```

## Scope

```text
This document does not authorize live orders.
This document does not authorize runtime activation.
This document only organizes the attended paper diagnostic.
```

The authoritative PASS/NO_GO/FAIL criteria live in
`docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md`; this checklist orders the steps and
restates the criteria for in-session use.

For phone/AnyDesk copy-paste operation, use
`docs/PAPER_DAY_MOBILE_REMOTE_OPERATOR_PACKET.md`.

## Prohibitions

Cursor/Claude never runs any of the following — only the human Operator runs the
market-session command, and only during the live session:

```text
--live-kis (executed by Cursor/Claude)
actual KIS network
actual startup smoke retry
actual 1-day pilot
live order
paper diagnostic live run
daemon
automatic restart
runtime activation
credential value logging
raw HTTP response logging
raw websocket frame logging
secret persistence
runtime behavior change
```

## Runtime freeze

```text
Do not change runtime hot-path files after this checklist is accepted unless
Reviewer explicitly reopens the runtime lane.
```

Frozen files:

```text
src/composition/attended_paper_day.py
ops/run_attended_paper_day.py
src/data/kis_ws_source.py
src/data/kis_ws_auth.py
src/broker/kis_transport.py
```

## Required HEAD

```text
Expected HEAD: latest reviewed RTM-7c.11 commit
Before RTM-7c.11 commit exists, expected base: 957303fe4666415eafff2d5ba771b856b28d7876
```

Paste the actual `git rev-parse HEAD` output into the handoff before running on
Monday, and confirm it is the latest reviewed commit.

## Pre-market checks

```bash
git rev-parse HEAD
git status --short
git ls-files runtime
PYTHONPATH=src uv run pytest tests/test_attended_paper_day.py
```

Env check (length / strip / placeholder only — never print a value):

```bash
PYTHONPATH=src uv run python - <<'PY'
import os
for k in ["KIS_LIVE_APP_KEY", "KIS_LIVE_APP_SECRET", "KIS_LIVE_ACCOUNT", "KIS_WS_READONLY_CONFIRM"]:
    v = os.environ.get(k, "")
    print(k, "len=", len(v), "strip_same=", v == v.strip(), "placeholder=", v in {"...", "YOUR_KEY", "YOUR_SECRET", "PLACEHOLDER"})
PY
```

Config check:

```bash
PYTHONPATH=src uv run python - <<'PY'
from config.settings import load_settings
ws = load_settings("config/config.toml").broker.kis_ws_read_only
print("enabled=", ws.enabled)
print("approval_base_url_set=", bool(ws.approval_base_url))
print("websocket_url_set=", bool(ws.websocket_url))
PY
```

```text
Never print secret values.
```

The `strip_same=False` flag is the quote-contamination guard (copied quote
characters around the key/secret). Re-export from the KIS portal with plain shell
quotes and re-check; never print the value. See the runbook's
quote-contamination note (startup-3 lengths `38`/`182` → startup-4 `36`/`180`).

## Market-session run

Operator-only command (tool-written envelope — RTM-7c.12 pattern):

```bash
RUN_DIR="runtime/paper-day/2026-06-22/day-1"
mkdir -p "$RUN_DIR"

PYTHONPATH=src uv run python ops/run_attended_paper_day.py \
  --config config/config.toml \
  --session-date 2026-06-22 \
  --symbol 005930 \
  --duration-seconds <MARKET_SESSION_BOUNDED_DURATION> \
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
`RUN_DIR` itself (the validator reads this file), so capture no longer depends on
a manual redirect that can be forgotten — the 2026-06-26 pilot-3 gap. The
`> "$RUN_DIR/stdout-envelope.shell.json"` redirect is a belt-and-suspenders
console capture only.

```text
Do not pipe through tee.
Do not use bash-only PIPESTATUS under zsh.
Use fresh RUN_DIR.
Set <MARKET_SESSION_BOUNDED_DURATION> before running.
```

## Immediate exit capture

Capture `PILOT_EXIT` immediately after the command returns (it is operator-supplied
and is **not** present in the artifacts). Record it in the handoff before running
anything else.

```text
PILOT_EXIT=$?   # captured directly from the Python process via stdout redirection
```

## Post-run artifact collection

Existence gate first — all four artifacts must be present before validation, and
the working tree must be clean of tracked runtime:

```bash
test -f "$RUN_DIR/summary.json"         && echo "summary.json OK"        || echo "summary.json MISSING"
test -f "$RUN_DIR/evidence.jsonl"       && echo "evidence.jsonl OK"      || echo "evidence.jsonl MISSING"
test -f "$RUN_DIR/stdout-envelope.json" && echo "stdout-envelope.json OK" || echo "stdout-envelope.json MISSING"
test -d "$RUN_DIR/db"                   && echo "db OK"                  || echo "db MISSING"
test -z "$(git status --short)"         && echo "git clean OK"          || echo "git DIRTY"
test -z "$(git ls-files runtime)"       && echo "runtime untracked OK"  || echo "runtime TRACKED"
```

A missing `stdout-envelope.json` means the five clean-exit clauses cannot be
verified from disk, so the offline validator reports `NEEDS_REVIEW`
(`missing_from_persisted_summary`). Do not hand-edit the envelope to backfill it —
re-capture from a fresh run.

```bash
cat "$RUN_DIR/summary.json"
cat "$RUN_DIR/evidence.jsonl"
cat "$RUN_DIR/stdout-envelope.json"
git status --short
git ls-files runtime
find "$RUN_DIR" -maxdepth 2 -type f | sort
```

## Offline validation

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json
```

## Report rendering

```bash
PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"

cat "$RUN_DIR/review-report.md"
```

## Failure triage

```text
If verdict is not PASS, use docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md.
Quote the First failure section from review-report.md.
Do not retry blindly.
Do not change strategy.
Do not run live orders.
Do not activate runtime.
```

## Reviewer handoff

The Operator must provide:

```text
HEAD
PILOT_EXIT
summary.json
evidence.jsonl
stdout-envelope.json
review-report.md
validator output
git status --short
git ls-files runtime
find "$RUN_DIR" -maxdepth 2 -type f | sort
```

## PASS criteria

A clean PASS requires **all** of:

```text
PILOT_EXIT = 0
outcome = PASS
summary_publication_outcome = WRITTEN
cleanup_outcome = CLEAN
runtime_lock_fd_closed = true
runtime_lock_absent_confirmed = true
runtime_lock_release_reason_code = null
source_kind = kis_live
paper_only = true
activation_authorized = false
automatic_restart = false
real_order_adapter_constructed = false
sensitive_data_present = false in all evidence rows
tracked runtime = 0
nonterminal_journal = 0
```

```text
orders > 0 is not required for PASS.
```

A valid day can be PASS with zero paper orders when trigger/health/decision
conditions do not require an order and the runtime closes cleanly.

## NO_GO / FAIL / NEEDS_REVIEW handling

```text
NO_GO  -> readiness/completion gate failed; isolate the first failed gate.
FAIL   -> runtime/source/publication/lock failure or hard safety violation.
NEEDS_REVIEW -> envelope-only fields missing/uncapturable; re-capture, do not invent.
```

Classify the first failed stage with `docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md`
and follow its retry/escalation policy. NO_GO/FAIL/NEEDS_REVIEW are not PASS; the
1-day pilot remains NO-GO until a Reviewer PASS.

## Safety proof

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
sensitive_data_present any row     false
tracked runtime                    0
daemon / auto-restart              none
```

Do not commit. The Operator commits after Reviewer review. The 1-day pilot remains
NO-GO until a Reviewer PASS on the Monday attended paper diagnostic.
