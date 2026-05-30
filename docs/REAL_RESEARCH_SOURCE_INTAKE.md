# Real Research Source Intake v1 — Design

> **Status:** 1A replay **implemented**; 1B FRED live-smoke **implemented** (urllib isolated in `fred_http_client.py`)  
> **Scope:** real external research data → existing Foundation **8B** intake path  
> **Not in scope:** Scout/Allocator/Analysis LLM agents, trading, broker, KIS, write mode

---

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
| `yfinance` | Yahoo Finance (yfinance library or equivalent read-only client) | `YFinancePriceAdapter` | `MarketDataPoint` → `market_data_point_to_source_record()` | `PRICE` | `symbol`, optional `market`; KR ticker conventions **must be verified at implementation** |
| `dart` | [DART Open API](https://opendart.fss.or.kr/) | `DartDisclosureAdapter` | `DisclosureRecord` → `disclosure_record_to_source_record()` | `DISCLOSURE` | One disclosure → one record; `source_url` when available |
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

**KR price note:** KIS-based KR `PRICE` remains **explicitly deferred** ([G3](#g3-data-source-boundary-vs-deferred-kis)). Until yfinance KR tickers or another non-KIS path is verified, operator may still add manual `PRICE` records for universe symbols.

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

# 4) Merge manual PRICE records if universe requires symbol coverage
#    (append to research_sources.jsonl)

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
| **yfinance `PRICE` fetcher** | Second source after FRED v1 proven; verify KR tickers + dependency policy |
| **DART `DISCLOSURE` fetcher** | Auth, corp-code map, rate limits, schema verification |
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
