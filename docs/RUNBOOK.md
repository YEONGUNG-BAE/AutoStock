# AutoStock Operations Runbook

Phase 0~16 구현 완료 이후, **자동매매가 아닌 수동·검증 중심 paper 운용**을 시작하기 위한 운영 절차서다.  
아직 구현되지 않은 entrypoint와 금지사항을 명확히 분리한다.

---

## 1. Current operating mode

현재 AutoStock의 상태는 **“자동매매 완성”이 아니다**.  
Phase 0~16까지 구현된 것은 **수동·검증 중심 paper 운용을 시작할 수 있는 MVP foundation**이다.

| 항목 | 현재 상태 |
|---|---|
| Live order submission | **구현되지 않음** — 실계좌 주문 경로 없음 |
| Scheduler / launchd | **구현되지 않음** — 정기 자동 실행 없음 |
| LLM orchestration entrypoint | **구현되지 않음** — Scout→Allocator→Analysis 일괄 실행 CLI 없음 |
| KIS 연동 | read-only / tiny-live dry-run **scaffold 수준** |
| Phase 16 recommendations | 사람이 검토할 **후보**이며 **자동 적용되지 않음** |
| Paper trading ledger | `PaperBrokerAdapter` + SQLite ledger — 장기 paper 성과의 source of truth |
| Risk / validation | Python 검증 계층 완비 — LLM 출력은 반드시 Python을 통과해야 함 |

**운용 원칙:** LLM은 판단만 한다. Python은 검증과 paper 실행만 한다. 라이브 주문은 명시적 후속 phase와 다중 게이트 없이는 절대 활성화하지 않는다.

---

## 2. Acceptance check

운용을 시작하기 전, regression gate를 통과해야 한다.

```bash
chmod +x ops/acceptance_check.sh
./ops/acceptance_check.sh
```

**기대 결과:**

- 11개 check 모두 `[PASS]`
- Summary: `11 PASS, 0 WARN, 0 FAIL`
- exit code `0`

**pytest gate:** acceptance check 내부 Check 1은 통과 수(pass count)가 아니라 **pytest exit code**로 판정한다(0=PASS, 그 외=FAIL). 실제 통과 수는 정보용으로만 출력되며 게이트 기준이 아니다 — 테스트 추가/삭제만으로는 WARN/FAIL이 발생하지 않고, 실제 실패(exit≠0)만 FAIL로 잡힌다.

**실패 시:** 다음 운용 단계(Ollama smoke, Date.md 갱신, PaperLoop one-shot 등)로 **진행하지 않는다**. FAIL 원인을 해결한 뒤 acceptance check를 재실행한다.

WARN은 exit code 1을 만들지 않는다. pytest는 합계 숫자가 아니라 exit code로 게이트되므로 baseline drift 자체가 게이트 신호가 아니다(실제 실패만 FAIL).

---

## 3. Ollama smoke procedure

### Smoke script

`ops/run_ollama_smoke.py` — 로컬 Ollama + `JsonRunner` JSON-only smoke (dummy schema only).

```bash
PYTHONPATH=src uv run python ops/run_ollama_smoke.py
```

옵션 override 예:

```bash
PYTHONPATH=src uv run python ops/run_ollama_smoke.py --host http://localhost:11434 --model qwen3.6:35b-mlx --verbose
```

Mac mini 기본 운용 모델은 `qwen3.6:35b-mlx`다. Fallback tested model: `qwen3.6:35b` (동일 smoke 5/5 PASS).

### 목적

1. Mac mini에서 Ollama server reachable 확인
2. `config.toml`에 설정된 model reachable 확인
3. `temperature=0` deterministic JSON call 확인
4. JSON parse / Pydantic validation 확인

### 절차

1. `./ops/acceptance_check.sh` PASS 확인.
2. Ollama 서버가 실행 중인지 확인한다 (`http://localhost:11434` 또는 config에 지정된 host).
3. 위 smoke script를 실행한다 (기본 config: `config/config.toml.example`).
4. smoke는 **투자 판단 schema가 아닌 script 내부 dummy schema**(`ok`, `message`, `number`)만 사용한다.
5. `JsonRunnerOptions.temperature`는 항상 `0`이다 — CLI override 없음 (`src/llm/json_runner.py`).
6. smoke 중에는 **broker / KIS / paper ledger를 호출하지 않는다**.
7. JSON parse 실패, markdown fence, Pydantic validation 실패 시 exit 1 — 원인 조사 후 재실행.

### 실패 시

Ollama smoke가 실패하면 **daily paper pilot으로 넘어가지 않는다**.

---

## 4. Date.md / Date-ID daily update

### Date-ID 5규칙 (필수)

1. **Date-ID source record**는 `SQLiteDateIdSourceStore`에 저장한다 (`src/data/date_id_store.py`).
2. **`Date.md`**는 사람과 LLM이 참조하는 **read-only export 문서**다.
3. LLM prompt에는 **`Date.md`에 존재하는 `date_id`만** 사용할 수 있다.
4. LLM output의 `reasons[].date_id`는 **Date-ID validator**로 검증한다.
5. **`Date.md`에 없는 `date_id`가 나오면 해당 LLM output은 폐기**한다 — 부분 채택하지 않는다.

### 추가 운영 규칙

- **Foundation 8B** (`ops/research_source_intake.py`)가 operator-prepared JSONL → `SQLiteDateIdSourceStore` → `Date.md` export를 제공한다.
- `Date.md`는 **read-only prompt reference**이며 store가 canonical이다.
- external API fetch / LLM / trading 호출 **없음**.

### Foundation 8B — research source intake (manual)

입력 convention: `runtime/research/YYYY-MM-DD/research_sources.jsonl`

```bash
# validate JSONL only (no store / no Date.md write)
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl runtime/research/YYYY-MM-DD/research_sources.jsonl \
  --validate-only \
  --json

# normal: JSONL → SQLite store → Date.md
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl runtime/research/YYYY-MM-DD/research_sources.jsonl \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --date-md-out runtime/research/YYYY-MM-DD/Date.md \
  --json

# export-only: existing store → Date.md
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --date-md-out runtime/research/YYYY-MM-DD/Date.exported.md \
  --export-only \
  --json
```

generated `runtime/research/` artifacts는 **commit하지 않는다**.

**Post-Foundation (1A replay + 1B live-smoke + 2A price replay + 2B yfinance live-smoke):** FRED snapshot replay는 `--replay --source fred`; generic PRICE snapshot replay는 `--replay --source price --symbol … --market …`; yfinance PRICE live-smoke는 `--live-smoke --source price --provider-symbol …` (비공식 외부 provider; immutable snapshot → 2A replay). live HTTP는 FRED `--live-smoke` (**stdlib HTTP는 `src/data/fred_http_client.py`에만 격리**). API key는 FRED env에서만 읽으며 stdout/stderr/JSON/snapshot에 **값이 기록되지 않는다**.

```bash
DAY=2026-05-29
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --replay \
  --source fred \
  --series-id DGS10 \
  --date-id 260529-1 \
  --as-of 2026-05-29T09:00:00+09:00 \
  --snapshot tests/fixtures/research/fred/raw_dgs10_success.json \
  --out-jsonl "runtime/research/${DAY}/research_sources.fred.jsonl" \
  --json

PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --replay \
  --source price \
  --symbol SYNTH-KR-0001 \
  --market KR \
  --date-id 260530-1 \
  --as-of 2026-05-30T09:00:00+09:00 \
  --snapshot tests/fixtures/research/price/raw_synth_kr_success.json \
  --out-jsonl "runtime/research/${DAY}/research_sources.price.jsonl" \
  --json

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.price.jsonl" \
  --validate-only \
  --json
```

PRICE replay → 8B normal → 8C smoke (enabled universe symbol coverage):

```bash
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.price.jsonl" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --json

PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --require-symbol-coverage \
  --json
# 기대: "missing_symbols": []
```

**yfinance PRICE live-smoke (operator-only; unofficial provider):**

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --live-smoke \
  --source price \
  --symbol SYNTH-KR-0001 \
  --market KR \
  --provider-symbol 005930.KS \
  --currency KRW \
  --date-id 260530-3 \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --snapshot-dir "runtime/research/${DAY}/sources/price" \
  --out-jsonl "runtime/research/${DAY}/research_sources.price.live.jsonl" \
  --json

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.price.live.jsonl" \
  --validate-only \
  --json
```

PRICE live-smoke → 8B normal → 8C smoke (symbol coverage):

```bash
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.price.live.jsonl" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --json

PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --require-symbol-coverage \
  --json
# 기대: "missing_symbols": []
```

**DART DISCLOSURE replay (fixture-only; no `--date-id`; uses `--store` for Date-ID allocation):**

운영 순서: **먼저** FRED/PRICE 등 prior staged JSONL에 대해 8B normal을 실행해 store에 Date-ID를 반영한 뒤, DART replay를 실행한다. DART fetch stage는 store에 기록하지 않으며, unstaged JSONL의 Date-ID는 allocation에 반영되지 않는다.

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --replay \
  --source dart \
  --symbol SYNTH-KR-0001 \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --as-of 2026-05-30T13:00:00+09:00 \
  --snapshot tests/fixtures/research/dart/raw_synth_dart_success.json \
  --out-jsonl "runtime/research/${DAY}/research_sources.dart.jsonl" \
  --json

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.dart.jsonl" \
  --validate-only \
  --json

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.dart.jsonl" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --json

PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --json
# DART-only: --require-symbol-coverage 사용 금지 (DISCLOSURE records have market=None)
```

DART-only Scout packet (8D): `require_symbol_coverage=False`이면 symbol-matched `DISCLOSURE`(`market=None`)가 Scout context에 포함되어 packet build가 성공할 수 있다. `--require-symbol-coverage`는 DART-only에서 여전히 실패한다.

**Post-Foundation (design):** broader real source intake 설계는 [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md).

### DART live-smoke planning boundary

- **3B2 implemented:** explicit operator `--live-smoke --source dart` only (no scheduler). stdlib HTTP isolated in `src/data/dart_http_client.py`.
- **3B1:** `dart_live_client.py` builds live-shaped snapshots from injected transport; CI uses fake `urlopen` only.
- Path: **live HTTP → immutable raw snapshot → 3A replay → 8B JSONL** (never direct `DateIdSourceRecord` from HTTP).
- API key: env var only (`DART_API_KEY` or `--api-key-env`); never commit keys or put them in snapshot/JSONL/Date.md/logs.
- `--corp-code` is OpenDART provider corp code (operator-supplied); internal universe `symbol` is separate.

**3C1 corp-code resolver (fixture/local XML only; no API key):**

```bash
PYTHONPATH=src uv run python ops/resolve_dart_corp_code.py \
  --corp-code-xml tests/fixtures/research/dart/corp_code_sample.xml \
  --stock-code 005930 \
  --json
```

**3C2 live corp-code master fetch (operator explicit; requires `DART_API_KEY`):**

```bash
export DART_API_KEY="..."
PYTHONPATH=src uv run python ops/resolve_dart_corp_code.py \
  --live-fetch \
  --api-key-env DART_API_KEY \
  --snapshot-dir "runtime/research/${DAY}/sources/dart_corp_code" \
  --stock-code 005930 \
  --json
```

Do **not** commit API keys or runtime snapshot ZIPs. Local XML/ZIP fallback (`--corp-code-xml` / `--corp-code-zip`) remains available without env vars.

**3D1 provider mapping registry (local TOML only; no API key):**

```bash
PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe config/universe.paper.toml.example \
  --provider-mapping config/provider_mappings.paper.toml.example \
  --json
```

Maps internal `(market, symbol)` to yfinance `provider_symbol` and DART `corp_code` via `config/provider_mappings.paper.toml.example`. No live API calls in this step.

**3E1 static KR real-company sample universe (local TOML only; no API key):**

```bash
PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe config/universe.kr-real.sample.toml \
  --provider-mapping config/provider_mappings.kr-real.sample.toml \
  --json
```

Static sample includes two locally verified KR companies (Samsung Electronics `005930`, SK hynix `000660`).

**3E2 KR real sample live PRICE smoke (operator explicit; yfinance only — no DART/FRED API key):**

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

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_price_260530.jsonl \
  --validate-only \
  --json
```

Optional 8C symbol coverage after 8B normal:

```bash
PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --require-symbol-coverage \
  --json
