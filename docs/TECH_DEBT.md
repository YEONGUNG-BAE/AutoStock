# Technical Debt / P3 Backlog


## P3 — Phase 12 Logs / DailySummary / Debug Events

- `ALLOCATOR_DATE_ID_FUTURE_SOURCE` / `ANALYSIS_DATE_ID_FUTURE_SOURCE` currently map to `DATE_ID_FORMAT_INVALID` because there is no dedicated future-source debug event code. Later consider adding a canonical `DATE_ID_FUTURE_SOURCE` or `EVIDENCE_FUTURE_SOURCE` code to `docs/DEBUG_EVENT_CODES.md`.
- `RISK_GOLD_TRADE_FREQUENCY_EXCEEDED` currently maps to `GOLD_TRADE_BLOCKED_MONTHLY_LIMIT` without distinguishing monthly vs quarterly violations. Later split the risk issue code or inspect issue metadata/path to map quarterly violations to `GOLD_TRADE_BLOCKED_QUARTERLY_LIMIT`.
- `DailySummary.range_violation_count` and `allocator_fallback_count` are Phase 12 foundation fields and currently default to 0 in projection. Later add explicit classifiers when validation/debug event semantics stabilize.

## P3 — Phase 11 Paper E2E Loop

- Phase 12 added `PAPER_NAV_SNAPSHOT_ERROR` catalog support and `debug_event_from_nav_snapshot_error()` helper. Future runtime integration may record this event if NAV snapshot write fails after a paper fill.
- Future cleanup: make `NavSnapshot` currency-agnostic instead of KRW-named fields (`total_nav_krw`, `cash_krw`, `invested_krw`).

## P3 — Phase 10 Risk

- Keep `tests/conftest.py` fixtures minimal as Phase 11 E2E tests grow.
- ~~Phase 11 must own target_weight_percent → executable quantity conversion~~ — done in Phase 11 (`QuantityResolver`).

## P3 — Phase 9 Analysis

- `ANALYSIS_CONFLICTING_PERSPECTIVES_UNSUPPORTED` is exported but not emitted in Phase 9. Revisit during Phase 10+ if explicit bear/bull conflict rules are introduced.

## P3 — Phase 8 Allocator

- `AllocatorAction` enum is currently not used by Phase 8 schema. Revisit during final API surface cleanup.

## P3 — Phase 7 Scout

- `ScoutInputBuilder` currently loads all records and filters fact types in Python. Later optimize by delegating single fact_type filters to `SQLiteDateIdSourceStore.list_records(fact_type=...)`.
- `SCOUT_SCHEMA_INVALID` currently returns one aggregate issue without Pydantic `loc` path. Later expand schema errors into per-field `ValidationIssue.path`.

## P3 — Phase 6 Data

- `DateIdGenerator` scans all records for date prefix. Later replace with SQL prefix/max sequence helper.

## P3 — Phase 4 Decision Store

- `save_decision_snapshot()` relies on callers using `SQLiteDecisionStore.transaction()`. Current tests and usage patterns enforce transaction-scoped writes, which is sufficient for Phase 4. Later hardening can either add a guard that rejects calls outside an active transaction or add an explicit docstring stating that `save_decision_snapshot()` must be called inside a transaction.

## P3 — Phase 3 Broker

- BUY insufficient-cash evaluation computes fee before rejection because cash sufficiency depends on fee-inclusive total cost. Keep fee calculators pure/no-side-effect; revisit only if fee calculation gains external dependencies or side effects.

## Reference — Completed Phase 3 Hardening Notes

These are already implemented and are not open backlog items.

- PaperBroker mismatch/currency validation added.
- LIMIT fill price uses `market_price.price`.
- fee/slippage calculated once per execution decision.
- `paper_cash_ledger` added.
- cash mutation public path restricted to `apply_cash_change`.
- duplicate/PENDING policies fixed by tests.