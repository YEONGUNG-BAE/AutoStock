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
-> preflight
-> resources open
-> deterministic publisher ready
-> source connect/subscription path
-> bounded monitor run
-> synchronous event finish
-> journal check
-> resource close
-> summary write
```

Startup-only mode validates configuration, opens and closes the pilot DBs, marks
subscription readiness in diagnostic counters, and exits without paper execution.
Actual KIS use requires an explicit operator `--live-kis` run; tests use replay
sources only.

