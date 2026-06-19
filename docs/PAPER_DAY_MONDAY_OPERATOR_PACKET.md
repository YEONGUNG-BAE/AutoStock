# Paper-Day Monday Operator Packet — 2026-06-22 (1-Day Attended Paper Diagnostic)

This packet is the in-session run sheet for the **Operator** on Monday
**2026-06-22**. It is consumed by a human Operator during a regular KR market
session. Cursor/Claude never executes any step in this packet: no `--live-kis`,
no actual KIS network call, no live order, no daemon, no activation, no commit.

The KIS startup-only smoke has already reached PASS
(`runtime/paper-day/2026-06-18/startup-4`, `outcome=PASS`,
`stop_reason=startup_only`, `source_kind=kis_live`). That clears the readiness
gate for a Monday attended paper diagnostic — it does **not** authorize runtime
activation, automatic restart, or live orders.

## Preconditions

```text
- KIS startup-only smoke PASS completed (startup-4: PASS/startup_only/kis_live).
- HEAD must be the latest reviewed commit.
- git status clean except ignored config/runtime.
- git ls-files runtime is empty (no tracked runtime artifacts).
- config/config.toml is gitignored and present locally.
- KIS env values set in the SAME shell that runs the command.
- No secret values are printed to the terminal or any artifact.
- Regular KR market session only (do not run pre-open, post-close, or weekend).
```

Confirm repo state before anything else:

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
Never print the value. Re-export from the KIS portal using plain shell quotes.
In the observed startup-3 failure the lengths were `APP_KEY=38` / `APP_SECRET=182`;
after re-export they were `36` / `180` and startup-4 passed. A `source_approval_failed`
with all env vars present is the signature of this contamination.

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
Run only during the regular KR market session on Monday 2026-06-22.
Do not run after close.
Do not run as a daemon.
Do not auto-restart.
Run once, attended, bounded duration. Watch the terminal for the whole run.
```

## Run command

Use a fresh run directory. Do not reuse `startup-4` or any prior path.

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
  --confirm-attended-paper \
  --live-kis \
  --json > "$RUN_DIR/stdout-envelope.json"

PILOT_EXIT=$?
cat "$RUN_DIR/stdout-envelope.json"
echo "PILOT_EXIT=$PILOT_EXIT"
```

`<MARKET_SESSION_BOUNDED_DURATION>` is **not** settled by existing docs. The
Operator must set an explicit bounded duration (in seconds) that fits entirely
inside the regular session before running. Do not hardcode a final duration here.

Shell safety: create `RUN_DIR` with `mkdir -p` **before** the run so the
redirect target's parent exists, and use plain stdout redirection (`>`), not a
`tee` pipeline. Do not pipe through `tee` unless the shell and pipe status
handling are explicitly verified. Use stdout redirection so `$?` captures the
Python process exit code directly in **both bash and zsh** — the bash-only
`${PIPESTATUS[0]}` is not safe under macOS's default zsh (its analogue is
`$pipestatus[1]`), and a pipeline's `$?` reflects `tee`, not the Python process.

The persisted `summary.json` holds only the mechanical summary; the
cleanup/publication/lock fields (`summary_publication_outcome`, `cleanup_outcome`,
`runtime_lock_*`) appear only in the stdout envelope. The redirect above captures
that envelope to `stdout-envelope.json` so the offline validator can read every
clean-exit clause. (`--json` is single-line, so the captured file is a valid
single JSON object.) The stdout-envelope file is required for envelope-only
clause verification — see the Post-run collection note below.

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

## Post-run collection

```bash
cat "$RUN_DIR/summary.json"
cat "$RUN_DIR/evidence.jsonl"
git status --short
git ls-files runtime
find "$RUN_DIR" -maxdepth 2 -type f | sort
```

