# Real Research Source Intake v1 — Design

> **Status:** 1A replay **implemented**; 1B FRED live-smoke **implemented** (urllib isolated in `fred_http_client.py`); 2A generic PRICE replay **implemented**; 2B yfinance PRICE live-smoke **implemented** (yfinance lazy-imported only in `price_live_client.py`); 3A DART `DISCLOSURE` replay/fixture **implemented**; 3A.1 Scout packet context for symbol-matched DART `DISCLOSURE` (`market=None`) **implemented**; combined FRED+PRICE+DART runtime smoke **verified** (8B/8C with symbol coverage + 8D Scout context) — **3B0–3B2** DART live-smoke **implemented**; **3C1** corp-code resolver fixture-first **implemented** (`dart_corp_code_resolver.py`); **3C2** live corp-code master fetch **implemented** (`dart_corp_code_http_client.py` + immutable ZIP snapshot); **3D1** provider mapping registry fixture-first **implemented** (`provider_mapping_registry.py`); **3E1** static KR real-company sample universe + provider mapping **implemented**; **3E2** KR real sample live PRICE smoke **implemented** (`ops/run_kr_real_price_smoke.py`); **3E3** KR real sample live DART disclosure smoke **implemented** (`ops/run_kr_real_dart_smoke.py`); **3E4** combined FRED+PRICE+DART context with Date.md/Scout budget caps **implemented**; **3F1** fixture-first KR universe/provider mapping generator **implemented** (`ops/generate_kr_provider_mapping.py`); **3F2** generator-based KR expansion workflow **implemented** (synthetic scale proof + operator-local real expansion path); **3G1** fixture-first sector-tagged KR candidate pool **implemented** (`ops/select_kr_candidates.py`); **3G2** operator-local real sector pool workflow **implemented** (`ops/build_kr_real_sector_pool_mapping.py`); **3G3-0** live discovery/ranking guardrails **documented** (design-only); **3G3-1** fixture-first ranking model **implemented** (`ops/rank_kr_candidates.py`); **3G3-2** operator-local real ranking input workflow **implemented** (`ops/build_kr_real_ranked_mapping.py`); **3G3-3** discovery snapshot replay adapter **implemented** (`ops/replay_kr_discovery_snapshot.py`); **3G3-4A** live-shaped fake-transport discovery snapshot fetcher **implemented** (`kr_discovery_live_client.py`); **3G3-4B** operator-triggered HTTP discovery live smoke **implemented** (`ops/run_kr_discovery_live_smoke.py`); **3G3-5** fixture-first KR discovery source schema mapper **implemented** (`ops/map_kr_discovery_fixture.py`); **3G3-6** operator-triggered source-specific KR discovery live endpoint adapter **implemented** (`ops/run_kr_discovery_source_live_smoke.py`); **3G3-6+** live factor scoring / adapter hardening **deferred**; **3G4-0** factor scoring guardrails **documented**; **3G4-1** through **3G4-5** + **3G4-H1** factor intake **implemented**; **3H0** operator end-to-end intake guardrail checkpoint **documented** (docs-only); **3H1** operator-local manifest/preflight helper **implemented** (`ops/preflight_kr_end_to_end_intake.py`); **3H2** preflight hardening **implemented**; **3H3** structured follow-up plan JSON artifact **implemented**; **3H4** structured follow-up plan validator **implemented** (`ops/validate_kr_end_to_end_preflight_plan.py`); **3H5** plan validator command-line safety hardening **implemented**; **3H6** plan validator optional validation report **implemented**; **3H7** operator handoff manifest / artifact integrity index **implemented** (`ops/build_kr_end_to_end_handoff_manifest.py`); **3H8** handoff manifest integrity verifier **implemented** (`ops/verify_kr_end_to_end_handoff_manifest.py`); **3H9** verifier schema hardening **implemented**; **3H10** verifier optional path containment **implemented**; **3H11** verifier optional verification report **implemented**; **3H12** verification report output path containment **implemented**; **3H13** verification report schema self-validation **implemented**; **3H14** handoff manifest builder validate-before-commit **implemented**; **3H15** handoff manifest builder base-dir containment **implemented**; **3H16** handoff bundle round-trip smoke **implemented**; **3H17** in-process CLI handoff bundle round-trip smoke **implemented**; **3H18** API/CLI handoff bundle parity smoke **implemented**; **3H19** handoff bundle tamper-detection smoke **implemented**; **3H20** CLI verifier tamper-rejection smoke **implemented**; **3H21** pipeline failure no-partial-output smoke **implemented**; **3H22** normalized handoff bundle reproducibility smoke **implemented**; **3H23** CLI stdout success payload contract smoke **implemented**; **3H24** CLI stdout known-error payload contract smoke **implemented**; **3H25** CLI stdout JSON channel discipline smoke **implemented**; **3H26** CLI argument-domain failure no-output smoke **implemented**; **3H27** CLI help/usage side-effect smoke **implemented**; **3H28** CLI help/usage wording contract smoke **implemented**; **3H29** CLI non-JSON human output smoke **implemented**; **3H30+** end-to-end hardening **deferred**  
> **Scope:** real external research data → existing Foundation **8B** intake path  
> **Not in scope:** Scout/Allocator/Analysis LLM agents, trading, broker, KIS, write mode

---

## Post-Day1 repeatability checkpoint

Controlled Day 1 no-write walk-through is closed, but that does not start a live pilot, KIS read-only run, write-mode paper loop, or broker path. This document remains a **research intake** design reference, not a trading or pilot runbook.

The next post-Day1 decision is a **repeatability/readiness decision** for the read-only Real Research Source Intake path: choose one already documented intake lane, run it as an operator-controlled snapshot/replay or live-smoke where explicitly supported, and verify that it still hands off through `DateIdSourceRecord` → 8B → Date.md / store without changing the 8C–8I no-write boundary. Scope stays **real external research data → existing Foundation 8B intake path**; Scout/Allocator/Analysis LLM agents, trading, broker, KIS, and write mode remain out of scope.

**Candidate lanes (examples only — use existing ops/docs; do not invent new commands):**

- FRED / PRICE / DART replay or live-smoke lanes (§1A–3B2, 2A–2B)
- KR real-sample PRICE / DART / combined context lanes (§3E1–3E4)
- 3G / 3H operator-local handoff and preflight lanes (§3G1–3G3-6, §3H0–3H29)

This checkpoint must **not** be interpreted as a new automatic 3H micro-smoke extension. The 3H line remains closed at **3H29** unless a separate hardening task is explicitly opened and justified; **3H30+** end-to-end hardening stays **deferred**.

Any new source integration, live endpoint hardening, factor/discovery hardening, pilot planning, KIS read-only planning, or write-mode planning requires a separate task, separate validation, and the same guards: snapshot/replay first, no checked-in runtime artifacts, no automatic config promotion, no broker, no KIS, no write-mode paper loop, and no trading actions or allocation outputs.

### No-network replay closure — R1–R4 + R1b

R1–R4 and R1b no-network replay readiness are closed. FRED replay, PRICE replay with symbol coverage, DART DISCLOSURE context-only replay, and the combined FRED+PRICE+DART replay all passed as operator-controlled runtime checks. The combined run produced one shared store/Date.md with four records total: FRED 1, PRICE 1, and DART 2. Final 8C symbol coverage passed with `missing_symbols == []`, and DART `market=None` disclosure records did not break PRICE coverage.

R1b also closed the same-DAY re-export loose end: `export-only --force-date-md` regenerated Date.md byte-identically from the unchanged store, with the same record count, Date-ID set, and Date.md hash before vs after. `export-only` without `--force-date-md` failed safely at the export preflight, and a duplicate same-date-id normal re-save was rejected at the store stage without changing store or Date.md. This confirms the safe same-DAY re-export policy for replay workflows.

In the combined flow, 8B normal re-exported Date.md from the full store. After the first source creates Date.md, later source normalizations use the documented `--force-date-md` path to regenerate Date.md from the accumulated store. This is a documented overwrite/re-export path, not hand-editing and not a new command.

This closure is no-network replay readiness only. It does not authorize live-smoke, external API fetch, env/API key use, KIS, broker, write-mode paper loop, `PaperLoopRunner.run()`, 8D Scout progression, or pilot planning. Runtime artifacts and detailed evidence remain local-only and must not be committed.

**Lane summary (operator-controlled, fixture read-only, no env/API key, no broker/KIS/write-mode):**

| Lane | Result |
|---|---|
| R1 — FRED replay clean-workspace repeatability | PASS |
| R2 — PRICE replay + 8C symbol coverage | PASS |
| R3 — DART DISCLOSURE replay context-only | PASS |
| R4 — combined FRED+PRICE+DART shared store/Date.md | PASS (FRED 1 + PRICE 1 + DART 2 = 4 records; 8C `--require-symbol-coverage` exit 0) |
| R1b — same-DAY Date.md re-export idempotence | PASS (export-only `--force-date-md` byte-identical; duplicate normal re-save rejected safely) |

**Next choices (separate decisions — not authorized by this closure):**

- L1-FRED, L1-PRICE, and L1-DART are closed for their respective live-smoke lanes.
- R1b same-day idempotence is closed.
- 8D–8I operator-controlled no-write chain is closed under separate explicit approvals; pilot, KIS, broker, and write-mode remain unauthorized.

---

### Live-smoke closure — L1-FRED

L1-FRED live-smoke is closed for the FRED MACRO path. The operator-authorized run used a fresh runtime workspace and performed the live FRED fetch through the documented env/network boundary, then staged an immutable raw snapshot and `DateIdSourceRecord` JSONL under `runtime/research`. The same staged JSONL passed 8B validate-only, then 8B normal and 8C smoke. FRED is a MACRO source, so `--require-symbol-coverage` was not used and missing universe symbols remain expected for this lane.

This was the first operator-authorized live-smoke with actual network and env/API key boundary use. Secret handling remained value-hidden: the API key value was not written to stdout/stderr, JSONL, snapshot, Date.md, or store; snapshot metadata records only the env var name and boolean presence, not key values. Runtime artifacts remain local-only and must not be committed.

This closure is limited to L1-FRED and does not authorize L1-PRICE, L1-DART, 8D Scout progression, pilot, KIS, broker, write-mode paper loop, `PaperLoopRunner.run()`, or `ops/run_paper_once.py`.

### Live-smoke closure — L1-PRICE

L1-PRICE live-smoke is closed for the yfinance PRICE path. The operator-authorized run used a fresh runtime workspace and performed the live yfinance fetch through the documented unofficial-provider boundary, then staged an immutable raw snapshot and DateIdSourceRecord JSONL under runtime/research. The staged PRICE JSONL passed 8B validate-only, then 8B normal and 8C smoke with --require-symbol-coverage. The provider symbol 005930.KS was staged as internal symbol SYNTH-KR-0001 / KR, and final 8C coverage passed with missing_symbols == [].

PRICE uses no API key/env var in this lane. yfinance is an unofficial external provider; provider/network boundary must be treated explicitly in operator planning. Runtime artifacts remain local-only and must not be committed. The observed price payload is research evidence only and must not be interpreted as buy/sell/hold/allocation/order/action output. This closure is limited to L1-PRICE and does not authorize L1-DART, 8D Scout progression, pilot, KIS, broker, write-mode paper loop, PaperLoopRunner.run(), or ops/run_paper_once.py.

### Live-smoke closure — L1-DART

L1-DART live-smoke is closed for the OpenDART DISCLOSURE context-only path. The operator-authorized run used a fresh runtime workspace and performed the live OpenDART fetch through the documented DART_API_KEY env/network boundary, then staged an immutable raw snapshot and DateIdSourceRecord JSONL under runtime/research. The staged DART JSONL passed 8B validate-only with 21/21 valid records, then 8B normal and 8C context-only smoke.

OpenDART is an external disclosure provider. The run used corp-code `00126380` and `bgn-de` `20260501` (`end-de` omitted); internal symbol was `SYNTH-KR-0001`. DART live-smoke does not use `--date-id`. It uses `--store` for Date-ID allocation, while record persistence remains the 8B normal responsibility. In this run, fetch-time store rows remained empty before 8B normal, and 8B normal saved/exported 21 disclosure records. DART disclosure Date-IDs may follow disclosure source timestamps rather than the runtime DAY; this is expected. DART-only 8C was run without `--require-symbol-coverage`, and a non-empty `missing_symbols` list (e.g. `["KR:SYNTH-KR-0001"]`) remains acceptable for this context-only lane because DART DISCLOSURE records are context-only and do not satisfy PRICE symbol coverage.

Secret handling remained value-hidden: the DART_API_KEY value was not written to stdout/stderr, snapshot, JSONL, Date.md, or store. Runtime artifacts remain local-only and must not be committed. The observed disclosure payload is research evidence only and must not be interpreted as buy/sell/hold/allocation/order/action output. This closure is limited to L1-DART and does not authorize 8D Scout progression, pilot, KIS, broker, write-mode paper loop, `PaperLoopRunner.run()`, or `ops/run_paper_once.py`.

### Post-intake no-write chain closure — 8D–8I

This section records operator-controlled downstream handoff observations completed under separate explicit approvals. It does **not** expand the source-intake scope of this document; Scout/Allocator/Analysis orchestration, trading, broker, KIS, and write mode remain outside source-intake implementation scope.

**8D / 8E Scout**

- Scout packet build completed using DART-only `DISCLOSURE` context.
- 21 disclosure records/date_ids were used as packet input.
- The packet builder produced input/prompt/summary only; it did not generate raw LLM output.
- The operator invoked a local LLM directly and saved the raw JSON.
- 8E validation: **PASS**.
- Validated Scout universe: `paper-v0`.
- Validated Scout cited Date-ID count: 10.
- Raw/validated/runtime evidence remains local-only.

**8F Allocator**

- Used an approved synthetic paper portfolio state; the portfolio state is a synthetic replay, not the current measured portfolio.
- Allocator target weights: KR 80, US 0, GOLD 20; cash target: 20.
- The operator saved local LLM raw output directly.
- Allocator validation: **PASS**.
- Cited Date-ID count: 1.
- Allocator output is allocation intent metadata, not executable trading.

**8G Analysis**

- Target market: KR; target symbol: `SYNTH-KR-0001`.
- Allocator tolerance context was intentionally omitted.
- Allocator KR=80 market allocation was **not** converted to per-symbol target 80.
- Final validated Analysis: action `sell`, `target_weight_percent` 25, `risk_manager.max_weight_percent` 25; cited Date-ID count: 4.
- Action/target are paper-pilot analysis intent metadata, not orders.

**8H PaperLoopInput**

- `run_id`: `paper-run-260611-1`; `broker_account_role`: PAPER; risk mode: rebalancing.
- Synthetic current symbol weight: 80; validated Analysis target: 25.
- `allocator_symbol_target_weight`: null; `allocator_tolerance_percent`: model default 5.
- Because `allocator_symbol_target_weight` is null, allocator tolerance comparison was not applied.
- PaperLoopInput model validation: **PASS**.
- Execution, order generation, broker, and KIS were not run.

**8I no-write rehearsal**

- Only the documented no-write rehearsal path was executed.
- Internally invoked `ops/run_paper_once.py` as a subprocess with `--validated-input` (PaperLoopInput), `--ledger-db`, `--decision-db`, `--no-write`, and `--json`.
- Outcome: **PASS**; status: `VALIDATION_ONLY`.
- Cited Date-ID union count: 10.
- Ledger DB: absent-before == absent-after; decision DB: absent-before == absent-after.
- `PaperLoopRunner.run()`, `PaperBroker`, and KIS were not called.
- Order generation, ledger writes, decision snapshot writes, and execution artifacts were not run or created.
- Only three rehearsal evidence artifacts (JSON/TXT/summary) were generated runtime-local.

The 8I rehearsal invoked `ops/run_paper_once.py` only through its documented `--no-write --json` validation-only subprocess path. It did not enter the write-mode branch, DB-open path, runner path, or broker path.

**Closure boundary**

- This closure closes only the 8D–8I operator-controlled no-write chain.
- It does **not** start the 30-trading-day pilot.
- It does **not** authorize KIS read-only.
- It does **not** authorize broker order submission.
- It does **not** authorize write-mode `ops/run_paper_once.py`.
- It does **not** authorize `PaperLoopRunner.run()`.
- It does **not** authorize ledger/decision DB writes, fills, NAV snapshots, daily summary, or postmortem automation.
- Runtime artifacts and detailed stdout/hash/log evidence remain local-only and must not be committed.

Runtime grouping DAY was `2026-06-11`. Portfolio and market-price snapshot used the approved `2026-05-28` synthetic paper replay — not a live account state or real-time market state on `2026-06-11`. This is not investment advice, live-trading intent, or order instruction.

## 1. Purpose

Foundation **8B–8I** and Controlled Day 1 no-write walk-through are **CLOSED**. Today, research evidence enters the system only through **operator-prepared JSONL** (`runtime/research/${DAY}/research_sources.jsonl`) consumed by `ops/research_source_intake.py`.

