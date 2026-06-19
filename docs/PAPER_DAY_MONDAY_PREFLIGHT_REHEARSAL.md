# Paper-Day Monday Preflight Offline Rehearsal

## Scope

Rehearse the Monday 1-day attended paper diagnostic **handoff flow** — validator
invocation, report-generator invocation, failure-triage reference, and the
Reviewer handoff bundle — entirely offline, using synthetic fixtures. No real KIS,
no network, no runtime execution. The point is muscle memory and a verified
artifact shape before the real session, not a real run. The authoritative
PASS/NO_GO/FAIL criteria live in `docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md`; the
verdict here is reused verbatim from `ops/validate_paper_day_summary.py` via
`ops/render_paper_day_report.py`. The ordered in-session run sheet for the real
Monday run is `docs/PAPER_DAY_MONDAY_EXECUTION_CHECKLIST.md`; this rehearsal walks
the same validator/report/handoff steps offline first.

## Prohibitions

This rehearsal never runs any of:

```text
--live-kis
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

It touches only synthetic fixtures and a throwaway working directory; it reads no
`config` and no credential, and it does not modify the runtime hot-path files.

## Inputs

Synthetic, secret-free fixtures (added in RTM-7c.9), each a `RUN_DIR`-shaped
directory with `summary.json`, `evidence.jsonl`, and (where a clean run would
produce one) `stdout-envelope.json`:

```text
tests/fixtures/paper_day_reports/pass_startup_like/
tests/fixtures/paper_day_reports/no_go_health_not_ready/
tests/fixtures/paper_day_reports/fail_source_approval_failed/
tests/fixtures/paper_day_reports/needs_review_missing_envelope/
tests/fixtures/paper_day_reports/fail_sensitive_data_present/
```

`needs_review_missing_envelope/` intentionally has **no** `stdout-envelope.json` —
it rehearses the case where the envelope was not captured.

## Rehearsal matrix

| fixture | expected validator verdict | expected report section | expected triage action |
| --- | --- | --- | --- |
| `pass_startup_like` | PASS | no first failure | no retry / wait for the real Monday run |
| `no_go_health_not_ready` | NO_GO | first failure `health_not_ready` | inspect health/data gating |
| `fail_source_approval_failed` | FAIL | first failure `source_approval_failed` | check approval/key/domain, no immediate retry |
| `needs_review_missing_envelope` | NEEDS_REVIEW | missing envelope-only fields | re-capture envelope, do not hand-edit |
| `fail_sensitive_data_present` | FAIL | hard_fail `sensitive_data_present` | stop/escalate, do not print artifact contents |

## Commands

Rehearse one fixture by hand (offline, read-only). Replace `FIXTURE` with any row
from the matrix:

```bash
REHEARSAL_DIR="$(mktemp -d)"
FIXTURE="tests/fixtures/paper_day_reports/pass_startup_like"

cp "$FIXTURE/summary.json" "$REHEARSAL_DIR/summary.json"
cp "$FIXTURE/evidence.jsonl" "$REHEARSAL_DIR/evidence.jsonl"
if [ -f "$FIXTURE/stdout-envelope.json" ]; then
  cp "$FIXTURE/stdout-envelope.json" "$REHEARSAL_DIR/stdout-envelope.json"
fi

PYTHONPATH=src uv run python ops/validate_paper_day_summary.py \
  --summary "$REHEARSAL_DIR/summary.json" \
  --evidence "$REHEARSAL_DIR/evidence.jsonl" \
  --envelope "$REHEARSAL_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --json

PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$REHEARSAL_DIR/summary.json" \
  --evidence "$REHEARSAL_DIR/evidence.jsonl" \
  --envelope "$REHEARSAL_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$REHEARSAL_DIR/review-report.md"

cat "$REHEARSAL_DIR/review-report.md"
```

For `needs_review_missing_envelope`, **omit** `--envelope` intentionally (the
`cp` guard above already skips the absent file). The validator and report then
report `missing_from_persisted_summary` / `NEEDS_REVIEW` rather than inventing the
envelope-only fields.

Optional one-shot helper (does the copy + render + verdict check in one offline,
read-only step; writes only under `--work-dir`, never the source fixture):

```bash
REHEARSAL_DIR="$(mktemp -d)"
PYTHONPATH=src uv run python ops/rehearse_paper_day_handoff.py \
  --fixture tests/fixtures/paper_day_reports/pass_startup_like \
  --work-dir "$REHEARSAL_DIR" \
  --expect-verdict PASS
```

The helper refuses a `--work-dir` inside the repository `runtime/` tree unless
`--allow-runtime-dir` is given, so a rehearsal can never masquerade as a real run
artifact. Exit `1` on a verdict mismatch, `2` on a precondition error.

## Expected outputs

For each fixture the rehearsal should produce:

```text
summary.json exists in the working dir
evidence.jsonl exists in the working dir
stdout-envelope.json exists when the fixture provides one
validator command runs and prints the matrix verdict
report command runs and writes review-report.md
review-report.md First-failure section matches the matrix
the triage playbook points to the same first-failure stage
the handoff bundle checklist below is complete
```

## Handoff bundle checklist

Before reporting back to the Reviewer (rehearsal or real run), confirm:

```text
[ ] summary.json present
[ ] evidence.jsonl present
[ ] stdout-envelope.json present (or its absence explained -> NEEDS_REVIEW)
[ ] review-report.md rendered
[ ] verdict recorded (PASS / NO_GO / FAIL / NEEDS_REVIEW)
[ ] First-failure stage quoted from review-report.md when not PASS
[ ] triage action chosen from docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md when not PASS
[ ] no sensitive_data_present=true evidence row (else hard FAIL / escalate)
[ ] git status --short recorded
[ ] git ls-files runtime recorded (empty)
[ ] PILOT_EXIT captured (real run only; operator-supplied, not in artifacts)
```

## Failure handling

The rehearsal deliberately exercises non-PASS verdicts. Treat each exactly as the
real run would — classify the first failed stage with
`docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md`, never retry blindly, never change
strategy, never print artifact contents. A `sensitive_data_present` row is a hard
FAIL: stop, preserve, escalate. The synthetic per-verdict report shapes are in
`docs/examples/paper_day_reports/README.md`.

## Safety proof

```text
actual KIS network run by Cursor   0
actual 1-day pilot run by Cursor   0
live order                         0
paper diagnostic live run          0
runtime hot-path file modified     0
config / credential read           0
network opened                     0
fixture files mutated              0
writes outside --work-dir          0
```

This rehearsal is docs/offline tooling only. The 1-day attended paper diagnostic
remains HOLD until the Monday 2026-06-22 regular market session and NO-GO until a
Reviewer PASS.
