# Technical Debt / P2·P3 Backlog


## P3 — Phase 16 Long Paper Trading Review / Parameter Review

- Real benchmark-relative review once benchmark series source is stable.
- DailySummary coverage projection if existing data is incomplete for review period gap analysis.
- Slippage/execution-quality review once reference prices are stored consistently in paper ledger inputs.
- Parameter recommendation human approval workflow before any config edits (Phase 16 produces candidates only).
- Explicit false-positive / missed-risk detector rules for MDD threshold review before those counts become non-zero.

## P3 — Phase 15 Emergency Triggers

- Review MDD liquidation overshoot behavior: current Phase 15 planning may sell full loss positions before proportional profitable-position sales. This is safe as a conservative emergency plan foundation, but before execution integration, decide whether loss positions should support partial sells to stay closer to the target cash percentage and the 1~3% residual mismatch rule.

## P3 — Phase 14 KIS Live Read-only / Tiny-live Rehearsal

- Validate KIS OpenAPI endpoint paths, TR IDs, request parameters, and response field variants against official KIS documentation before real read-only smoke.
- `StdlibKisHttpTransport` is a manual-smoke scaffold and is not connected to scheduler/runtime default paths.
- `broker.kis_read_only.enabled=true` is not an automatic execution trigger. Read-only smoke must remain explicitly invoked unless a later phase adds a reviewed CLI or runner.
- Tiny-live actual order submission is intentionally not implemented in Phase 14. Handle it only in a later explicit manual rehearsal phase.

## P3 — Phase 13 Postmortem

- `parse_postmortem_tag_summary_from_markdown()` accepts a single fenced `json` block anywhere in the document, but rule 08 requires the tag summary to be at the **end** of every Postmortem. Before live Postmortem authoring begins, harden the parser to require the JSON block to be the last non-whitespace content (reject if any prose follows it).
- `validate_postmortem_error_tags()` coerces non-string keys via `str(raw_tag)` before catalog validation. JSON input always has string keys so this is benign in practice, but direct Python callers can pass non-string keys and have them silently stringified. Consider rejecting non-string keys explicitly to match the strictness of other validators.
- `PostmortemTagSummary` currently rejects empty `error_tags`. This matches the "오답노트" spirit, but real operations may need to record a Postmortem with no flagged mistakes (e.g., a clean week). Decide whether to allow `error_tags={}` + `top_error_tags=()` before Phase 13 outputs are consumed by Top 3 aggregation in production.

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

## Reference — Completed AccountRole Vocabulary Alignment

- Migrated domain `AccountRole` from product/account-name values (`ISA`, `GENERAL`, `CMA`) to semantic portfolio roles (`KR_TAX_ADVANTAGED`, `US_REGULAR`, `CASH_BUFFER`, `PAPER`).
- Mapping: `ISA` → `KR_TAX_ADVANTAGED`, `GENERAL` → `US_REGULAR`, `CMA` → `CASH_BUFFER`, `PAPER` → `PAPER`.
- `PAPER` remains internal PaperBroker-only and is not a live broker account.
- No KIS live routing or account-number mapping was implemented in this cleanup.
- Old enum values are rejected; no alias or silent normalization.
- Persisted SQLite `account_role` values in existing runtime DBs are not auto-migrated; explicit migration required if old values exist.

## Reference — Completed Phase 3 Hardening Notes

These are already implemented and are not open backlog items.

- PaperBroker mismatch/currency validation added.
- LIMIT fill price uses `market_price.price`.
- fee/slippage calculated once per execution decision.
- `paper_cash_ledger` added.
- cash mutation public path restricted to `apply_cash_change`.
- duplicate/PENDING policies fixed by tests.