```

PRICE intake는 양수·currency·symbol/provider mapping 일치·source_timestamp·8B/8C round-trip을 검증한다. 종목별 hard-coded price magnitude band는 장기 시세 변동을 잘못 차단할 수 있으므로 두지 않는다.

**3E3 KR real sample live DART disclosure smoke (operator explicit; DART only — no yfinance/FRED):**

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

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_dart_260530.jsonl \
  --validate-only \
  --json
```

Optional 8B normal + 8C without symbol coverage (DART records have `market=None` — context only, not PRICE coverage):

```bash
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_dart_260530.jsonl \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --json

PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --json
```

Do **not** use `--require-symbol-coverage` on 8C for DART-only JSONL — disclosure records do not satisfy PRICE symbol coverage.

**3E4 KR real combined FRED + PRICE + DART context (operator explicit; context budget caps):**

Manual per-source intake (recommended):

```bash
DAY=2026-05-30
# 1) FRED macro JSONL (existing 1B live-smoke or replay)
# 2) 3E2 KR real PRICE smoke → /tmp/autostock_kr_real_price_260530.jsonl
# 3) 3E3 KR real DART smoke with --page-count 10 → /tmp/autostock_kr_real_dart_260530.jsonl

cat /tmp/autostock_fred_260530.jsonl \
  /tmp/autostock_kr_real_price_260530.jsonl \
  /tmp/autostock_kr_real_dart_260530.jsonl \
  > /tmp/autostock_kr_real_combined_260530.jsonl

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_combined_260530.jsonl \
  --validate-only \
  --json

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl /tmp/autostock_kr_real_combined_260530.jsonl \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --context-budget-profile kr-real-smoke \
  --force-date-md \
  --json

PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --require-symbol-coverage \
  --json

PYTHONPATH=src uv run python ops/build_scout_manual_packet.py \
  --universe config/universe.kr-real.sample.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/research/${DAY}/scout" \
  --market-scope KR \
  --require-symbol-coverage \
  --force \
  --json
```

Or use the orchestration helper after concat JSONL exists:

```bash
PYTHONPATH=src uv run python ops/build_kr_real_combined_context_smoke.py \
  --universe config/universe.kr-real.sample.toml \
  --source-jsonl /tmp/autostock_kr_real_combined_260530.jsonl \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --scout-out-dir "runtime/research/${DAY}/scout" \
  --context-budget-profile kr-real-smoke \
  --force-date-md \
  --force-scout \
  --json
```

Context budget profile `kr-real-smoke` caps Date.md export only (store unchanged): macro/global latest 5 per `(fact_type, source_name)`, PRICE latest 1 per `(market, symbol, source_name)`, DISCLOSURE latest 5 per `(symbol, source_name)`. Scout follows capped Date.md date_ids; 60KB guard remains active.

**3F1 KR universe/provider mapping generator (local files only; no live API/env/network):**

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

PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe /tmp/universe.kr-real.generated.toml \
  --provider-mapping /tmp/provider_mappings.kr-real.generated.toml \
  --json
```

Candidate TOML must not include `corp_code`; DART `corp_code`/`corp_name` come from local corp-code XML/ZIP via existing resolver. yfinance `provider_symbol` must be explicit (`.KS`/`.KQ`). Text fields reject ASCII control characters (codepoint `< 0x20` or `0x7F`) at parse/CLI/render before write. Sector discovery remains deferred.

**3F2 operator-local KR universe expansion (real 3–5 companies; not checked into repo):**

Checked-in fixtures verify only two real listed companies (`005930` / `000660` in `tests/fixtures/research/dart/corp_code_sample.xml`). Do **not** commit candidate TOML or generated mapping with guessed `corp_code` for Hyundai, NAVER, LG Chem, etc. Live corp-code master snapshot is a **runtime artifact** — never commit under `runtime/` or elsewhere.

1. **Obtain/refresh local corp-code master snapshot (3C2):**

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/resolve_dart_corp_code.py \
  --live-fetch \
  --api-key-env DART_API_KEY \
  --snapshot-dir "runtime/research/${DAY}/sources/dart_corp_code" \
  --stock-code 005930 \
  --json
```

   `--snapshot-dir`는 immutable ZIP snapshot이 기록될 **디렉터리**다(`--live-fetch`에 필수). `--stock-code`는 snapshot 생성과 함께 corp_code 1건을 resolve하기 위해 required다 — 생성된 ZIP snapshot 자체는 전체 corp-code master로 재사용된다. 명령 출력 JSON의 **`snapshot_path`** 값(생성된 ZIP 경로)을 확인해 step 3에 전달한다.

2. **Prepare operator-curated candidate TOML** (copy from `tests/fixtures/research/kr_candidates/kr_real_candidates.sample.toml` pattern): explicit `yfinance_provider_symbol`, `corp_name` for disambiguation, **no** `corp_code` field.

3. **Generate universe + provider mapping:**

```bash
# SNAPSHOT = step 1 출력 JSON의 snapshot_path 값 (파일명은 도구가 생성)
SNAPSHOT="<snapshot_path from step 1>"
PYTHONPATH=src uv run python ops/generate_kr_provider_mapping.py \
  --candidates /path/to/operator/kr_candidates.local.toml \
  --corp-code-zip "$SNAPSHOT" \
  --universe-out /tmp/universe.kr-real.local.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-real.local.toml \
  --universe-name kr-real-local-v1 \
  --provider-mapping-name kr-real-provider-mappings-local-v1 \
  --force \
  --json
```

4. **Validate generated files:**

```bash
PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe /tmp/universe.kr-real.local.toml \
  --provider-mapping /tmp/provider_mappings.kr-real.local.toml \
  --json
```

5. **Use generated files in 3E2/3E3/3E4 flows** (`--universe`, `--provider-mapping` on smoke scripts / combined context builder).

Generator N-scale proof (synthetic, checked-in): `uv run pytest tests/test_kr_real_generated_universe_expansion.py -v`. Sector discovery / automatic ranking remains **deferred** (3E5+).

**3G1 sector-tagged KR candidate pool (local files only; no live API/env/network):**

Selected candidate TOML **drops pool-only metadata** before 3F1 generator: root `base_market` and entry fields `sector`, `industry`, `eligible`, `priority`, `notes`, `corp_code` are excluded from export.

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

# Chain: selected candidates → 3F1 generator → validation → 3E smoke flows
PYTHONPATH=src uv run python ops/generate_kr_provider_mapping.py \
  --candidates /tmp/kr_candidates.selected.toml \
  --corp-code-xml tests/fixtures/research/dart/corp_code_synthetic_multi.xml \
  --universe-out /tmp/universe.kr-selected.generated.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-selected.generated.toml \
  --universe-name kr-selected-generated-v1 \
  --provider-mapping-name kr-selected-provider-mappings-v1 \
  --force \
  --json

PYTHONPATH=src uv run python ops/validate_provider_mapping.py \
  --universe /tmp/universe.kr-selected.generated.toml \
  --provider-mapping /tmp/provider_mappings.kr-selected.generated.toml \
  --json
```

Live sector discovery, ranking, and automatic universe expansion remain **deferred** (3E5+).

**3G2 operator-local sector pool → universe/mapping workflow (local files only; no live API/env/network in tests):**

Prerequisites: operator sector-tagged candidate pool TOML (explicit `yfinance_provider_symbol`; no `corp_code`) + local 3C2 corp-code master ZIP/XML snapshot (**runtime artifact; never commit**).

```bash
DAY=2026-05-30
# Step 1: 3C2 corp-code master snapshot (operator-local; output JSON snapshot_path → use in step 2)
SNAPSHOT="<snapshot_path from 3C2 --live-fetch>"

# Step 2–4: select → generate → validate (single helper)
PYTHONPATH=src uv run python ops/build_kr_real_sector_pool_mapping.py \
  --candidate-pool /path/to/operator/kr_sector_pool.local.toml \
  --corp-code-zip "$SNAPSHOT" \
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

# Step 5: use generated files in 3E2/3E3/3E4 flows
```

Missing corp-code **mode** (neither `--corp-code-xml` nor `--corp-code-zip`) fails at `stage="args"`. Missing corp-code **file** fails at `stage="resolve"`. Selected candidate TOML drops pool-only metadata (`base_market`, `sector`, `industry`, `eligible`, `priority`, `notes`) before 3F1 generator.

Synthetic workflow proof: `uv run pytest tests/test_kr_real_sector_pool_workflow.py -v`.

**3G3-0 operator note — live discovery/ranking not available yet:**

The **current approved real expansion path** remains operator-local only:

1. Obtain **3C2** corp-code master snapshot (runtime artifact; never commit).
2. Prepare operator **real sector pool TOML** (explicit `yfinance_provider_symbol`; no `corp_code`).
3. Run **`ops/build_kr_real_sector_pool_mapping.py`** (3G2 helper).
4. Validate generated universe/provider mapping.
5. Use generated files in **3E2/3E3/3E4** flows.

**Do not:**

- run live **discovery** commands — they **do not exist yet** (fixture ranking via `ops/rank_kr_candidates.py` is advisory metadata only)
- proceed directly from ranking/discovery output to trading or portfolio execution
- mutate checked-in `config/universe*.toml` or `config/provider_mappings*.toml` from discovery output

Future ranking output is **advisory metadata only** (see [3G3-0 guardrails](REAL_RESEARCH_SOURCE_INTAKE.md#3g3-0-live-discoveryranking-guardrails-design-only) in Real Research Source Intake design doc).

**3G3-1 fixture-first candidate ranking (local files only; not trading instruction):**

Ranked JSON is reviewable metadata only. Do **not** treat scores as buy/sell/hold signals or allocation guidance.

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

# Approved path: ranked artifact → selected candidate TOML → 3F1 → validation → 3E2/3E3/3E4 → operator review
PYTHONPATH=src uv run python ops/generate_kr_provider_mapping.py \
  --candidates /tmp/kr_candidates.ranked.selected.toml \
  --corp-code-xml tests/fixtures/research/dart/corp_code_synthetic_multi.xml \
  --universe-out /tmp/universe.kr-ranked.generated.toml \
  --provider-mapping-out /tmp/provider_mappings.kr-ranked.generated.toml \
  --universe-name kr-ranked-generated-v1 \
  --provider-mapping-name kr-ranked-provider-mappings-v1 \
  --force \
  --json
```

Live discovery transport and live factor scoring remain **deferred** (3G3-4B+). Synthetic proof: `uv run pytest tests/test_kr_candidate_ranker.py -v`.

**3G3-2 operator-local ranked mapping (local files only; not trading instruction):**

Operator prepares real sector pool TOML, local ranking signal TOML, and local corp-code snapshot. Ranked JSON is reviewable metadata only — do **not** treat scores as buy/sell/hold signals or allocation guidance.

```bash
PYTHONPATH=src uv run python ops/build_kr_real_ranked_mapping.py \
  --candidate-pool /path/to/operator/kr_sector_pool.local.toml \
  --ranking-signals /path/to/operator/kr_ranking_signals.local.toml \
  --corp-code-xml tests/fixtures/research/dart/corp_code_synthetic_multi.xml \
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

Approved path: real sector pool + ranking signals + corp-code snapshot → ranked JSON → selected candidate TOML → generated universe/provider mapping → validation → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_real_ranked_mapping_workflow.py -v`.

**3G3-3 discovery snapshot replay (local files only; candidate pool output only):**

Discovery replay produces a sector-tagged candidate pool TOML — **not** a generated universe. Live discovery commands **do not exist yet**.

```bash
PYTHONPATH=src uv run python ops/replay_kr_discovery_snapshot.py \
  --snapshot tests/fixtures/research/kr_discovery/raw_kr_discovery_synthetic_success.json \
  --candidate-pool-out /tmp/kr_discovery_candidate_pool.toml \
  --pool-name kr-discovery-synthetic-pool-v1 \
  --pool-description "Synthetic replayed KR discovery candidate pool." \
  --force \
  --json
```

Approved path: discovery snapshot → candidate pool → 3G1 selector → 3G3-1 ranker (local signals) → 3F1 generator (local corp-code) → validation → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_discovery_source_adapter.py -v`.

**3G3-4B operator-triggered HTTP discovery live smoke:**

Operator supplies `--endpoint-url` (no hardcoded KRX endpoint). This command reads **no env vars and no API keys**. Avoid putting secrets in endpoint URLs; error paths redact query strings wholesale, but operators should prefer secret-free URLs.

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

Approved follow-up: raw snapshot / candidate pool → 3G3-2 ranked mapping workflow → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_discovery_http_client.py tests/test_kr_discovery_live_smoke_cli.py -v`.