Then run the offline validator (network-free, secret-free, read-only). Pass the
captured stdout envelope so the publication/lock clauses are checked:

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json
```

If `stdout-envelope.json` is missing, empty, malformed, or captured from the
wrong run, the validator must not infer the envelope-only fields. It returns
`NEEDS_REVIEW` (`missing_from_persisted_summary`) when the envelope is absent, or
`NEEDS_REVIEW`/`FAIL` per its existing rules when the supplied JSON is malformed
or contradicts the persisted summary. In that case re-capture the stdout envelope
from the correct run (or treat the run as unverifiable) before claiming PASS — do
not hand-edit `stdout-envelope.json` to fill in the missing fields.

The validator verdict is advisory and mechanical; the PASS/NO_GO/FAIL criteria
below are authoritative.

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
`NEEDS_REVIEW`). `PILOT_EXIT` and `tracked_runtime` are not present in the
artifacts — the report labels them operator-supplied, so capture them from the
shell at run time. Include `$RUN_DIR/review-report.md` in the Reviewer handoff.
See `docs/PAPER_DAY_REVIEW_REPORT_TEMPLATE.md` for the section skeleton.

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
sensitive_data_present = false (every evidence row)
tracked runtime = 0
nonterminal_journal = 0
```

Also review the paper-only execution counters per existing runtime semantics:

```text
normalized_trades
normalized_quotes
health_hold / health_pass
trigger_evaluations
publication_slot_outcomes
journal_committed
orders
fills
nonterminal_journal
reason_counts
```

`orders > 0` is **not** required. A valid day can be PASS with zero paper orders
when trigger/health/decision conditions do not require an order, provided the
runtime closes cleanly and the evidence supports the result.

## NO_GO criteria

`outcome = NO_GO` (exit 1). The run reached the lock owner and produced
artifacts, but a readiness/completion gate failed. Interpret `stop_reason`:

```text
transport_not_ready      source ended / readiness not reached before required startup readiness
subscription_rejected    KIS rejected channel subscription — check tr_id / symbol / permission
health_not_ready         market health / readiness not sufficient
trade_not_observed        no trade tick observed before the completion verdict
quote_not_observed        no quote observed before the completion verdict
trigger_not_evaluated     trigger engine never evaluated
journal_uncertain         stop and review journal/ledger consistency
reconcile_required        stop and review journal/ledger consistency
nonterminal_journal       journal left non-terminal — review journal/ledger
resource_close_failure    cleanup/source lifecycle issue — do not rerun blindly
runtime_lock_exists       a duplicate runtime lock was detected (admission refused, zero files written)
```

NO_GO is not success. Do not retry blindly; isolate the gate that failed first.

## FAIL criteria

`outcome = FAIL` (exit 1), or any hard safety violation. Interpret `stop_reason`:

```text
source_config_gate_failed
  config/env issue. Confirm broker.kis_ws_read_only.enabled, env vars, symbol,
  approval_base_url / websocket_url. Do not retry until fixed.

source_approval_failed
  KIS approval issue. Check live vs. mock key mismatch, approval URL, app
  permission, and the quote-contamination gotcha. Do not print raw response.
  Do NOT immediately retry with the same settings.

source_connect_failed
  websocket connect/open issue. Check URL, DNS, TLS, network/firewall. Do not
  print raw frame. Do NOT immediately retry with the same settings.

source_failed
  unclassified factory/consumer fallback (likely a bug). Inspect evidence/summary
  and review code.

source_close_failed / source_close_timeout
  source lifecycle / cancellation issue. Treat the source as defective; do not
  retry in-process.

evidence_failed
  evidence write failure. No PASS summary file. Inspect filesystem/permissions.

summary_failed / summary_published_incomplete / summary_publication_uncertain
  publication-state issue. Manual artifact inspection required.

runtime_lock_parent_unreadable / runtime_lock_acquire_failed /
runtime_lock_acquire_uncertain / runtime_lock_release_failed /
runtime_lock_release_uncertain / runtime_lock_identity_mismatch
  lock-state issue. Do not delete the lock blindly unless the runtime contract
  says it is safe.

db_failed / internal_runtime_error
  runtime error. Inspect evidence/summary and review.
```

Hard safety failures (treat as FAIL regardless of `outcome`): any evidence row
with `sensitive_data_present=true`, `paper_only != true`,
`activation_authorized = true`, `automatic_restart = true`, or
`real_order_adapter_constructed = true`.

## Safety proof

For the Monday run, the handoff must show:

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

Report back to the Reviewer with:

```text
## Run identity
RUN_DIR, session_date, symbol, duration_seconds, HEAD commit
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
(the Safety proof block above, with observed values)
## Git/runtime hygiene
git status --short, git ls-files runtime
## Verdict
PASS / NO_GO / FAIL and the first failed stage if not PASS
```

Do not commit. The Operator commits after Reviewer review. The 1-day pilot
remains NO-GO until a Reviewer PASS on the Monday attended paper diagnostic.
