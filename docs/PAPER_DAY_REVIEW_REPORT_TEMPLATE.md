# Paper-Day Review Report Template

This is the skeleton emitted by `ops/render_paper_day_report.py`. The generator
is offline and read-only — it opens no network, reads no `config`/credential, and
mutates nothing except the explicit `--out` file. The PASS / NO_GO / FAIL /
NEEDS_REVIEW verdict is **reused verbatim** from
`ops/validate_paper_day_summary.py` (`classify`); the report never recomputes it.

Generate it after a run (see `docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md`):

```bash
PYTHONPATH=src uv run python ops/render_paper_day_report.py \
  --summary "$RUN_DIR/summary.json" \
  --evidence "$RUN_DIR/evidence.jsonl" \
  --envelope "$RUN_DIR/stdout-envelope.json" \
  --expect-source-kind kis_live \
  --out "$RUN_DIR/review-report.md"
```

Without `--envelope`, the cleanup/publication/lock clauses are
`missing_from_persisted_summary` and the verdict cannot be PASS (it is reported
`NEEDS_REVIEW`). A missing/empty/malformed envelope is treated as absent — the
generator never invents envelope-only fields.

Section skeleton (values shown are illustrative placeholders):

```markdown
# Paper Day Diagnostic Review Report

## Run identity
| field | value |
| --- | --- |
| run_id | <run_id> |
| session_date | 2026-06-22 |
| symbol | 005930 |
| market | KR |
| source_kind | kis_live |
| schema_version | paper_day_diagnostic.v1 |

## Verdict
- **verdict: PASS | NO_GO | FAIL | NEEDS_REVIEW**
- runtime outcome: <outcome>
- stop_reason: <stop_reason>
- PILOT_EXIT: operator-supplied, not present in artifacts
- (hard_fail / pass_blockers / missing_from_persisted_summary when applicable)

## Clean-exit clauses
| clause | value | status |
| --- | --- | --- |
| summary_publication_outcome | WRITTEN | ok |
| cleanup_outcome | CLEAN | ok |
| runtime_lock_fd_closed | true | ok |
| runtime_lock_absent_confirmed | true | ok |
| runtime_lock_release_reason_code | null | ok |
| nonterminal_journal | 0 | ok |

## Source readiness
| counter | value |
| --- | --- |
| connect_attempts / connected / subscription_requests / subscription_acks |
| subscription_rejections / all_subscribed / disconnects |
(plus source_* reason counts, or "none")

## Paper-only safety proof
| field | value | status |
| --- | --- | --- |
| paper_only | true | ok |
| activation_authorized | false | ok |
| automatic_restart | false | ok |
| real_order_adapter_constructed | false | ok |
| sensitive_data_present_any | false (0 rows) | ok |
| tracked_runtime | operator-supplied, not present in artifacts | n/a |

## Evidence timeline
| recorded_at | stage | event | reason_code |
(compact; truncated past --max-timeline-rows)

## First failure
(first event == failed_closed row, or "None observed.")

## Counters
normalized_trades, normalized_quotes, health_hold, health_pass,
trigger_evaluations, publication_slot_outcomes, journal_committed, orders, fills
(plus reason_counts and timestamps). Absent counters are shown as "missing",
distinct from an explicit 0.

## Orders and fills
- orders: <value|missing>
- fills: <value|missing>
`orders > 0` is not required for PASS.

## Journal/completion state
nonterminal_journal, stop_reason, journal_committed, and any
journal_uncertain / reconcile_required reason counts.

## Publication and lock state
summary_publication_outcome, summary_publication_reason_codes,
runtime_lock_fd_closed, runtime_lock_absent_confirmed,
runtime_lock_identity_matched, runtime_lock_release_reason_code.

## Operator git/runtime hygiene
(git status --short and git ls-files runtime — run and record manually.)

## Reviewer checklist
- [ ] PILOT_EXIT captured immediately
- [ ] summary/envelope consistency reviewed
- [ ] evidence has no sensitive_data_present=true
- [ ] source readiness reviewed
- [ ] paper-only flags reviewed
- [ ] journal/completion state reviewed
- [ ] git status --short reviewed
- [ ] git ls-files runtime reviewed
- [ ] no live order path observed

## Remaining NO-GO items
(verdict note when not PASS; pilot NO-GO until Reviewer PASS; live order /
activation / auto-restart prohibited.)
```

`PILOT_EXIT` and `tracked_runtime` are **not** present in the artifacts — the
Operator supplies them from the shell at run time. The report labels them so the
Reviewer never mistakes their absence for a verified value.