**3G3-5 fixture-first discovery schema mapper (local fixture mapping; not live endpoint integration):**

Maps a source-specific local fixture payload (`synthetic-provider-v1`) into the canonical discovery transport shape expected by 3G3-4A. This is **not** live discovery and **not** endpoint-specific live integration.

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

Approved path: source-specific fixture → canonical snapshot → candidate pool → 3G1 selector → 3G3-1 ranker (local signals) → 3F1 generator (local corp-code) → validation → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_discovery_schema_mapper.py -v`.

Live factor scoring and source-specific live adapter hardening remain **deferred** (3G3-6+).

**3G3-6 operator-triggered source-specific KR discovery live endpoint adapter:**

Operator supplies `--endpoint-url` returning source-specific JSON (`synthetic-provider-v1`). This command reads **no env vars and no API keys**. Avoid putting secrets in endpoint URLs; error paths redact query strings wholesale, but operators should prefer secret-free URLs.

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

Approved follow-up: candidate pool → 3G3-2 ranked mapping workflow → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_discovery_source_live_smoke.py -v`.

**3G4 factor scoring guardrails:**

3G4 factor scoring is not trading logic.

It may generate reviewable ranking-signal inputs, but it must not emit orders, action labels, target allocations, or broker/PaperLoop/KIS write inputs.

Approved high-level path:

```text
local fixture / operator-local factor bundle
→ factor signal generator
→ 3G3-1 ranking signal TOML
→ rank_kr_candidates
→ selected candidates
→ 3F1 generator
→ provider mapping validation
→ 3E2/3E3/3E4 smoke
→ operator review
```

Direct shortcuts are forbidden:

- factor scorer → checked-in config mutation
- factor scorer → broker/PaperLoop/KIS
- factor scorer → buy/sell/hold/order/allocation output

