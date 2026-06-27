# Paper-Day Reviewer Intake Checklist — Reviewer-side acceptance of an Operator handoff

> **Reviewer-side checklist.** Use this **after** an Operator completes a future
> attended paper-day run (per `docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md`) and hands
> you the `RUN_DIR` artifacts. It tells the Reviewer exactly what to verify, with
> what commands, to turn an Operator handoff into a reproducible verdict — **without
> ever requesting secrets, raw frames, or live access.**

The Reviewer's job is to confirm, from on-disk artifacts only, that the run is a
clean paper-only diagnostic and that the reproducible validator verdict is PASS.
Everything below is **offline, network-free, secret-free, and read-only**. The
Reviewer never runs live KIS, never activates runtime, and never edits artifacts.

A future live run, if any, is for **envelope/runbook validation only** — parser
verification (H0STASP0 62-field live quote) is already complete on disk and is not
re-litigated here.

Export the handed-off run directory once before running any command below:

```bash
RUN_DIR="<OPERATOR_HANDED_OFF_RUN_DIR>"   # e.g. runtime/paper-day/<SESSION_DATE>/<RUN_LABEL>
```

## Required artifacts

Confirm every artifact the Operator handed off is present in `RUN_DIR` before
trusting any verdict:

```bash
test -f "$RUN_DIR/summary.json"         && echo "summary.json OK"
test -f "$RUN_DIR/evidence.jsonl"       && echo "evidence.jsonl OK"
test -f "$RUN_DIR/stdout-envelope.json" && echo "stdout-envelope.json OK"
test -d "$RUN_DIR/db"                   && echo "db OK"
test -f "$RUN_DIR/review-report.md"     && echo "review-report.md OK (if rendered)"
```

- `summary.json` — the persisted run summary (paper-only counters, identity).
- `evidence.jsonl` — the per-row evidence stream.
- `stdout-envelope.json` — the tool-written envelope (clean-exit clauses +
  `_envelope_capture`); produced by `--stdout-envelope-out`, not a shell redirect.
- `db/` — the run's database directory.
- `review-report.md` — the rendered Reviewer report, **if** the Operator rendered
  it. If absent, render it yourself (see "Report render command").

A missing `stdout-envelope.json` means the clean-exit clauses are not
disk-verifiable: the validator returns `NEEDS_REVIEW`
(`missing_from_persisted_summary`) and the run **cannot** be claimed PASS. Do not
hand-edit an envelope to backfill the fields — send it back for re-capture.

## Required terminal values from the Operator

The Operator's handoff text must include these observed terminal values:

- `PILOT_EXIT` — the process exit code (`$?`) of the attended run.
- The printed `stdout-envelope` **or** the `RUN_DIR/stdout-envelope.json` file
  path — so the persisted envelope can be matched against what the Operator saw.

These are operator-attested terminal values. Keep them labeled as attested and
verify them against the on-disk artifacts; do not fabricate a `stdout-envelope.json`
from the attested text if the file is missing.

## Git / runtime hygiene

The handoff tree must be clean and carry no tracked runtime artifacts:

```bash
git status --short
git ls-files runtime
```

Both must be empty. A nonempty `git status --short` (beyond ignored
config/runtime) or any tracked runtime file is a hygiene failure — do not PASS.

## Validator command (offline, reproducible verdict)

Run the offline validator and **pass the captured stdout envelope** so the
publication/cleanup/lock clauses are checked from disk:

```bash
PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json
```

The validator verdict is the reproducible on-disk verdict. Commit/record that
verdict; keep any operator-attested terminal values separate and labeled.

## Report render command

If the Operator did not include `review-report.md`, render it yourself with the
**same** captured envelope (the verdict is reused verbatim from the validator and
never recomputed):

```bash
PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"
```

Without `--envelope`, the cleanup/publication/lock clauses are
`missing_from_persisted_summary` and the verdict cannot be PASS. See
`docs/PAPER_DAY_REVIEW_REPORT_TEMPLATE.md` for the section skeleton.

## Same-run envelope checks

The `stdout-envelope.json` you validate **must come from the same run** as its
`summary.json` and `evidence.jsonl`. The validator cross-checks the envelope's
identity against the summary on these fields:

- `run_id`
- `session_date`
- `symbol`
- `_envelope_capture.run_id`

All four must match the summary. A wrong-run envelope (copied, reused, or
hand-edited from a prior pilot, a different `RUN_DIR`, or a different symbol/date)
is blocked as `envelope_run_mismatch` and returns `NEEDS_REVIEW` — it can **never**
be PASS. If you see `envelope_run_mismatch`, send the run back for re-capture from
the correct run; do not copy, reuse, or hand-edit `stdout-envelope.json` to make it
match.

## PASS blockers (verdict is NEEDS_REVIEW, not PASS)

If the validator reports any of these in `pass_blockers`, the verdict is at best
`NEEDS_REVIEW` and the run cannot be accepted as PASS:

- `missing_from_persisted_summary` — envelope absent / clauses not on disk.
- `envelope_malformed` — supplied envelope JSON is unreadable/contradictory.
- `envelope_run_mismatch` — envelope is from the wrong run.
- `summary_publication_outcome` not `WRITTEN`.
- `cleanup_outcome` not `CLEAN`.
- runtime lock clauses missing or failing (`runtime_lock_fd_closed`,
  `runtime_lock_absent_confirmed`, `runtime_lock_release_reason_code`).

## Hard FAIL signals (immediate FAIL — safety violation)

Any of these is an immediate, non-negotiable FAIL. They indicate a safety
violation, not merely an incomplete handoff:

- `sensitive_data_present` is `true` on any evidence row.
- `paper_only` is `false`.
- `activation_authorized` is `true`.
- `real_order_adapter_constructed` is `true`.
- `automatic_restart` is `true`.
- `orders` / `fills` nonzero.
- `nonterminal_journal` nonzero.

## NO_GO examples (run never produced a verdictable diagnostic)

These mean the run should not have proceeded / produced no valid diagnostic
window; treat as NO_GO, not FAIL:

- `invalid_session_window`.
- market closed / non-`OPEN` session (`session_state != OPEN`).
- health not ready.

## Explicit prohibitions (Reviewer side)

The Reviewer must **never**:

- request or paste **secrets** (app keys, app secrets, approval keys, account
  values, tokens).
- request **raw websocket frames / payloads** or raw field values.
- request **URLs, tokens, app keys, approval keys, accounts, or tracebacks**.
- hand-edit any artifact (`summary.json`, `evidence.jsonl`,
  `stdout-envelope.json`, `db/`) to make a verdict pass.
- rerun **live KIS** from Cursor/Claude — live KIS is Operator-only.

If a verdict is not PASS, classify the first failed stage via
`docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md` and return the run to the Operator with
the specific blocker/hard-fail signal. Do not work around it by editing artifacts.
