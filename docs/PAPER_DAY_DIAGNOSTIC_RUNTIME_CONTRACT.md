# Paper-Day Diagnostic Runtime Contract

RTM-7c.5a/5b adds a bounded attended diagnostic composition for one paper-only
KR symbol.

Scope:

- market `KR`
- symbol `005930`
- universe `KR_LARGE`
- KIS market-data channels `H0STCNT0` trade and `H0STASP0` quote
- deterministic four-slot publisher at `09:30`, `11:00`, `13:00`, `14:50`
- paper broker, SQLite ledger, SQLite trigger journal
- single process, attended, bounded duration

This mode is not activation. It does not authorize runtime trading, does not
construct a real-order adapter, does not implement automatic restart, and does
not select an operational DB path implicitly. Existing
`ops/run_paper_fast_loop.py --run` remains `NO_GO` with exit `2`.

Runtime graph:

```text
MarketEventSource
-> MarketMonitor
-> LatestMarketStateStore
-> RollingTradeHistoryStore
-> MarketHealthTracker
-> SessionHealthExecutionGate
-> ActiveDecisionStore read
-> FastLoopExecutionOrchestrator
-> TriggerEngine
-> TriggerOrderBridge
-> PaperExecutionCoordinator
-> PaperBrokerAdapter
-> SqliteTriggerJournal
-> SQLiteLedger
```

The deterministic publisher is the only writer to `ActiveDecisionStore`. The
fast loop reads active decisions and never derives a `TriggerPlan` from decision
prose.

Lifecycle:

```text
validate
-> acquire create-new runtime lock
-> open create-new evidence
-> resources open
-> source connect/subscription path only when required
-> bounded monitor run or startup probe
-> source stop
-> resource close
-> post-close journal/quiescence inspection
-> synchronous event finish
-> summary write
-> evidence final record/close
-> release lock
```

Transport readiness counters are owned by source lifecycle events only:
connect attempts, connected state, subscription requests, ACKs, rejects, and
disconnects are never prefilled by the runtime body. Offline replay may emit an
explicit synthetic replay lifecycle (`source_kind=replay`); live KIS uses
`KisWsMarketEventSource` transport events (`source_kind=kis_live`).

Startup-only semantics:

- offline startup-only: validate, lock, open pilot DB resources, close resources,
  then write a post-close summary. It does not claim connected or subscription
  ACK readiness.
- live startup-only: validate, lock, open pilot DB resources, obtain KIS
  approval, connect websocket, observe trade and quote subscription ACKs, close
  source/resources, then write a post-close summary. It must not pass market
  events to the trigger engine or paper execution stack.

Runtime summary outcome taxonomy:

```text
PASS: completed, startup_only
NO_GO: health_not_ready, journal_uncertain, reconcile_required,
       nonterminal_journal, resource_close_failure, runtime_lock_exists
FAIL: invalid_input, source_failed, evidence_failed, summary_failed,
      db_failed, internal_runtime_error
```

CLI exit: `PASS -> 0`, `NO_GO/FAIL -> 1`. Existing
`ops/run_paper_fast_loop.py --run` remains `NO_GO` with exit `2`.

Lock/path ownership: `db_dir` is explicit, evidence and summary outputs must not
overlap it, final symlink components are rejected, evidence is create-new, a
runtime lock is create-new/no-overwrite, existing DB sidecars are rejected, and
an existing non-empty pilot DB directory requires an explicit reuse policy.

Immediate stop: `UNCERTAIN` and `RECONCILE_REQUIRED` stop event intake after the
first occurrence and return `NO_GO`; later events must not reach orchestration.
Resource close failures are reflected in summary/return/exit. The summary is
written after resource close and includes `shutdown_completed_at`.

Actual KIS network execution remains Operator-only. Cursor/test work must use
replay or lifecycle-aware fakes. A 1-day pilot remains **NO-GO** until Reviewer
PASS.
