# Paper-Day Report Examples (synthetic)

These are **synthetic** example shapes of the Markdown report emitted by
`ops/render_paper_day_report.py` for each verdict the Monday 1-day attended paper
diagnostic can produce. They are illustrative fixtures, **not** real run output:
they contain no secret, app key/secret, approval key, raw HTTP response, raw
websocket frame, or traceback. Renderable copies of the same inputs live under
`tests/fixtures/paper_day_reports/` and are exercised by
`tests/test_paper_day_report_fixtures.py`.

Render any example yourself (offline, network-free, read-only):

```bash
PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary tests/fixtures/paper_day_reports/<name>/summary.json \
  --evidence tests/fixtures/paper_day_reports/<name>/evidence.jsonl \
  --envelope tests/fixtures/paper_day_reports/<name>/stdout-envelope.json \
  --expect-source-kind kis_live
```

To triage any non-PASS verdict, use
`docs/PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md`. `PILOT_EXIT` and `tracked_runtime`
are not present in the artifacts — the report labels them operator-supplied.

## PASS / startup_like (`pass_startup_like`)

Clean startup-only probe: both subscription ACKs, clean publication and lock
release, no sensitive evidence. `orders = 0` is fine.

```markdown
## Verdict

- **verdict: PASS**
- runtime outcome: PASS
- stop_reason: startup_only
- PILOT_EXIT: operator-supplied, not present in artifacts

## Clean-exit clauses

| clause | value | status |
| --- | --- | --- |
| summary_publication_outcome | WRITTEN | ok |
| cleanup_outcome | CLEAN | ok |
| runtime_lock_fd_closed | true | ok |
| runtime_lock_absent_confirmed | true | ok |
| runtime_lock_release_reason_code | null | ok |
| nonterminal_journal | 0 | ok |

## First failure

None observed.
```

## NO_GO / health_not_ready (`no_go_health_not_ready`)

Source ran and published cleanly, but the readiness/health gate never passed
(`health_hold` accumulated, `health_pass = 0`). The first failed stage is the
health gate.

```markdown
## Verdict

- **verdict: NO_GO**
- runtime outcome: NO_GO
- stop_reason: health_not_ready
- PILOT_EXIT: operator-supplied, not present in artifacts

## First failure

- stage: health
- reason_code: health_not_ready
- recorded_at: 2026-06-22T09:35:00+09:00
```

Triage: NO_GO taxonomy → `health_not_ready`. Inspect data availability and health
gating evidence. Do not retry outside the market session, do not change strategy,
do not force PASS.

## FAIL / source_approval_failed (`fail_source_approval_failed`)

KIS approval key issuance failed before the subscription stage; the summary was
never written. The cleanup/lock release still completed.

```markdown
## Verdict

- **verdict: FAIL**
- runtime outcome: FAIL
- stop_reason: source_approval_failed
- PILOT_EXIT: operator-supplied, not present in artifacts
- pass_blockers: summary_publication_outcome

## Clean-exit clauses

| clause | value | status |
| --- | --- | --- |
| summary_publication_outcome | NOT_WRITTEN | fail |
| cleanup_outcome | CLEAN | ok |
| ... | ... | ... |

## First failure

- stage: source
- reason_code: source_approval_failed
```

Triage: FAIL taxonomy → `source_approval_failed`. Check live/mock key mismatch,
approval URL, app permission, and the quote-contamination gotcha. Do not print the
raw HTTP response or the secret. Do **not** immediately retry with the same
settings.

## NEEDS_REVIEW / missing envelope (`needs_review_missing_envelope`)

The summary reads `outcome = PASS`, but no `stdout-envelope.json` was supplied, so
the cleanup/publication/lock clauses are `missing_from_persisted_summary`. The
verdict cannot be PASS.

```markdown
## Verdict

- **verdict: NEEDS_REVIEW**
- runtime outcome: PASS
- stop_reason: startup_only
- PILOT_EXIT: operator-supplied, not present in artifacts
- pass_blockers: missing_from_persisted_summary
- missing_from_persisted_summary: summary_publication_outcome, cleanup_outcome,
  runtime_lock_fd_closed, runtime_lock_absent_confirmed,
  runtime_lock_release_reason_code
- envelope: not provided — PASS cannot be confirmed (NEEDS_REVIEW for envelope clauses)

## Clean-exit clauses

| clause | value | status |
| --- | --- | --- |
| summary_publication_outcome | missing | missing |
| ... | ... | ... |
```

Triage: NEEDS_REVIEW is not PASS. Re-render with the captured envelope from the
correct run. Do not infer the envelope-only fields; do not hand-edit
`stdout-envelope.json`.

## FAIL / sensitive_data_present (`fail_sensitive_data_present`)

The mechanical outcome reads `PASS`, but an evidence row carries
`sensitive_data_present = true`. That is a **hard FAIL** regardless of outcome.

```markdown
## Verdict

- **verdict: FAIL**
- runtime outcome: PASS
- stop_reason: startup_only
- PILOT_EXIT: operator-supplied, not present in artifacts
- hard_fail: sensitive_data_present

## Paper-only safety proof

| field | value | status |
| --- | --- | --- |
| ... | ... | ... |
| sensitive_data_present_any | true (1 rows) | fail |
```

Triage: hard safety violation. Stop, preserve artifacts, escalate. Do not print
the captured content, do not retry. See the Paper-only safety violations and
Escalation policy sections of the triage playbook.