**Real Research Source Intake v1** adds a **read-only external data layer** that:

1. Fetches (or replays) data from approved external services.
2. Normalizes responses into **`DateIdSourceRecord`-compatible** records.
3. Hands those records to the **unchanged** 8B → store → `Date.md` → 8C–8I chain.

Scout continues to consume **`Date.md` / `ScoutInput` only**. Fetchers never call Scout, Allocator, Analysis, `PaperLoopRunner`, broker, or KIS.

---

## 2. Non-goals

| Non-goal | Rationale |
|---|---|
| Autonomous trading agent | LLM decides; Python validates. Fetchers do not decide. |
| Scout / Allocator / Analysis LLM orchestration | Manual LLM handoff remains; no new LLM entrypoints. |
| Bypassing `SQLiteDateIdSourceStore` | Store is canonical; raw API payloads never go into prompts. |
| Scheduling / launchd / cron | v1 fetchers are **operator-triggered** only. |
| New `FactType` enum members | Use existing `FactType` values only; gaps go to [§15 Deferred items](#15-deferred-items). |
| KIS read-only or KR broker price path | **Explicitly out of scope** for v1 ([G3](#mandatory-design-guards-g1g4)). |
| Write-mode paper loop, ledger, orders, fills | Walk-through still ends at **8I no-write** ([G4](#mandatory-design-guards-g1g4)). |
| Live 30-trading-day pilot start | Separate readiness decision after v1 intake is proven repeatable. |
| News / social / web scraping | `FactType.NEWS` intake deferred until source + schema are agreed. |

---

## 3. Current Foundation data path

Verified repo flow (manual today):

```text
operator JSONL (DateIdSourceRecord objects)
  → ops/research_source_intake.py
      → parse_jsonl_records() / DateIdSourceRecord.model_validate()
      → SQLiteDateIdSourceStore.save_record()
      → render_date_md() → Date.md (summary + payload_hash only; no raw payload)
  → ops/run_date_md_smoke.py (8C)
  → ops/build_scout_manual_packet.py (8D) — ScoutInput + scout_prompt
  → manual Scout LLM → ops/validate_scout_raw_json.py (8E)
  → … 8F / 8G / 8H …
  → ops/rehearse_paper_loop_no_write.py (8I) — ends at no-write rehearsal
```

**Canonical model** (`src/domain/source.py`):

- `DateIdSourceRecord`: `date_id`, `fact_type` (`FactType` enum), `source_name`, `source_timestamp`, `created_at`, `summary`, `payload`, optional `symbol`, `market`, `source_url`.
- `source_timestamp` and `created_at` must be **timezone-aware** datetimes.

**Existing read-only adapters** (Phase 6, client-injected, no live HTTP in tests):

| Module | Intermediate model | → `DateIdSourceRecord` helper | `FactType` |
|---|---|---|---|
| `src/data/yfinance_adapter.py` | `MarketDataPoint` | `market_data_point_to_source_record()` | `PRICE` |
| `src/data/fred_adapter.py` | `MacroDataPoint` | `macro_data_point_to_source_record()` | `MACRO` |
| `src/data/dart_adapter.py` | `DisclosureRecord` | `disclosure_record_to_source_record()` | `DISCLOSURE` |

**Date-ID assignment** (`src/data/date_id_generator.py`):

- Format: `YYMMDD-N` (KST calendar date from `source_timestamp`).
- `DateIdGenerator.next_id(source_timestamp)` reads store state; **save between calls** when issuing multiple IDs same day.

---

## Mandatory design guards (G1–G4)

These guards are **non-negotiable** for Real Research Source Intake v1 and all follow-on implementation PRs.

### G1. Read-only input edge

**Fetchers ONLY read external data and emit `DateIdSourceRecord`-compatible records.** They feed the existing **8E validator / 8H assembler / 8I no-write** chain **unchanged**. No write/order/broker path is touched.

- Fetcher output stops at `research_sources.jsonl` (or equivalent staging file) plus immutable raw snapshots.
- Downstream ops scripts (`validate_scout_raw_json.py`, `assemble_paper_loop_input.py`, `rehearse_paper_loop_no_write.py`) are **not modified** to call external APIs.
- Fetchers must not emit target weights, analysis actions, order intents, or portfolio decisions.

### G2. Fetch → snapshot → replay (determinism)

**Every live fetch MUST first persist the raw external response** to an immutable snapshot under `runtime/`. All downstream steps (normalization → `DateIdSourceRecord` → store → `Date.md`) consume the **SNAPSHOT**, never a live call.

- Guarantees a given research `DAY` is **fully replayable/debuggable offline** after one fetch.
- Snapshot path convention: [§8 Runtime paths + snapshot/replay convention](#8-runtime-paths--snapshotreplay-convention-g2).
- **Offline/replay mode** re-normalizes from existing snapshots only (no network).

### G3. Data-source boundary vs deferred KIS

**KIS read-only remains DEFERRED and OUT OF SCOPE.** Per candidate source, the design names the **external service** that provides data. **No v1 source routes through KIS.**

- KR listed-equity **price** via KIS Open API is a **deferred** path (`docs/TECH_DEBT.md` KIS verification backlog).
- v1 candidates: FRED API, Yahoo Finance (yfinance-style), DART Open API — each independent of broker infrastructure.

### G4. Still terminates at 8I no-write

This increment adds **real INPUT only**. **Write mode**, ledger/decision writes, order generation, broker, KIS, and `PaperLoopRunner.run()` **remain forbidden**. The operator walk-through still **ends at 8I no-write rehearsal**.

- After v1 intake + 8B export, the Controlled Day 1 chain (8C→8I) is unchanged.
- `ops/run_paper_once.py` without `--no-write` is not part of v1 acceptance.

---

## 4. Proposed Real Research Source Intake v1 architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│  Real Research Source Intake v1 (NEW — read-only, pre-8B)       │
│                                                                 │
│  config/research_sources.*.example  +  env API keys (local only) │
│       │                                                         │
│       ▼                                                         │
│  Source registry ──► SourceFetcher (per source)                 │
│       │                                                         │
│       ├── dry-run        (plan only, no network)                │
│       ├── live-smoke     (operator --live-smoke, fetch once)    │
│       └── offline/replay (read snapshot dir only)               │
│       │                                                         │
│       ▼                                                         │
│  immutable raw snapshot  runtime/research/${DAY}/sources/...      │
│       │                                                         │
│       ▼                                                         │
│  source adapter normalize(snapshot) → intermediate model        │
│       │                                                         │
│       ▼                                                         │
│  map → DateIdSourceRecord (+ DateIdGenerator / explicit id)     │
│       │                                                         │
│       ▼                                                         │
│  staged JSONL  runtime/research/${DAY}/research_sources.jsonl   │
│  (append or merge; operator review before 8B)                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Foundation 8B (EXISTING — unchanged contract)                  │
│  ops/research_source_intake.py → store → Date.md                │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
              8C smoke → 8D Scout packet → … → 8I no-write
```

**Placement:** fetchers sit **before** 8B. They **replace manual JSONL authoring** for supported facts; they do **not** replace 8B validation, store semantics, or Date.md export rules.

**Ops entrypoint:** `ops/fetch_research_sources.py` — `--replay` / `--dry-run` / `--live-smoke`. stdlib HTTP는 **`src/data/fred_http_client.py` only**; API key leakage hardening applies to live-smoke snapshots and CLI errors.

---

## 5. SourceFetcher contract

Design-time protocol (Python `Protocol` or explicit ABC in a later PR):

| Method / property | Responsibility |
|---|---|
| `source_key: str` | Stable registry id (e.g. `"fred"`, `"yfinance"`, `"dart"`). Matches snapshot subdirectory name. |
| `external_service: str` | Human-readable provider name for runbooks (not KIS). |
| `fact_types: tuple[FactType, ...]` | Fact types this fetcher may emit (subset of existing enum). |
| `plan_fetch(config, *, day)` | **Dry-run:** list would-fetch identifiers (series ids, symbols), URLs (redacted keys), expected snapshot filenames. **No network.** |
| `fetch_and_snapshot(config, *, day, snapshot_dir)` | **Live-smoke only:** HTTP/read external API → write **one immutable** raw JSON file → return snapshot metadata. |
| `normalize_snapshot(snapshot_path, *, as_of)` | Parse raw snapshot → intermediate model(s) → **`DateIdSourceRecord` candidates** (without store write). **No network.** |
| `default_timeout_seconds`, `max_retries` | Per-source limits (design defaults; override in config). |

**Invariants:**

- `fetch_and_snapshot` never writes to `SQLiteDateIdSourceStore`, `Date.md`, or paper ledger paths.
- `normalize_snapshot` is **pure** w.r.t. network (snapshot bytes in → records out).
- Client objects (`FredMacroAdapter(client=...)`) stay **injected** so unit tests use fakes (existing Phase 6 pattern).

**Staging output:** normalized records serialize to JSONL lines compatible with `parse_jsonl_records()` — same field names/types as manual 8B input.

---

## 6. Source registry / config

**Registry:** static map `source_key → SourceFetcher instance factory` (no dynamic plugin loading in v1).

**Config convention (committed examples only):**

| Path | Purpose |
|---|---|
| `config/research_sources.toml.example` | Per-source enable flags, series/symbol lists, timeouts ( **forward-looking; not loaded by `settings.py` today** ) |
| `config/universe.paper.toml.example` | Unchanged; Scout/universe symbol coverage (8C) |
| `config/config.full.example` `[data_apis]` | Documents env var **names** only: `FRED_API_KEY`, `DART_API_KEY`, etc. |

**Per-source config fields (minimal v1):**

```toml
# config/research_sources.toml.example (illustrative)
[fetcher.fred]
enabled = true
series_ids = ["DGS10"]          # implementation must verify against FRED docs
timeout_seconds = 15
source_name = "fred"            # must match adapter SOURCE_NAME

[fetcher.yfinance]
enabled = false                 # deferred after fred v1
symbols = ["SPY"]
timeout_seconds = 15
source_name = "yfinance"

[fetcher.dart]
enabled = false                 # deferred
symbols = ["005930"]
limit = 5
timeout_seconds = 20
source_name = "dart"
```

**Secrets:** API keys **only** via environment variables or local ignored config (`.env`, `config/config.toml` — gitignored). Never committed. Ops scripts read env var **names** from config, not values.

**Operator local file:** `runtime/research/research_sources.toml` (optional override; **commit forbidden**).

---

## 7. DateIdSourceRecord mapping (incl. FactType mapping)

| Source key | External service | Adapter | Intermediate → Record | `FactType` | Required mapping notes |
|---|---|---|---|---|---|
| `fred` | [FRED API](https://fred.stlouisfed.org/docs/api/fred/) | `FredMacroAdapter` | `MacroDataPoint` → `macro_data_point_to_source_record()` | `MACRO` | `series_id` in payload; `summary` cites series + value + observation time |
| `price` | generic local price snapshot (replay/fixture) | `GenericPriceSnapshotReplayFetcher` | `MarketDataPoint` → `market_data_point_to_source_record()` | `PRICE` | `symbol`, `market`; **2A implemented** — satisfies universe symbol coverage via replay |
| `yfinance` | Yahoo Finance (yfinance library; **unofficial external provider**) | `price_live_client` → `GenericPriceSnapshotReplayFetcher` | `MarketDataPoint` → `market_data_point_to_source_record()` | `PRICE` | **2B live-smoke implemented** — live fetch writes generic PRICE snapshot then replays via 2A; operator maps `--provider-symbol` to universe `--symbol`/`--market` |
| `dart` | DART-like local disclosure snapshot (replay/fixture) | `DartDisclosureSnapshotReplayFetcher` | `DisclosureRecord` → `disclosure_record_to_source_record()` | `DISCLOSURE` | **3A implemented** — multi-record; auto Date-ID via `--store`; `market=None` preserved (does not satisfy 8C symbol coverage). **3A.1:** symbol-matched disclosures included in Scout packet context via `ops/build_scout_manual_packet.py` when universe scope enables the symbol |
| `kis` | KIS Open API | — | — | — | **DEFERRED** — not v1 ([G3](#g3-data-source-boundary-vs-deferred-kis)) |
| `news` | TBD (Finnhub/Naver/etc.) | — | — | `NEWS` | **DEFERRED** — no adapter ops path in v1 |

**Summary rules (prompt-safe):**

- Concise natural language; include **symbol or series**, **numeric fact**, and **source_timestamp** (or observation date).
- **Never** embed raw JSON payload in `summary`.
- `Date.md` export continues to show `summary` + `payload_hash` only (`render_date_md()` in `ops/research_source_intake.py`).

**`created_at` vs `source_timestamp`:**

- `source_timestamp`: when the external fact was observed/published (from API).
- `created_at`: when AutoStock normalized the record (`as_of` at fetch/replay time, timezone-aware).

**Date-ID assignment policy:**

1. Prefer `DateIdGenerator(store).next_id(record.source_timestamp)` **at JSONL staging time** when store path is known.
2. Allow operator-prepared explicit `date_id` in staged JSONL for replay/debug (same as manual 8B today).
3. On 8B `save_record`: `DuplicateDateIdError` → **fail closed** (existing behavior).
4. Re-normalizing the **same snapshot** with unchanged mapping must produce **byte-identical** canonical record fields except `created_at` if `as_of` differs — document `as_of` in replay manifest for audit.

---

## 8. Runtime paths + snapshot/replay convention (G2)

All paths under `runtime/` — **gitignored, never committed**.

```text
runtime/research/${DAY}/
├── sources/
│   ├── manifest.jsonl                    # one line per snapshot (metadata, not raw body)
│   ├── fred/
│   │   └── raw_<UTC compact>_<sha8>.json
│   ├── yfinance/
│   │   └── raw_<UTC compact>_<sha8>.json
│   └── dart/
│       └── raw_<UTC compact>_<sha8>.json
├── research_sources.jsonl                # staged DateIdSourceRecord lines (8B input)
├── research_sources.provenance.jsonl     # optional: snapshot_path + record date_id map
├── date_id_sources.sqlite3               # 8B output store
└── Date.md                               # 8B output prompt reference
```

**Snapshot filename pattern:**

```text
runtime/research/${DAY}/sources/<source_key>/raw_<YYYYMMDDTHHMMSSZ>_<sha8>.json
```

- `<source_key>`: registry key (`fred`, `yfinance`, `dart`).
- `<sha8>`: first 8 hex chars of SHA-256 of **canonical raw body bytes** (detect accidental duplicate writes).
- Files are **append-only**; re-fetch same day → **new file**, never overwrite.

**`manifest.jsonl` line (illustrative):**

```json
{
  "snapshot_path": "runtime/research/2026-05-29/sources/fred/raw_20260529T010203Z_a1b2c3d4.json",
  "source_key": "fred",
  "external_service": "FRED API",
  "fetched_at": "2026-05-29T01:02:03+00:00",
  "operator_mode": "live-smoke",
  "request_summary": "series_id=DGS10",
  "http_status": 200,
  "sha256": "…"
}
```

**Modes:**

| Mode | CLI flag (proposed) | Network | Writes snapshot | Writes JSONL | Runs 8B |
|---|---|---|---|---|---|
| **dry-run** | `--dry-run` | No | No | No | No |
| **live-smoke** | `--live-smoke` | Yes (explicit) | Yes | Yes (stage) | Optional separate step |
| **offline/replay** | `--replay` | No | No | Yes (from snapshots) | Optional separate step |

**Replay contract:** `--replay --day ${DAY}` reads `sources/manifest.jsonl` + snapshot files only; ignores stale network. Operator may pin `--snapshot fred/raw_….json` for single-file replay.

**Paper day folder:** unchanged — operator copies or references `runtime/research/${DAY}/Date.md` into `runtime/paper/${DAY}/date/` per `docs/PAPER_PILOT_WORKFLOW.md`.

---

## 9. Error handling and idempotency

| Scenario | Behavior |
|---|---|
| HTTP timeout | Fail fetch; **no** partial snapshot promoted to JSONL; log sanitized error in manifest |
| HTTP 4xx/5xx | Same; optional `--continue-on-partial-failure` records failed source in manifest only |
| Rate limit (429) | Exponential backoff within `max_retries`; then fail closed for that source |
| Invalid JSON body | Snapshot saved with `_parse_error` sidecar note; normalization fails with clear stage |
| Adapter validation error | Fail that record; other sources in multi-source run may still stage if `--continue-on-partial-failure` |
| Duplicate `date_id` in staged JSONL | Fail at 8B parse (existing) |
| Duplicate `date_id` in store | `DuplicateDateIdError` at 8B (existing) |
| Re-run live-smoke same day | New snapshot file; operator merges/replaces staged JSONL consciously |
| Re-run replay | Deterministic records from fixed snapshots |

**Idempotency keys (normalization):**

- FRED: `(series_id, observation_date/value from raw)`
- yfinance: `(symbol, source_timestamp, price)`
- DART: `(symbol, disclosure_id or title+source_timestamp from raw)`

Implementation must verify official id fields before coding.

**Partial failure default:** **fail closed** for v1 live-smoke (no silent omission of a configured source).

---

## 10. Candidate source assessment

> **Schema warning:** External API response shapes below are **illustrative**. Implementation PRs **must verify official documentation** before coding each client.

### 10.1 yfinance-style market data

| Aspect | Assessment |
|---|---|
| **External service** | Yahoo Finance (via `yfinance` Python package or minimal read-only HTTP client) |
| **Not via** | KIS ([G3](#g3-data-source-boundary-vs-deferred-kis)) |
| **`FactType`** | `PRICE` |
| **Repo readiness** | `YFinancePriceAdapter`, `market_data_point_to_source_record()` exist |
| **Friction** | Low auth (no API key for basic quotes); **new pip dependency** likely if using `yfinance`; KR/US ticker mapping needs verification; Yahoo unofficial API may change |
| **Universe fit** | Good — supports 8C `--require-symbol-coverage` when universe symbols are fetched |
| **Rate limits** | Informal; conservative timeouts + manual operator trigger |

### 10.2 FRED-style macro

| Aspect | Assessment |
|---|---|
| **External service** | [FRED API](https://fred.stlouisfed.org/docs/api/fred/) (Federal Reserve Bank of St. Louis) |
| **Not via** | KIS |
| **`FactType`** | `MACRO` |
| **Repo readiness** | `FredMacroAdapter`, `macro_data_point_to_source_record()` exist |
| **Friction** | **Low** — free API key via env `FRED_API_KEY`; stable docs; **stdlib HTTP viable** (no new dependency required for v1 ops client); one series → one record |
| **Universe fit** | Macro context for Scout; **does not** satisfy per-symbol price coverage alone — pair with manual or later PRICE source |
| **Rate limits** | Documented; modest volume for daily operator run |

### 10.3 DART-style disclosure

| Aspect | Assessment |
|---|---|
| **External service** | [DART Open API](https://opendart.fss.or.kr/) (Financial Supervisory Service, KR) |
| **Not via** | KIS |
| **`FactType`** | `DISCLOSURE` |
| **Repo readiness** | `DartDisclosureAdapter`, `disclosure_record_to_source_record()` exist |
| **Friction** | **High** — API key registration, corp-code mapping, disclosure list schema, pagination, KR-only symbols, rate limits, doc verification |
| **Universe fit** | Strong for KR pilot eventually; heavy for **first** source |
| **Rate limits** | Strict daily caps; requires careful batching (still manual trigger in v1) |

---

## 3B DART live-smoke design

> **3B0 (this section):** design and fixture-first guardrails only. No live OpenDART HTTP, no new dependencies, no API key reads, no ops code changes.

### Purpose

- Fetch real DART disclosure data **only** under explicit operator command (future **3B2**).
- Convert the live HTTP response into an **immutable raw snapshot** on disk.
- **Replay** that snapshot through the **existing 3A** path (`DartDisclosureSnapshotReplayFetcher` → `DartDisclosureAdapter` → `DisclosureRecord` → `disclosure_record_to_source_record()`).
- Emit **8B-compatible JSONL** for the unchanged `ops/research_source_intake.py` chain.
- Keep DART `DISCLOSURE` as **symbol-level Scout context** via **3A.1** (`market=None`).
- Keep **`PRICE`** as the source of **8C symbol coverage** (DART never substitutes for `(market, symbol)` coverage).

**Target architecture (mandatory):**

```text
OpenDART live fetch
  → immutable raw DART live snapshot
  → DART snapshot replay/normalization path from 3A
  → DartDisclosureAdapter
  → DisclosureRecord
  → disclosure_record_to_source_record()
  → DateIdSourceRecord JSONL
  → existing 8B intake
  → Date.md/store
  → Scout context via 3A.1
```

### Non-goals

| Non-goal | Rationale |
|---|---|
| Autonomous trading | LLM decides; fetchers do not trade. |
| Broker / KIS / PaperLoop execution | v1 intake stops at 8B → 8I no-write rehearsal. |
| Scheduling / launchd / cron | Operator-triggered only in v1. |
| Scout / Allocator / Analysis automation | Manual LLM handoff unchanged. |
| Date-ID store bypass | **8B normal** remains the only store writer. |
| Direct `DateIdSourceRecord` from live HTTP | Violates snapshot→replay boundary ([§3B Required boundary](#required-boundary)). |
| Live API work in **3B0** | Docs-only; no network in this step. |
| 30-trading-day pilot start | Separate readiness decision. |

### Required boundary

Live DART HTTP must **not** directly construct `DateIdSourceRecord`. The **only** allowed path:

```text
live response → raw snapshot → existing DART replay fetcher/adapter/mapper
```

Equivalent chain:

```text
DART live HTTP → raw snapshot → 3A replay path → 8B JSONL
```

Any future `dart_http_client` (or similar) writes **raw snapshot bytes only**. Normalization reuses `DartDisclosureSnapshotReplayFetcher` and existing mappers — same as FRED (1B) and yfinance PRICE (2B).

### Snapshot immutability

- Raw snapshots are **never overwritten** once written (same policy as FRED/yfinance live-smoke).
- `--force` may apply **only** to output JSONL staging paths, **not** to raw snapshot files.
- Snapshot path collision must **fail closed** before JSONL write (operator must pick a new path or new day folder).
- Re-running live-smoke the same day creates a **new** snapshot file; operator merges/replaces staged JSONL consciously.

### Secret handling

- OpenDART API key (`DART_API_KEY` or equivalent) must **never** appear in: raw snapshot body, staged JSONL, `Date.md`, store rows, logs, stdout, stderr, or error JSON payloads.
- Request metadata in snapshots may record **sanitized** fields only (e.g. env var **name**, key **present** boolean) — **never** the key value.
- Later-phase tests (**3B1+**) must include **no-leak** assertions (mirror FRED 1B / yfinance 2B hardening).

### Corp-code mapping

- OpenDART list/detail APIs typically require a **provider-specific** company identifier (corp code), not necessarily the internal AutoStock universe `symbol`.
- **3B design separates:**
  - **Internal `symbol`** — e.g. `SYNTH-KR-0001` in `config/universe.paper.toml.example`
  - **Provider corp code / ticker / DART identifier** — passed explicitly in a later phase (`--corp-code`, config table, or operator mapping file)
- Do **not** assume internal `symbol` is directly usable by OpenDART without an explicit mapping step.
- Corp-code download/cache is **3B3** optional hardening, not required for first live-smoke.

### Date-ID allocation

- Live DART may return **multiple** disclosures per fetch; keep **3A** policy:
  - Replay/normalize stage reads **store state only** (plus in-memory reservation during a single fetch invocation).
  - Date-ID allocation uses store-seeded canonical logic + in-memory reservation (same as 3A replay).
  - **Fetch stage does not write store.**
  - **`ops/research_source_intake.py` normal mode** remains the **only** store writer.
- **Staging order warning:** run **8B normal** for prior source outputs (FRED, PRICE, etc.) **before** DART replay/live normalization so Date-ID allocation sees existing store state. Unstaged JSONL Date-IDs are **not** visible to allocation during fetch.

### 8C / Scout semantics

| Rule | Behavior |
|---|---|
| DART `DISCLOSURE` | `market=None` preserved end-to-end |
| 8C symbol coverage | Satisfied by **`PRICE`** (or other `(market, symbol)` records), **not** DART |
| DART-only + `--require-symbol-coverage` | Must **fail** (unchanged) |
| Scout context (3A.1) | Include when `fact_type == DISCLOSURE`, symbol matches scope-enabled universe symbol, `market is None` |
| Combined smoke | FRED `MACRO` + yfinance/generic `PRICE` + DART `DISCLOSURE` in one Scout packet — **verified** at runtime; PRICE supplies coverage, DART adds context |

### Fixture-first plan

| Phase | Scope | Network | Notes |
|---|---|---|---|
| **3B0** | Design + guardrails (this doc) | None | No code, no tests, no deps |
| **3B1** | Fixture-first live snapshot normalizer + fake transport | **Fake HTTP only** in tests | **Implemented:** `src/data/dart_live_client.py` + `tests/test_dart_live_client.py`; snapshot → 3A replay → 8B `--validate-only`; collision + no-leak guards; no real network |
| **3B2** | Operator-triggered DART `--live-smoke` | Real OpenDART (operator explicit) | **Implemented:** `dart_http_client.py` + `ops/fetch_research_sources.py --live-smoke --source dart`; snapshot→3A replay→JSONL; no scheduler/broker/KIS |
| **3B3** | Optional hardening | Real (operator) | Rate limits, pagination, corp-code cache, retry/backoff, error-schema guards |

**3B1 test requirements (implementation PR, not 3B0):**

- No real DART network in CI or unit tests.
- Injected / fake HTTP transport only.
- Golden raw snapshot fixtures under `tests/fixtures/research/dart/` (live-shaped bodies distinct from 3A replay fixtures if needed).
- Path: snapshot → `DartDisclosureSnapshotReplayFetcher` → JSONL → 8B `--validate-only`.
- Assert: DART-only `--require-symbol-coverage` still fails; combined macro + price + disclosure Scout packet still succeeds when `require_symbol_coverage=False` and PRICE present for coverage.
- Assert: snapshot collision fails before JSONL write; API key never in snapshot/stdout/stderr/error JSON.

**3B2 operator constraints (implementation PR, not 3B0):**

- Explicit CLI invocation only (`ops/fetch_research_sources.py --live-smoke --source dart` or equivalent).
- No launchd/cron, no PaperLoop hook, no KIS, no ledger writes.

---

## 3C DART corp-code resolver (fixture-first + live master fetch)

> **3C1 (implemented):** local corp-code master XML/ZIP → `stock_code` → `corp_code`. No network, no API key, no env read.
> **3C2 (implemented):** operator `--live-fetch` → OpenDART corpCode master ZIP snapshot → parse → `stock_code` → `corp_code`. Provider registry still deferred.

| Phase | Scope | Network |
|---|---|---|
| **3C1** | `src/data/dart_corp_code_resolver.py` + `tests/fixtures/research/dart/corp_code_*.xml` | None |
| **3C2** | `dart_corp_code_http_client.py` + immutable ZIP snapshot + resolver | Operator explicit |
| **Next** | Provider mapping registry: internal symbol → yfinance ticker / DART corp_code | Design follow-on |

**Resolver rules:**

- Parse OpenDART corp-code master XML (`<result><list>…</list></result>`).
- Normalize `stock_code` to 6 digits; accept unpadded input and optional `KR:` prefix.
- Blank/unlisted entries (`stock_code` empty) are stored but not matched by stock lookup.
- Duplicate listed `stock_code` with same `corp_name` fails at parse; different `corp_name` requires `corp_name` at resolve time.
- Optional ZIP: read single `.xml` member in-process (no `extractall`).

**Ops helper:** `ops/resolve_dart_corp_code.py --corp-code-xml … --stock-code 005930 --json` (local) or `--live-fetch --snapshot-dir … --json` (operator live master)

---

## 3D Provider mapping registry (fixture-first)

> **3D1 (implemented):** separate TOML registry maps internal `(market, symbol)` → yfinance `provider_symbol` + DART `corp_code`. Universe schema unchanged.

| Phase | Scope | Network |
|---|---|---|
| **3D1** | `src/data/provider_mapping_registry.py` + `config/provider_mappings.paper.toml.example` | None |
| **3E1** | `config/universe.kr-real.sample.toml` + `config/provider_mappings.kr-real.sample.toml` | None |
| **3E2** | `ops/run_kr_real_price_smoke.py` (live PRICE smoke for KR real sample) | Operator explicit |
| **3E3** | `ops/run_kr_real_dart_smoke.py` (live DART disclosure smoke for KR real sample) | Operator explicit |
| **3E4** | combined FRED+PRICE+DART context + Date.md/Scout budget caps | Operator explicit |
| **Next** | **3E5+** expand to 3–5 real companies | Operator explicit |

**Registry rules:**

- Provider IDs live outside universe TOML (`UniverseSymbol` extra fields forbidden).
- KR yfinance `provider_symbol` must end with `.KS` or `.KQ`.
- KR DART `corp_code` must be 8 digits; `stock_code` normalized via `normalize_stock_code()`.
- US mappings must not include DART provider.
- Disabled registry entries still pass schema validation but are excluded from enabled coverage counts.

**Ops helper:** `ops/validate_provider_mapping.py --universe … --provider-mapping … --json`

---

## 3E Static KR real-company sample universe (3E1–3E4)

> **3E1 (implemented):** operator-defined static KR real-company sample universe + matching provider mapping registry. Local TOML only — no network, no env/API key, no Scout/Allocator/PaperLoop execution.
> **3E2 (implemented):** operator-triggered live PRICE smoke for the KR real sample universe via provider mapping → yfinance snapshot → generic PRICE replay → JSONL. DART disclosure fetch not included.
> **3E3 (implemented):** operator-triggered live DART disclosure smoke for the KR real sample universe via provider mapping → OpenDART snapshot → adapter replay → combined-batch Date-ID → JSONL. DART records are Scout **context** (`market=None`); they do not satisfy 8C PRICE symbol coverage.
> **3E4 (implemented):** combined FRED macro + KR real PRICE + KR real DART context smoke with deterministic Date.md/Scout export caps (`--context-budget-profile kr-real-smoke`). Store retains all intake records; caps apply only at Date.md export boundary. Scout follows capped Date.md date_ids; 60KB guard unchanged.

| Phase | Scope | Network |
|---|---|---|
| **3E1** | `config/universe.kr-real.sample.toml` + `config/provider_mappings.kr-real.sample.toml` + `tests/test_kr_real_sample_universe.py` | None |
| **3E2** | `ops/run_kr_real_price_smoke.py` + `tests/test_kr_real_price_smoke.py` | Operator explicit (yfinance only) |
| **3E3** | `ops/run_kr_real_dart_smoke.py` + `tests/test_kr_real_dart_smoke.py` | Operator explicit (DART only) |
| **3E4** | `source_record_context_selector.py` + capped 8B export + `ops/build_kr_real_combined_context_smoke.py` + `tests/test_combined_context_budget.py` | Operator explicit (concat JSONL) |
| **Next** | **3E5+** expand to 3–5 companies | Operator explicit |

**3E1 universe (locally verified corp_code only):**

| Symbol | display_name | DART corp_code (fixture source) |
|---|---|---|
| `005930` | Samsung Electronics | `00126380` — `tests/fixtures/research/dart/corp_code_sample.xml` |
| `000660` | SK hynix | `00164779` — same fixture |

Synthetic paper files (`config/universe.paper.toml.example`, `config/provider_mappings.paper.toml.example`) are unchanged. Additional real companies (Hyundai, NAVER, Kakao, etc.) are deferred until live corp-code master snapshot/resolver verification in **3E5+**.

**3E1 ops helper (static validation):**

```bash
PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe config/universe.kr-real.sample.toml \
  --provider-mapping config/provider_mappings.kr-real.sample.toml \
  --json
```

**3E2 ops helper (live PRICE smoke — operator explicit; no DART/FRED):**

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/run_kr_real_price_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --provider-mapping config/provider_mappings.kr-real.sample.toml \
  --store "runtime/research/${DAY}/date_id_sources.kr_real_price.sqlite3" \
  --snapshot-dir "runtime/research/${DAY}/sources/price" \
  --out-jsonl "/tmp/autostock_kr_real_price_260530.jsonl" \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --force \
  --json
```

Path: provider mapping registry → `fetch_live_price_snapshot()` → immutable generic PRICE snapshot → store-seeded Date-ID allocation → `GenericPriceSnapshotReplayFetcher.normalize_snapshot()` → JSONL → existing 8B intake.

**3E3 ops helper (live DART disclosure smoke — operator explicit; no yfinance/FRED):**

```bash
DAY=2026-05-30
export DART_API_KEY="..."
PYTHONPATH=src uv run python ops/run_kr_real_dart_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --provider-mapping config/provider_mappings.kr-real.sample.toml \
  --store "runtime/research/${DAY}/date_id_sources.kr_real_dart.sqlite3" \
  --snapshot-dir "runtime/research/${DAY}/sources/dart" \
  --out-jsonl "/tmp/autostock_kr_real_dart_260530.jsonl" \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --bgn-de 20250101 \
  --api-key-env DART_API_KEY \
  --force \
  --json
```

Path: provider mapping registry → `fetch_live_dart_snapshot()` → immutable DART snapshot → `DartSnapshotReplayClient` + `DartDisclosureAdapter` → combined-batch `allocate_date_ids_for_records()` → `disclosure_record_to_source_record()` → JSONL → existing 8B intake → 8C without `--require-symbol-coverage` → Scout packet context via existing 3A.1 symbol-only inclusion.

**3E4 combined context + budget caps (operator explicit; concat JSONL + capped 8B export):**

```bash
cat /tmp/autostock_fred_260530.jsonl \
  /tmp/autostock_kr_real_price_260530.jsonl \
  /tmp/autostock_kr_real_dart_260530.jsonl \
  > /tmp/autostock_kr_real_combined_260530.jsonl

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_combined_260530.jsonl \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --context-budget-profile kr-real-smoke \
  --force-date-md \
  --json
```

Profile defaults: macro/global latest **5** per `(fact_type, source_name)`; PRICE latest **1** per `(market, symbol, source_name)`; DISCLOSURE latest **5** per `(symbol, source_name)`. Store unchanged; Scout follows capped Date.md.

---

## 3F Fixture-first KR provider mapping generator (3F1)

> **3F1 (implemented):** operator-curated KR candidate TOML + local corp-code XML/ZIP → generated universe TOML + provider mapping TOML. DART `corp_code` is resolver-proven; candidate file must not include `corp_code`. yfinance `provider_symbol` is explicit in candidate file. **Not** sector/universe discovery.

| Phase | Scope | Network |
|---|---|---|
| **3F1** | `kr_provider_mapping_generator.py` + `ops/generate_kr_provider_mapping.py` + fixture candidates | None |
| **Next** | **3E5+** sector discovery / expand to 3–5 companies | Deferred |

**3F1 ops helper (local files only):**

```bash
PYTHONPATH=src uv run python ops/generate_kr_provider_mapping.py \
  --candidates tests/fixtures/research/kr_candidates/kr_real_candidates.sample.toml \
  --corp-code-xml tests/fixtures/research/dart/corp_code_sample.xml \
  --universe-out /tmp/universe.kr-real.generated.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-real.generated.toml \
  --universe-name kr-real-generated-v1 \
  --provider-mapping-name kr-real-provider-mappings-generated-v1 \
  --force \
  --json
```

Post-generate validation: `ops/validate_provider_mapping.py --universe … --provider-mapping … --json`

---

## 3F2 Generator-based KR expansion workflow (3F2)

> **3F2 (implemented):** proves the existing 3F1 generator scales deterministically to 3–5 operator-curated candidates via **synthetic** checked-in fixtures only. Real large-cap expansion (Hyundai, NAVER, LG Chem, etc.) remains **operator-local** — requires a live/local 3C2 corp-code master snapshot; do **not** commit guessed or blank `corp_code` values for real companies. **Not** sector/universe discovery or ranking.

| Phase | Scope | Network |
|---|---|---|
| **3F2 (A)** | Synthetic multi-candidate fixture (`corp_code_synthetic_multi.xml` + `kr_real_candidates.synthetic_multi.toml`) + generator scale tests | None |
| **3F2 (B)** | Operator-local real 3–5 company expansion workflow (documented in RUNBOOK) | 3C2 snapshot fetch only (runtime artifact; never commit) |
| **Next** | **3E5+** sector discovery / automatic universe expansion | Deferred |

**3F2 synthetic scale proof (checked-in fixtures):**

```bash
uv run pytest tests/test_kr_real_generated_universe_expansion.py -v
```

Fixtures use explicit `SYNTH-*` company names and `9000xx` stock codes — not real company impersonation. DART `corp_code` is resolver-proven from the synthetic XML; candidate TOML must not include `corp_code`.

**3F2 operator-local real expansion (not checked in):**

1. Refresh local corp-code master snapshot via 3C2 (`ops/resolve_dart_corp_code.py --live-fetch …` → runtime ZIP; **never commit**).
2. Prepare operator-curated candidate TOML with explicit `yfinance_provider_symbol` (`.KS`/`.KQ`); **no** `corp_code` field.
3. Run `ops/generate_kr_provider_mapping.py` with local snapshot + candidates.
4. Validate outputs with `ops/validate_provider_mapping.py`.
5. Point 3E2/3E3/3E4 smoke flows at generated universe/mapping files.

Repo checked-in corp-code fixtures contain verified real listed entries for **two** companies only (`005930` / `000660` in `corp_code_sample.xml`). A third real company requires operator-supplied snapshot data.

---

## 3G Fixture-first sector-tagged KR candidate pool (3G1)

> **3G1 (implemented):** sector/industry-tagged KR candidate pool TOML + deterministic selector + export to existing 3F1 candidate TOML schema. Pool-only metadata (`base_market`, `sector`, `industry`, `eligible`, `priority`, `notes`) is dropped on export. **Not** live sector discovery, ranking, or automatic universe expansion.

| Phase | Scope | Network |
|---|---|---|
| **3G1** | `kr_candidate_pool.py` + `ops/select_kr_candidates.py` + synthetic sector pool fixture | None |
| **Next** | **3E5+** live sector discovery / ranking / factor scoring / automatic universe expansion | Deferred |

**3G1 ops helper (local files only):**

```bash
PYTHONPATH=src uv run python ops/select_kr_candidates.py \
  --candidate-pool tests/fixtures/research/kr_candidates/kr_sector_candidate_pool.synthetic.toml \
  --sector semiconductors \
  --sector internet \
  --max-total 3 \
  --max-per-sector 2 \
  --out-candidates /tmp/kr_candidates.selected.toml \
  --force \
  --json
```

**3G1 → 3F1 → 3E chain (local files only):**

1. Select from sector-tagged pool → 3F1 candidate TOML (pool metadata stripped).
2. Generate universe/provider mapping via `ops/generate_kr_provider_mapping.py` + local corp-code snapshot.
3. Validate via `ops/validate_provider_mapping.py`.
4. Use generated files in 3E2/3E3/3E4 smoke flows.

Automatic/live sector discovery, ranking, and factor scoring remain **deferred**.

---

## 3G2 Operator-local real sector pool workflow (3G2)

> **3G2 (implemented):** single CLI chains 3G1 select/export → 3F1 generate → provider mapping validation for operator-supplied sector pool + local corp-code snapshot. **Not** live sector discovery, ranking, or automatic universe construction.

| Phase | Scope | Network |
|---|---|---|
| **3G2** | `ops/build_kr_real_sector_pool_mapping.py` + workflow tests | None (operator supplies local pool + corp-code snapshot) |
| **Next** | **3E5+** live sector discovery / ranking / factor scoring / automatic universe expansion | Deferred |

**3G2 ops helper (operator-local; no live API in tests):**

```bash
PYTHONPATH=src uv run python ops/build_kr_real_sector_pool_mapping.py \
  --candidate-pool /path/to/operator/kr_sector_pool.local.toml \
  --corp-code-zip runtime/research/${DAY}/sources/dart_corp_code/<snapshot>.zip \
  --sector semiconductors \
  --sector internet \
  --max-total 5 \
  --max-per-sector 2 \
  --selected-candidates-out /tmp/kr_candidates.selected.toml \
  --universe-out /tmp/universe.kr-real.generated.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-real.generated.toml \
  --selection-name kr-real-selected-v1 \
  --selection-description "Operator-selected KR candidates." \
  --universe-name kr-real-generated-v1 \
  --provider-mapping-name kr-real-provider-mappings-generated-v1 \
  --force \
  --json
```

**Operator chain:** 3C2 corp-code snapshot (runtime artifact; never commit) → operator sector pool TOML (explicit yfinance symbols) → 3G2 helper → generated universe/mapping → 3E2/3E3/3E4 flows.

Error stages: missing corp-code **mode** → `args`; missing corp-code **file** → `resolve` (from 3F1 generator).

---

## 3G3-0 Live discovery/ranking guardrails (design-only)

> **3G3-0 (documented, not implemented):** design checkpoint before live sector discovery, ranking, or factor scoring. **3G1/3G2** closed the operator-local sector pool workflow. Live/automatic discovery, ranking, factor scoring, and automatic universe expansion remain **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-0** | Guardrails G1–G6 documented below | None |
| **Next** | **3G3-4B+** live discovery transport, factor scoring | Deferred |

### G1 — Operator control and read-only boundary

Live sector discovery/ranking must **not** directly mutate:

- universe config
- provider mapping config
- runtime trading state
- broker state
- PaperLoop state

Any discovered or ranked candidates must first be written as a **reviewable candidate pool or candidate list artifact**.

Operator approval remains required before:

- generating universe/provider mapping
- running PRICE/DART/FRED smoke
- building Scout context
- any downstream portfolio process

### G2 — Source provenance and snapshot/replay boundary

Any live discovery source must have a **raw snapshot or deterministic local fixture** before it can influence generated configs.

Examples:

- KRX listing / sector source → raw snapshot or checked-in fixture
- DART corp-code master → existing **3C2** local snapshot path
- price/fundamental metadata → raw snapshot first, then replay/normalize

Live response must **not** directly mutate universe/provider mapping.

### G3 — Ranking/factor scoring boundary

Ranking/factor scoring is **analysis metadata**, not trading instruction.

Future ranking output must be:

- deterministic
- explainable
- local-file reproducible
- separated from execution
- free of broker/write side effects

Ranking must **not** call:

- `PaperLoopRunner`
- broker `submit_order`
- KIS write path
- allocator execution path

Ranking may produce candidate annotations such as:

- sector
- industry
- liquidity proxy
- market cap proxy (if source exists)
- operator notes
- score version
- source timestamp

Ranking must **not** produce:

- buy/sell orders
- allocation percentages
- executable trading decisions

### G4 — Secret/network isolation

Any future live discovery implementation must:

- read env only in an explicit live operator command
- use source-specific env names
- never default one provider’s key to another provider’s env variable
- sanitize stdout/stderr/error JSON
- keep raw keys out of snapshots and generated configs
- be testable with fake transport / fixtures only
- avoid live network calls in tests

### G5 — Approved universe expansion path

The **only** approved path from discovery/ranking output to downstream research smoke:

```text
live/fixture source
→ raw snapshot/local fixture
→ sector-tagged candidate pool (3G1 pool schema)
→ 3G1 selector/export
   - export_selected_candidates()
   - drops root base_market
   - drops entry sector/industry/eligible/priority/notes/corp_code
→ 3F1 generator
   - generate_kr_provider_mapping_files()
   - DART corp_code = resolver-only
   - yfinance provider_symbol = explicit candidate input
→ provider mapping validation
   - validate_provider_mappings_cover_universe(require_yfinance=True, require_dart=True)
→ 3E2 PRICE smoke
→ 3E3 DART smoke
→ 3E4 combined context
→ operator review
```

**Not allowed:**

- automatic direct mutation of `config/universe*.toml` or `config/provider_mappings*.toml`
- any shortcut that skips raw snapshot/local fixture, 3G1 export, 3F1 generator, provider mapping validation, or operator review

### G6 — Recommended phase split (future; not implemented now)

| Phase | Scope |
|---|---|
| **3G3-1** | Fixture-first ranking model — synthetic pool only; deterministic score fields; no live API; no trading; no universe mutation; output is ranked pool or selected candidate TOML |
| **3G3-2** | Operator-local real ranking input — operator supplies real pool + optional local snapshots; ranking local-only; output reviewable before generation |
| **3G3-3** | Discovery snapshot replay adapter — fixture-first; raw snapshot → 3G1 candidate pool; no live transport |
| **3G3-4A** | Live-shaped fake-transport discovery snapshot fetcher — injected transport only; immutable raw snapshot |
| **3G3-4B** | Operator-triggered HTTP discovery live smoke — operator-supplied endpoint; sanitized errors; optional candidate pool replay |
| **3G3-5** | Fixture-first discovery source schema mapper — source-specific local fixture → canonical transport → 3G3-4A snapshot |
| **3G3-6** | Operator-triggered source-specific live endpoint adapter — HTTP → source snapshot → mapper → 4A canonical snapshot |
| **3G3-6+** | Source-specific live adapter hardening / factor hardening |
| **3G4+** | Factor scoring / ranking hardening — scoring versioning; source timestamps; explainability fields; regression fixtures; operator approval path |

Do **not** implement these phases until a separate intake task explicitly requests them.

---

## 3G3-1 Fixture-first KR candidate ranking model (3G3-1)

> **3G3-1 (implemented):** local sector pool + fixture ranking signals → reviewable ranked JSON (+ optional clean 3F1 candidate export). Ranking output is **metadata only** — not trading instruction, allocation, or investment recommendation. Live discovery and live factor scoring remain **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-1** | `kr_candidate_ranker.py` + `ops/rank_kr_candidates.py` + synthetic ranking signal fixture | None |
| **3G3-2** | `ops/build_kr_real_ranked_mapping.py` — operator-local real sector pool + ranking signals → ranked JSON → selected candidate TOML → 3F1 → validation | None |
| **3G3-3** | `kr_discovery_source_adapter.py` + `ops/replay_kr_discovery_snapshot.py` — discovery snapshot → 3G1 candidate pool | None |
| **3G3-4A** | `kr_discovery_live_client.py` — injected fake transport → immutable raw discovery snapshot | None |
| **Next** | **3G3-4B+** live discovery transport, operator live command | Deferred |

**Score version:** `kr-ranking-fixture-v1`  
**Precision:** 4 decimal places for contributions and final clamped score.

**Weighted formula:**

```text
raw_score =
  0.35 * liquidity_score
+ 0.25 * market_cap_score
+ 0.20 * quality_score
+ 0.20 * momentum_score
- 0.20 * risk_penalty
```

Then clamp to `[0.0, 1.0]`. Tie-break: score DESC → sector ASC → priority ASC (missing last) → symbol ASC.

**3G3-1 ops helper (local files only):**

```bash
PYTHONPATH=src uv run python ops/rank_kr_candidates.py \
  --candidate-pool tests/fixtures/research/kr_candidates/kr_sector_candidate_pool.synthetic.toml \
  --ranking-signals tests/fixtures/research/kr_candidates/kr_ranking_signals.synthetic.toml \
  --sector semiconductors \
  --sector internet \
  --max-total 5 \
  --max-per-sector 3 \
  --ranked-out /tmp/kr_candidates.ranked.json \
  --selected-candidates-out /tmp/kr_candidates.ranked.selected.toml \
  --top-n 3 \
  --selection-name kr-ranked-selected-v1 \
  --selection-description "Ranked synthetic KR candidates." \
  --force \
  --json
```

**Approved downstream path (ranking is advisory only):**

```text
ranked JSON artifact
→ optional selected candidate TOML (no ranking metadata)
→ 3F1 generator
→ provider mapping validation
→ 3E2/3E3/3E4
→ operator review
```

Ranked JSON must not contain trading/action/allocation/order fields. Missing ranking signal for a selected candidate fails at `stage="rank"`.

---

## 3G3-2 Operator-local real ranking input workflow (3G3-2)

> **3G3-2 (implemented):** operator supplies real sector pool TOML + local ranking signal TOML + local corp-code snapshot → ranked JSON → selected clean 3F1 candidate TOML → generated universe/provider mapping → validation. Ranking output remains **metadata only** — not trading instruction. Live discovery and live factor scoring remain **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-2** | `ops/build_kr_real_ranked_mapping.py` chains 3G3-1 ranker + 3F1 generator + provider mapping validation | None |
| **Next** | **3G3-4B+** live discovery transport, operator live command, factor scoring hardening | Deferred |

**Operator-local path:**

```text
operator real sector pool TOML
→ local ranking signal TOML (manual or external process)
→ 3G3-1 rank_kr_candidates()
→ ranked JSON artifact
→ selected clean 3F1 candidate TOML
→ 3F1 generate_kr_provider_mapping_files()
→ provider mapping validation
→ 3E2/3E3/3E4 (operator review required)
```

**3G3-2 ops helper (local files only):**

```bash
PYTHONPATH=src uv run python ops/build_kr_real_ranked_mapping.py \
  --candidate-pool /path/to/operator/kr_sector_pool.local.toml \
  --ranking-signals /path/to/operator/kr_ranking_signals.local.toml \
  --corp-code-zip runtime/research/${DAY}/sources/dart_corp_code/<snapshot>.zip \
  --sector semiconductors \
  --sector internet \
  --max-total 5 \
  --max-per-sector 2 \
  --top-n 3 \
  --ranked-out /tmp/kr_candidates.ranked.json \
  --selected-candidates-out /tmp/kr_candidates.ranked.selected.toml \
  --universe-out /tmp/universe.kr-real.ranked.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-real.ranked.toml \
  --selection-name kr-ranked-selected-v1 \
  --selection-description "Operator-ranked KR candidates." \
  --universe-name kr-real-ranked-v1 \
  --provider-mapping-name kr-real-ranked-provider-mappings-v1 \
  --force \
  --json
```

No live KRX crawling, yfinance, OpenDART API calls, env/API key reads, or automatic factor extraction. DART `corp_code` comes from local corp-code XML/ZIP resolver only; `yfinance_provider_symbol` remains explicit from candidate pool.

Synthetic proof: `uv run pytest tests/test_kr_real_ranked_mapping_workflow.py -v`.

---

## 3G3-3 Discovery snapshot replay adapter (3G3-3)

> **3G3-3 (implemented):** fixture-first discovery snapshot replay → 3G1 sector-tagged candidate pool TOML. Establishes the snapshot/replay boundary for future live discovery sources. Output is a **candidate pool only** — not a generated universe, not trading instruction. Live discovery transport remains **deferred to 3G3-4B**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-3** | `kr_discovery_source_adapter.py` + `ops/replay_kr_discovery_snapshot.py` + synthetic discovery snapshot fixture | None |
| **Next** | **3G3-4B+** live discovery transport, operator live command, factor scoring hardening | Deferred |

**Snapshot schema (v1):**

- Root: `source_key="kr_discovery"`, `snapshot_version=1`, `market="KR"`, timezone-aware `fetched_at`/`as_of`, non-empty `records[]`
- Records map to 3G1 candidate pool entries; `corp_code` forbidden; `source_timestamp`/`source_url` are snapshot provenance only (stripped from pool output)
- DART `corp_code` still comes only from 3C resolver + 3F1 generator + local corp-code snapshot

**Replay path:**

```text
discovery snapshot JSON (fixture or future live snapshot)
→ replay_kr_discovery_snapshot()
→ 3G1 candidate pool TOML
→ 3G1 selector/export
→ 3G3-1 ranker (local ranking signals)
→ 3F1 generator (local corp-code snapshot)
→ provider mapping validation
→ 3E2/3E3/3E4 (operator review required)
```

**3G3-3 ops helper (local files only):**

```bash
PYTHONPATH=src uv run python ops/replay_kr_discovery_snapshot.py \
  --snapshot tests/fixtures/research/kr_discovery/raw_kr_discovery_synthetic_success.json \
  --candidate-pool-out /tmp/kr_discovery_candidate_pool.toml \
  --pool-name kr-discovery-synthetic-pool-v1 \
  --pool-description "Synthetic replayed KR discovery candidate pool." \
  --force \
  --json
```

Live discovery commands (`--live`, KRX endpoints, env/API keys) **do not exist yet**. Synthetic proof: `uv run pytest tests/test_kr_discovery_source_adapter.py -v`.

---

## 3G3-4A Live-shaped fake-transport discovery snapshot fetcher (3G3-4A)

> **3G3-4A (implemented):** injected fake transport → validated immutable raw discovery snapshot compatible with 3G3-3 replay. Output is **raw discovery snapshot only** — not candidate pool, not universe, not trading. Real HTTP transport is **3G3-4B**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-4A** | `kr_discovery_live_client.py` + fake-transport tests | None |
| **3G3-4B** | `kr_discovery_http_client.py` + `ops/run_kr_discovery_live_smoke.py` | Operator HTTP only |
| **Next** | **3G3-5+** source-specific adapter, factor hardening | Deferred |

**Fetch path (test-only fake transport):**

```text
injected transport(records-only response)
→ build_live_discovery_snapshot_payload()  # explicit root fields only
→ temp write + load_kr_discovery_snapshot() validate
→ atomic rename to raw_<timestamp>_<sha8>.json
→ 3G3-3 replay (downstream; proven in tests)
```

**Rules:**

- No default transport; `transport=None` fails at `stage="args"`
- Request metadata never embedded in snapshot (no root `request`)
- Transport root fields are **not** splatted into snapshot — only `records` extracted
- Immutable: existing snapshot path → `FileExistsError`; no `--force` overwrite
- Validate-before-commit: invalid payload never reaches final snapshot path

Synthetic proof: `uv run pytest tests/test_kr_discovery_live_client.py -v`.

---

## 3G3-4B Operator-triggered HTTP discovery live smoke (3G3-4B)

> **3G3-4B (implemented):** operator-supplied `--endpoint-url` → isolated HTTP client → 3G3-4A immutable raw snapshot → optional 3G3-3 candidate pool replay. No env/API keys; no hardcoded KRX endpoint; endpoint URL not echoed in success output. Live factor scoring remains **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-4B** | `kr_discovery_http_client.py` + `ops/run_kr_discovery_live_smoke.py` | Operator HTTP GET only |
| **Next** | **3G3-5+** source-specific adapter, endpoint schema mapping, factor hardening | Deferred |

**Operator command:**

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/run_kr_discovery_live_smoke.py \
  --endpoint-url "https://operator-supplied.example/discovery.json" \
  --snapshot-dir "runtime/research/${DAY}/sources/kr_discovery" \
  --candidate-pool-out "/tmp/kr_discovery_candidate_pool.toml" \
  --pool-name "kr-discovery-live-candidate-pool-v1" \
  --pool-description "Operator-triggered KR discovery live smoke replay." \
  --fetched-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --market KR \
  --universe-hint operator-supplied-discovery \
  --external-service operator-http-discovery \
  --timeout-seconds 15 \
  --force \
  --json
```

**Rules:**

- HTTP fetch/parse errors surface **before** 3G3-4A transport (`fetch` / `parse` stages preserved)
- Raw snapshots are immutable (`--force` does not overwrite snapshots)
- `--force` applies to candidate pool replay output only
- Error paths sanitize URL query strings and credential-like tokens; avoid secrets in endpoint URLs
- Approved follow-up: snapshot/candidate pool → 3G3-2 ranked mapping → 3E2/3E3/3E4 → operator review

Synthetic proof: `uv run pytest tests/test_kr_discovery_http_client.py tests/test_kr_discovery_live_smoke_cli.py -v`.

---

## 3G3-5 Fixture-first KR discovery source schema mapper (3G3-5)

> **3G3-5 (implemented):** source-specific local fixture payload (`synthetic-provider-v1`) → canonical discovery transport payload → 3G3-4A immutable raw snapshot → optional 3G3-3 candidate pool replay. Output is **canonical raw discovery snapshot only** (optional candidate pool) — not universe, not trading. Live factor scoring remains **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-5** | `kr_discovery_schema_mapper.py` + `ops/map_kr_discovery_fixture.py` + synthetic provider payload fixture | None |
| **3G3-6** | `kr_discovery_source_payload_snapshot.py` + `ops/run_kr_discovery_source_live_smoke.py` | Operator HTTP GET only |
| **Next** | **3G3-6+** adapter hardening, factor hardening | Deferred |

**Mapper path:**

```text
source-specific local fixture payload (synthetic-provider-v1)
→ map_synthetic_provider_payload_to_transport_payload()  # {"records": [...]}
→ fetch_live_kr_discovery_snapshot() with constant transport (3G3-4A)
→ optional replay_kr_discovery_snapshot() (3G3-3)
→ 3G1 selector/export → 3G3-1 ranker → 3F1 generator → validation
```

**3G3-5 ops helper (local files only; not live endpoint integration):**

```bash
PYTHONPATH=src uv run python ops/map_kr_discovery_fixture.py \
  --source-payload tests/fixtures/research/kr_discovery/source_payload_synthetic_provider_v1.json \
  --snapshot-dir /tmp/kr_discovery_snapshots \
  --fetched-at 2026-05-30T00:00:00+09:00 \
  --as-of 2026-05-30T00:00:00+09:00 \
  --universe-hint synthetic-provider-v1 \
  --external-service synthetic-provider-fixture \
  --candidate-pool-out /tmp/kr_discovery_candidate_pool.toml \
  --pool-name kr-discovery-mapped-pool-v1 \
  --pool-description "Synthetic provider mapped KR discovery candidate pool." \
  --force \
  --json
```

**Rules:**

- Strict source schema: unknown root/item fields rejected; `corp_code` forbidden in source payload
- `sectorCode` / `industryLabel` map deterministically to canonical slugs; unknown codes fail at `stage="map"`
- `yfinance_provider_symbol` comes from explicit item `ticker` (not inferred)
- 3G3-4A boundary errors remapped to `KrDiscoverySchemaMappingError(stage="snapshot")`
- Raw snapshots immutable (`--force` does not overwrite snapshots); `--force` applies to candidate pool replay only
- No env/API key reads; no hardcoded KRX endpoint; no universe/provider mapping direct write by mapper CLI

Synthetic proof: `uv run pytest tests/test_kr_discovery_schema_mapper.py -v`.

---

## 3G3-6 Operator-triggered source-specific KR discovery live endpoint adapter (3G3-6)

> **3G3-6 (implemented):** operator-supplied HTTP endpoint → source-specific JSON (`synthetic-provider-v1`) → immutable raw source-payload snapshot → 3G3-5 mapper → 3G3-4A canonical discovery snapshot → optional 3G3-3 candidate pool replay. Endpoint is operator-supplied only — no hardcoded KRX URL, no env/API keys. Live factor scoring remains **deferred**.

| Phase | Scope | Network |
|---|---|---|
| **3G3-6** | `kr_discovery_source_payload_snapshot.py` + `ops/run_kr_discovery_source_live_smoke.py` + tests | Operator HTTP GET only |
| **Next** | **3G3-6+** adapter hardening, factor hardening | Deferred |

**Live source-specific path:**

```text
operator-supplied endpoint URL
→ fetch_kr_discovery_http_payload()  # 3G3-4B HTTP client
→ write_source_payload_snapshot()    # raw_source_<timestamp>_<sha8>.json
→ parse/map via 3G3-5 mapper
→ fetch_live_kr_discovery_snapshot() # 3G3-4A canonical raw_<timestamp>_<sha8>.json
→ optional replay_kr_discovery_snapshot()  # 3G3-3 candidate pool
→ operator review
```

**3G3-6 ops helper (operator-triggered; no env/API keys):**

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/run_kr_discovery_source_live_smoke.py \
  --endpoint-url "https://operator-supplied.example/synthetic-provider-v1.json" \
  --source-snapshot-dir "runtime/research/${DAY}/sources/kr_discovery_source_payload" \
  --canonical-snapshot-dir "runtime/research/${DAY}/sources/kr_discovery" \
  --candidate-pool-out "/tmp/kr_discovery_candidate_pool.toml" \
  --pool-name "kr-discovery-source-live-pool-v1" \
  --pool-description "Operator-triggered source-specific KR discovery live smoke replay." \
  --fetched-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --universe-hint synthetic-provider-v1-live-smoke \
  --external-service synthetic-provider-live-endpoint \
  --timeout-seconds 15 \
  --force \
  --json
```

**Rules:**

- HTTP `fetch`/`parse` stages preserved from 3G3-4B; mapper-local `parse`/`map` remapped to CLI `stage="map"`
- Source snapshot: `raw_source_` prefix; key-based forbidden-field validation (not value substring scan for broad trading words)
- Source/canonical snapshots immutable (`--force` does not overwrite); `--force` applies to candidate pool replay only
- Success/error JSON omits `endpoint_url`; HTTP errors sanitized via 3G3-4B client
- Approved follow-up: candidate pool → 3G3-2 ranked mapping → 3E2/3E3/3E4 → operator review

Synthetic proof: `uv run pytest tests/test_kr_discovery_source_live_smoke.py -v`.

---

## 3G4-0 — factor scoring guardrail checkpoint

**Status:** implemented (docs-only)

**Purpose:**
Define the safe boundary for factor scoring before 3G4-1 implementation.

Factor scoring is advisory research metadata only. It may produce normalized factor components and ranking-signal inputs, but it must not produce executable trading decisions.

**Allowed output:**

- `liquidity_score`
- `market_cap_score`
- `quality_score`
- `momentum_score`
- `risk_penalty`
- `score_version`
- `as_of`
- explanations / provenance
- reviewable JSON/TOML artifacts

**Forbidden output:**

- `action`
- `side`
- `buy`
- `sell`
- `hold`
- `target_weight`
- `target_allocation`
- `quantity`
- `order`
- `order_type`
- `price_target`
- `stop_loss`
- `take_profit`
- executable decision labels
- broker/PaperLoop/KIS command inputs

**Guardrails:**

**G4-1. Fixture-first only:**
The first factor scorer must use local fixtures only.
No live market/news/disclosure calls.
No env/API key reads.

**G4-2. Deterministic/versioned:**
All factor formulas must declare `factor_score_version`.
All component transforms must be deterministic.
Rounding precision must be fixed and tested.

**G4-3. Input provenance:**
Factor scorer inputs must come from explicit local artifacts:

- candidate pool fixture
- ranking-signal fixture
- source snapshots
- Date.md/store exports
- manually supplied local JSON/TOML

No hidden data fetches.

**G4-4. Output boundary:**
The scorer may produce ranking-signal-compatible artifacts.
It must not mutate checked-in universe/provider mapping config.
It must not directly call 3F/3G mapping generation unless explicitly in an orchestration test.

**G4-5. Operator review:**
Factor outputs are reviewable artifacts.
Operator approval remains required before any universe/provider mapping update.

**G4-6. No trading:**
Factor scoring must not call:

- broker
- KIS write paths
- PaperLoopRunner
- `submit_order`
- allocation/execution logic

**Phase split:**

- **3G4-1:** fixture-first factor signal generator
- **3G4-2:** factor scorer → 3G3-1 ranking signal TOML integration
- **3G4-3:** operator-local real factor input bundle — **implemented** (see [3G4-3](#3g4-3--operator-local-factor-input-bundle-workflow))
- **3G4-4:** source-specific factor adapter — **implemented** (see [3G4-4](#3g4-4--fixture-first-source-specific-factor-adapter))
- **3G4-5:** first operator-triggered live factor smoke — **implemented** (see [3G4-5](#3g4-5--operator-triggered-live-factor-source-smoke))
- ~~**3G4-H1:** factor intake hardening cleanup~~ — **implemented** (see [3G4-H1](#3g4-h1--factor-intake-hardening-cleanup))
- **3G4+ hardening:** calibration, provenance, drift checks, explainability — deferred

---

## 3G4-1 — fixture-first factor signal generator

**Status:** implemented

**Purpose:**
Convert explicit local factor input artifacts into 3G3-1-compatible ranking signal TOML. Advisory research metadata only — not live factor scoring, not trading instruction, not universe/provider mapping mutation.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3G4-1** | `kr_factor_signal_generator.py` + `ops/generate_kr_factor_signals.py` + tests | None |

**Fixture-first path:**

```text
local factor input TOML
→ generate_kr_factor_signals.py
→ ranking signal TOML (3G3-1 schema)
→ rank_kr_candidates
→ selected candidates
→ 3F1 generator (candidate pool yfinance + corp-code snapshot)
→ provider mapping validation
→ operator review
```

**Rules:**

- Output is ranking-signal-compatible TOML only (`liquidity_score`, `market_cap_score`, `quality_score`, `momentum_score`, `risk_penalty`, `score_version`, `as_of`, optional `notes`)
- No `action`/`buy`/`sell`/`hold`/order/allocation fields
- No `corp_code`, no `yfinance_provider_symbol`, no provider mapping/universe output
- Symbol normalization via existing `normalize_stock_code` (must match candidate pool lookup keys)
- Self-validation via existing `parse_ranking_signals_toml`
- Live factor scoring remains **deferred** (3G4-3+)

**3G4-1 ops helper (local files only; no env/API keys):**

```bash
PYTHONPATH=src uv run python ops/generate_kr_factor_signals.py \
  --factor-inputs tests/fixtures/research/kr_factors/kr_factor_inputs.synthetic.toml \
  --out-signals /tmp/kr_ranking_signals.generated.toml \
  --output-name kr-factor-signals-synthetic-v1 \
  --output-description "Synthetic fixture-first KR factor signals." \
  --force \
  --json
```

Synthetic proof: `uv run pytest tests/test_kr_factor_signal_generator.py -v`.

---

## 3G4-2 — factor signal → ranked mapping workflow integration

**Status:** implemented

**Purpose:**
Thin local orchestration helper connecting 3G4-1 factor signal generation with the existing 3G3-2 ranked mapping workflow. No new scoring/ranking/provider-mapping logic — reviewable artifacts only.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3G4-2** | `ops/build_kr_factor_ranked_mapping.py` + tests | None |

**Orchestration path:**

```text
factor input TOML
→ generate_kr_factor_signals_file()   # 3G4-1
→ run_build_kr_real_ranked_mapping()  # 3G3-2 (3G3-1 rank + 3F1 generate + validate)
→ factor signals TOML + ranked JSON + selected candidates + universe/mapping TOML
→ operator review
```

**Rules:**

- Scoring formula remains in **3G4-1**; ranking remains in **3G3-1/3G3-2**
- Output is reviewable artifacts only — no trading/action/allocation fields
- `--force` propagates to all five output paths
- Lower-level error stages preserved (`parse`, `generate`, `rank`, `resolve`, `write`, `validate`)
- Live factor scoring remains **deferred** (3G4-3+)

**3G4-2 ops helper (local files only; no env/API keys):**

```bash
PYTHONPATH=src uv run python ops/build_kr_factor_ranked_mapping.py \
  --candidate-pool tests/fixtures/research/kr_candidates/kr_sector_candidate_pool.synthetic.toml \
  --factor-inputs tests/fixtures/research/kr_factors/kr_factor_inputs.synthetic.toml \
  --corp-code-xml tests/fixtures/research/dart/corp_code_synthetic_multi.xml \
  --factor-signals-out /tmp/kr_factor_signals.generated.toml \
  --ranked-out /tmp/kr_candidates.factor_ranked.json \
  --selected-candidates-out /tmp/kr_candidates.factor_ranked.selected.toml \
  --universe-out /tmp/universe.kr-factor-ranked.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-factor-ranked.toml \
  --factor-output-name kr-factor-signals-synthetic-v1 \
  --factor-output-description "Synthetic fixture-first KR factor signals." \
  --selection-name kr-factor-ranked-selected-v1 \
  --selection-description "Factor-ranked KR candidates." \
  --universe-name kr-factor-ranked-universe-v1 \
  --provider-mapping-name kr-factor-ranked-provider-mappings-v1 \
  --top-n 3 \
  --force \
  --json
```

Synthetic proof: `uv run pytest tests/test_kr_factor_ranked_mapping_workflow.py -v`.

---

## 3G4-3 — operator-local factor input bundle workflow

**Status:** implemented

**Purpose:**
Thin operator-local bundle manifest wrapper over 3G4-2. Bundles candidate pool path, factor input TOML path, and local corp-code XML/ZIP snapshot path into one reviewable manifest. No new scoring/ranking/provider-mapping logic — reviewable artifacts only.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3G4-3** | `ops/build_kr_factor_bundle_mapping.py` + synthetic bundle fixture + tests | None |

**Orchestration path:**

```text
operator-local bundle manifest TOML
→ run_build_kr_factor_ranked_mapping()  # 3G4-2 (3G4-1 + 3G3-2)
→ factor signals TOML + ranked JSON + selected candidates + universe/mapping TOML
→ operator review
```

**Rules:**

- Reuses **3G4-2**; scoring formula remains in **3G4-1**; ranking remains in **3G3-1/3G3-2**
- Output is reviewable artifacts only — no trading/action/allocation fields
- `--out-dir` override recommended for operator runs (`/tmp/...` or `runtime/...`)
- Lower-level error stages preserved (`parse`, `generate`, `rank`, `resolve`, `write`, `validate`)
- No live factor scoring

**3G4-3 ops helper (local files only; no env/API keys):**

```bash
PYTHONPATH=src uv run python ops/build_kr_factor_bundle_mapping.py \
  --bundle tests/fixtures/research/kr_factors/kr_factor_bundle.synthetic.toml \
  --out-dir /tmp/kr_factor_bundle_outputs \
  --force \
  --json
```

Approved follow-up: generated universe/provider mapping → 3E2/3E3/3E4 smoke → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_bundle_workflow.py -v`.

---

## 3G4-4 — fixture-first source-specific factor adapter

**Status:** implemented

**Purpose:**
Convert provider-shaped local factor source payloads into canonical 3G4-1 factor input TOML. Advisory research metadata only — not live factor scoring, not trading instruction, not universe/provider mapping mutation.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3G4-4** | `kr_factor_source_adapter.py` + `ops/map_kr_factor_fixture.py` + tests | None |

**Fixture-first path:**

```text
source-specific factor payload JSON (fixture)
→ map_kr_factor_fixture.py
→ canonical factor input TOML (3G4-1 schema)
→ generate_kr_factor_signals.py (3G4-1)
→ build_kr_factor_ranked_mapping.py (3G4-2)
→ build_kr_factor_bundle_mapping.py (3G4-3, optional)
→ operator review
```

**Rules:**

- Output is canonical factor input TOML only (`version`, `name`, `description`, `as_of`, `factor_score_version`, `[[factors]]`)
- No `action`/`buy`/`sell`/`hold`/order/allocation fields
- No `corp_code`, no `yfinance_provider_symbol`, no provider mapping/universe output in adapter output
- Source-only fields (`displayName`, `sectorCode`, `lastUpdated`, `external_service`, `universe_hint`) accepted in source payload but not emitted
- Symbol normalization via existing `normalize_stock_code` (must match candidate pool lookup keys)
- Self-validation via existing `load_kr_factor_inputs_toml`; adapter remaps failures to `stage="validate"`
- Live factor transport: operator-triggered smoke only in **3G4-5** (not scheduled/automatic)

**3G4-4 ops helper (local files only; no env/API keys):**

```bash
PYTHONPATH=src uv run python ops/map_kr_factor_fixture.py \
  --source tests/fixtures/research/kr_factors/raw_kr_factor_source_synthetic_success.json \
  --factor-inputs-out /tmp/kr_factor_inputs.generated.toml \
  --output-name kr-factor-inputs-from-source-v1 \
  --output-description "Synthetic source-mapped KR factor inputs." \
  --factor-score-version kr-factor-fixture-v1 \
  --force \
  --json
```

Approved follow-up: mapped factor input TOML → 3G4-1 / 3G4-2 / 3G4-3 workflows → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_source_adapter.py -v`.

---

## 3G4-5 — operator-triggered live factor source smoke

**Status:** implemented

**Purpose:**
Operator-triggered live-shaped proof that a source-specific factor JSON endpoint can be fetched, snapshotted immutably, validated through the 3G4-4 parser, and optionally replayed into canonical 3G4-1 factor input TOML. Not scheduled fetch, not automatic scoring, not trading.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3G4-5 HTTP client** | `kr_factor_source_http_client.py` | urllib stdlib (module-isolated) |
| **3G4-5 snapshot** | `kr_factor_source_payload_snapshot.py` | None |
| **3G4-5 ops** | `ops/run_kr_factor_source_live_smoke.py` + tests | HTTP via injected client in tests only |

**Operator path:**

```text
operator-supplied endpoint URL
→ sanitized HTTP fetch (stdlib urllib, module-isolated)
→ JSON source payload object
→ immutable raw source payload snapshot (no wrapper envelope)
→ load_kr_factor_source_payload() validation (3G4-4)
→ optional replay_kr_factor_source_payload() → canonical factor input TOML only
→ operator follow-up: 3G4-1 / 3G4-2 / 3G4-3
```

**Rules:**

- Endpoint URL is **operator-supplied**; no hardcoded live endpoint; **no env/API key read**
- Raw snapshots are **immutable**; `--force` applies only to optional `--factor-inputs-out`
- Snapshot file is the **raw source payload itself** (not `{payload: ...}` wrapper; no `request`/endpoint/metadata envelope)
- Optional replay output is **canonical factor input TOML only** — no ranking signal / ranked JSON / universe / provider mapping
- Error/success JSON must not echo endpoint URL, query strings, or secrets
- Downstream ranking/mapping remains **operator follow-up** after review

**3G4-5 ops helper:**

```bash
PYTHONPATH=src uv run python ops/run_kr_factor_source_live_smoke.py \
  --endpoint-url "https://example.test/factor-source.json" \
  --snapshot-dir "runtime/research/2026-05-30/sources/kr_factor_source" \
  --fetched-at "2026-05-30T03:00:00+00:00" \
  --factor-inputs-out "/tmp/kr_factor_inputs.live_smoke.toml" \
  --output-name kr-factor-inputs-live-smoke-v1 \
  --output-description "Operator-triggered KR factor source live smoke." \
  --factor-score-version kr-factor-live-smoke-v1 \
  --force \
  --json
```

Synthetic proof: `uv run pytest tests/test_kr_factor_source_live_smoke.py -v`.

---

## 3G4-H1 — factor intake hardening cleanup

**Status:** implemented

**Purpose:**
Tighten error/snapshot contracts in 3G4-4/3G4-5 factor intake code without changing workflow semantics.

**Changes (no new business functionality):**

- **3G4-4 writer:** `write_kr_factor_inputs_toml()` now temp-write → 3G4-1 self-validate → atomic commit; invalid output never replaces an existing valid file.
- **3G4-5 snapshot:** unexpected write/rename failures sanitize to type-only messages (`factor source snapshot write failed: PermissionError`); explicit validation failures remain useful.
- **Programmatic args:** naive `fetched_at` in snapshot/live-smoke programmatic APIs normalize to stage-aware errors (`snapshot` / `args`), not bare `ValueError`.

Synthetic proof: `uv run pytest tests/test_kr_factor_source_adapter.py tests/test_kr_factor_source_live_smoke.py -v`.

---

## 3H0 — operator end-to-end intake guardrail checkpoint

**Status:** implemented (docs-only)

**Purpose:**
Define the approved operator-local end-to-end path from discovered/ranked/factor-scored KR candidates through generated universe/provider mapping, 3E combined research context, and Scout packet — **before** any new orchestration helper is added. This checkpoint is documentation and guardrails only; it introduces **no new ops command**.

**Scope:**

| In scope | Out of scope |
|---|---|
| Approved artifact flow across existing ops scripts | Automatic promotion of generated configs into `config/` |
| Operator review gate before config promotion | Live trading, broker, PaperLoop, KIS write path |
| Explicit 3E PRICE/DART/combined context smoke boundaries | Automatic universe mutation |
| 8B validate-only → 8B normal → 8C → 8D Scout packet | Scheduled/automatic fetch |
| Forbidden shortcuts list | Full orchestration (**3H1** is preflight/checklist only) |

This is **not** a workflow implementation, **not** live trading, **not** auto-promotion, **not** automatic universe mutation, and **not** broker/PaperLoop/KIS integration.

### Approved end-to-end artifact flow

All stages produce **reviewable local artifacts** under `/tmp` or `runtime/` first. Checked-in `config/universe*.toml` and `config/provider_mappings*.toml` are **never auto-mutated**.

```text
[A] Candidate source / discovery (reviewable sector-tagged candidate pool)
    fixture replay:     ops/replay_kr_discovery_snapshot.py
    live HTTP smoke:    ops/run_kr_discovery_live_smoke.py
    source live smoke:  ops/run_kr_discovery_source_live_smoke.py
    optional local map: ops/map_kr_discovery_fixture.py
    → sector-tagged candidate pool TOML (not universe/mapping)

[B] Factor source (immutable raw snapshot + optional canonical factor input TOML)
    fixture map:        ops/map_kr_factor_fixture.py
    live smoke:         ops/run_kr_factor_source_live_smoke.py
    → raw factor source snapshot (immutable)
    → optional canonical factor input TOML (3G4-1 schema)

[C] Ranking / generation (reviewable ranking + mapping artifacts)
    factor signals:     ops/generate_kr_factor_signals.py
    factor-ranked:      ops/build_kr_factor_ranked_mapping.py
    bundle workflow:    ops/build_kr_factor_bundle_mapping.py
    (also: ops/rank_kr_candidates.py, ops/build_kr_real_ranked_mapping.py,
           ops/select_kr_candidates.py, ops/generate_kr_provider_mapping.py)
    → ranking signal TOML / ranked JSON / selected candidate TOML
    → generated universe TOML + provider mapping TOML (operator-local)

[D] Provider mapping validation (required before 3E smoke)
    load_universe_toml()
    load_provider_mapping_toml()
    validate_provider_mappings_cover_universe(require_yfinance=True, require_dart=True)
    (CLI: ops/validate_provider_mapping.py)

[E] Research source intake (operator explicit per source)
    PRICE smoke:        ops/run_kr_real_price_smoke.py
                        (--universe / --provider-mapping = reviewed generated paths only)
    DART smoke:         ops/run_kr_real_dart_smoke.py
                        (--universe / --provider-mapping = reviewed generated paths only)
    FRED macro JSONL:   separate source-specific path (existing FRED replay/live-smoke)
    concat JSONL:       operator explicit (e.g. cat fred + price + dart → combined JSONL)
    combined context:   ops/build_kr_real_combined_context_smoke.py
                        (or manual 8B with --context-budget-profile kr-real-smoke)

[F] Context / Scout (no broker/write path)
    8B validate-only first
    8B normal with --context-budget-profile kr-real-smoke
    8C --require-symbol-coverage (PRICE supplies coverage)
    DART DISCLOSURE remains context-only (market=None)
    8D Scout packet — Scout consumes capped Date.md date_ids only

[G] Operator review gate
    all generated artifacts reviewable before any downstream step
    no automatic promotion into checked-in config
    no automatic trading decision
    no execution output (orders, fills, broker, PaperLoop, KIS)
```

**Stage rules (summary):**

| Stage | Allowed output | Must not |
|---|---|---|
| Discovery smokes | Raw snapshot + optional candidate pool TOML | Write universe/provider mapping directly |
| Factor live smoke | Immutable raw snapshot + optional factor input TOML | Write ranking/universe/mapping directly |
| Factor fixture map | Canonical factor input TOML only | Write universe/provider mapping |
| Ranking/generation | Reviewable signals, ranked JSON, selected candidates, generated TOML pair | Auto-commit into `config/` |
| 3E smokes | Staged JSONL + store + Date.md | Skip operator review of mapping paths |
| Scout | Capped Date.md + Scout packet | Forward to broker/PaperLoop/KIS |

### Forbidden shortcuts

| Shortcut | Why forbidden |
|---|---|
| Live response directly mutating checked-in config | Operator review gate required |
| Discovery/factor smoke directly writing universe/provider mapping | Approved path requires explicit generation + validation |
| Generated universe/provider mapping auto-committed into `config/` | Promotion is manual PR only after review |
| Ranking score interpreted as buy/sell/hold/action | Ranking is advisory metadata only ([3G3-0](#3g3-0-live-discoveryranking-guardrails-design-only), [3G4-0](#3g4-0--factor-scoring-guardrail-checkpoint)) |
| Factor score interpreted as allocation/target weight | Factor output is ranking-signal input only |
| Scout output forwarded to broker/PaperLoop/KIS | Intake terminates at Scout packet / 8I no-write boundary |
| Any stage emitting order quantity/order type/price target/stop loss/take profit | No trading instruction in intake path |
| Source endpoints or error JSON echoing secrets | Sanitized errors only; keys never in snapshots/logs |
| Runtime snapshots tracked in git | `runtime/` is gitignored; never commit |

### 3H1 — operator-local end-to-end manifest/preflight helper

**Status:** implemented

**Purpose:**
Thin preflight/checklist helper for the [3H0](#3h0--operator-end-to-end-intake-guardrail-checkpoint) documented path. Reads a local manifest TOML of **already-existing** artifact paths, validates existence/parse, validates universe/provider mapping coverage, and emits a reviewable summary JSON plus optional follow-up command plan. **Not** an orchestrator — does not fetch, smoke, mutate config, or trade.

**Scope:**

| Component | Path | Network |
|---|---|---|
| **3H1 ops** | `ops/preflight_kr_end_to_end_intake.py` + tests | None |

**Preflight path:**

```text
operator manifest TOML (artifact paths only)
→ load_kr_end_to_end_preflight_manifest()
→ required universe/provider_mapping exist + parse
→ validate_provider_mappings_cover_universe(require_yfinance=..., require_dart=...)
→ optional artifact checks (parse where loaders exist)
→ summary JSON (+ optional plan Markdown with review-only commands)
→ operator review
```

**Rules:**

- Validates **existing** artifacts only — no live fetch, no 3E smoke execution, no 8B/8C/Scout execution
- `require_symbol_coverage` is a manifest setting for follow-up **command plan** only — **not** passed to `validate_provider_mappings_cover_universe`
- `--force` applies only to `summary_out` / `plan_out` — never modifies input artifacts or checked-in config
- Follow-up commands reference **existing** ops scripts only; preflight does **not** execute them
- No env/API key read; no broker/write/execution path

**3H1 ops helper (local manifest only; no network/env):**

```bash
PYTHONPATH=src uv run python ops/preflight_kr_end_to_end_intake.py \
  --manifest tests/fixtures/research/kr_end_to_end/kr_end_to_end_preflight.synthetic.toml \
  --summary-out /tmp/kr_end_to_end_preflight_summary.json \
  --plan-out /tmp/kr_end_to_end_preflight_plan.md \
  --force \
  --json
```

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H2 — end-to-end preflight hardening cleanup

**Status:** implemented

**Purpose:**
Harden the [3H1](#3h1--operator-local-end-to-end-manifestpreflight-helper) preflight helper without expanding workflow semantics. Tightens write/error/plan contracts only.

**Hardening (no semantics change):**

| Area | Change |
|---|---|
| **Atomic writes** | `summary_out` / `plan_out` use same-directory temp file → atomic replace; existing file preserved on mid-write failure |
| **Error sanitization** | Known errors map to `KrEndToEndPreflightError(stage=...)`; write failures report exception type only (no raw path/secret leakage); CLI known errors emit JSON without traceback |
| **Command plan allowlist** | Generated follow-up commands validated against positive allowlist of existing ops scripts before return/write; comment lines (`# cat ...`) excluded |

**Rules preserved from 3H1:**

- No live fetch, smoke execution, config mutation, env/API key read, or trading
- `require_symbol_coverage` still not passed to `validate_provider_mappings_cover_universe`
- Summary JSON keys, CLI flags, manifest schema, and stage taxonomy unchanged

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H3 — structured follow-up plan JSON artifact

**Status:** implemented

**Purpose:**
Add optional structured follow-up plan JSON parallel to the existing Markdown plan so operators and future tooling can inspect follow-up steps without parsing Markdown.

**Behavior (review-only; no execution):**

| Area | Change |
|---|---|
| **Manifest** | Optional `[outputs].structured_plan_out` |
| **CLI** | `--structured-plan-out PATH` overrides manifest output path |
| **Internal steps** | Single `FollowupStep` representation drives Markdown command lines and structured JSON |
| **Structured JSON** | `version=1`, `mode=kr-end-to-end-intake-followup-plan`, `steps[]`, `forbidden_shortcuts`, `warnings` |
| **Summary JSON** | When written: `structured_plan_out`, `structured_plan_steps_count`, `structured_plan_generated` only (no inlined steps) |
| **Atomic writes** | Reuses existing `_write_output()` with `field_name=structured_plan_out`; `--force` applies |

**Rules preserved from 3H1/3H2:**

- No live fetch, smoke execution, config mutation, env/API key read, or trading
- Generated commands/steps are not executed by preflight
- Positive allowlist validation for executable step scripts; comment/manual steps have `script=null`

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H4 — structured follow-up plan validator

**Status:** implemented

**Purpose:**
Independent read-only validator for the 3H3 structured follow-up plan JSON artifact — schema lock, positive command allowlist drift guard, review-only flags, and handoff/audit safety only.

**Behavior (read-only; no execution):**

| Area | Behavior |
|---|---|
| **CLI** | `ops/validate_kr_end_to_end_preflight_plan.py --structured-plan PATH [--json]` |
| **Validation** | `version=1`, `mode=kr-end-to-end-intake-followup-plan`, `generated_by`, `review_only=true`, non-empty `steps[]`, canonical step-id subset/order, per-step allowlist + `script`/`command` consistency |
| **Safety** | Rejects endpoint URLs, env/API key references, trading/order/allocation **structured fields**, config-promotion commands, invented 3H0/3H1 commands; command-line safety uses exact unsafe execution token guard (not broad trading substring scan) |
| **Execution** | Does **not** execute plan commands, live fetches, smokes, or config mutation |

Optional but recommended before operator handoff or downstream tooling ingestion.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H5 — structured follow-up plan validator command-line safety hardening

**Status:** implemented

**Purpose:**
Refine 3H4 validator command-line safety without changing the public validator contract — preserve schema/allowlist/review-only checks while avoiding broad command substring false positives (e.g. harmless `reorder`, `transaction`, `threshold` arguments).

**Behavior (read-only; no execution change):**

| Area | Behavior |
|---|---|
| **Command-line safety** | Endpoint/env/invented/config-promotion rejection unchanged; broad trading substring scan removed from command strings |
| **Exact-token guard** | Boundary-aware unsafe execution token check for manual/comment steps outside positive allowlist |
| **Structured fields** | Trading/order/allocation forbidden keys still rejected via `_TRADING_FORBIDDEN_KEYS` / `_walk_forbidden_field_names` |
| **Execution** | Does **not** execute plan commands, live fetches, smokes, or config mutation |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H6 — structured plan validator optional validation report

**Status:** implemented

**Purpose:**
Optional compact validation report JSON artifact after successful structured plan validation — operator handoff/audit only; no command execution.

**Behavior (read-only; no execution):**

| Area | Behavior |
|---|---|
| **CLI** | `ops/validate_kr_end_to_end_preflight_plan.py --structured-plan PATH [--report-out PATH] [--force] [--json]` |
| **Report** | Written only after successful validation; atomic same-directory temp → replace; compact summary (counts, step IDs, allowlisted scripts) — no raw commands, artifact bodies, or timestamps |
| **Safety** | `--force` applies only to `--report-out`; validation failure never writes report; report is review/audit only |
| **Execution** | Does **not** execute plan commands, live fetches, smokes, or config mutation |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H7 — operator handoff manifest / artifact integrity index

**Status:** implemented

**Purpose:**
Optional compact handoff manifest JSON that indexes preflight/handoff artifact paths with sha256/size metadata — operator audit/handoff only; no command execution.

**Behavior (read-only; no execution):**

| Area | Behavior |
|---|---|
| **CLI** | `ops/build_kr_end_to_end_handoff_manifest.py --manifest-out PATH [--preflight-summary PATH] [--plan-md PATH] [--structured-plan PATH] [--validation-report PATH] [--force] [--json]` |
| **Manifest** | Indexes supplied artifacts only; records role/path/exists/size_bytes/sha256 and optional JSON mode/status/stage for known JSON artifacts; embeds no artifact bodies |
| **Safety** | `--force` applies only to `--manifest-out`; validate-before-write; temp write → existing 3H8 verifier validate-before-commit → atomic same-directory replace |
| **Execution** | Does **not** execute plan commands, live fetches, smokes, or config mutation |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H8 — operator handoff manifest integrity verifier

**Status:** implemented

**Purpose:**
Optional read-only verifier for 3H7 handoff manifest JSON — recomputes artifact size/sha256 and validates recorded JSON metadata against current artifacts; operator audit/handoff only; no command execution.

**Behavior (read-only; no execution):**

| Area | Behavior |
|---|---|
| **CLI** | `ops/verify_kr_end_to_end_handoff_manifest.py --manifest PATH [--json]` |
| **Verification** | Manifest exact-key schema lock (3H9); artifact existence/file checks; size/sha256 recompute; recorded-vs-actual JSON metadata compare |
| **Safety** | Writes no files; creates no temp files; does not mutate manifest or referenced artifacts |
| **Execution** | Does **not** execute plan commands, live fetches, smokes, or config mutation |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H9 — handoff manifest verifier schema hardening

**Status:** implemented

**Purpose:**
Exact-key schema lock for 3H8 handoff manifest verifier — unknown top-level manifest keys and unknown artifact entry keys are rejected at `validate` stage.

**Behavior (read-only; no execution):**

| Area | Behavior |
|---|---|
| **Schema lock** | Top-level manifest object must contain exactly the 10 expected keys; each artifact entry must contain exactly the 9 expected keys |
| **Unknown keys** | Rejected at `stage="validate"` — no broader JSON body scan of referenced artifacts |
| **Compatibility** | Manifests emitted by 3H7 builder pass unchanged |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H10 — handoff manifest verifier optional path containment

**Status:** Implemented (read-only; no execution).

Optional `--base-dir PATH` on `ops/verify_kr_end_to_end_handoff_manifest.py` enforces that the manifest file and every referenced artifact path resolve within an operator-supplied base directory. Uses canonical resolved paths only (`Path.resolve()` + `Path.is_relative_to()`); writes no files; does not execute commands.

| Area | Behavior |
|---|---|
| **CLI** | `ops/verify_kr_end_to_end_handoff_manifest.py --manifest PATH [--base-dir PATH] [--json]` |
| **Containment** | When `--base-dir` is supplied: manifest path checked before read; each artifact path checked after entry schema validation and before stat/read |
| **Success JSON** | Adds `base_dir` and `path_containment_verified=true` only when `--base-dir` is supplied |
| **Compatibility** | Omitting `--base-dir` preserves 3H8/3H9 behavior unchanged |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H11 — handoff manifest verifier optional verification report

**Status:** Implemented (audit artifact only; no execution).

Optional `--verification-report-out PATH` and `--force` on `ops/verify_kr_end_to_end_handoff_manifest.py` emit a compact verification report JSON **only after successful verification**. Report summarizes verification results (artifact roles, counts, flags) for operator handoff/archive/audit — excludes artifact bodies, manifest body, and command lines. Public `verify_kr_end_to_end_handoff_manifest(...)` API remains read-only; CLI uses `run_verify_kr_end_to_end_handoff_manifest(...)`.

| Area | Behavior |
|---|---|
| **CLI** | `ops/verify_kr_end_to_end_handoff_manifest.py --manifest PATH [--base-dir PATH] [--verification-report-out PATH] [--force] [--json]` |
| **Report mode** | `kr-end-to-end-handoff-manifest-verification-report` (distinct from CLI success mode and input manifest mode) |
| **Write ordering** | Report existence/force checked and written only after verification succeeds; validation failures remain `parse`/`validate` |
| **Compatibility** | Omitting `--verification-report-out` preserves 3H8/3H9/3H10 success JSON key set unchanged |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H12 — verification report output path containment

**Status:** Implemented (rejection hardening only; no new workflow behavior).

When `--base-dir` is supplied together with `--verification-report-out`, the report output path must resolve inside the same base directory (canonical resolved paths only). Containment is checked only after successful manifest/artifact verification and before report existence/force/write handling. Omitting either flag preserves prior 3H10/3H11 behavior.

| Area | Behavior |
|---|---|
| **CLI** | Same as 3H11; with `--base-dir`, point `--verification-report-out` inside the bundle directory |
| **Failure** | `verification_report_out path escapes base directory` at `validate` stage; no report file or parent directories created |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H13 — verification report schema self-validation

**Status:** Implemented (write hardening only; no new workflow behavior).

Before atomic write, the in-memory verification report payload built by `_build_verification_report(...)` is validated against an exact 16-key schema (unknown/missing keys rejected; value/type checks; `artifact_roles` reuses `_validate_artifact_roles`). Invalid report payloads never reach disk; parent directories are not created on self-validation failure. Public verifier API and CLI success JSON key sets remain unchanged.

| Area | Behavior |
|---|---|
| **Ordering** | Manifest/artifact verification → report build → report self-validation → exists/force check → atomic write |
| **Failure** | `validate` stage; no report body in error output |
| **Compatibility** | Omitting `--verification-report-out` unchanged; 3H11/3H12 report schema and containment rules unchanged |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H14 — handoff manifest builder validate-before-commit

**Status:** Implemented (write hardening only; no new workflow behavior).

Before atomic replace, the generated handoff manifest JSON is written to a same-directory temp file and validated with the existing 3H8 verifier (`verify_kr_end_to_end_handoff_manifest`). Invalid generated manifests never reach `--manifest-out`; temp files are cleaned up on validation or write failure. Builder success JSON key set remains unchanged; no verification report output on the builder (see **3H15** for optional `--base-dir`).

| Area | Behavior |
|---|---|
| **Ordering** | Artifact index → in-memory manifest → temp write → verifier validation → exists/force already checked → atomic replace |
| **Failure** | Verifier failures remap to builder `stage="validate"`; final output is not created or replaced |
| **Compatibility** | 3H7 manifest schema unchanged; 3H8 verifier remains the single validation authority |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H15 — handoff manifest builder optional path containment

**Status:** Implemented (producer-side rejection hardening only; no new workflow behavior).

Optional `--base-dir` on `ops/build_kr_end_to_end_handoff_manifest.py` enforces canonical resolved-path containment for every supplied artifact path and `--manifest-out` before indexing or write. When supplied, the existing 3H14 validate-before-commit path also calls the 3H8 verifier with the same resolved base directory. Builder manifest schema, API return key set, and CLI success payload key set remain unchanged.

| Area | Behavior |
|---|---|
| **Containment** | Supplied artifact paths and `manifest_out` must resolve inside `--base-dir`; failures map to `stage="validate"` before read/write |
| **Compatibility** | Omitted `--base-dir` preserves 3H14 behavior; 3H7 manifest schema unchanged; 3H8 verifier remains validation authority |
| **Safety** | No raw resolved paths in containment errors; no manifest/base_dir metadata added to manifest dict |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

### 3H16 — end-to-end handoff bundle round-trip smoke

**Status:** Implemented (fixture-only integration test coverage only; no new workflow behavior).

`tests/test_kr_end_to_end_preflight.py` exercises a no-exec API round-trip inside a temp `bundle_dir`: checked-in synthetic preflight manifest → preflight summary / plan markdown / structured plan → structured plan validation report (validator `main([...])` in-process) → handoff manifest builder with `--base-dir` → handoff manifest verifier with `--base-dir` and verification report output. Generated follow-up command strings are not executed; no subprocess; no network/env/API key read.

| Area | Behavior |
|---|---|
| **Scope** | Fixture-only; preflight input fixture may remain outside `bundle_dir`; all generated handoff artifacts must resolve inside `bundle_dir` |
| **Containment** | Builder/verifier `base_dir` rejection for outside artifact/report paths is covered in the same round-trip context |
| **Compatibility** | No new ops CLI; no production behavior change unless a real integration bug is found |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k handoff_bundle_round_trip -v`.

**Next step — 3H17 (implemented below)**

Detail cross-reference: [`docs/RUNBOOK.md`](RUNBOOK.md) § 3H1 preflight note.

---

### 3H17 — in-process CLI handoff bundle round-trip smoke

**Status:** Implemented (integration test coverage only; no new workflow behavior; no new ops CLI).

3H16 proved the **API** chain. 3H17 adds the complementary proof that the **operator-facing CLI argument wiring** works end-to-end. `tests/test_kr_end_to_end_preflight.py` runs the same chain through in-process `main([...])` calls only — `preflight_main` → `validate_plan_main` → `build_handoff_manifest_main` → `verify_handoff_manifest_main` — inside a temp `cli_bundle` directory. No subprocess, no `os.system`/`exec`/`eval`, no network/env/API key read.

| Area | Behavior |
|---|---|
| **CLI return convention** | Each `main(argv)` returns `0` on success; known domain errors are caught inside `main`, emitted to stderr / `--json` payload, and return `1` (never re-raised). Containment-rejection tests assert `rc == 1`, not `pytest.raises(...)` |
| **Flags exercised** | `--manifest/--summary-out/--plan-out/--structured-plan-out/--emit-followup-commands` (preflight), `--report-out` (validator), `--base-dir/--manifest-out` (builder), `--verification-report-out/--base-dir` (verifier); all with `--force --json` |
| **Output override** | Synthetic fixture has no `[outputs]` section, so all three preflight `--*-out` paths are passed explicitly inside the bundle — no fixture-default / repo / runtime writes |
| **Containment** | Builder rejects an outside input artifact and verifier rejects an outside `--verification-report-out`; both return `rc == 1` (error payload `stage == "validate"`) and create no outside parent directory |
| **Compatibility** | No new ops CLI; no production behavior change; manifest/report schema unchanged |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k cli_round_trip -v`.

### 3H18 — API/CLI handoff bundle parity smoke

**Status:** Implemented (integration test coverage only; no new workflow behavior; no new ops CLI).

3H16 (API) and 3H17 (CLI) each prove their chain independently but never compare outputs — so a future drift could make both paths "pass" while diverging in artifact roles, metadata modes, report flags, or containment semantics. 3H18 runs the **API** round-trip (`_run_handoff_bundle_round_trip`) and the **in-process CLI** round-trip (`_run_handoff_bundle_cli_round_trip`) in separate temp bundles (`api_bundle` / `cli_bundle`) and asserts the two are **semantically equivalent** for operator handoff purposes.

| Area | Behavior |
|---|---|
| **Normalization** | Each handoff manifest and verification report is reduced to a path/hash-independent semantic summary (top keys, version/mode/status/stage/generated_by, roles, kinds, entry-key sets, json mode/status/stage, flags) and the two summaries must be equal |
| **Excluded from comparison** | Absolute paths, sha256 **values**, and `base_dir` strings differ per bundle and are never compared across paths — only sha256 **shape** (`^[0-9a-f]{64}$`), `size_bytes > 0`, and `base_dir is not None` are compared |
| **Containment** | Asserted per bundle separately: every manifest artifact path and each report `base_dir` resolves inside its own bundle directory |
| **Body-free** | Both API and CLI manifest/report outputs carry no `steps`/`command(s)`/`followup_commands`/`content`/`body`, validated via `_walk_forbidden_fields` |
| **No-exec** | A parity test fails `subprocess.run` via monkeypatch and still completes both round-trips, calling only the two in-process helpers (never the older subprocess-based `_run_handoff_manifest_cli`) |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k api_cli_round_trip -v`.

### 3H19 — generated handoff bundle tamper-detection smoke

**Status:** Implemented (integration test coverage only; no new workflow behavior; no new ops CLI).

3H16/3H17 prove happy-path round-trips but do not prove that post-build artifact drift is caught. 3H19 builds a real handoff bundle via `_run_handoff_bundle_round_trip`, then mutates indexed artifacts or manifest entries and re-runs `verify_kr_end_to_end_handoff_manifest` / `run_verify_kr_end_to_end_handoff_manifest` with `base_dir`.

| Area | Behavior |
|---|---|
| **Integrity tamper** | Modified or deleted indexed artifacts fail at `validate` (size/sha256 mismatch or missing file) without updating manifest entries |
| **Malformed JSON** | Invalid JSON text with manifest `size_bytes`/`sha256` refreshed to match file fails at `parse` |
| **Semantic drift** | Structured plan / validation report JSON mutated with integrity refreshed but recorded `json_mode`/`json_status`/`json_stage` unchanged fails at `validate` |
| **Manifest tamper** | Wrong recorded `sha256`/`size_bytes` or artifact path outside `base_dir` fails at `validate` |
| **Report wrapper** | `run_verify_kr_end_to_end_handoff_manifest` does not write `--verification-report-out` on failure |
| **Body-free errors** | Verifier error messages do not echo tampered artifact body text |
| **No-exec** | Uses 3H16 in-process API helper only; no subprocess; no network/env |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k handoff_bundle_tamper -v`.

### 3H20 — CLI verifier tamper-rejection smoke

**Status:** Implemented (integration test coverage only; no new workflow behavior; no new ops CLI).

3H19 proves API-level tamper rejection via `pytest.raises` on `verify_kr_end_to_end_handoff_manifest`. 3H20 covers the operator-facing verifier CLI path: in-process `verify_handoff_manifest_main([... "--json"])` on 3H16-generated bundles after post-build tampering.

| Area | Behavior |
|---|---|
| **CLI error contract** | `rc == 1`; JSON stdout has `status == "error"`, exact `mode == "kr-end-to-end-handoff-manifest-verification"`, expected `stage`, non-empty `message`; no `pytest.raises` around CLI `main` |
| **Integrity tamper** | Modified/deleted indexed artifacts → `stage == "validate"` |
| **Malformed JSON** | Invalid JSON with manifest integrity refreshed → `stage == "parse"` |
| **Semantic drift** | Structured plan / validation report JSON mutated with integrity refreshed → `stage == "validate"` |
| **Manifest tamper** | Wrong recorded `sha256`/`size_bytes` or path outside `base_dir` → `stage == "validate"` |
| **Report wrapper** | `--verification-report-out` not written on CLI tamper failure |
| **Body-free / no traceback** | Error payload and stdout/stderr do not echo tampered artifact body or `Traceback` |
| **No-exec** | In-process CLI only; no subprocess; no network/env |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k tamper_cli -v`.

### 3H21 — handoff pipeline failure no-partial-output smoke (test coverage only)

3H21 proves the in-process handoff pipeline stays fail-closed when an upstream stage fails: downstream artifacts and parent directories are not created; `--force` does not destroy existing output when validation fails before write; CLI known errors return `rc == 1` with safe JSON payloads (no traceback, no raw artifact body echo).

| Area | Behavior |
|---|---|
| **Validator failure** | Invalid structured plan → no `validation_report.json`; `mode == "kr-end-to-end-preflight-plan-validation"`; `stage == "parse"` or `"validate"` |
| **Builder failure** | Invalid/mismatched validation report → no `handoff_manifest.json`; `mode == "kr-end-to-end-handoff-manifest-build"`; `stage == "validate"` |
| **Verifier failure** | Tampered indexed artifact → no verification report; `mode == "kr-end-to-end-handoff-manifest-verification"`; `stage == "validate"` |
| **Base-dir containment** | With `--base-dir`, outside `manifest_out` / `verification_report_out` parents are not created on failure |
| **Failed overwrite** | `--force` + existing validation report preserved when re-validation fails before write |
| **Body-free / no traceback** | Error JSON and stdout/stderr do not echo corrupted artifact bodies or `Traceback` |
| **No-exec** | In-process CLI `main([...])` only; no subprocess; no network/env |

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k handoff_pipeline -v`.

### 3H22 — normalized handoff bundle reproducibility smoke (test coverage only)

3H22 proves repeated API and in-process CLI handoff bundle round-trips with the same checked-in fixture produce **semantically stable** manifest and verification report contracts. Two separate temp bundles per path (`api_a`/`api_b`, `cli_a`/`cli_b`) are compared via 3H18 path/hash-independent normalization (role order, artifact kinds, JSON metadata modes/statuses/stages, schema key sets, verification flags, review-only/no-exec flags, sha256 shape, `size_bytes > 0`, `base_dir` presence only — never absolute paths, sha256 values, size_bytes values, or base_dir strings across bundles). Per-bundle containment and exact-key body-free contracts are re-asserted. No subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k reproducible -v`.

### 3H23 — CLI stdout success payload contract smoke (test coverage only)

3H23 locks down operator-facing `--json` success stdout for the four handoff CLIs on the no-exec fixture path: exact `mode`, `status == "ok"`, `stage == "complete"`, compact exact key sets per invocation, no embedded artifact bodies, no sensitive/trading fields, advisory-only `followup_commands` on preflight (not executed), and output path containment inside `tmp_path` bundle. In-process CLI `main([...])` only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k cli_success_payload -v`.

### 3H24 — CLI stdout known-error payload contract smoke (test coverage only)

3H24 locks down operator-facing `--json` known-domain-error stdout for the four handoff CLIs on representative failure inputs: exact `mode`, `status == "error"`, expected `stage`, exact four-key set `{status, stage, message, mode}`, non-empty safe `message`, no traceback, no raw artifact body echo, no partial downstream outputs when applicable, and no sensitive/trading field keys in the parsed payload. Preflight uses missing-manifest domain error (`stage == "parse"`) and intentionally excludes the conflicting-flags args path (which omits `mode`). In-process CLI `main([...])` only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k cli_error_payload -v`.

### 3H25 — CLI stdout JSON channel discipline smoke (test coverage only)

3H25 locks down operator-facing `--json` stdout channel discipline for the four handoff CLIs on both success and known-domain-error paths: exactly one JSON object on stdout (`json.JSONDecoder().raw_decode` consumes the whole stripped string; pretty-printed multi-line JSON allowed; no human prefix/suffix), no traceback on stdout or stderr, no JSON payload on stderr, and no generated command execution. Success path asserts expected output files exist; known-error path asserts blocked downstream outputs are not created. In-process CLI `main([...])` only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k json_channel -v`.

### 3H26 — CLI argument-domain failure no-output smoke (test coverage only)

3H26 locks down operator-facing `--json` argument-domain failures for validator/builder/verifier handoff CLIs (not argparse/SystemExit paths): `rc == 1`, exact four-key error payload `{status, stage, message, mode}`, expected `mode`/`stage` (`args` for stable blank paths, `validate` for missing/not-directory `base_dir`, `write` for output exists without `--force`), byte preservation when output pre-exists, no downstream output when blocked, no traceback, and no sentinel body echo. Blank `--base-dir` and validator `--report-out ""` are intentionally excluded. In-process CLI `main([...])` only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k arg_failure -v`.

### 3H27 — CLI help/usage side-effect smoke (test coverage only)

3H27 locks down operator-facing argparse help/usage paths for the four handoff CLIs (`preflight_main`, `validate_plan_main`, `build_handoff_manifest_main`, `verify_handoff_manifest_main`): `main(["--help"])` raises `SystemExit(0)` with human `usage:` text (not JSON); `main([])` raises non-zero `SystemExit` with usage error text; no traceback; stdout/stderr do not start with `{`; `tmp_path` remains empty (no output files, no output parent directories, no runtime artifacts); no generated command execution. Intentionally distinct from 3H24–3H26 (no `--json`, no domain-error payload contract). In-process CLI only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k "help_exits or usage_error or help_does_not_emit or usage_error_does_not_emit or help_usage_does_not_execute" -v`.

### 3H28 — CLI help/usage wording contract smoke (test coverage only)

3H28 extends 3H27 with minimal operator-facing discoverability contracts for the four handoff CLIs (`preflight_main`, `validate_plan_main`, `build_handoff_manifest_main`, `verify_handoff_manifest_main`): `--help` output includes operator-critical flag tokens (inputs, outputs, `--force`/`--json`, and `--base-dir`/`--report-out`/`--verification-report-out` where supported); `main([])` usage errors include `usage:` without traceback or JSON payloads; help and usage text omit forbidden compound operational tokens (runtime-constructed; no bare `kis` substring check). No full help snapshot; no exact `prog` or usage-line assertion; no `--json` on help/usage paths. In-process CLI only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k "help_lists_operator or usage_errors_include_usage or help_usage_omits_forbidden" -v`.

### 3H29 — CLI non-JSON human output smoke (test coverage only)

3H29 locks down operator-facing human CLI output when `--json` is omitted for the four handoff CLIs (`preflight_main`, `validate_plan_main`, `build_handoff_manifest_main`, `verify_handoff_manifest_main`): success returns `rc == 0` with short non-JSON stdout and expected artifact writes; known-domain errors return `rc == 1` with short non-JSON stderr (preflight missing-manifest domain path only; conflicting-flags path intentionally excluded); no traceback; no raw artifact/marker body echo; no generated command execution; output-exists without `--force` preserves existing bytes (validator representative). Intentionally distinct from 3H23–3H26 (`--json` payload contracts) and 3H27–3H28 (argparse help/usage). In-process CLI only; no subprocess; no new operator command; no command execution.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -k "handoff_cli_human" -v`.

**Next deferred step — 3H30+ (unimplemented)**

**3H30+** — end-to-end hardening (calibration, drift checks, richer provenance) remains deferred. Broker/PaperLoop/KIS integration remains out of scope for Real Research Source Intake.

---

## 11. Recommended first source

### Choice: **FRED (`FactType.MACRO`)**

**Rationale:**

1. **Lowest implementation friction** among real APIs: single-series smoke, documented REST, env-key only (`FRED_API_KEY` already named in `config/config.full.example`).
2. **No new pip dependency required** if v1 ops client uses stdlib HTTP + injected client (matches existing test pattern in `tests/test_fred_adapter.py`).
3. **Complete adapter → record mapping** already in `src/data/market_data.py`.
4. **Small blast radius:** one fetch → one snapshot → one `DateIdSourceRecord` for first live-smoke.
5. **Deterministic replay:** FRED observation JSON is stable and easy to fixture.
6. DART is deferred due to auth + corp-code + schema friction ([§10.3](#103-dart-style-disclosure)).
7. yfinance is **second** candidate: valuable for `PRICE` + universe coverage but adds third-party package / unofficial API surface risk.

**KR price note:** KIS-based KR `PRICE` remains **explicitly deferred** ([G3](#g3-data-source-boundary-vs-deferred-kis)). Generic PRICE replay (2A) and yfinance live-smoke (2B, unofficial provider) can satisfy universe symbol coverage; snapshot→2A replay boundary is mandatory for live path.

### Minimal fields to ingest (FRED v1)

| Field | Source |
|---|---|
| `series_id` | Config `series_ids[]` (start with one: e.g. `DGS10` — verify in FRED catalog at implementation) |
| `value` | Latest observation from API |
| `source_timestamp` | Observation date/end period from API (timezone-aware) |
| `units`, `frequency` | Optional payload metadata |

### Example `DateIdSourceRecord` (illustrative)

Uses existing `FactType.MACRO` — **do not invent new enum members**.

```json
{
  "date_id": "260529-1",
  "fact_type": "macro",
  "source_name": "fred",
  "source_timestamp": "2026-05-28T00:00:00+00:00",
  "created_at": "2026-05-29T09:00:00+09:00",
  "summary": "DGS10 US 10-Year Treasury yield 4.25% as of 2026-05-28",
  "payload": {
    "series_id": "DGS10",
    "value": "4.25",
    "units": "Percent"
  },
  "symbol": null,
  "market": null,
  "source_url": "https://fred.stlouisfed.org/series/DGS10"
}
```

(`date_id` assigned via `DateIdGenerator` or operator explicit id before 8B.)

### Required test fixtures (implementation PR)

| Fixture | Purpose |
|---|---|
| `tests/fixtures/research/fred/raw_dgs10_success.json` | Canonical raw FRED-like snapshot body |
| `tests/fixtures/research/fred/raw_dgs10_missing_value.json` | Normalization failure path |
| `FakeFredClient` (extend existing test pattern) | Adapter unit tests — **no live API** |
| `tests/fixtures/research/fred/expected_dgs10_record.json` | Golden `DateIdSourceRecord` after normalize |
| Snapshot replay test | `normalize_snapshot(fixture_path)` → equals golden record |

---

## 12. Testing strategy

| Layer | Approach |
|---|---|
| Adapter (existing) | Fake injected client; no network (`tests/test_fred_adapter.py` pattern) |
| Normalization | Golden snapshot → record; property: same snapshot → same payload hash |
| SourceFetcher ops (future) | Temp dir under pytest `tmp_path`; mock HTTP transport |
| 8B integration | Staged JSONL from fixtures → existing `test_research_source_intake` / smoke paths |
| Live-smoke | **Manual operator-only**; not in CI; optional local checklist |
| Scout downstream | Unchanged — Scout tests still use fixture `Date.md`/store, not fetchers |

**CI rule:** `pytest` never calls FRED/Yahoo/DART/KIS endpoints.

---

## 13. Operator runbook (v1 target workflow)

Prerequisites: Controlled Day 1 **PASS**; `./ops/acceptance_check.sh` green; `FRED_API_KEY` in local env (live-smoke only).

```bash
DAY=2026-05-29
mkdir -p "runtime/research/${DAY}/sources"

# 1) Plan (no network)
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --day "${DAY}" \
  --config runtime/research/research_sources.toml \
  --dry-run

# 2) Live-smoke (operator explicit; writes snapshot + stages JSONL)
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --day "${DAY}" \
  --config runtime/research/research_sources.toml \
  --live-smoke --source fred

# 3) Or replay prior snapshot (no network)
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --day "${DAY}" \
  --replay

# 4) Or replay generic PRICE snapshot for universe symbol coverage (no network)
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --replay --source price --symbol SYNTH-KR-0001 --market KR \
  --date-id 260530-1 --as-of 2026-05-30T09:00:00+09:00 \
  --snapshot tests/fixtures/research/price/raw_synth_kr_success.json \
  --out-jsonl "runtime/research/${DAY}/research_sources.price.jsonl"

# 5) Existing 8B (unchanged)
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.jsonl" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md"

# 6) Continue Controlled Day 1 chain from 8C … through 8I no-write
```

**Note:** `ops/fetch_research_sources.py` implements **1A replay/fixture staging** only. Live FRED HTTP (`--live-smoke`) remains **1B**.

---

## 14. Acceptance criteria

### This design task (docs-only)

- [ ] `docs/REAL_RESEARCH_SOURCE_INTAKE.md` merged with sections 1–15 and **G1–G4 explicit**
- [ ] `docs/PAPER_PILOT_WORKFLOW.md` and `docs/RUNBOOK.md` reference the design and next-stage boundary
- [ ] No changes to `src/`, `ops/`, `tests/`, pytest baseline, or acceptance checks
- [ ] No runtime files committed

### Future implementation v1 (separate PRs)

- [ ] FRED live-smoke writes immutable snapshot under `runtime/research/${DAY}/sources/fred/`
- [ ] Normalization produces valid `DateIdSourceRecord` JSONL storable by **unchanged** `ops/research_source_intake.py`
- [ ] Offline `--replay` reproduces same records from snapshot
- [ ] Unit tests use fixtures/mocks only; CI green
- [ ] Full chain still ends at **8I no-write** without write mode / KIS / broker
- [ ] Scout prompts still built from `Date.md` / store — no raw API in prompts
- [ ] Secrets not committed; env-var names documented

---

## 15. Deferred items

| Item | Notes |
|---|---|
| **KIS read-only KR `PRICE`** | Explicitly out of v1 ([G3](#g3-data-source-boundary-vs-deferred-kis)); see `docs/TECH_DEBT.md` |
| **yfinance live `PRICE` fetcher** | **2B implemented** — live-smoke only; writes generic PRICE snapshot then replays via 2A |
| **DART live API / corp-code / API key** | **3B0 documented** — live-smoke design + fixture-first plan in [§3B](#3b-dart-live-smoke-design); **3B1** fake transport + fixtures; **3B2** operator live-smoke; **3B3** hardening |
| **`FactType.NEWS` intake** | No Phase 6 news adapter in repo; Finnhub/Naver env names exist in `config.full.example` only |
| **`FactType.FLOW` / `FX`** | No adapter yet — new intermediate models + mapping PR required before fetcher |
| **New `FactType` enum members** | Not in v1; requires domain rule review |
| **Scheduling / launchd** | Manual operator trigger only |
| **Automatic 8B invoke after fetch** | Prefer explicit two-step (fetch → review JSONL → 8B) for v1 |
| **30-trading-day pilot start** | After repeatable intake + readiness decision |
| **Write-mode paper loop** | After pilot criteria met; not part of intake v1 |

---

## References

| Document / module | Role |
|---|---|
| `ops/research_source_intake.py` | 8B intake (unchanged target) |
| `src/domain/source.py` | `DateIdSourceRecord`, `FactType` |
| `src/data/date_id_store.py` | Canonical SQLite store |
| `src/data/date_id_generator.py` | `YYMMDD-N` assignment |
| `docs/RUNBOOK.md` | Controlled Day 1, ops entrypoints |
| `docs/PAPER_PILOT_WORKFLOW.md` | Daily folder conventions |
| `docs/TECH_DEBT.md` | KIS / pilot backlog |
