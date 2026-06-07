# Technical Debt / P2·P3 Backlog

## P3 — Ops / KIS / Paper Review Backlog

### KIS read-only / tiny-live
- ~~Add manual KIS read-only smoke ops entrypoint.~~ Done: `ops/run_kis_read_only_smoke.py` (explicit `--run` opt-in; no orders).
- Verify KIS OpenAPI endpoint paths and TR IDs in `src/broker/kis_client.py` against the official KIS Open API documentation before real read-only smoke.
- Keep `StdlibKisHttpTransport` as a manual smoke scaffold only. Do not connect it to scheduler/runtime default paths without a separate review.
- `broker.kis_read_only.enabled=true` is not an automatic trigger. Read-only smoke must remain explicit/manual.
- Tiny-live actual order submission remains intentionally unimplemented. Do not add `submit_tiny_live_order()` / `place_tiny_live_order()` before a separate manual rehearsal.

### KIS real-time market monitor (fast loop — GitHub Issue #1)
- ~~RTM-1: fixture-first market-event domain + transport contract.~~ Done: `src/market_data/models.py` (normalized trade/quote/heartbeat + provider sequence, discriminated union), `src/market_data/protocols.py` (read-only async `MarketEventSource`), `src/market_data/kis_ws_parser.py` (parses only `provider_contract="kis-ws-fixture-v1"`; fail-closed; per-channel sequence dup/decrease/gap detection; no raw-frame/credential leak). Network-free, broker-free, ledger-free, scheduler-free.
  - **Official KIS WebSocket transport and field mapping remain UNVERIFIED and are deferred to RTM-6.** The `kis-ws-fixture-v1` envelope is a declared fixture contract, NOT a claim about real KIS frame layout / field positions. Do not build a real WS client against guessed offsets before RTM-6 verifies the official contract.
  - RTM-2..RTM-8 (latest-state store, monitor daemon, condition engine, paper execution bridge, KIS WS live read-only smoke, 4x/day decision refresh, 1-day unattended paper pilot) are sequenced in Issue #1; each opens only on a separate explicit manual gate.

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
- ~~Controlled Day 1 Readiness 0A — runbook contract / no-write boundary smoke.~~ Done: `tests/test_controlled_day1_readiness.py` (static docs-contract only; no 8B–8I execution). **Next human/operator action:** actual Controlled Day 1 manual walk-through per `docs/RUNBOOK.md` § Controlled Day 1 paper walk-through (stops at 8I no-write; broker/PaperLoop/KIS/write-mode out of scope).
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
- ~~Real Intake **3C2** — live OpenDART corpCode master download~~ Done: `dart_corp_code_http_client.py`, `dart_corp_code_live_client.py`, `--live-fetch` in `ops/resolve_dart_corp_code.py`; immutable ZIP snapshot → 3C1 parse/resolve; tests use injected transport only.
- ~~Real Intake **3D1** — provider mapping registry fixture-first~~ Done: `provider_mapping_registry.py`, `config/provider_mappings.paper.toml.example`, `ops/validate_provider_mapping.py`; maps internal symbol → yfinance provider_symbol / DART corp_code; no network/env.
- ~~Real Intake **3E1** — static KR real-company sample universe + provider mapping~~ Done: `config/universe.kr-real.sample.toml`, `config/provider_mappings.kr-real.sample.toml`, `tests/test_kr_real_sample_universe.py`; two locally verified companies (Samsung Electronics `005930`, SK hynix `000660`); corp_code from `tests/fixtures/research/dart/corp_code_sample.xml`; no network/env.
- ~~Real Intake **3E2** — KR real sample live PRICE smoke~~ Done: `ops/run_kr_real_price_smoke.py`, `tests/test_kr_real_price_smoke.py`; provider mapping → yfinance snapshot → generic PRICE replay → JSONL; store-seeded Date-ID allocation; 8B validate-only + 8C symbol coverage verified in tests; no DART/FRED/env. Non-goal: 종목별 hard-coded PRICE magnitude band (장기 시세 변동을 잘못 차단할 수 있음).
- ~~Real Intake **3E3** — KR real sample live DART disclosure smoke~~ Done: `ops/run_kr_real_dart_smoke.py`, `tests/test_kr_real_dart_smoke.py`; provider mapping → OpenDART snapshot → adapter replay → combined-batch Date-ID → JSONL; 8B/8C no-require-coverage + Scout 3A.1 context verified in tests; no yfinance/FRED.
- ~~Real Intake **3E4** — combined FRED+PRICE+DART context with Date.md/Scout budget caps~~ Done: `source_record_context_selector.py`, capped 8B `--context-budget-profile kr-real-smoke`, `ops/build_kr_real_combined_context_smoke.py`, `tests/test_combined_context_budget.py`; store retains all records; export cap only; 60KB guard unchanged.
- ~~Real Intake **3F1** — fixture-first KR universe/provider mapping generator~~ Done: `kr_provider_mapping_generator.py`, `ops/generate_kr_provider_mapping.py`, candidate fixture + disambiguation XML; corp_code from local resolver only; stock_code normalization; self-validates via existing loaders. P3 cleanup: control-char rejection at parse/CLI/render (write-safety).
- ~~Real Intake **3F2** — generator-based KR expansion workflow~~ Done: synthetic 5-candidate fixtures + scale tests (`test_kr_real_generated_universe_expansion.py`); operator-local real expansion documented in RUNBOOK (3C2 snapshot → candidate TOML → generator → validate → 3E2/3E3/3E4). Provider auto-generation from operator-curated candidates is supported; sector discovery/ranking is not.
- ~~Real Intake **3G1** — fixture-first sector-tagged KR candidate pool~~ Done: `kr_candidate_pool.py`, `ops/select_kr_candidates.py`, synthetic sector pool fixture + tests; deterministic sector/priority selection; export to 3F1 candidate TOML with pool metadata stripped.
- ~~Real Intake **3G2** — operator-local real sector pool workflow~~ Done: `ops/build_kr_real_sector_pool_mapping.py` chains 3G1 export → 3F1 generate → provider mapping validation; workflow tests use synthetic pool + corp-code fixtures only.
- ~~Real Intake **3G3-0** — live discovery/ranking guardrails~~ Done (docs-only): G1–G6 boundary checkpoint in `docs/REAL_RESEARCH_SOURCE_INTAKE.md`; approved expansion path and phase split documented; no code/tests.
- ~~Real Intake **3G3-1** — fixture-first KR candidate ranking model~~ Done: `kr_candidate_ranker.py`, `ops/rank_kr_candidates.py`, synthetic ranking signal fixture + tests; deterministic weighted score with explainable components; ranked JSON is reviewable metadata only.
- ~~Real Intake **3G3-2** — operator-local real ranking input workflow~~ Done: `ops/build_kr_real_ranked_mapping.py` chains 3G3-1 rank → 3F1 generate → provider mapping validation; operator supplies real sector pool + local ranking signals + corp-code snapshot; no live API.
- ~~Real Intake **3G3-3** — discovery snapshot replay adapter~~ Done: `kr_discovery_source_adapter.py`, `ops/replay_kr_discovery_snapshot.py`, synthetic discovery snapshot fixture + tests; snapshot → 3G1 candidate pool only; live transport deferred to 3G3-4.
- ~~Real Intake **3G3-4A** — live-shaped fake-transport discovery snapshot fetcher~~ Done: `kr_discovery_live_client.py` + tests; injected transport → immutable raw discovery snapshot; validate-before-commit; real live transport deferred to 3G3-4B.
- ~~Real Intake **3G3-4B** — operator-triggered HTTP discovery live smoke~~ Done: `kr_discovery_http_client.py`, `ops/run_kr_discovery_live_smoke.py` + tests; operator-supplied endpoint URL; sanitized HTTP errors; optional 3G3-3 candidate pool replay; no env/API keys.
- ~~Real Intake **3G3-5** — fixture-first KR discovery source schema mapper~~ Done: `kr_discovery_schema_mapper.py`, `ops/map_kr_discovery_fixture.py`, synthetic provider payload fixture + tests; source-specific fixture → canonical transport → 3G3-4A snapshot → optional 3G3-3 candidate pool; no network/env.
- ~~Real Intake **3G3-6** — operator-triggered source-specific KR discovery live endpoint adapter~~ Done: `kr_discovery_source_payload_snapshot.py`, `ops/run_kr_discovery_source_live_smoke.py` + tests; HTTP → immutable source snapshot → 3G3-5 mapper → 3G3-4A canonical snapshot → optional 3G3-3 candidate pool; no env/API keys.
- Real Intake **3G3-6+** — source-specific live adapter hardening — deferred.
- ~~Real Intake **3G4-0** — factor scoring guardrail checkpoint~~ — Done.
- ~~Real Intake **3G4-1** — fixture-first factor signal generator~~ Done: `kr_factor_signal_generator.py`, `ops/generate_kr_factor_signals.py`, synthetic factor input fixture + tests; local factor input → 3G3-1 ranking signal TOML; self-validates via existing ranker parser; no network/env.
- ~~Real Intake **3G4-2** — factor scorer → ranked mapping workflow integration~~ Done: `ops/build_kr_factor_ranked_mapping.py` + tests; thin orchestration over 3G4-1 + 3G3-2; reviewable artifacts only; no duplicated scoring/ranking/mapping logic; no network/env.
- ~~Real Intake **3G4-3** — operator-local real factor input bundle~~ Done: `ops/build_kr_factor_bundle_mapping.py` + synthetic bundle fixture + tests; operator-local bundle manifest wrapper over 3G4-2; reviewable artifacts only; no live factor scoring; no network/env.
- ~~Real Intake **3G4-4** — source-specific factor adapter~~ Done: `kr_factor_source_adapter.py`, `ops/map_kr_factor_fixture.py`, synthetic source payload fixture + tests; source-specific fixture → canonical factor input TOML → 3G4-1/3G4-2 downstream proof; no live factor transport; no network/env.
- ~~Real Intake **3G4-5** — operator-triggered live factor source smoke~~ Done: `kr_factor_source_http_client.py`, `kr_factor_source_payload_snapshot.py`, `ops/run_kr_factor_source_live_smoke.py` + tests; HTTP → immutable raw snapshot → optional 3G4-4 replay; no env/API keys; no scheduled fetch.
- ~~Real Intake **3G4-H1** — factor intake hardening cleanup~~ Done: validate-before-commit factor input TOML writer; snapshot unexpected-error sanitization; programmatic ValueError stage normalization; no workflow semantics change.
- ~~Real Intake **3H0** — operator end-to-end intake guardrail checkpoint~~ Done (docs-only): approved artifact flow discovery → factor → ranking/generation → validation → 3E smokes → Scout documented in `docs/REAL_RESEARCH_SOURCE_INTAKE.md` + RUNBOOK operator note; no code/tests; no new command.
- ~~Real Intake **3H1** — operator-local end-to-end manifest/preflight helper~~ Done: `ops/preflight_kr_end_to_end_intake.py` + synthetic manifest fixture + tests; validates existing artifact paths + provider mapping coverage; optional review-only follow-up command plan; no live fetch/smoke/8B/8C/Scout execution; no config mutation; no network/env.
- ~~Real Intake **3H2** — end-to-end preflight hardening cleanup~~ Done: atomic summary/plan writes; write-error sanitization; positive allowlist validation for follow-up command plan; no workflow semantics change; no network/env.
- ~~Real Intake **3H3** — structured follow-up plan JSON artifact~~ Done: optional `[outputs].structured_plan_out` / `--structured-plan-out`; single internal `FollowupStep` representation for Markdown + JSON; review-only; no command execution; no network/env.
- ~~Real Intake **3H4** — structured follow-up plan validator~~ Done: `ops/validate_kr_end_to_end_preflight_plan.py`; read-only schema/allowlist/review-only audit for 3H3 JSON; drift guard vs preflight allowlist; no command execution; no network/env.
- ~~Real Intake **3H5** — structured plan validator command-line safety hardening~~ Done: `_validate_command_line_safety()` uses exact unsafe execution token guard + structured-field rejection; broad command substring false positives removed; no contract/execution change.
- ~~Real Intake **3H6** — structured plan validator optional validation report~~ Done: `--report-out` / `--force` on `ops/validate_kr_end_to_end_preflight_plan.py`; compact audit JSON after successful validation only; atomic write; no command execution; no network/env.
- ~~Real Intake **3H7** — operator handoff manifest / artifact integrity index~~ Done: `ops/build_kr_end_to_end_handoff_manifest.py`; indexes artifact paths/sha256 only; embeds no artifact bodies; atomic write; no command execution; no network/env.
- ~~Real Intake **3H8** — operator handoff manifest integrity verifier~~ Done: `ops/verify_kr_end_to_end_handoff_manifest.py`; recomputes size/sha256 and validates recorded JSON metadata; writes no files; no command execution; no network/env.
- ~~Real Intake **3H9** — handoff manifest verifier schema hardening~~ Done: exact-key schema lock on top-level manifest and artifact entries; unknown keys rejected at validate stage; no workflow behavior change; no network/env.
- ~~Real Intake **3H10** — handoff manifest verifier optional path containment~~ Done: optional `--base-dir` on `ops/verify_kr_end_to_end_handoff_manifest.py`; canonical resolved-path containment for manifest and artifact paths; read-only; writes no files; no network/env.
- ~~Real Intake **3H11** — handoff manifest verifier optional verification report~~ Done: `--verification-report-out` / `--force` on `ops/verify_kr_end_to_end_handoff_manifest.py`; compact audit JSON after successful verification only; atomic write; public verifier API remains read-only; no command execution; no network/env.
- ~~Real Intake **3H12** — verification report output path containment~~ Done: when `--base-dir` and `--verification-report-out` are both supplied on `ops/verify_kr_end_to_end_handoff_manifest.py`, report output must resolve inside base_dir; validate-stage rejection before write; no new success payload keys; no command execution; no network/env.
- ~~Real Intake **3H13** — verification report schema self-validation~~ Done: in-memory exact-key validation of verification report payload before atomic write; invalid payloads never reach disk; `artifact_roles` reuses `_validate_artifact_roles`; no new success payload keys; no command execution; no network/env.
- ~~Real Intake **3H14** — handoff manifest builder validate-before-commit~~ Done: temp write → existing 3H8 verifier validation → atomic replace; invalid generated manifests never reach `--manifest-out`; no new CLI flags; no command execution; no network/env.
- ~~Real Intake **3H15** — handoff manifest builder optional path containment~~ Done: optional `--base-dir` on `ops/build_kr_end_to_end_handoff_manifest.py`; canonical resolved-path containment for supplied artifacts and `manifest_out` before read/write; 3H14 validate-before-commit calls verifier with same resolved base; manifest/API/CLI success key sets unchanged; no command execution; no network/env.
- ~~Real Intake **3H16** — end-to-end handoff bundle round-trip smoke~~ Done: fixture-only no-exec API round-trip in `tests/test_kr_end_to_end_preflight.py` (preflight summary/plan/structured plan → validation report → handoff manifest builder with `base_dir` → verifier with `base_dir` + verification report); no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H17** — in-process CLI handoff bundle round-trip smoke~~ Done: no-subprocess in-process `main([...])` round-trip in `tests/test_kr_end_to_end_preflight.py` (preflight/validator/builder/verifier CLI wiring with `--base-dir` + `--*-out` inside bundle); known-error CLI rejection asserts `rc == 1`; no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H18** — API/CLI handoff bundle round-trip parity smoke~~ Done: API round-trip vs in-process CLI round-trip compared via path/hash-independent semantic normalization in `tests/test_kr_end_to_end_preflight.py` (roles/kinds/modes/flags/entry-key sets equal; sha256 shape + size>0 + base_dir presence only; per-bundle containment; body-free; no-exec); no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H19** — generated handoff bundle tamper-detection smoke~~ Done: `tests/test_kr_end_to_end_preflight.py` mutates 3H16 round-trip bundles and re-verifies (integrity/parse/metadata/containment/report-not-written/body-free); no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H20** — CLI verifier tamper-rejection smoke~~ Done: in-process `verify_handoff_manifest_main([... "--json"])` on tampered 3H16-generated bundles (`rc == 1`; exact CLI error mode; stage mapping; no traceback/raw-body echo; report not written on failure); no subprocess; no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H21** — handoff pipeline failure no-partial-output smoke~~ Done: in-process CLI pipeline failure tests in `tests/test_kr_end_to_end_preflight.py` (no partial downstream outputs; base-dir parent not created; `--force` preserves existing report on pre-write failure; safe JSON errors; no subprocess; no network/env).
- ~~Real Intake **3H22** — normalized handoff bundle reproducibility smoke~~ Done: repeated API/CLI in-process round-trips compared via 3H18 path/hash-independent normalization in `tests/test_kr_end_to_end_preflight.py` (semantic manifest/report contract equality; per-bundle containment; exact-key body-free; no-exec); no new ops CLI; no command execution; no network/env.
- ~~Real Intake **3H23** — CLI stdout success payload contract smoke~~ Done: in-process `--json` success payload contracts for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (exact mode/stage/status, exact key sets, body-free, no sensitive/trading fields, advisory-only preflight followup_commands, output containment; no-exec); no new ops CLI; no subprocess; no network/env.
- ~~Real Intake **3H24** — CLI stdout known-error payload contract smoke~~ Done: in-process `--json` known-domain-error payload contracts for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (exact mode/stage/status, exact four-key set, safe message, no traceback/raw-body echo, no partial downstream outputs, no sensitive/trading field keys; preflight missing-manifest domain path only; no-exec); no new ops CLI; no subprocess; no network/env.
- ~~Real Intake **3H25** — CLI stdout JSON channel discipline smoke~~ Done: in-process `--json` stdout channel discipline for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (single-object stdout via raw_decode, no human prefix/suffix, no traceback, no stderr JSON payload, success output existence, known-error no-partial outputs, no-exec; no subprocess; no network/env).
- ~~Real Intake **3H26** — CLI argument-domain failure no-output smoke~~ Done: in-process `--json` argument-domain failure tests for validator/builder/verifier handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (`write` output-exists preservation, `validate` missing/not-dir base_dir no partial output, stable blank path `args` errors; exact four-key error payload; no traceback/sentinel echo; no subprocess; no network/env).
- ~~Real Intake **3H27** — CLI help/usage side-effect smoke~~ Done: in-process argparse `--help`/`main([])` tests for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (`SystemExit` clean/non-zero; human usage text; no traceback/JSON; empty `tmp_path`; no subprocess/generated command execution; no network/env).
- ~~Real Intake **3H28** — CLI help/usage wording contract smoke~~ Done: in-process `--help`/`main([])` discoverability tests for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (operator-critical flag tokens in help; usage without traceback/JSON; forbidden compound operational tokens absent; no full help snapshot; no subprocess; no network/env).
- ~~Real Intake **3H29** — CLI non-JSON human output smoke~~ Done: in-process human CLI output tests for four handoff CLIs in `tests/test_kr_end_to_end_preflight.py` (success `rc == 0` + artifact writes; known-domain error `rc == 1` + no partial outputs; non-JSON stdout/stderr; no traceback/raw-body echo; validator output-exists preservation; no-exec; preflight conflicting-flags path excluded; no subprocess; no network/env).
- Real Intake **3H30+** — end-to-end hardening (richer provenance, drift checks, orchestration ergonomics) — Deferred. Broker/PaperLoop/KIS remains out of scope for Real Research Source Intake.
- Real Intake **3G4+** — calibration / provenance / drift checks / explainability hardening — Deferred.
- Real Intake **3E5+** — provider auto-refresh, automatic universe expansion — deferred.
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