See [3G4-0 guardrails](REAL_RESEARCH_SOURCE_INTAKE.md#3g4-0--factor-scoring-guardrail-checkpoint) in Real Research Source Intake design doc.

**3G4-1 fixture-first factor signal generator (local files only; ranking-signal TOML output only):**

Local factor input TOML → 3G3-1-compatible ranking signal TOML. Not live factor scoring; not trading instruction.

```bash
PYTHONPATH=src uv run python ops/generate_kr_factor_signals.py \
  --factor-inputs tests/fixtures/research/kr_factors/kr_factor_inputs.synthetic.toml \
  --out-signals /tmp/kr_ranking_signals.generated.toml \
  --output-name kr-factor-signals-synthetic-v1 \
  --output-description "Synthetic fixture-first KR factor signals." \
  --force \
  --json
```

Approved follow-up: generated factor signals → 3G3-1 ranker → 3G3-2 ranked mapping → 3E2/3E3/3E4 → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_signal_generator.py -v`.

**3G4-2 factor-ranked mapping workflow (local orchestration only; reviewable artifacts only):**

Thin orchestration helper chaining 3G4-1 factor signal generation + 3G3-2 ranked mapping. Scoring formula remains in 3G4-1; ranking remains in 3G3-1/3G3-2. Not live factor scoring; not trading instruction. Use `/tmp` or `runtime/` output paths first — do not point at checked-in config samples unless explicitly intended.

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

Approved follow-up: generated universe/provider mapping → 3E2/3E3/3E4 smoke → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_ranked_mapping_workflow.py -v`.

**3G4-3 factor input bundle workflow (local manifest wrapper over 3G4-2; reviewable artifacts only):**

Operator-local bundle manifest TOML chains candidate pool + factor inputs + corp-code snapshot paths into 3G4-2. Scoring formula remains in 3G4-1; ranking remains in 3G3-1/3G3-2. Not live factor scoring; not trading instruction. Use `--out-dir /tmp/...` or `runtime/...` (recommended) — do not point at checked-in config samples unless explicitly intended.

```bash
PYTHONPATH=src uv run python ops/build_kr_factor_bundle_mapping.py \
  --bundle tests/fixtures/research/kr_factors/kr_factor_bundle.synthetic.toml \
  --out-dir /tmp/kr_factor_bundle_outputs \
  --force \
  --json
```

Approved follow-up: generated universe/provider mapping → 3E2/3E3/3E4 smoke → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_bundle_workflow.py -v`.

**3G4-4 fixture-first source-specific factor adapter (local files only; canonical factor input TOML output only):**

Provider-shaped local factor payload JSON → canonical 3G4-1 factor input TOML. Not live factor scoring; not live factor transport; not trading instruction.

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

Approved follow-up: mapped factor input TOML → 3G4-1 `generate_kr_factor_signals.py` → 3G4-2 `build_kr_factor_ranked_mapping.py` → 3G4-3 bundle workflow (optional) → operator review.

Synthetic proof: `uv run pytest tests/test_kr_factor_source_adapter.py -v`.

**3G4-5 operator-triggered KR factor source live smoke (HTTP → immutable raw snapshot → optional canonical factor input TOML only):**

Operator-supplied endpoint URL only — **no env/API key read**, no hardcoded live endpoint. Do **not** put secrets in the endpoint URL query string; prefer a server-side session or headerless public JSON path where possible.

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/run_kr_factor_source_live_smoke.py \
  --endpoint-url "https://example.test/factor-source.json" \
  --snapshot-dir "runtime/research/${DAY}/sources/kr_factor_source" \
  --fetched-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --factor-inputs-out "/tmp/kr_factor_inputs.live_smoke.toml" \
  --output-name kr-factor-inputs-live-smoke-v1 \
  --output-description "Operator-triggered KR factor source live smoke." \
  --factor-score-version kr-factor-live-smoke-v1 \
  --force \
  --json
```

- Raw snapshots under `runtime/research/.../sources/kr_factor_source` are **immutable** (`--force` never overwrites them).
- Omit `--factor-inputs-out` to fetch + snapshot only; add replay flags when ready to map through 3G4-4.
- Approved follow-up after operator review: generated factor input TOML → 3G4-1 `generate_kr_factor_signals.py` → 3G4-2 `build_kr_factor_ranked_mapping.py` → 3G4-3 bundle workflow (optional).

Synthetic proof: `uv run pytest tests/test_kr_factor_source_live_smoke.py -v`.

**3H0 operator note — end-to-end intake guardrail checkpoint (docs-only; no new command):**

**3H0** documents the approved operator-local path from KR candidate discovery/ranking/factor scoring through 3E combined research context and Scout packet. It is a **guardrail checkpoint only** — there is **no** `ops/run_3h0_*.py` command.

**3H1 preflight note — manifest/preflight helper (validates existing artifacts only; does not execute smokes):**

After discovery/factor/ranking workflows produce reviewable local artifacts, run preflight before optional 3E smokes. Preflight reads a manifest TOML, validates universe/provider mapping coverage, checks optional artifact paths, and writes a reviewable summary + optional follow-up command plan. **Follow-up commands are operator-run manually** — preflight does not execute them. **3H2 hardening:** summary/plan writes are atomic (same-directory temp → replace); follow-up command plan is positive-allowlist validated; no workflow semantics change. **3H3:** optional structured follow-up plan JSON (`--structured-plan-out`) for review/tooling only — not execution. **3H4:** optional structured plan validator (`ops/validate_kr_end_to_end_preflight_plan.py`) — read-only schema/allowlist audit before handoff; does not execute commands. **3H6:** optional validation report JSON (`--report-out`) — compact audit summary after successful validation; excludes raw commands and artifact bodies. **3H7:** optional handoff manifest JSON (`ops/build_kr_end_to_end_handoff_manifest.py`) — artifact path/sha256 integrity index only; no artifact bodies; no command execution.

```bash
PYTHONPATH=src uv run python ops/preflight_kr_end_to_end_intake.py \
  --manifest /path/to/operator/kr_end_to_end_preflight.local.toml \
  --summary-out /tmp/kr_end_to_end_preflight_summary.json \
  --plan-out /tmp/kr_end_to_end_preflight_plan.md \
  --structured-plan-out /tmp/kr_end_to_end_preflight_structured_plan.json \
  --force \
  --json
```

Use **`/tmp`** or **`runtime/`** for summary/plan outputs first. Do **not** commit generated universe/provider mapping until operator review completes. Synthetic fixture: `tests/fixtures/research/kr_end_to_end/kr_end_to_end_preflight.synthetic.toml`.

Synthetic proof: `uv run pytest tests/test_kr_end_to_end_preflight.py -v`.

**3H4 structured plan validator (optional; recommended before handoff/tooling):**

```bash
PYTHONPATH=src uv run python ops/validate_kr_end_to_end_preflight_plan.py \
  --structured-plan /tmp/kr_end_to_end_preflight_structured_plan.json \
  --report-out /tmp/kr_end_to_end_preflight_plan_validation_report.json \
  --force \
  --json
```

Read-only — validates schema, allowlist, review-only flags; does not execute generated commands. **3H5:** command-line safety avoids broad trading substring false positives; structured-field rejection and exact unsafe execution token guard preserved. **3H6:** optional `--report-out` writes compact validation report JSON after successful validation only; report excludes raw commands and artifact bodies.

**3H7 handoff manifest builder (optional; review-only artifact integrity index):**

```bash
PYTHONPATH=src uv run python ops/build_kr_end_to_end_handoff_manifest.py \
  --preflight-summary /tmp/kr_end_to_end_preflight_summary.json \
  --plan-md /tmp/kr_end_to_end_preflight_plan.md \
  --structured-plan /tmp/kr_end_to_end_preflight_structured_plan.json \
  --validation-report /tmp/kr_end_to_end_preflight_plan_validation_report.json \
  --manifest-out /tmp/kr_end_to_end_handoff_manifest.json \
  --force \
  --json
```

Optional — indexes supplied preflight/handoff artifact paths with sha256/size metadata only; embeds no artifact bodies; does not execute commands. **3H14:** generated manifest is verifier-validated (existing 3H8 path) before atomic commit to `--manifest-out`. **3H15:** optional `--base-dir` — when used, all supplied artifact paths and `--manifest-out` must resolve inside that base directory (recommended for operator-local handoff bundles).

**3H8 handoff manifest verifier (optional; recommended before handoff/archive):**

```bash
PYTHONPATH=src uv run python ops/verify_kr_end_to_end_handoff_manifest.py \
  --manifest /tmp/kr_end_to_end_handoff_manifest.json \
  --json
```

Read-only — recomputes artifact size/sha256 and verifies recorded JSON metadata; exact-key schema lock rejects unknown manifest/entry keys (3H9); optional `--base-dir` path containment (3H10); optional `--verification-report-out` compact audit report after successful verification (3H11); with `--base-dir`, report output must also resolve inside that base (3H12); report payload is schema-validated in memory before atomic write (3H13); default path writes no files; does not execute commands.

When validating a handoff bundle in a known directory, pass `--base-dir /tmp/...` so the manifest and every referenced artifact path must resolve within that base (recommended). When also writing a verification report, point `--verification-report-out` inside the same `--base-dir` bundle directory.

Optional verification report (written only after successful verification and in-memory schema self-validation; excludes artifact/manifest bodies):

```bash
PYTHONPATH=src uv run python ops/verify_kr_end_to_end_handoff_manifest.py \
  --manifest /tmp/kr_end_to_end_handoff_manifest.json \
  --base-dir /tmp/kr_handoff_bundle \
  --verification-report-out /tmp/kr_handoff_bundle/handoff_manifest_verification_report.json \
  --force \
  --json
```

```bash
PYTHONPATH=src uv run python ops/verify_kr_end_to_end_handoff_manifest.py \
  --manifest /tmp/kr_end_to_end_handoff_manifest.json \
  --base-dir /tmp \
  --json
```

**Approved high-level order of operations** (each step uses **existing** ops scripts; detail in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md` § 3H0](REAL_RESEARCH_SOURCE_INTAKE.md#3h0--operator-end-to-end-intake-guardrail-checkpoint)):

1. **Discovery side** — produce a reviewable sector-tagged candidate pool only (`ops/replay_kr_discovery_snapshot.py`, `ops/run_kr_discovery_live_smoke.py`, `ops/run_kr_discovery_source_live_smoke.py`, optional `ops/map_kr_discovery_fixture.py`). No direct universe/provider mapping mutation.
2. **Factor side** — immutable raw factor snapshot + optional canonical factor input TOML (`ops/run_kr_factor_source_live_smoke.py`, `ops/map_kr_factor_fixture.py`). No direct ranking/universe/mapping mutation from live factor smoke.
3. **Ranking/generation** — factor input TOML → `ops/generate_kr_factor_signals.py`; factor-ranked mapping → `ops/build_kr_factor_ranked_mapping.py`; bundle → `ops/build_kr_factor_bundle_mapping.py`. Generated universe/provider mapping TOML remains operator-reviewable; checked-in `config/universe*.toml` and `config/provider_mappings*.toml` are **not** auto-mutated.
4. **Validation** — generated mapping must pass `load_universe_toml`, `load_provider_mapping_toml`, and `validate_provider_mappings_cover_universe(require_yfinance=True, require_dart=True)` (CLI: `ops/validate_provider_mapping.py` or **3H1** `ops/preflight_kr_end_to_end_intake.py`).
5. **3E research intake** — operator **explicitly** runs PRICE smoke (`ops/run_kr_real_price_smoke.py`) and DART smoke (`ops/run_kr_real_dart_smoke.py`) using **reviewed** generated universe/mapping paths only when chosen; FRED macro JSONL stays separate; concatenate JSONL sources explicitly; combined context via `ops/build_kr_real_combined_context_smoke.py` with context budget cap.
6. **Context/Scout** — 8B validate-only first; 8B normal with `--context-budget-profile kr-real-smoke`; 8C `--require-symbol-coverage` relies on PRICE; DART disclosures remain context-only (`market=None`); Scout consumes capped Date.md date_ids. No broker/PaperLoop/KIS/write path.

**Operator reminders:**

- Write all generated artifacts to **`/tmp`** or **`runtime/`** first — never auto-promote into checked-in `config/`.
- Do **not** commit generated universe/provider mapping until operator review completes.
- PRICE, DART, and combined context smokes remain **separate explicit operator commands** — no automatic chaining in 3H0.
- Ranking/factor scores are **advisory metadata only** — not buy/sell/hold signals or allocation guidance.

**3H16 handoff bundle round-trip smoke (test coverage only):** `tests/test_kr_end_to_end_preflight.py` covers a fixture-only no-exec API round-trip inside a temp bundle directory (preflight → structured plan validation report → handoff manifest builder/verifier with `base_dir`). No new operator command is required.

**3H17 in-process CLI handoff bundle round-trip smoke (test coverage only):** the same test suite also covers the identical chain through in-process `main([...])` CLI calls (preflight/validator/builder/verifier argument wiring with `--base-dir` and explicit `--*-out` paths inside the bundle). The suite now covers both API and in-process CLI no-exec handoff bundle round-trips; CLI known-error paths assert `rc == 1`. No subprocess; no new operator command is required.

**3H18 API/CLI handoff bundle parity smoke (test coverage only):** the suite also runs the API and in-process CLI round-trips in separate temp bundles and asserts they are semantically equivalent (handoff manifest + verification report normalized to path/hash-independent summaries: roles/kinds/modes/flags/entry-key sets equal; only sha256 shape, `size_bytes > 0`, and `base_dir` presence compared — never absolute paths, sha256 values, or `base_dir` strings across bundles). Per-bundle containment, body-free, and no-exec are re-asserted. No new operator command is required.

**3H19 handoff bundle tamper-detection smoke (test coverage only):** the suite builds a real handoff bundle via the 3H16 API round-trip helper, then mutates indexed artifacts or manifest metadata and asserts `verify_kr_end_to_end_handoff_manifest` / `run_verify_kr_end_to_end_handoff_manifest` reject the bundle (integrity, parse, metadata, containment; verification report not written on failure; error messages body-free). No new operator command is required.

**3H20 CLI verifier tamper-rejection smoke (test coverage only):** the suite also tampered 3H16-generated bundles and asserts in-process `verify_handoff_manifest_main([... "--json"])` rejects them safely (`rc == 1`; JSON error payload with exact `mode == "kr-end-to-end-handoff-manifest-verification"`, expected `stage`, non-empty `message`; no traceback; no raw artifact body echo; verification report not written on failure). No subprocess; no new operator command is required.

**3H21 handoff pipeline failure no-partial-output smoke (test coverage only):** the suite asserts upstream CLI stage failures do not leave partial downstream outputs (validation report, handoff manifest, verification report, or outside-base parent directories); `--force` preserves existing validation report bytes when re-validation fails before write; known errors use `rc == 1` with safe JSON (`status == "error"`, exact stage/mode, no traceback, no raw artifact body echo). In-process CLI only; no subprocess; no new operator command is required.

**3H22 normalized handoff bundle reproducibility smoke (test coverage only):** the suite runs repeated API and in-process CLI handoff bundle round-trips in separate temp bundles and asserts semantically equivalent normalized manifest and verification report contracts (role order, artifact kinds, JSON metadata modes/statuses/stages, schema key sets, verification flags, review-only/no-exec flags; sha256 shape, `size_bytes > 0`, and `base_dir` presence only — never absolute paths, sha256 values, size_bytes values, or base_dir strings across bundles). Per-bundle containment, exact-key body-free, and no-exec are re-asserted. No subprocess; no new operator command is required.

**3H23 CLI stdout success payload contract smoke (test coverage only):** the suite runs the four handoff CLIs in-process with `--json` on the fixture no-exec path and asserts compact/safe success payloads (exact `mode`, `status == "ok"`, `stage == "complete"`, exact key sets per invocation, no embedded artifact bodies, no sensitive/trading fields, advisory-only preflight `followup_commands`, output path containment). No subprocess; no new operator command is required.

**3H24 CLI stdout known-error payload contract smoke (test coverage only):** the suite runs the four handoff CLIs in-process with `--json` on representative known-domain-error inputs and asserts compact/safe error payloads (`status == "error"`, exact `mode`, expected `stage`, exact four-key set `{status, stage, message, mode}`, non-empty safe `message`, no traceback, no raw artifact body echo, no partial downstream outputs, no sensitive/trading field keys). Preflight uses missing-manifest domain error (not conflicting-flags/argparse paths). No subprocess; no new operator command is required.

**3H25 CLI stdout JSON channel discipline smoke (test coverage only):** the suite runs the four handoff CLIs in-process with `--json` on success and known-domain-error paths and asserts clean machine-readable stdout (exactly one JSON object via `raw_decode` on stripped stdout; no human prefix/suffix; pretty-printed multi-line JSON allowed; no traceback; no JSON payload on stderr; success outputs exist; known-error blocked outputs not created; no generated command execution). No subprocess; no new operator command is required.

**3H26 CLI argument-domain failure no-output smoke (test coverage only):** the suite runs validator/builder/verifier handoff CLIs in-process with `--json` on representative argument-domain failures (output exists without `--force` at `write` stage with byte preservation; missing/not-directory `base_dir` at `validate` stage with no downstream output; stable blank path args at `args` stage) and asserts `rc == 1`, exact four-key error payload `{status, stage, message, mode}`, expected `mode`/`stage`, no traceback, no sentinel body echo, and no partial output creation/overwrite. Blank `--base-dir` and validator `--report-out ""` are intentionally excluded (CLI `Path("")` maps to cwd; empty report_out may be treated as omitted). No subprocess; no new operator command is required.

**3H27 CLI help/usage side-effect smoke (test coverage only):** the suite runs the four handoff CLIs in-process on argparse paths only (`main(["--help"])` → `SystemExit(0)`; `main([])` → non-zero `SystemExit`) and asserts human `usage:` text, no traceback, stdout/stderr are not JSON payloads (no `--json`), no files or parent directories created under `tmp_path`, and no generated command execution. Distinct from 3H24–3H26 domain-error contracts. No subprocess; no new operator command is required.

**3H28 CLI help/usage wording contract smoke (test coverage only):** the suite asserts handoff CLI `--help` exposes operator-critical flag tokens (manifest/inputs, outputs, `--force`/`--json`, and base-dir/report flags where supported), `main([])` usage errors reference `usage:` without traceback/JSON, and help/usage text omits forbidden compound operational tokens. No full argparse snapshot; no new operator command is required.

**3H29 CLI non-JSON human output smoke (test coverage only):** the suite runs the four handoff CLIs in-process **without** `--json` on success and representative known-domain-error paths and asserts safe human output (`rc == 0`/`1`; stdout/stderr are not JSON payloads; no traceback; no raw artifact/marker echo; success writes expected outputs; known errors do not create/overwrite blocked outputs; validator output-exists preserves bytes; no generated command execution). Preflight conflicting-flags path intentionally excluded. Distinct from 3H23–3H28. No subprocess; no new operator command is required.

**Deferred next step:** **3H30+** end-to-end hardening (unimplemented).

**3G3-4A live-shaped fake-transport fetcher (test-only; raw snapshot output only):**

`kr_discovery_live_client.fetch_live_kr_discovery_snapshot()` accepts injected fake transport only — used in tests to prove transport → immutable raw snapshot → 3G3-3 replay chain.

Synthetic proof: `uv run pytest tests/test_kr_discovery_live_client.py -v`.

```bash
DAY=2026-05-30
PYTHONPATH=src uv run python ops/fetch_research_sources.py \
  --live-smoke \
  --source dart \
  --symbol SYNTH-KR-0001 \
  --corp-code <OPERATOR_SUPPLIED_CORP_CODE> \
  --bgn-de 20260530 \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --as-of 2026-05-30T13:00:00+09:00 \
  --snapshot-dir "runtime/research/${DAY}/sources/dart" \
  --out-jsonl "/tmp/autostock_dart_live_${DAY}.jsonl" \
  --force \
  --json
# --api-key-env 생략 시 DART_API_KEY env 사용 (FRED parser default와 분리)

PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "/tmp/autostock_dart_live_${DAY}.jsonl" \
  --validate-only \
  --json
```

- DART-only: do **not** use `--require-symbol-coverage` on 8C without PRICE coverage records.
- Combined FRED+PRICE+DART smoke (8B/8C/8D) is **verified** with replay/fixture paths; live DART remains a separate follow-on.
- Detail: [`docs/REAL_RESEARCH_SOURCE_INTAKE.md` §3B](REAL_RESEARCH_SOURCE_INTAKE.md#3b-dart-live-smoke-design).

### Foundation 8C — Universe v0 + Date.md prompt-reference smoke

로컬 universe 파일 준비:

```bash
cp config/universe.paper.toml.example runtime/paper/universe.paper.toml
```

8B로 Date.md/store 생성 후 8C smoke:

```bash
PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --require-symbol-coverage \
  --json
```

external API / LLM / trading 호출 **없음**. smoke script는 output file을 생성하지 않는다.

### Foundation 8D — Scout Once manual LLM call packet

8B intake + 8C smoke 후 Scout manual packet 생성:

```bash
PYTHONPATH=src uv run python ops/build_scout_manual_packet.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/scout \
  --require-symbol-coverage \
  --market-scope KR \
  --max-records 20 \
  --json
```

DART `DISCLOSURE`만 있는 경우: `--require-symbol-coverage` 없이 실행하면 symbol-matched disclosure가 Scout context에 포함된다 (`market=None` 유지). DART-only + `--require-symbol-coverage`는 8C와 동일하게 실패한다.

생성 파일: `scout_input.json`, `scout_prompt.md`, `scout_packet_summary.json`.

운영자는 `scout_prompt.md`를 LLM/Ollama UI에 **수동 paste**하고, raw JSON을 suggested path에 **수동 저장**한다.
automatic LLM call / raw Scout validation / trading **없음**.

### Foundation 8E — Scout raw JSON intake validator

8D packet + operator manual raw JSON 저장 후 ScoutSummary validation:

```bash
PYTHONPATH=src uv run python ops/validate_scout_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/scout/scout_output.kr.raw.json \
  --scout-input runtime/paper/YYYY-MM-DD/scout/scout_input.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/scout \
  --json
```

생성 파일: `scout_output.validated.json`, `scout_validation.txt`, `scout_validation_summary.json`.

raw JSON은 **단일 JSON object만** 허용한다. markdown fence, fence 밖 prose, array/string root는 거부한다.
`ScoutSummary.created_at`은 timezone-aware datetime이면 충분하며, 8E는 `ScoutInput.created_at` 대비 freshness ordering을 검사하지 않는다.
automatic LLM call / Allocator / Analysis validation / trading **없음**.

### Foundation 8F — Portfolio state snapshot + Allocator Once

portfolio state 준비 + validated ScoutSummary 기반 Allocator manual packet:

```bash
cp docs/examples/portfolio_state.paper.example.json runtime/paper/YYYY-MM-DD/portfolio/portfolio_state.json
PYTHONPATH=src uv run python ops/build_allocator_manual_packet.py \
  --validated-scout runtime/paper/YYYY-MM-DD/scout/scout_output.validated.json \
  --scout-validation-summary runtime/paper/YYYY-MM-DD/scout/scout_validation_summary.json \
  --portfolio-state runtime/paper/YYYY-MM-DD/portfolio/portfolio_state.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --universe runtime/paper/universe.paper.toml \
  --out-dir runtime/paper/YYYY-MM-DD/allocator \
  --json
```

운영자는 `allocator_prompt.md`를 LLM/Ollama UI에 **수동 paste**하고 raw JSON을 `allocator_output.raw.json`에 **수동 저장**한 뒤 validator 실행:

```bash
PYTHONPATH=src uv run python ops/validate_allocator_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/allocator/allocator_output.raw.json \
  --allocator-input runtime/paper/YYYY-MM-DD/allocator/allocator_input.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/allocator \
  --json
```

생성 파일: `allocator_output.validated.json`, `allocator_validation.txt`, `allocator_validation_summary.json`.

raw allocator JSON은 **단일 JSON object만** 허용한다. markdown fence, fence 밖 prose, array/string root는 거부한다.
`AllocatorDecision.created_at`은 timezone-aware datetime이면 충분하며, 8F는 ScoutSummary/portfolio snapshot `as_of` 대비 freshness ordering을 검사하지 않는다.
automatic LLM call / Analysis / PaperLoopInput assembly / trading **없음**.

### Foundation 8G — Analysis Once (per-symbol)

validated ScoutSummary + validated AllocatorDecision + portfolio state 기반 Analysis manual packet (symbol/market 1건):

```bash
PYTHONPATH=src uv run python ops/build_analysis_manual_packet.py \
  --validated-scout runtime/paper/YYYY-MM-DD/scout/scout_output.validated.json \
  --validated-allocator runtime/paper/YYYY-MM-DD/allocator/allocator_output.validated.json \
  --allocator-validation-summary runtime/paper/YYYY-MM-DD/allocator/allocator_validation_summary.json \
  --portfolio-state runtime/paper/YYYY-MM-DD/portfolio/portfolio_state.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --universe runtime/paper/universe.paper.toml \
  --market KR \
  --symbol SYNTH-KR-0001 \
  --out-dir runtime/paper/YYYY-MM-DD/analysis \
  --json
```

운영자는 `analysis_prompt.<market>.<symbol>.md`를 LLM/Ollama UI에 **수동 paste**하고 raw JSON을 `analysis_output.<market>.<symbol>.raw.json`에 **수동 저장**한 뒤 validator 실행:

```bash
PYTHONPATH=src uv run python ops/validate_analysis_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/analysis/analysis_output.kr.SYNTH-KR-0001.raw.json \
  --analysis-input runtime/paper/YYYY-MM-DD/analysis/analysis_input.kr.SYNTH-KR-0001.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/analysis \
  --json
```

생성 파일: `analysis_output.<market>.<symbol>.validated.json`, `analysis_validation.<market>.<symbol>.txt`, `analysis_validation_summary.<market>.<symbol>.json`.

raw analysis JSON은 **단일 JSON object만** 허용한다. markdown fence, fence 밖 prose, array/string root는 거부한다.
`AnalysisDecision.created_at`은 timezone-aware datetime이면 충분하며, 8G는 ScoutSummary/AllocatorDecision/portfolio `as_of` 대비 freshness ordering을 검사하지 않는다.
per-symbol allocator tolerance(`--allocator-target-weight-percent`, `--tolerance-percent`)는 **선택**이며, AllocatorDecision aggregate `target_weights`에서 per-symbol weight를 **추론하지 않는다**.
automatic LLM call / PaperLoopInput assembly / trading **없음**.

### Foundation 8H — Production PaperLoopInput Assembler (per-symbol)

validated Layer A + local paper-only context로 PaperLoopInput 조립 (symbol/market 1건, **실행 없음**):

```bash
mkdir -p runtime/paper/YYYY-MM-DD/paper_loop
cp docs/examples/paper_loop_context.paper.example.json \
  runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_context.json
PYTHONPATH=src uv run python ops/assemble_paper_loop_input.py \
  --validated-scout runtime/paper/YYYY-MM-DD/scout/scout_output.validated.json \
  --validated-allocator runtime/paper/YYYY-MM-DD/allocator/allocator_output.validated.json \
  --validated-analysis runtime/paper/YYYY-MM-DD/analysis/analysis_output.kr.SYNTH-KR-0001.validated.json \
  --portfolio-state runtime/paper/YYYY-MM-DD/portfolio/portfolio_state.json \
  --paper-loop-context runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_context.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/paper_loop \
  --json
```

생성 파일: `paper_loop_input.<market>.<symbol>.json`, `paper_loop_input_assembly.<market>.<symbol>.txt`, `paper_loop_input_summary.<market>.<symbol>.json`.

`broker_account_role`는 반드시 **PAPER**이다. market price는 operator가 context JSON에 수동 기록한다(외부 API fetch 없음).
8H는 PaperLoopInput 조립·`PaperLoopInput.model_validate()`까지만 수행한다. PaperLoopRunner 실행·주문 생성·broker/KIS 호출·ledger/fill/daily summary/postmortem 기록 **없음**. 8I no-write rehearsal은 별도 단계.

### Foundation 8I — End-to-End no-write rehearsal

8H `paper_loop_input.<market>.<symbol>.json`을 입력으로 전체 chain의 validation-only 경로를 rehearsal한다. **외부 API·자동 LLM·실거래 없음.** `PaperLoopRunner.run()`을 호출하지 않으며, 기존 `ops/run_paper_once.py --no-write`만 subprocess로 실행한다. `--no-write`는 DB를 열지 않고 runner를 구성하지 않는 validation-only 경로이다. ledger DB·decision DB는 rehearsal 전후로 **없거나 byte/hash 동일**해야 한다. 기본 `--ledger-db`/`--decision-db`는 `runtime/paper/*.sqlite3`이지만, operator는 더 안전한 임시 경로를 넘길 수 있다.

```bash
mkdir -p runtime/paper/YYYY-MM-DD/rehearsal
PYTHONPATH=src uv run python ops/rehearse_paper_loop_no_write.py \
  --paper-loop-input runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input.kr.SYNTH-KR-0001.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --ledger-db runtime/paper/ledger.sqlite3 \
  --decision-db runtime/paper/decisions.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/rehearsal \
  --no-write \
  --json
sed -n '1,180p' runtime/paper/YYYY-MM-DD/rehearsal/paper_loop_no_write_rehearsal.kr.SYNTH-KR-0001.txt
```

생성 파일: `paper_loop_no_write_rehearsal.<market>.<symbol>.json`, `paper_loop_no_write_rehearsal.<market>.<symbol>.txt`, `paper_loop_no_write_rehearsal_summary.<market>.<symbol>.json`.

## Controlled Day 1 paper walk-through

**Readiness contract (0A):** `tests/test_controlled_day1_readiness.py`가 이 섹션의 no-write boundary, 8B–8I step-flow 참조, ops entrypoint 존재, git safety, after-Day-1 non-auto-progression을 **정적으로** 검증한다. operator 추가 command 없음 — actual Controlled Day 1 manual walk-through는 별도 1회 human action이다.

Foundation 8B–8I 구현이 **CLOSED**된 뒤, operator가 **1회** 수행하는 end-to-end manual walk-through다. post-Foundation runtime smoke(8G prompt-hardening → 8G validator → 8H assembler → 8I no-write rehearsal)가 통과한 상태에서, 동일 도구 chain을 **운영 절차**로 문서화한 것이다.

**Operator checklist (pre-walk-through):** 실행 전 비실행 계획·abort 기준·증거 수집은 [`docs/CONTROLLED_DAY1_OPERATOR_CHECKLIST.md`](CONTROLLED_DAY1_OPERATOR_CHECKLIST.md)를 참조한다. 이 문서는 0A readiness-contract를 대체하지 않으며, canonical shell command flow는 본 섹션의 Step-by-step command flow만 따른다.

### Purpose

- Foundation 8B–8I ops entrypoint가 실제 runtime artifact와 연결되는지 **수동으로 1회** 검증한다.
- Layer A(manual research → manual LLM → validated JSON)와 Layer B assembly/no-write validation 경계가 operator workflow에서 동작하는지 확인한다.
- **write-mode paper loop, broker, KIS, external API fetch 없이** chain closure를 확인한다.

### Scope

| 포함 | 제외 |
|---|---|
| manual/file-based research JSONL intake | real API fetchers (FRED/DART/yfinance/news HTTP) |
| manual Ollama/LLM UI copy-paste | automatic LLM orchestration |
| 8E/8F/8G raw JSON validators | validator bypass / hand-edited validated JSON |
| 8H PaperLoopInput assembly | `PaperLoopRunner.run()` |
| 8I no-write rehearsal (`run_paper_once_status == "VALIDATION_ONLY"`) | write-mode `ops/run_paper_once.py` |
| ledger/decision DB **unchanged** invariant check | ledger writes, decision snapshots, fills, NAV, daily summary, postmortem |

Controlled Day 1은 **30-trading-day paper pilot 시작이 아니다.**

### Prerequisites

운용 시작 전 regression gate (기대 결과 `11 PASS, 0 WARN, 0 FAIL`, exit code 0 — pytest는 통과 수가 아니라 exit code로 게이트된다. [§2 Acceptance check](#2-acceptance-check) 참조):

```bash
./ops/acceptance_check.sh
```

| 항목 | 요구 |
|---|---|
| Universe TOML | `runtime/paper/universe.paper.toml` **우선**; 없으면 `cp config/universe.paper.toml.example runtime/paper/universe.paper.toml` |
| Research JSONL | operator-prepared `runtime/research/${DAY}/research_sources.jsonl` |
| Portfolio state | `runtime/paper/${DAY}/portfolio/portfolio_state.json` (예: `docs/examples/portfolio_state.paper.example.json` 복사 후 조정) |
| Paper loop context | `runtime/paper/${DAY}/paper_loop/paper_loop_context.json` (예: `docs/examples/paper_loop_context.paper.example.json` 복사 후 조정) |
| Ollama | 8D/8F/8G **manual LLM call** 단계에만 필요; ops script가 자동 호출하지 않음 |
| KIS credentials | **불필요** |
| External API credentials | **불필요** |
| Git | `/runtime/`은 `.gitignore` 대상 — runtime output **commit 금지** |

`DAY`는 trading/operating date placeholder다. 예: `DAY=2026-05-28`.

### Runtime directory convention

```bash
DAY=YYYY-MM-DD
```

```text
runtime/research/${DAY}/research_sources.jsonl
runtime/research/${DAY}/date_id_sources.sqlite3
runtime/research/${DAY}/Date.md

runtime/paper/${DAY}/scout/
runtime/paper/${DAY}/allocator/
runtime/paper/${DAY}/analysis/
runtime/paper/${DAY}/portfolio/
runtime/paper/${DAY}/paper_loop/
runtime/paper/${DAY}/rehearsal/
```

### Step-by-step command flow

#### A. Prepare runtime directories

```bash
DAY=YYYY-MM-DD
mkdir -p "runtime/research/${DAY}"
mkdir -p "runtime/paper/${DAY}/"{scout,allocator,analysis,portfolio,paper_loop,rehearsal}

# universe (prefer local runtime copy)
test -f runtime/paper/universe.paper.toml || \
  cp config/universe.paper.toml.example runtime/paper/universe.paper.toml

# portfolio + paper loop context (from approved examples; adjust locally)
cp docs/examples/portfolio_state.paper.example.json \
  "runtime/paper/${DAY}/portfolio/portfolio_state.json"
cp docs/examples/paper_loop_context.paper.example.json \
  "runtime/paper/${DAY}/paper_loop/paper_loop_context.json"
```

operator는 `research_sources.jsonl`을 **직접 작성**한다. missing JSONL은 synthesize하지 않는다.

#### B. 8B — Research source intake (manual JSONL → store → Date.md)

real fetcher 없음. JSONL → `SQLiteDateIdSourceStore` → `Date.md`.

```bash
# optional: JSONL shape validation only (no store / no Date.md write)
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.jsonl" \
  --validate-only \
  --json

# normal: JSONL → date_id_sources.sqlite3 → Date.md
PYTHONPATH=src uv run python ops/research_source_intake.py \
  --source-jsonl "runtime/research/${DAY}/research_sources.jsonl" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --date-md-out "runtime/research/${DAY}/Date.md" \
  --json
```

**Gate:** exit 0, `date_id_sources.sqlite3` 및 `Date.md` 생성.

#### C. 8C — Date.md smoke (universe + store coverage)

Controlled Day 1에서는 enabled symbol coverage 필수.

```bash
PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --require-symbol-coverage \
  --json
```

**Gate:** exit 0, stdout JSON parseable.

#### C2. Manual Ollama JSON invocation contract (Scout/Allocator/Analysis manual LLM calls)

Scout · Allocator · Analysis 단계(아래 D/E/F의 manual LLM call)는 ops script가 자동 호출하지 않는다. operator가 직접 Ollama `/api/generate`를 호출할 때 아래 envelope를 **반드시** 사용한다.

```text
- model: operator-approved model
- temperature: 0          (mandatory; JsonRunnerOptions가 타 값 거부)
- seed: 42
- think: false
- stream: false
- format: json
- keep_alive: 24h
- num_ctx: model manifest value
    - qwen3.6:35b smoke profile: 32768
    - JsonRunnerOptions global default: 4096
```

Safety:

```text
- zsh 예약변수 PROMPT를 변수명으로 쓰지 않는다 (PROMPT_PATH 사용)
- Ollama HTTP response 전체의 .error 와 빈 .response 를 먼저 검사한다
- final raw path 로 직접 redirect 하지 않는다 (먼저 candidate 파일로 받는다)
- candidate JSON 을 syntax/schema/semantic 검사한 뒤 atomic mv 한다
- raw LLM output 은 수동 편집하지 않는다
- temperature 미지정으로 생성된 현재 작업 산출물은 canonical evidence 로 쓰지 않고 재생성한다
```

#### D. 8D — Scout manual packet → manual LLM → 8E validation

**8D packet build** (LLM 호출 없음):

```bash
PYTHONPATH=src uv run python ops/build_scout_manual_packet.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/paper/${DAY}/scout" \
  --require-symbol-coverage \
  --market-scope KR \
  --json
```

**Manual LLM handoff:** `runtime/paper/${DAY}/scout/scout_prompt.md`를 Ollama/UI에 paste → raw JSON을 suggested path에 **수동 저장** (예: `scout_output.kr.raw.json`). ops script는 LLM을 호출하지 않는다.

**8E validation:**

```bash
PYTHONPATH=src uv run python ops/validate_scout_raw_json.py \
  --raw-json "runtime/paper/${DAY}/scout/scout_output.kr.raw.json" \
  --scout-input "runtime/paper/${DAY}/scout/scout_input.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/paper/${DAY}/scout" \
  --json
```

**Gate:** `scout_output.validated.json` 생성.

#### E. 8F — Allocator packet → manual LLM → validation (`--store` required)

**8F packet build:**

```bash
PYTHONPATH=src uv run python ops/build_allocator_manual_packet.py \
  --validated-scout "runtime/paper/${DAY}/scout/scout_output.validated.json" \
  --scout-validation-summary "runtime/paper/${DAY}/scout/scout_validation_summary.json" \
  --portfolio-state "runtime/paper/${DAY}/portfolio/portfolio_state.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --universe runtime/paper/universe.paper.toml \
  --out-dir "runtime/paper/${DAY}/allocator" \
  --json
```

**Manual LLM handoff:** `allocator_prompt.md` paste → `allocator_output.raw.json` 수동 저장.

**8F validation** (`--store` **required**):

```bash
PYTHONPATH=src uv run python ops/validate_allocator_raw_json.py \
  --raw-json "runtime/paper/${DAY}/allocator/allocator_output.raw.json" \
  --allocator-input "runtime/paper/${DAY}/allocator/allocator_input.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/paper/${DAY}/allocator" \
  --json
```

**Gate:** `allocator_output.validated.json` 생성.

#### F. 8G — Analysis packet → manual LLM → validation (per-symbol)

8G prompt는 **AnalysisReason object schema**를 명시한다. 모든 `reasons` 필드(top-level, `bear`, `bull`, `risk_manager`, `fund_manager`)는 **string 배열이 아니라 object 배열**이어야 한다.

**8G packet build:**

```bash
PYTHONPATH=src uv run python ops/build_analysis_manual_packet.py \
  --validated-scout "runtime/paper/${DAY}/scout/scout_output.validated.json" \
  --validated-allocator "runtime/paper/${DAY}/allocator/allocator_output.validated.json" \
  --allocator-validation-summary "runtime/paper/${DAY}/allocator/allocator_validation_summary.json" \
  --portfolio-state "runtime/paper/${DAY}/portfolio/portfolio_state.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --universe runtime/paper/universe.paper.toml \
  --market KR \
  --symbol SYNTH-KR-0001 \
  --out-dir "runtime/paper/${DAY}/analysis" \
  --json
```

prompt에 `## Required reasons object schema`, `## Minimal JSON skeleton`, `Never output reasons as strings.` 포함 여부 확인.

**Manual LLM handoff:** `analysis_prompt.kr.SYNTH-KR-0001.md` paste → `analysis_output.kr.SYNTH-KR-0001.raw.json` 수동 저장.

**8G validation:**

```bash
PYTHONPATH=src uv run python ops/validate_analysis_raw_json.py \
  --raw-json "runtime/paper/${DAY}/analysis/analysis_output.kr.SYNTH-KR-0001.raw.json" \
  --analysis-input "runtime/paper/${DAY}/analysis/analysis_input.kr.SYNTH-KR-0001.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/paper/${DAY}/analysis" \
  --json
```

**Gate:** `analysis_output.kr.SYNTH-KR-0001.validated.json` 생성.

#### G. 8H — PaperLoopInput assembler (no execution)

```bash
PYTHONPATH=src uv run python ops/assemble_paper_loop_input.py \
  --validated-scout "runtime/paper/${DAY}/scout/scout_output.validated.json" \
  --validated-allocator "runtime/paper/${DAY}/allocator/allocator_output.validated.json" \
  --validated-analysis "runtime/paper/${DAY}/analysis/analysis_output.kr.SYNTH-KR-0001.validated.json" \
  --portfolio-state "runtime/paper/${DAY}/portfolio/portfolio_state.json" \
  --paper-loop-context "runtime/paper/${DAY}/paper_loop/paper_loop_context.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --out-dir "runtime/paper/${DAY}/paper_loop" \
  --json
```

assembly log 확인:

```bash
sed -n '1,180p' "runtime/paper/${DAY}/paper_loop/paper_loop_input_assembly.kr.SYNTH-KR-0001.txt"
```

**Gate:** log에 `PaperLoopInput model validation: PASS`, `execution: NOT RUN`, `order generation: NOT RUN`, `broker: NOT CALLED`, `KIS: NOT CALLED`.

#### H. 8I — No-write rehearsal

rehearsal 전 DB state capture:

```bash
ls -l runtime/paper/ledger.sqlite3 runtime/paper/decisions.sqlite3 2>/dev/null || true
shasum -a 256 runtime/paper/ledger.sqlite3 runtime/paper/decisions.sqlite3 2>/dev/null || true
```

```bash
PYTHONPATH=src uv run python ops/rehearse_paper_loop_no_write.py \
  --paper-loop-input "runtime/paper/${DAY}/paper_loop/paper_loop_input.kr.SYNTH-KR-0001.json" \
  --date-md "runtime/research/${DAY}/Date.md" \
  --store "runtime/research/${DAY}/date_id_sources.sqlite3" \
  --ledger-db runtime/paper/ledger.sqlite3 \
  --decision-db runtime/paper/decisions.sqlite3 \
  --out-dir "runtime/paper/${DAY}/rehearsal" \
  --no-write \
  --json
```

rehearsal log 확인:

```bash
sed -n '1,180p' "runtime/paper/${DAY}/rehearsal/paper_loop_no_write_rehearsal.kr.SYNTH-KR-0001.txt"
```

rehearsal 후 DB state capture:

```bash
ls -l runtime/paper/ledger.sqlite3 runtime/paper/decisions.sqlite3 2>/dev/null || true
shasum -a 256 runtime/paper/ledger.sqlite3 runtime/paper/decisions.sqlite3 2>/dev/null || true
```

**Gate:** stdout JSON `status == "ok"`, `run_paper_once_status == "VALIDATION_ONLY"`. txt에 `run_paper_once --no-write: PASS`, `ledger_db unchanged: PASS`, `decision_db unchanged: PASS`, `PaperLoopRunner.run: NOT CALLED`, `PaperBroker: NOT CALLED`, `KIS: NOT CALLED`, `Order generation: NOT RUN`, `Execution artifacts: NOT CREATED`.

### Validation gates (summary)

| Step | Gate |
|---|---|
| 8B | store + Date.md from manual JSONL |
| 8C | Date.md smoke PASS + symbol coverage |
| 8E | Scout validation PASS |
| 8F | Allocator validation PASS (`--store` required) |
| 8G | Analysis validation PASS (reason objects, not strings) |
| 8H | PaperLoopInput assembly PASS + execution guard lines |
| 8I | no-write rehearsal PASS + `VALIDATION_ONLY` |

### Failure handling

- validator 실패 시 **즉시 중단**. raw JSON 또는 upstream input을 수정한 뒤 해당 step부터 재실행.
- validator **우회 금지**. validated JSON **수동 편집 금지**.
- required runtime artifact **synthesize 금지** (missing raw LLM output → manual LLM step 재실행).
- missing portfolio/context → approved example/template에서 **준비**; 임의 placeholder invent 금지.
- Date-ID membership 실패 → Date.md/store/input citation **수정**; check disable 금지.
- no-write invariant(ledger/decision DB changed) 실패 → **즉시 중단**, DB path·side effect 조사 후 재개.

### Success criteria

Controlled Day 1 성공 조건:

- 8B: manual JSONL → Date.md/store
- 8C: Date.md smoke PASS
- 8E: Scout validation PASS
- 8F: Allocator validation PASS
- 8G: Analysis validation PASS
- 8H: PaperLoopInput assembly PASS
- 8I: no-write rehearsal PASS
- `run_paper_once_status == "VALIDATION_ONLY"`
- ledger DB absent or byte/hash-identical before vs after
- decision DB absent or byte/hash-identical before vs after
- `git ls-files runtime` empty
- external API / KIS / broker / write-mode paper loop **미사용**

### Completion note — Controlled Day 1

Controlled Day 1 was completed on `2026-06-03` as a Claude-assisted,
operator-authorized manual walk-through. The run used a fresh
`DAY=2026-06-03` runtime directory and replayed the previously
operator-prepared 2026-05-29 input set, including the previously saved
Scout/Allocator/Analysis raw LLM outputs. This was a replay, not a fresh
manual-LLM session; it did not synthesize new research and did not invoke
automatic LLM orchestration.

The chain ended at 8I no-write rehearsal with
`run_paper_once_status == "VALIDATION_ONLY"`. Ledger and decision DBs were
absent or unchanged before vs after, `git ls-files runtime` remained empty,
and external API / KIS / broker / write-mode paper loop paths were not used.

Runtime artifacts and detailed stdout/log/hash evidence remain local-only
and must not be committed. This completion does not start the
30-trading-day pilot and does not authorize KIS read-only, write-mode
`ops/run_paper_once.py`, `PaperLoopRunner.run()`, broker orders, ledger
writes, fills, NAV snapshots, daily summary, or postmortem automation.

### Artifacts generated (local only)

| Area | Examples |
|---|---|
| research | `research_sources.jsonl`, `date_id_sources.sqlite3`, `Date.md` |
| scout | `scout_input.json`, `scout_prompt.md`, `scout_output.validated.json`, validation logs |
| allocator | `allocator_input.json`, `allocator_prompt.md`, `allocator_output.validated.json`, validation logs |
| analysis | `analysis_input.*.json`, `analysis_prompt.*.md`, `analysis_output.*.validated.json`, validation logs |
| paper_loop | `paper_loop_input.*.json`, assembly txt/summary |
| rehearsal | `paper_loop_no_write_rehearsal.*` json/txt/summary |

### Git safety

```bash
git status -uall --short
git ls-files runtime
```

Expected:

```text
git ls-files runtime
# empty output
```

- **`runtime/` commit 금지**
- Day 1 runtime output은 operational artifact이며 repository source가 아님
- docs/source 변경만 의도적으로 commit

### Next-step boundary (after Day 1)

Controlled Day 1 PASS 후 **다음 설계 단계**는 [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md) (**Real Research Source Intake v1**). read-only fetch → immutable snapshot → `DateIdSourceRecord` → **기존 8B** 경로; Scout/8C–8I chain은 unchanged; walk-through는 여전히 **8I no-write**에서 종료.

Controlled Day 1 PASS 후에도 **아래는 자동으로 진행하지 않는다**:

1. real API fetcher **implementation** (FRED/yfinance/DART ops — design only until separate PR)
2. 30-trading-day paper pilot start
3. KIS read-only `--run`
4. write-mode `ops/run_paper_once.py` / `PaperLoopRunner.run()`
5. broker order submission, ledger/decision DB writes, fills, NAV snapshots, daily summary, postmortem

Post-Day1 frontier clarification: after the Controlled Day 1 PASS, the next decision is a **Real Research Source Intake repeatability/readiness decision**, not pilot launch or write-mode authorization. Before any pilot, KIS read-only, or write-mode planning, the operator must select an already documented read-only intake lane and verify that it remains repeatable through the snapshot/replay → `DateIdSourceRecord` → 8B path while preserving the unchanged 8C–8I no-write boundary. Any new live source, fetcher, endpoint hardening, or intake hardening work remains a separate task/PR and must preserve the same no-runtime-commit, no-broker, no-KIS, and no-write-mode guards.

다음 단계는 신규 구현 착수가 아니라, 기존 문서화된 Real Research Source Intake lane 중 하나를 선택해 repeatability/readiness를 검증하는 별도 결정이다. 새 live source, fetcher, endpoint hardening, intake hardening, pilot, KIS read-only, write-mode 계획은 각각 별도 task/PR과 별도 검증 후에만 다룬다.

R1–R4 plus R1b no-network replay readiness closure is recorded in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md). This closes replay-only readiness for FRED, PRICE, DART, the combined store/Date.md path, and same-DAY Date.md re-export idempotence, but does not authorize live-smoke, 8D Scout progression, pilot, KIS, broker, or write-mode planning.

L1-FRED live-smoke closure is recorded in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md) for the FRED MACRO path only; L1-PRICE, L1-DART, 8D Scout progression, pilot, KIS, broker, and write-mode planning remain separate decisions.

L1-PRICE live-smoke closure is recorded in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md) for the yfinance PRICE path only; L1-DART, 8D Scout progression, pilot, KIS, broker, and write-mode planning remain separate decisions.

L1-DART live-smoke closure is recorded in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md) for the OpenDART DISCLOSURE context-only path only; 8D Scout progression, pilot, KIS, broker, and write-mode planning remain separate decisions.

Operator-controlled 8D–8I downstream no-write closure is recorded in [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md). The documented 8I rehearsal invoked `ops/run_paper_once.py` only through its `--no-write` validation-only path; pilot, KIS, broker submission, write-mode execution, `PaperLoopRunner.run()`, and ledger/decision DB writes remain separate and unauthorized decisions.

- Date-ID stale validation은 **Python validation layer**가 담당한다.
- Date-ID가 없는 LLM 판단은 **부분 채택하지 않는다** — Allocator/Analysis 출력 전체를 폐기하고 `previous_targets` 또는 안전 상태를 유지한다.

### 수동 갱신 절차 (개요)

1. 당일 근거를 `research_sources.jsonl`에 operator-prepared record로 작성한다.
2. 8B script로 store에 저장하고 `Date.md`를 export한다.
3. LLM 호출 전 `Date.md`의 date_id 목록과 store record가 일치하는지 확인한다.
4. stale evidence(오래된 date_id)는 Python validator가 거부할 수 있으므로, 당일 relevant evidence만 prompt에 포함한다.

---

## 5. Scout → Allocator → Analysis → Risk → PaperLoop orchestration

일일 운용은 **두 Layer**로 나뉜다.

```text
Layer A — LLM orchestration
Date-ID / ScoutInput
→ Scout LLM
→ Allocator LLM
→ Analysis LLM
→ JSON parse
→ Pydantic validation
→ Date-ID validation
→ validated decision bundle

Layer B — Paper execution loop
validated AllocatorDecision + AnalysisDecision
→ RiskFilter
→ OrderIntent generation
→ QuantityResolver
→ PaperBroker
→ Fill / Cash / Position / NavSnapshot
```

### Layer 구분

| Layer | 담당 | 현재 상태 |
|---|---|---|
| **Layer A** | Scout / Allocator / Analysis LLM + validation | 개별 모듈 구현 완료, **일괄 entrypoint 미구현** |
| **Layer B** | `PaperLoopRunner` | 구현 완료 — validated decision bundle 입력 필요 |

### 중요 사항

- **`PaperLoopRunner`는 Layer B**다. 이미 validated decision bundle을 입력으로 받는다.
- Scout→Allocator→Analysis 전체를 묶는 **entrypoint는 아직 없다**.
- 향후 `ops/run_paper_once.py`는 처음에 **`--validated-input` mode**부터 구현하는 것이 안전하다 (사전 검증된 JSON/fixture 입력).
- **`--llm` mode**는 Ollama smoke와 Date.md 절차가 안정된 뒤 구현한다.

### Layer A 실패 시

- malformed JSON, schema failure, missing/invalid Date-ID → 해당 LLM output **전체 폐기**
- Debug event 기록 (`docs/DEBUG_EVENT_CODES.md` 참조)
- Layer B(PaperLoop)로 진행하지 않는다

---

## 6. PaperLoopRunner one-shot operation

### 목적

validated decision bundle을 paper ledger에 반영한다.

### 입력

| 입력 | 설명 |
|---|---|
| `AllocatorDecision` | 검증 완료된 Allocator 출력 |
| `AnalysisDecision` | 검증 완료된 Analysis 출력 |
| `RiskFilterContext` | 포트폴리오 상태, MDD, execution mode 등 |
| market price | 종목별 현재가 |
| SQLite ledger | `SQLiteLedger` |
| `PaperBrokerAdapter` | paper 체결 시뮬레이터 |

### 출력

| 출력 | 설명 |
|---|---|
| `OrderIntent` | RiskFilter 통과 후 생성된 주문 의도 |
| `Fill` | paper 체결 결과 |
| Cash / Position | ledger 갱신 |
| `NavSnapshot` | 체결 후 NAV 스냅샷 |
| `DecisionSnapshot` | 리플레이용 decision 기록 |

### 한계 (현재 MVP)

- **multi-currency NAV는 아직 미지원** — KRW 기준 필드 사용
- **fee / slippage / tax 모델은 단순화**되어 있음 (고정 비율 기반)
- duplicate `run_id` / `decision_id`는 write 전 fail-closed

### one-shot 실행

```bash
PYTHONPATH=src uv run python ops/run_paper_once.py --validated-input path/to/paper_loop_input.json
```

validation-only dry run (DB 파일/스키마 생성 없음):

```bash
PYTHONPATH=src uv run python ops/run_paper_once.py --validated-input path/to/paper_loop_input.json --no-write
```

**Layer B only:** 이 script는 LLM/Ollama/Scout/Allocator/Analysis orchestration을 하지 않는다. `PaperBrokerAdapter` + SQLite paper ledger만 사용한다.

**기본 DB 경로** (`.gitignore`의 `*.sqlite3` 패턴으로 ignored):

- `runtime/paper/ledger.sqlite3`
- `runtime/paper/decisions.sqlite3`

**입력:** production Layer A 출력 또는 dev synthetic builder(`ops/dev/build_synthetic_paper_loop_input.py`)로 준비한다. upstream Layer A orchestration entrypoint는 아직 없다.

### Dev-only synthetic PaperLoopInput builder

`ops/dev/build_synthetic_paper_loop_input.py` — **dev-only** deterministic synthetic fixture builder.

- production Layer A가 **아니다**
- Scout/Allocator/Analysis orchestration이 **아니다**
- output: `runtime/synthetic/paper_loop_input.<scenario>.SYNTH.json`
- 모든 id는 `SYNTH-` prefix
- deterministic output (동일 명령 → byte-identical JSON)
- generated JSON은 **commit하지 않는다**

```bash
PYTHONPATH=src uv run python ops/dev/build_synthetic_paper_loop_input.py --scenario normal-buy
PYTHONPATH=src uv run python ops/run_paper_once.py --validated-input runtime/synthetic/paper_loop_input.normal-buy.SYNTH.json --no-write
PYTHONPATH=src uv run python ops/run_paper_once.py --validated-input runtime/synthetic/paper_loop_input.normal-buy.SYNTH.json
```

scenario: `normal-buy` (default), `noop`, `risk-blocked`

write mode 실행 전 `--no-write`로 먼저 검증한다. generated JSON과 runtime paper DB는 commit하지 않는다.

**운영 규칙:**

- `--initial-cash-krw`는 Decimal string으로 처리한다 (`float` 사용 금지).
- 기존 ledger에 `Currency.KRW / AccountRole.PAPER` cash가 있으면 initial cash를 중복 seed하지 않는다.
- duplicate `run_id`는 initial cash seed 전에 fail-closed → exit 1.
- DB는 append-only; script가 DB를 자동 reset/delete하지 않는다.

### one-shot 절차

```text
1. acceptance_check PASS 확인
2. Ollama smoke PASS 확인 (Layer A LLM 사용 시)
3. upstream Layer A에서 validated PaperLoopInput JSON 준비
4. ops/run_paper_once.py --validated-input ... 실행
5. OrderIntent / Fill / NavSnapshot / DecisionSnapshot 결과 검토
```

---

## 7. DailySummary / DebugEvent operation

### DailySummary

- paper operation **결과를 요약**하는 일일 운영 로그다.
- actions attempted/executed, fills, portfolio state, asset/cash weights, range violations, Allocator fallback 등을 기록한다.
- DailySummary는 **오답노트가 아니다** — investment error tag를 확정하지 않는다.

### DebugEvent

- **operational event code**다 (`docs/DEBUG_EVENT_CODES.md` catalog).
- malformed LLM JSON, schema failure, missing Date-ID, broker API error, gold trade block 등 **기술/운영 실패**를 기록한다.
- Debug event code는 **investment error tag가 아니다**.

### 분리 규칙 (필수)

| 저장소 | 용도 | Top 3 Error Tags |
|---|---|---|
| `Debug.md` / DebugEvent | 기술/운영 실패 | **사용 금지** |
| Weekly/Monthly Postmortem | 투자 판단 오답노트 | **유일한 source** |

- Postmortem `error_tags`를 DebugEvent에 저장하지 않는다.
- Debug event code를 Postmortem `error_tags`로 사용하지 않는다.

---

## 8. Weekly / Monthly Postmortem operation

**Source of truth:**

- `docs/POSTMORTEM_ERROR_TAGS.md`
- `.cursor/rules/08-logs-debug-postmortem.mdc`

### 운영 규칙

- Weekly / Monthly Postmortem은 **별도 기록**이다 (KR/US 분리).
- Postmortem `error_tags`는 Debug event code와 **분리**한다.
- **Top 3 Error Tags**는 Postmortem tag summary에서만 계산한다.
- DebugEvent / DailySummary에 Postmortem `error_tags`를 **저장하지 않는다**.
- **LLM postmortem generation loop는 아직 구현하지 않는다** — Postmortem 작성은 수동.

### Weekly Postmortem

- 주간 마감 후 작성
- **이전 주** 거래/판단을 평가 (현재 주 평가 금지)
- DailySummary, Date.md, fill logs, portfolio logs 참조
- 문서 끝에 machine-readable tag summary JSON block 포함

### Monthly Postmortem

- 월간 마감 후 작성
- **이전 월** 반복 패턴, Allocator 품질, gold/cash 판단, 시스템 행동 평가
- tag summary JSON block 포함

### tag summary 형식 (예시)

```json
{
  "market": "KR",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "#정보_과신": 2,
    "#추격_매수": 1
  },
  "top_error_tags": ["#정보_과신", "#추격_매수"]
}
```

---

## 9. Emergency trigger offline operation

Phase 15 emergency trigger는 **offline detection / planning foundation**이다.

| 항목 | 현재 상태 |
|---|---|
| Scheduler polling | **없음** — intraday 1분 polling 미구현 |
| Actual broker/KIS submission | **없음** |
| MDD_KILLSWITCH | **Python-only** — LLM bypass, liquidation rule 적용 |
| Candidate OrderIntent | **검토용** — 자동 실행하지 않음 |

### Trigger 종류 (참고)

| Trigger | LLM call | Scope |
|---|---|---|
| STOCK_DROP | yes | 해당 종목 + 동일 섹터 |
| INDEX_CRASH | yes | 해당 market holdings |
| PORTFOLIO_LOSS | yes | top 3 loss contributors |
| MDD_KILLSWITCH | **no** | Python-only liquidation |
| PROFIT_RUN | staged | 해당 종목 |

### offline 운용 절차

1. DailySummary / portfolio snapshot / intraday price data로 trigger 조건을 **수동 또는 offline script**로 평가한다.
2. trigger 감지 시 candidate OrderIntent와 execution plan을 **기록만** 한다.
3. MDD_KILLSWITCH candidate는 Python liquidation rule 결과를 검토용으로 출력한다.
4. **자동 broker submission은 하지 않는다.**
5. emergency reduction 후 `below_invested_min = true` 상태는 recovery review warning으로 다음 regular analysis에 반영한다.

---

## 10. Phase 16 long paper review operation

Phase 16은 장기 paper trading 데이터를 기반으로 **파라미터 변경 후보**를 생성한다.

### Sample sufficiency

| calendar days | sufficiency | 의미 |
|---:|---|---|
| < 90 | `INSUFFICIENT` | 표본 부족 — parameter change 후보 생성 금지 |
| 90 ~ 179 | `PARTIAL` | 부분 표본 — 제한적 관찰만 |
| ≥ 180 | `SUFFICIENT` | 충분한 표본 — human review 가능 |

### Recommendation 규칙

- recommendation은 **후보일 뿐**이다.
- **`auto_apply=false`** — 모든 recommendation에 적용.
- **`human_approval_required=true`** — 사람 승인 없이 config 변경 금지.
- config change는 **별도 PR**로만 한다 — review report에서 자동 반영하지 않는다.

### review 실행

```bash
PYTHONPATH=src uv run python ops/build_paper_review_report.py --review-input path/to/paper_review_input.json
```

optional markdown:

```bash
PYTHONPATH=src uv run python ops/build_paper_review_report.py \
  --review-input path/to/paper_review_input.json \
  --markdown-out runtime/paper_review/report.md
```

optional store:

```bash
PYTHONPATH=src uv run python ops/build_paper_review_report.py \
  --review-input path/to/paper_review_input.json \
  --store runtime/paper_review/reports.jsonl
```

**Collector가 아님:** 이 script는 ledger/log/postmortem/emergency store를 자동으로 읽지 않는다. `PaperReviewInput` JSON은 후속 production collector 또는 dev synthetic builder로 준비한다.

### Dev-only synthetic PaperReviewInput builder

`ops/dev/build_synthetic_paper_review_input.py` — **dev-only** deterministic synthetic fixture builder.

- production collector가 **아니다**
- ledger/decision/log/postmortem/emergency store를 **자동으로 읽지 않는다**
- output: `runtime/synthetic/paper_review_input.<scenario>.SYNTH.json`
- 모든 id는 `SYNTH-` prefix
- deterministic output (동일 명령 → byte-identical JSON)
- generated JSON은 **commit하지 않는다**

```bash
PYTHONPATH=src uv run python ops/dev/build_synthetic_paper_review_input.py --scenario partial-with-trade

PYTHONPATH=src uv run python ops/build_paper_review_report.py \
  --review-input runtime/synthetic/paper_review_input.partial-with-trade.SYNTH.json

PYTHONPATH=src uv run python ops/build_paper_review_report.py \
  --review-input runtime/synthetic/paper_review_input.partial-with-trade.SYNTH.json \
  --markdown-out runtime/synthetic/paper_review.partial-with-trade.SYNTH.md
```

scenario: `insufficient-minimal` (default), `partial-with-trade`, `sufficient-drawdown`

`--markdown-out` 또는 `--store` 사용 시 runtime artifact가 생성된다. generated JSON/markdown/store JSONL은 commit하지 않는다. recommendations는 사람이 검토할 후보이며 auto-apply 금지.

**기본 report 실행:** input 검증 + in-memory report 생성 + text summary 출력. 파일 write 없음.

**artifact 주의:** `runtime/paper_review/*.jsonl`과 markdown output은 현재 `.gitignore` 일반 패턴으로 자동 ignore되지 않을 수 있다. `--store` / `--markdown-out` 사용 후 커밋 전 `git status` 확인.

### review 절차

```text
1. PaperReviewInput 준비 (nav_snapshots, daily_summaries, postmortem_records, emergency_events, order_intents, fills)
2. ops/build_paper_review_report.py --review-input ... 실행
3. optional: --markdown-out 또는 --store
4. recommendations 섹션을 사람이 검토
5. 승인된 변경만 별도 PR로 config.toml.example / domain constants 수정
```

---

## 11. KIS read-only smoke procedure

`ops/run_kis_read_only_smoke.py` — **명시적 수동** KIS read-only smoke script.

- default invocation은 **KIS HTTP 호출을 하지 않는다**
- 실제 KIS 호출은 **`--run` flag가 있을 때만** 수행한다
- **`broker.kis_read_only.enabled=true`는 자동 trigger가 아니다** — script를 수동으로 실행해야 한다
- **order endpoint 호출 금지** — token / balance / quote / orderbook read-only만
- **secrets / account numbers / access token 출력 금지** — account number는 masked만 출력

### P3 backlog

- KIS endpoint / TR ID는 **공식 문서 대조 전까지 P3 backlog**다 (`docs/TECH_DEBT.md` 참조).

### 필요 env vars

| env var | 필요 여부 |
|---|---|
| `KIS_LIVE_APP_KEY` | required |
| `KIS_LIVE_APP_SECRET` | required |
| `KIS_ISA_ACCOUNT` | required if `use_isa_for_kr_and_gold=true` |
| `KIS_US_REGULAR_ACCOUNT` | optional (현재 KR-focused smoke에서 미사용) |
| `KIS_CMA_ACCOUNT` | optional/skipped unless `use_cma_for_order_execution=true` |

KR smoke symbol **`411060`**은 Phase 14 기본 ISA/KR read-only smoke symbol이며, quote/orderbook smoke 기본값이다.

### config / dry-run (HTTP 없음)

```bash
PYTHONPATH=src uv run python ops/run_kis_read_only_smoke.py

PYTHONPATH=src uv run python ops/run_kis_read_only_smoke.py --check-config-only

PYTHONPATH=src uv run python ops/run_kis_read_only_smoke.py --dry-run

PYTHONPATH=src uv run python ops/run_kis_read_only_smoke.py --dry-run --json
```

### 실제 KIS read-only run (명시적 opt-in)

```bash
PYTHONPATH=src uv run python ops/run_kis_read_only_smoke.py --run --kr-symbol 411060
```

운영 전 확인:

1. `config/config.toml`에서 live gates가 **비활성** 상태인지 확인한다 (`trading.mode=paper`, `allow_live_trading=false`).
2. KIS credentials는 **환경변수**로만 제공 — repo에 commit하지 않는다.
3. 확인 대상: **token / balance / current price / orderbook** (read-only).

### 실패 시

KIS read-only smoke 실패는 paper pilot 진행을 차단하지 않지만, live/tiny-live rehearsal 전까지 KIS 연동을 신뢰하지 않는다.

---

## 12. Tiny-live dry-run procedure

Phase 14 tiny-live는 **dry-run gate/scaffold**다.

| 항목 | 상태 |
|---|---|
| `submit_tiny_live_order()` | **존재하면 안 됨** |
| `place_tiny_live_order()` | **존재하면 안 됨** |
| Actual order submission | **후속 explicit manual rehearsal 전까지 금지** |

### dry-run gate (개념)

1. paper mode + acceptance check PASS 확인
2. KIS read-only smoke PASS 확인
3. `TINY_LIVE_CONFIRM` 환경변수 + config tiny-live gate 확인
4. capped notional (`DEFAULT_MAX_TINY_LIVE_NOTIONAL_KRW = 100,000`) 범위 내 manual OrderIntent 준비
5. **LLM-triggered order 금지** — 사람이 작성한 OrderIntent만
6. actual submission은 **별도 explicit manual rehearsal phase**에서만

---

## 13. Forbidden operations

다음은 **현재 phase에서 절대 수행하지 않는다:**

| 금지 항목 | 이유 |
|---|---|
| automatic live trading | live gates + explicit phase 미완 |
| scheduler / polling loop | 미구현 |
| live order endpoint 호출 | Phase 14 scaffold only |
| tiny-live actual submission | dry-run gate only |
| LLM-triggered order | LLM은 판단만 |
| KIS mock adapter | MVP default path에서 제외 |
| config auto-write | LLM/config 자동 변경 금지 |
| parameter auto-apply | Phase 16 recommendations 수동 PR only |
| Date.md automatic generator | 수동 운영 우선 |
| Postmortem tags in DebugEvent | catalog 분리 |
| Debug event codes as Postmortem error_tags | Top 3 source 오염 방지 |

---

## 14. Recommended pilot sequence

아래 순서로 pilot을 진행한다. 각 단계는 이전 단계 PASS 후에만 진행한다.

**하루 단위 daily workflow 상세:** [docs/PAPER_PILOT_WORKFLOW.md](PAPER_PILOT_WORKFLOW.md) — Date.md → Scout → Allocator → Analysis → Risk → PaperLoop → DailySummary → Postmortem 후보까지의 folder convention, 수동/자동 구분, Go/No-Go checklist.

```text
1. acceptance_check
2. Ollama smoke
3. Date.md / Date-ID manual update
4. validated decision bundle generation
5. PaperLoopRunner one-shot
6. DailySummary / DebugEvent review
7. weekly Postmortem
8. emergency offline detection
9. 30 trading day pilot
10. 90+ day paper review
11. KIS read-only smoke
```

### 단계별 gate

| Step | Gate | 실패 시 |
|---:|---|---|
| 1 | `./ops/acceptance_check.sh` exit 0 | 운용 중단 |
| 2 | Ollama reachable + dummy JSON validation | daily pilot 중단 |
| 3 | Date.md date_id ⊆ store records | LLM 호출 중단 |
| 4 | Layer A validation 전체 PASS | Layer B 진행 금지 |
| 5 | PaperLoop one-shot Fill + NavSnapshot 확인 | ledger 검토 |
| 6 | DebugEvent에 CRITICAL 없음 | 원인 조사 |
| 7 | Postmortem tag summary 작성 | Top 3 집계 보류 |
| 8 | offline trigger candidate 기록 | 자동 submission 금지 |
| 9 | 30 trading day 데이터 축적 | — |
| 10 | sample sufficiency ≥ PARTIAL (90+ days) | parameter review 보류 |
| 11 | KIS read-only balance/positions 확인 | live rehearsal 보류 |

---

## 참고 문서

| 문서 | 용도 |
|---|---|
| `docs/PAPER_PILOT_WORKFLOW.md` | 30거래일 paper pilot daily workflow skeleton |
| `CODING_RULES.md` | Phase numbering, 핵심 원칙 |
| `docs/DEBUG_EVENT_CODES.md` | Debug event catalog (human-only) |
| `docs/POSTMORTEM_ERROR_TAGS.md` | Postmortem error tag catalog |
| `docs/TECH_DEBT.md` | P2/P3 backlog |
| `.cursor/rules/08-logs-debug-postmortem.mdc` | Log/Postmortem 규칙 |
| `.cursor/rules/11-runtime-config-and-mode.mdc` | Runtime config / live gate |
| `.cursor/rules/14-broker-api-and-paper-broker.mdc` | Broker / PaperBroker 규칙 |
