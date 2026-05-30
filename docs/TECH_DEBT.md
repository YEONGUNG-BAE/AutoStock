# Technical Debt / P2·P3 Backlog

## P3 — Ops / KIS / Paper Review Backlog

### KIS read-only / tiny-live
- ~~Add manual KIS read-only smoke ops entrypoint.~~ Done: `ops/run_kis_read_only_smoke.py` (explicit `--run` opt-in; no orders).
- Verify KIS OpenAPI endpoint paths and TR IDs in `src/broker/kis_client.py` against the official KIS Open API documentation before real read-only smoke.
- Keep `StdlibKisHttpTransport` as a manual smoke scaffold only. Do not connect it to scheduler/runtime default paths without a separate review.
- `broker.kis_read_only.enabled=true` is not an automatic trigger. Read-only smoke must remain explicit/manual.
- Tiny-live actual order submission remains intentionally unimplemented. Do not add `submit_tiny_live_order()` / `place_tiny_live_order()` before a separate manual rehearsal.

### AccountRole / legacy runtime data
- If old runtime SQLite databases contain legacy `account_role` values (`ISA`, `GENERAL`, `CMA`), add an explicit migration script or discard those runtime DBs. Current semantic values are `KR_TAX_ADVANTAGED`, `US_REGULAR`, `CASH_BUFFER`, `PAPER`.

### Emergency / MDD
- Add optional DailySummary projection for emergency/MDD events without changing Phase 12 store semantics.
- Consider settings/config override for emergency thresholds. Current implementation uses module constants.
- Review MDD liquidation overshoot behavior. Loss-position-first liquidation can exceed target cash; partial sell sizing may be needed before execution integration.
- Define explicit detectors for `false_positive_suspected_count` and `missed_risk_suspected_count`. Phase 16 currently keeps them at default 0.

### Paper review / report
- ~~Run `ops/build_paper_review_report.py` with a valid `PaperReviewInput` bundle once a safe bundle exists.~~ Done: dev synthetic builder + manual smoke via `ops/dev/build_synthetic_paper_review_input.py`.
- Implement a `PaperReviewInput` collector/exporter that explicitly assembles NAV snapshots, DailySummary records, Postmortem records, Emergency events, OrderIntent records, and Fill records.
- Avoid running `--store` and `--markdown-out` together until partial-side-effect behavior is either accepted or hardened. Recommended operation: generate markdown first, then save to JSONL store after review.

### Paper operation entrypoints
- ~~Add a safe `PaperLoopInput` validated bundle builder for manual paper one-shot testing.~~ Done: `ops/dev/build_synthetic_paper_loop_input.py` (dev-only SYNTH fixtures).
- ~~Run `ops/run_paper_once.py` against a valid bundle after the builder exists.~~ Done after datetime JSON roundtrip hardening in domain validators.

### Paper pilot workflow (Foundation 8A–8I)
- ~~Document 30-trading-day paper pilot daily workflow skeleton.~~ Done: `docs/PAPER_PILOT_WORKFLOW.md`.
- ~~Foundation 8B: Research Source Intake + Date.md Export.~~ Done: `ops/research_source_intake.py`.
- ~~Foundation 8C: Universe v0 + Date.md prompt-reference smoke.~~ Done: `config/universe.paper.toml.example`, `ops/run_date_md_smoke.py`.
- ~~Foundation 8D: Scout Once manual LLM call packet.~~ Done: `ops/build_scout_manual_packet.py`.
  - **P2 hardening candidate (post-Foundation):** 8D/8E/8F/8G/8H/8I ops scripts write three output files sequentially via `write_text()` after preflight existence checks. Mid-sequence failure can leave partial artifacts; recovery is `--force` re-run or manual cleanup. Apply atomic-write pattern before 30-trading-day pilot start as a separate ops-script hardening pass.
  - **P3 static-guard hardening candidate (post-Foundation):** Replace coarse substring-based forbidden-token static guards in ops tests with AST/import/call-based guards. Context: 8I `ops/rehearse_paper_loop_no_write.py` currently builds log strings such as `PaperLoopRunner.run: NOT CALLED` via runtime string concatenation to avoid false positives from source-level substring scans. This is functionally safe but a code smell caused by over-broad static tests. Handle in post-Foundation ops hardening; do not change 8I behavior now.
- ~~Foundation 8E: Manual LLM JSON Intake Validator (Scout).~~ Done: `ops/validate_scout_raw_json.py`.
- ~~Foundation 8F: Portfolio state snapshot + Allocator Once.~~ Done: `ops/build_allocator_manual_packet.py`, `ops/validate_allocator_raw_json.py`, `docs/examples/portfolio_state.paper.example.json`.
- ~~Foundation 8G: Analysis Once (per-symbol).~~ Done: `ops/build_analysis_manual_packet.py`, `ops/validate_analysis_raw_json.py`.
- ~~Foundation 8H: Production PaperLoopInput Assembler (per-symbol).~~ Done: `ops/assemble_paper_loop_input.py`, `docs/examples/paper_loop_context.paper.example.json`.
- ~~Foundation 8I: End-to-End no-write rehearsal.~~ Done: `ops/rehearse_paper_loop_no_write.py`.
- **Evidence-based follow-ups (not pre-implemented):**
  - DailySummary writer helper — trigger: template omission ≥ 2 times per week
  - Postmortem weekly template helper — trigger: tag summary format errors ≥ 2 times

