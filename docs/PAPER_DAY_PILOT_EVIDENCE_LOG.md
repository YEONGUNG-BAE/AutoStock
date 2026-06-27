# Paper-Day Pilot Evidence Log

Committed record of attended 1-day live paper-day diagnostic runs. Runtime
artifacts (`runtime/paper-day/.../`) are gitignored; this log preserves the
verifiable evidence and the formal offline verdict for each run.

The formal verdict is the one produced by `ops/validate_paper_day_summary.py`
from the **persisted on-disk artifacts** (`summary.json` + `evidence.jsonl`).
Per the runbook, the five clean-exit clauses (`summary_publication_outcome`,
`cleanup_outcome`, `runtime_lock_*`) live only in the run's stdout envelope and
are never written to `summary.json`. When `stdout-envelope.json` was not captured
to disk, those clauses are `missing_from_persisted_summary` and the reproducible
verdict is `NEEDS_REVIEW` — even when the operator observed a clean `PASS` in the
live terminal. Envelope-only values transcribed from the operator's terminal are
recorded below as **operator-attested** and are explicitly not on-disk evidence.

---

## 2026-06-26 — pilot-3

**Headline: the H0STASP0 62-field live quote parser fix is verified by on-disk
evidence.** 46,865 live KIS quote frames were each normalized 1:1
(`quote_frames == normalized_quotes`), with zero parse failures, zero malformed
evidence rows, and no first-failure record. This substantiates the parser fix
committed in `1dffe1f` / `509c85e` / `62d6b7c` independently of any envelope.

**Formal offline verdict: `NEEDS_REVIEW`** (`ops/validate_paper_day_summary.py`,
`--expect-source-kind kis_live`, no `--envelope`). Sole blocker:
`missing_from_persisted_summary` — `stdout-envelope.json` was not captured for
this run, so the five clean-exit clauses cannot be verified from disk. Every
other check passed.

### Run identity
| field | value |
| --- | --- |
| run_id | `8ada96300d514e27b08b32c5bfaa07fc` |
| session_date | 2026-06-26 |
| symbol | 005930 |
| market | KR |
| source_kind | kis_live |
| schema_version | paper_day_diagnostic.v1 |
| persisted outcome | PASS |
| stop_reason | completed |
| **formal offline verdict** | **NEEDS_REVIEW** (`missing_from_persisted_summary`) |

### On-disk evidence (verifiable)
| check | value |
| --- | --- |
| evidence rows | 692,004 |
| malformed evidence rows | 0 |
| sensitive_data_present (any row) | false |
| first_failure | none |
| paper_only | true |
| activation_authorized | false |
| real_order_adapter_constructed | false |
| automatic_restart | false |
| nonterminal_journal | 0 |

### Market-data / parser counters (verifiable)
| counter | value |
| --- | --- |
| quote_frames | 46,865 |
| normalized_quotes | 46,865 |
| trade_frames | 297,118 |
| normalized_trades | 297,118 |
| parse_success | 344,533 |
| health_pass | 341,896 |
| health_hold | 2,087 |
| trigger_evaluations | 343,983 |
| publication_slot_outcomes | 4 |
| orders | 0 |
| fills | 0 |

Zero paper orders is a valid clean day: trigger/health/decision conditions did
not require an order (`condition_false=275,422`, `max_fires_reached=63,674`).
`source_error` reason_subcodes were benign reconnect noise
(`malformed_control_after_ack=656`, `websocket_closed_after_ack=1`).

Session timing (KST): subscriptions ready 12:23:59, first quote 12:30:12, first
trade 12:40:30, resource close 18:53:59; latest heartbeat session_state CLOSED.

### Operator-attested (terminal only — NOT on-disk evidence)
- PILOT_EXIT = 0
- outcome = PASS
- cleanup_outcome = CLEAN
- summary_publication_outcome = WRITTEN

These lift the run to a clean operator-observed PASS but were not persisted to
`stdout-envelope.json`, so the offline validator cannot confirm them.

### Process note (next run)
Capture the stdout envelope with the **tool-written** flag so the clean-exit
clauses are verifiable from disk and the formal verdict can reach PASS. The
`stdout-envelope.json` that the offline validator reads is produced by
`--stdout-envelope-out`, not by the shell redirect:

```bash
--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"
--json > "$RUN_DIR/stdout-envelope.shell.json"
```

`stdout-envelope.json` is tool-written and validator-read;
`stdout-envelope.shell.json` is only a belt-and-suspenders console capture (see
`docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md`). Do not hand-edit an envelope file to
backfill the missing fields.

---

## 2026-06-27 — stdout-envelope hardening verification (offline)

Closes the pilot-3 envelope gap at the tooling level. Local sanitized
verification only — no live KIS, no network, no live rerun.

- stdout-envelope hardening local verification **passed** (commit `7d70ba0`).
- Both PASS and FAIL CLI paths now persist a tool-written
  `stdout-envelope.json` when `--stdout-envelope-out` is supplied (the FAIL
  exception paths previously did not).
- A missing envelope remains the explicit `NEEDS_REVIEW` fallback; the tool
  never fabricates or backfills the clean-exit clauses.
- The Monday operator packet now distinguishes `stdout-envelope.json`
  (tool-written via `--stdout-envelope-out`, the file the validator reads) from
  `stdout-envelope.shell.json` (belt-and-suspenders console redirect).
- Focused local sanitized tests: **106 passed**
  (`test_paper_day_stdout_envelope.py`,
  `test_paper_day_monday_operator_packet_docs.py`,
  `test_paper_day_monday_execution_checklist_docs.py`,
  `test_kis_official_ws_parser.py`, `test_kis_ws_source.py`).
- No live KIS rerun performed or required for this hardening review.