### Date-ID / Date.md
- ~~Add a controlled Date.md export/update helper only after the manual Date-ID workflow is stable.~~ Done in Foundation 8B (`ops/research_source_intake.py`); real API fetchers remain deferred.
- ~~Design Real Research Source Intake v1 (read-only fetch → snapshot → DateIdSourceRecord → 8B).~~ Done: `docs/REAL_RESEARCH_SOURCE_INTAKE.md` (design-only; FRED first; G1–G4 guards).
- ~~Implement Real Intake 1A — FRED replay/fixture-only staging (`ops/fetch_research_sources.py --replay`).~~ Done: `src/data/research_source_fetcher.py`, `src/data/fred_source_fetcher.py`, fixtures + tests; JSONL round-trips through 8B `--validate-only`.
- ~~Implement Real Intake 1B — FRED live-smoke HTTP client~~ Done: `src/data/fred_http_client.py` (urllib isolated), `--live-smoke`, API-key leakage hardening + tests.
- ~~Implement Real Intake 2A — generic PRICE snapshot replay (`--replay --source price`)~~ Done: `src/data/price_source_fetcher.py`, fixtures + tests; JSONL round-trips through 8B and satisfies 8C symbol coverage for matching universe symbols.
- ~~Implement Real Intake 2B — yfinance PRICE live-smoke (`--live-smoke --source price`)~~ Done: `src/data/price_live_client.py` (lazy yfinance import), immutable generic PRICE snapshot → 2A replay; tests use injected ticker factory only.
- ~~Implement Real Intake 3A — DART DISCLOSURE replay/fixture (`--replay --source dart`)~~ Done: `src/data/dart_source_fetcher.py`, store-seeded Date-ID batch allocation, fixtures + tests; JSONL round-trips through 8B; does not satisfy 8C symbol coverage.
- ~~Implement Real Intake 3A.1 — Scout packet context for symbol-matched DART DISCLOSURE~~ Done: `ops/build_scout_manual_packet.py` symbol-only selection; `market=None` preserved; 8C coverage semantics unchanged.
- ~~Real Intake **3B1** — DART live snapshot normalizer + fake HTTP transport tests~~ Done: `src/data/dart_live_client.py`, `tests/test_dart_live_client.py`; injected transport only; snapshot→3A replay→8B validate-only; collision + no-leak guards.
- ~~Real Intake **3B2** — DART operator `--live-smoke`~~ Done: `dart_http_client.py`, `run_live_smoke_dart`; env API key read only in DART live branch; snapshot→3A replay→JSONL.
- Real Intake **3B3** — DART hardening (pagination, error schema, corp-code cache, retry/backoff) — optional after 3B2.
- ~~Real Intake **3C1** — DART corp-code resolver fixture-first (stock_code → corp_code)~~ Done: `src/data/dart_corp_code_resolver.py`, `ops/resolve_dart_corp_code.py`, fixtures + tests; no network/env.
- Real Intake **3C2** — live OpenDART corpCode master download — deferred.
- Provider mapping registry (internal symbol → yfinance / DART corp_code) — deferred.
- Operator-defined real 3–5 company universe + combined FRED/PRICE/DART smoke repeat — deferred.
- Date.md must remain a read-only reference for LLM prompts; Date-ID validation failures must reject the corresponding LLM output.

### Ollama operation
- Track local model smoke/latency stability for `qwen3.6:35b-mlx` and fallback `qwen3.6:35b`.
- Do not auto-pull or auto-install Ollama models from scripts.

## P3 — Phase 16 Long Paper Trading Review / Parameter Review

- Real benchmark-relative review once benchmark series source is stable.
- DailySummary coverage projection if existing data is incomplete for review period gap analysis.
- Slippage/execution-quality review once reference prices are stored consistently in paper ledger inputs.
- Parameter recommendation human approval workflow before any config edits (Phase 16 produces candidates only).
- Explicit false-positive / missed-risk detector rules for MDD threshold review before those counts become non-zero.
- `MddThresholdReview.false_positive_suspected_count` / `missed_risk_suspected_count` currently default to 0 but the model itself does not reject non-zero values. After explicit detector rules are agreed, consider adding a validator that rejects non-zero values until those detectors exist, or document the allowed source.
- `PaperReviewInput` validates that `nav_snapshots`, `daily_summaries`, `postmortem_records`, and `emergency_events` fall within the review period, but does not yet validate that `order_intents` and `fills` timestamps fall within the period. Add explicit timestamp range validation if stale order/fill data could enter review inputs.

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