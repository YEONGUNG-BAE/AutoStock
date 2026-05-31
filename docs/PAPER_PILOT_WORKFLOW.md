# Paper Pilot Daily Workflow (Foundation 8A)

30거래일 paper pilot을 시작하기 **전**에, 하루 단위 paper 운용 절차와 산출물 convention을 고정한다.

> **이 문서는 workflow skeleton이다.** 자동 orchestration, collector, scheduler를 구현하지 않는다.  
> Foundation 8B (Research Source Intake + Date.md Export), 8C (Universe v0 + Date.md smoke), 8D (Scout Once manual packet), 8E (Scout raw JSON validator), 8F (Portfolio state + Allocator Once), 8G (Analysis Once per-symbol), 8H (PaperLoopInput assembler), **8I (End-to-End no-write rehearsal)**는 **Day 0 roadmap**에 포함된다.

---

## 1. Status and scope

| 항목 | 현재 상태 |
|---|---|
| Automatic daily paper trading system | **아님** — manual + validated paper operation |
| LLM orchestration entrypoint (Scout→Allocator→Analysis 일괄) | **없음** |
| Production PaperLoopInput assembler (per-symbol) | **있음** — Foundation 8H `ops/assemble_paper_loop_input.py` |
| Date.md export helper | **있음** — Foundation 8B `ops/research_source_intake.py`, manual/file-based only (scheduler/collector 없음) |
| Universe v0 + Date.md smoke | **있음** — Foundation 8C `ops/run_date_md_smoke.py`, validation only (LLM/API/trading 없음) |
| Scout Once manual packet | **있음** — Foundation 8D `ops/build_scout_manual_packet.py`, packet only (LLM call/raw validation 없음) |
| Scout raw JSON validator | **있음** — Foundation 8E `ops/validate_scout_raw_json.py`, ScoutSummary validation only |
| Allocator Once manual packet + validator | **있음** — Foundation 8F `ops/build_allocator_manual_packet.py`, `ops/validate_allocator_raw_json.py` |
| Analysis Once manual packet + validator (per-symbol) | **있음** — Foundation 8G `ops/build_analysis_manual_packet.py`, `ops/validate_analysis_raw_json.py` |
| DailySummary / Postmortem 자동 생성기 | **없음** |
| Scheduler / launchd | **없음** |
| KIS / live / tiny-live order | **이 workflow와 무관** — paper ledger only |

**Pilot 범위:** KR/US 모두 확장 가능한 convention이지만, pilot은 **한 시장 또는 제한된 universe**로 시작해도 된다.  
예: KR large-cap만, 또는 단일 symbol smoke 후 확장.

**준비된 ops entrypoint (참고):**

| Script | 역할 |
|---|---|
| `ops/acceptance_check.sh` | regression gate |
| `ops/run_ollama_smoke.py` | Ollama JSON smoke |
| `ops/run_paper_once.py` | Layer B paper one-shot |
| `ops/build_paper_review_report.py` | Phase 16 review (장기) |
| `ops/run_kis_read_only_smoke.py` | KIS read-only (pilot blocking 아님) |
| `ops/dev/build_synthetic_paper_loop_input.py` | **dev-only** SYNTH fixture |
| `ops/dev/build_synthetic_paper_review_input.py` | **dev-only** SYNTH fixture |

**30거래일 pilot은 “버튼 하나로 시작”되지 않는다.** 운영자가 아래 단계를 매 거래일 수동으로 수행한다.

---

## 2. One-day paper pilot overview

`YYYY-MM-DD` = 해당 **US market trading date** (KST 기준 운영 메모는 daily folder README에 기록).

| Step | 단계 | Input | Output | Location | Pass gate | 실패 시 |
|---:|---|---|---|---|---|---|
| 0 | Daily folder 준비 | trading date | folder tree | `runtime/paper/YYYY-MM-DD/` | folder 생성 | — |
| 1 | acceptance_check | repo | PASS summary | 터미널 / `README.md` 메모 | `11 PASS, 0 WARN, 0 FAIL` | **중단** |
| 2 | Ollama smoke | config + Ollama | PASS summary | 터미널 / `README.md` 메모 | `Ollama smoke: PASS` | **중단** |
| 3 | Date.md / Date-ID | sources, news, macro | Date.md + store export | `date/` | date_id ⊆ store | **LLM 호출 중단** |
| 4 | Scout input 준비 | Date-ID store, market context | ScoutInput JSON | `scout/scout_input.*.json` | builder/수동 JSON valid | Scout LLM 중단 |
| 5 | Scout LLM (수동) | ScoutInput + prompt | raw JSON | `scout/scout_output.*.raw.json` | — | — |
| 6 | Scout validation | raw JSON | validated JSON + log | `scout/scout_output.*.validated.json` | schema + Date-ID PASS | **Allocator 중단** |
| 7 | Allocator LLM (수동) | Scout + portfolio | raw JSON | `allocator/allocator_output.raw.json` | — | — |
| 8 | Allocator validation | raw JSON | validated JSON + log | `allocator/allocator_output.validated.json` | schema + Date-ID + range PASS | **Analysis/PaperLoop 중단** |
| 9 | Analysis LLM (수동) | symbol context | raw JSON per symbol | `analysis/analysis_output.*.raw.json` | — | — |
| 10 | Analysis validation | raw JSON | validated JSON + log | `analysis/analysis_output.*.validated.json` | schema + Date-ID PASS | 해당 symbol **폐기** |
| 11 | Python validation | validated decisions | validation notes | `*_validation.txt` | no CRITICAL | PaperLoopInput 조립 중단 |
| 12 | PaperLoopInput 조립 (수동) | validated Layer A + risk + price | JSON | `paper_loop/paper_loop_input.json` | `PaperLoopInput` valid | **run_paper_once 중단** |
| 13 | run_paper_once `--no-write` | PaperLoopInput | summary text | `paper_loop/paper_once.no_write.txt` | exit 0 | **write mode 금지** |
| 14 | run_paper_once write | PaperLoopInput | summary + ledger | `paper_loop/paper_once.write.txt` | exit 0, status reviewed | ledger 검토 |
| 15 | Result review | ledger, fills, NAV | operator sign-off | `README.md` | fills/NAV 합리적 | DailySummary 보류 |
| 16 | DailySummary 작성 | run results | markdown | `summary/daily_summary.md` | template filled | — |
| 17 | DebugEvent review | runtime logs / notes | markdown | `summary/debug_review.md` | no unresolved CRITICAL | 원인 조사 |
| 18 | Postmortem 후보 메모 | 주간 관찰 | notes | `postmortem/weekly_notes.md` | — | — |

---

## 3. Daily folder convention

**실행 시** 운영자가 로컬에 생성한다. **repo에 commit하지 않는다.**  
`.gitignore`는 이번 phase에서 수정하지 않았으므로, 커밋 전 `git status -uall --short`로 generated artifact를 확인한다.

```
runtime/paper/YYYY-MM-DD/
├── README.md                          # 당일 run_id, gate 결과, manual notes
├── date/
│   ├── Date.md                        # read-only prompt reference
│   └── date_id_sources.jsonl          # SQLiteDateIdSourceStore export snapshot
├── scout/
│   ├── scout_input.kr.json
│   ├── scout_output.kr.raw.json
│   ├── scout_output.kr.validated.json
│   └── scout_validation.txt
├── allocator/
│   ├── allocator_input.json
│   ├── allocator_output.raw.json
│   ├── allocator_output.validated.json
│   └── allocator_validation.txt
├── analysis/
│   ├── analysis_input.kr.<symbol>.json
│   ├── analysis_output.kr.<symbol>.raw.json
│   ├── analysis_output.kr.<symbol>.validated.json
│   └── analysis_validation.kr.<symbol>.txt
├── risk/
│   └── risk_context.json
├── paper_loop/
│   ├── paper_loop_input.json
│   ├── paper_once.no_write.txt
│   └── paper_once.write.txt
├── summary/
│   ├── daily_summary.md
│   └── debug_review.md
└── postmortem/
    └── weekly_notes.md
```

**US market 확장 시:** `scout_input.us.json`, `analysis_input.us.<symbol>.json` 등 market prefix를 병행한다.

**장기 ledger (day folder 밖):**

- `runtime/paper/ledger.sqlite3`
- `runtime/paper/decisions.sqlite3`

append-only; day folder는 **당일 운영 기록**이고 ledger는 **누적 source of truth**다.

---

## 4. Date.md / Date-ID workflow

### 원칙

- `Date.md`는 **read-only prompt reference**다. LLM이 evidence로 인용하는 date_id 목록을 담는다.
- Source record의 canonical store는 `SQLiteDateIdSourceStore` (`src/data/date_id_store.py`)다.
- LLM prompt에는 **Date.md에 존재하는 date_id만** 사용한다.
- LLM output의 `reasons[].date_id`는 Python Date-ID validator로 검증한다.
- Date.md에 없는 date_id가 하나라도 나오면 **해당 LLM output 전체를 폐기**한다 (부분 salvage 금지).
- Foundation **8B**가 manual/file-based **Date.md export helper** (`ops/research_source_intake.py`)를 제공한다.
- `Date.md`는 **read-only prompt reference**이며, **SQLiteDateIdSourceStore**가 canonical store다.
- pilot 초기 intake 경로: `runtime/research/YYYY-MM-DD/research_sources.jsonl` → store → `Date.md`
- paper day folder의 `date/Date.md`는 research export를 복사하거나 symlink 없이 운영자가 당일 folder로 가져온다.

### 파일 convention

| 파일 | 용도 |
|---|---|
| `runtime/research/YYYY-MM-DD/research_sources.jsonl` | operator-prepared intake JSONL |
| `runtime/research/YYYY-MM-DD/date_id_sources.sqlite3` | optional local Date-ID store |
| `runtime/research/YYYY-MM-DD/Date.md` | exported prompt reference (8B) |
| `runtime/paper/YYYY-MM-DD/date/Date.md` | 당일 paper run prompt reference |
| `runtime/paper/YYYY-MM-DD/date/date_id_sources.jsonl` | optional store export snapshot |

### Pass gate

- Scout/Allocator/Analysis validation 전: `date_id` cited ⊆ `Date.md` entries ⊆ store records
- FAIL → downstream LLM output 사용 금지

---

## 5. Scout output workflow

### 현재 gap

- Scout actual LLM call entrypoint **없음**
- `ScoutInputBuilder` (`src/scout/input_builder.py`)는 input 조립용; orchestration CLI 없음

### 수동 절차

1. `ScoutInput` JSON을 준비한다 (`scout/scout_input.kr.json`).
2. 운영자가 Scout prompt를 **수동 실행** (Ollama UI, CLI, 또는 별도 prompt runner).
3. raw response를 **그대로** 저장: `scout_output.kr.raw.json`
4. Python으로 schema + Date-ID validation 수행.
5. PASS → `scout_output.kr.validated.json` + `scout_validation.txt`
6. FAIL (malformed JSON / schema fail / Date-ID fail) → **Allocator로 진행하지 않음**

### raw vs validated

| 파일 | 설명 |
|---|---|
| `*.raw.json` | LLM 원문 (수정 금지, audit trail) |
| `*.validated.json` | Python 검증 통과본만 downstream 사용 |
| `*_validation.txt` | pass/fail, issue summary (secret 없음) |

---

## 6. Allocator output workflow

### 현재 gap

- Allocator actual LLM call entrypoint **없음**

### 수동 절차

1. validated Scout output + portfolio state로 `allocator_input.json` 작성.
2. Allocator prompt **수동 실행** → `allocator_output.raw.json`
3. Python validation: schema, Date-ID, target weight range, cash/gold policy.
4. PASS → `allocator_output.validated.json`
5. range violation / schema fail / Date-ID fail → **Analysis 및 PaperLoop 진행 금지**
6. allocator fallback 발생 시 `DailySummary.allocator_fallback_count` 후보로 기록.

---

## 7. Analysis output workflow

### 현재 gap

- Analysis actual LLM call entrypoint **없음**

### 수동 절차

1. 종목(또는 market)별 `analysis_input.kr.<symbol>.json` 작성.
2. Analysis prompt **수동 실행** → `analysis_output.kr.<symbol>.raw.json`
3. Python validation: schema, Date-ID, action enum.
4. `reasons[].date_id` FAIL → **해당 Analysis output 전체 폐기**
5. invalid action (예: schema 밖 action) → **PaperLoopInput에 포함하지 않음**
6. PASS → `analysis_output.kr.<symbol>.validated.json`

Pilot 초기에는 **단일 symbol** 또는 **소수 종목**만 Analysis 실행해도 된다.

---

## 8. Risk / PaperLoopInput assembly workflow

### 현재 gap

- Production PaperLoopInput assembler — Foundation 8H `ops/assemble_paper_loop_input.py` (**조립·검증만**, 실행 없음)
- `ops/dev/build_synthetic_paper_loop_input.py`는 **dev-only SYNTH fixture** — production input 아님

### 수동 조립

validated 다음을 운영자가 `PaperLoopInput` JSON으로 조립:

- `AllocatorDecision` (validated)
- `AnalysisDecision` (validated, symbol별)
- `RiskFilterContext` → `risk/risk_context.json` 참고
- `MarketPrice` (reference price)
- `broker_account_role`: `AccountRole.PAPER`
- `run_id`, `created_at`, `correlation_id`

### 실행 순서 (필수)

```bash
# 1) no-write preflight
PYTHONPATH=src uv run python ops/run_paper_once.py \
  --validated-input runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input.json \
  --no-write \
  | tee runtime/paper/YYYY-MM-DD/paper_loop/paper_once.no_write.txt

# 2) write mode (no-write PASS 후에만)
PYTHONPATH=src uv run python ops/run_paper_once.py \
  --validated-input runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input.json \
  | tee runtime/paper/YYYY-MM-DD/paper_loop/paper_once.write.txt
```

### Pass gate

- `--no-write` exit 0
- write mode: status (`FILLED` / `NOOP` / `RISK_BLOCKED` 등) 운영자 review
- duplicate `run_id`는 fail-closed (script preflight)

---

## 9. DailySummary / DebugEvent workflow

### 분리 원칙

| 개념 | 용도 | catalog |
|---|---|---|
| `DailySummary` | paper run **운영 요약** (투자 postmortem 아님) | `src/logs/models.py` |
| `DebugEvent` | 기술/운영 이벤트 (`event_code`) | `docs/DEBUG_EVENT_CODES.md` |
| Postmortem `error_tags` | 투자 판단 오답노트 | `docs/POSTMORTEM_ERROR_TAGS.md` |

**금지:**

- Postmortem `error_tags`를 DailySummary / DebugEvent에 저장하지 않는다.
- Debug `event_code`를 Postmortem `error_tags`로 사용하지 않는다.

### 파일 convention

- `summary/daily_summary.md` — 당일 운영 요약
- `summary/debug_review.md` — DebugEvent review 메모

### DailySummary template (markdown)

운영자가 `summary/daily_summary.md`에 아래 항목을 채운다. **runtime file은 이번 문서 작업에서 생성하지 않는다.**

```markdown
# Daily Summary — YYYY-MM-DD

## Meta
- date: YYYY-MM-DD
- market: KR | US | BOTH
- run_id:
- operator:

## Pre-run gates
- acceptance_check: PASS | FAIL
- ollama_smoke: PASS | FAIL
- Date.md status: updated | stale | missing

## Layer A validation
- Scout: PASS | FAIL | SKIPPED
- Allocator: PASS | FAIL | SKIPPED
- Analysis (<symbol>): PASS | FAIL | SKIPPED
- Date-ID failures: none | list symbols/scenarios
- Schema failures: none | brief note

## PaperLoop
- status: FILLED | NOOP | RISK_BLOCKED | FAILED | SKIPPED
- orders attempted:
- orders executed:
- fills:
- cash (KRW):
- nav (KRW):
- range_violation_count:
- allocator_fallback_count:

## Debug events
- CRITICAL: none | list event_code (no secrets)
- other notable events:

## Manual notes
- blockers:
- follow-ups:
- weekly postmortem candidates:
```

---

## 10. Weekly / Monthly Postmortem workflow

### 원칙

- Postmortem은 **weekly / monthly** 별도 작성한다.
- Top 3 Error Tags는 **Postmortem tag summary**에서만 계산한다.
- Source of truth: `docs/POSTMORTEM_ERROR_TAGS.md`, `.cursor/rules/08-logs-debug-postmortem.mdc`
- LLM postmortem generation loop **없음**

### Pilot 중 practice

- 매일: `postmortem/weekly_notes.md`에 후보만 메모
- 주간 마감 후: 정식 `PostmortemRecord` / markdown으로 옮김

### Future convention (제안만 — 생성하지 않음)

```
memory/postmortem/weekly/YYYY-Www.KR.md
memory/postmortem/monthly/YYYY-MM.KR.md
```

`memory/` folder는 **아직 생성하지 않는다.**

---

## 11. Go / No-Go checklist

### 하루 실행 **전** (Go gate)

- [ ] `git status` clean 또는 expected local runtime files만 untracked
- [ ] `./ops/acceptance_check.sh` → `11 PASS, 0 WARN, 0 FAIL`
- [ ] `ops/run_ollama_smoke.py` → PASS
- [ ] `date/Date.md` 당일분 갱신됨
- [ ] cited date_id ⊆ Date.md ⊆ store
- [ ] Scout / Allocator / Analysis **validated** JSON 준비 (raw만 있으면 NO-GO)
- [ ] schema validation fail 없음
- [ ] Date-ID fail 없음
- [ ] unresolved CRITICAL DebugEvent 없음 (전일 carry-over 포함)
- [ ] `PaperLoopInput` `--no-write` PASS

### 하루 실행 **후** (Close gate)

- [ ] write mode result reviewed
- [ ] ledger change가 기대와 일치
- [ ] fills reviewed (quantity, price, side)
- [ ] NAV reviewed
- [ ] `summary/daily_summary.md` updated
- [ ] `summary/debug_review.md` updated
- [ ] allocator fallback / range violation 기록됨
- [ ] `postmortem/weekly_notes.md` updated (필요 시)

---

## 12. Foundation roadmap (8B–8I) and evidence-based follow-ups

### Day 0 roadmap (Foundation numbering — operational, not Phase numbering)

| ID | 이름 | 목적 |
|---|---|---|
| **8B** | Research Source Intake + Date.md Export | operator JSONL → validated store → Date.md |
| **8C** | Universe v0 + Date.md prompt-reference smoke | `config/universe.paper.toml.example` + `ops/run_date_md_smoke.py` |
| **8D** | Scout Once manual LLM call | `ops/build_scout_manual_packet.py` — ScoutInput + scout_prompt (LLM call 없음) |
| **8E** | Manual LLM JSON Intake Validator (Scout) | `ops/validate_scout_raw_json.py` — raw → validated ScoutSummary |
| **8F** | Portfolio state snapshot + Allocator Once | `ops/build_allocator_manual_packet.py` + `ops/validate_allocator_raw_json.py` |
| **8G** | Analysis Once | `ops/build_analysis_manual_packet.py` + `ops/validate_analysis_raw_json.py` (per-symbol) |
| **8H** | Production PaperLoopInput Assembler | `ops/assemble_paper_loop_input.py` (per-symbol, no execution) |
| **8I** | End-to-End no-write rehearsal | `ops/rehearse_paper_loop_no_write.py` (validation-only, no PaperLoopRunner.run) |

**8B·8C·8D·8E·8F·8G·8H·8I (Foundation)는 CLOSED.** 다음 운영 단계는 **Controlled Day 1 paper walk-through** — 8B~8I를 순서대로 **1회** 수동 실행하고 8I no-write rehearsal에서 종료한다. 절차는 `docs/RUNBOOK.md` § Controlled Day 1 paper walk-through 참조.

### Universe v0 convention (Foundation 8C)

| Path | 용도 |
|---|---|
| `config/universe.paper.toml.example` | committed synthetic example (copy only) |
| `runtime/paper/universe.paper.toml` | operator local universe file (**commit 금지**) |
| `runtime/paper/YYYY-MM-DD/universe.paper.toml` | optional daily reference copy |

8C smoke (`ops/run_date_md_smoke.py`)는 Universe TOML + exported Date.md (+ optional store)를 검증한다. **LLM을 호출하지 않는다.**

```bash
cp config/universe.paper.toml.example runtime/paper/universe.paper.toml
PYTHONPATH=src uv run python ops/run_date_md_smoke.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --require-symbol-coverage \
  --json
```

### Scout manual packet convention (Foundation 8D)

| Path | 용도 |
|---|---|
| `runtime/paper/YYYY-MM-DD/scout/scout_input.json` | ScoutInput JSON (packet builder output) |
| `runtime/paper/YYYY-MM-DD/scout/scout_prompt.md` | manual LLM copy/paste prompt |
| `runtime/paper/YYYY-MM-DD/scout/scout_packet_summary.json` | machine-readable packet summary |
| `runtime/paper/YYYY-MM-DD/scout/scout_output.<scope>.raw.json` | operator manual raw LLM output (**8D가 생성하지 않음**) |

8D packet builder (`ops/build_scout_manual_packet.py`)는 ScoutInput + scout_prompt를 생성한다.
**LLM을 호출하지 않으며**, raw/validated Scout output을 생성하지 않는다.

```bash
PYTHONPATH=src uv run python ops/build_scout_manual_packet.py \
  --universe runtime/paper/universe.paper.toml \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/scout \
  --require-symbol-coverage \
  --market-scope KR \
  --json
```

### Scout raw JSON validation convention (Foundation 8E)

| Path | 용도 |
|---|---|
| `runtime/paper/YYYY-MM-DD/scout/scout_output.<scope>.raw.json` | operator manual raw LLM output |
| `runtime/paper/YYYY-MM-DD/scout/scout_output.validated.json` | validated ScoutSummary JSON |
| `runtime/paper/YYYY-MM-DD/scout/scout_validation.txt` | human-readable validation log |
| `runtime/paper/YYYY-MM-DD/scout/scout_validation_summary.json` | machine-readable validation summary |

8E validator (`ops/validate_scout_raw_json.py`)는 raw Scout JSON만 검증한다.
Allocator/Analysis validation은 **8E 범위 밖**이다.
`ScoutSummary.created_at`은 timezone-aware datetime이면 충분하며, 8E는 `ScoutInput.created_at` 대비 freshness ordering을 검사하지 않는다.

```bash
PYTHONPATH=src uv run python ops/validate_scout_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/scout/scout_output.kr.raw.json \
  --scout-input runtime/paper/YYYY-MM-DD/scout/scout_input.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/scout \
  --json
```

### Portfolio state + Allocator Once convention (Foundation 8F)

| Path | 용도 |
|---|---|
| `docs/examples/portfolio_state.paper.example.json` | committed synthetic example (copy only) |
| `runtime/paper/YYYY-MM-DD/portfolio/portfolio_state.json` | operator local portfolio state (**commit 금지**) |
| `runtime/paper/YYYY-MM-DD/allocator/allocator_input.json` | Allocator packet builder output |
| `runtime/paper/YYYY-MM-DD/allocator/allocator_prompt.md` | manual LLM copy/paste prompt |
| `runtime/paper/YYYY-MM-DD/allocator/allocator_packet_summary.json` | machine-readable packet summary |
| `runtime/paper/YYYY-MM-DD/allocator/allocator_output.raw.json` | operator manual raw LLM output (**8F packet builder가 생성하지 않음**) |
| `runtime/paper/YYYY-MM-DD/allocator/allocator_output.validated.json` | validated AllocatorDecision JSON |

8F packet builder + validator는 validated ScoutSummary + portfolio state + Date.md/store context를 사용한다.
**LLM을 호출하지 않으며**, 주문 실행·PaperLoopInput assembly·Analysis validation은 **8F 범위 밖**이다.
`AllocatorDecision.created_at`은 timezone-aware datetime이면 충분하며, 8F는 ScoutSummary/portfolio `as_of` 대비 freshness ordering을 검사하지 않는다.

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
PYTHONPATH=src uv run python ops/validate_allocator_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/allocator/allocator_output.raw.json \
  --allocator-input runtime/paper/YYYY-MM-DD/allocator/allocator_input.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/allocator \
  --json
```

### Analysis Once convention (Foundation 8G)

| Path | 용도 |
|---|---|
| `runtime/paper/YYYY-MM-DD/analysis/analysis_input.<market>.<symbol>.json` | Analysis packet builder output (lowercase market in filename) |
| `runtime/paper/YYYY-MM-DD/analysis/analysis_prompt.<market>.<symbol>.md` | manual LLM copy/paste prompt |
| `runtime/paper/YYYY-MM-DD/analysis/analysis_packet_summary.<market>.<symbol>.json` | machine-readable packet summary |
| `runtime/paper/YYYY-MM-DD/analysis/analysis_output.<market>.<symbol>.raw.json` | operator manual raw LLM output (**8G packet builder가 생성하지 않음**) |
| `runtime/paper/YYYY-MM-DD/analysis/analysis_output.<market>.<symbol>.validated.json` | validated AnalysisDecision JSON |

8G packet builder + validator는 validated ScoutSummary + validated AllocatorDecision + portfolio state + Date.md/store context를 **symbol/market 1건**에 대해 사용한다.
**LLM을 호출하지 않으며**, 주문 실행·PaperLoopInput assembly·PaperBroker/KIS 경로는 **8G 범위 밖**이다.
`AnalysisDecision.created_at`은 timezone-aware datetime이면 충분하며, 8G는 ScoutSummary/AllocatorDecision/portfolio snapshot `as_of` 대비 freshness ordering을 검사하지 않는다.
per-symbol allocator tolerance는 operator가 `--allocator-target-weight-percent`와 `--tolerance-percent`를 **둘 다** 명시할 때만 적용하며, AllocatorDecision aggregate `target_weights`에서 per-symbol weight를 **추론하지 않는다**.

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
PYTHONPATH=src uv run python ops/validate_analysis_raw_json.py \
  --raw-json runtime/paper/YYYY-MM-DD/analysis/analysis_output.kr.SYNTH-KR-0001.raw.json \
  --analysis-input runtime/paper/YYYY-MM-DD/analysis/analysis_input.kr.SYNTH-KR-0001.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/analysis \
  --json
```

### PaperLoopInput assembly convention (Foundation 8H)

| Path | 용도 |
|---|---|
| `docs/examples/paper_loop_context.paper.example.json` | committed synthetic example (copy only) |
| `runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_context.json` | operator local context (**commit 금지**) |
| `runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input.<market>.<symbol>.json` | validated PaperLoopInput JSON |
| `runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input_assembly.<market>.<symbol>.txt` | human-readable assembly log |
| `runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input_summary.<market>.<symbol>.json` | machine-readable summary |

8H assembler는 validated ScoutSummary(optional) + AllocatorDecision + AnalysisDecision + paper loop context + Date.md/store로 **PaperLoopInput만** 생성한다.
**LLM·외부 시세 API·PaperLoopRunner·PaperBroker·KIS·OrderIntent 생성·ledger/fill/daily summary/postmortem 기록 없음.** `broker_account_role`는 **PAPER** 고정.

```bash
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

### No-write rehearsal convention (Foundation 8I)

| Path | 용도 |
|---|---|
| `runtime/paper/YYYY-MM-DD/rehearsal/paper_loop_no_write_rehearsal.<market>.<symbol>.json` | machine-readable rehearsal record |
| `runtime/paper/YYYY-MM-DD/rehearsal/paper_loop_no_write_rehearsal.<market>.<symbol>.txt` | human-readable rehearsal log |
| `runtime/paper/YYYY-MM-DD/rehearsal/paper_loop_no_write_rehearsal_summary.<market>.<symbol>.json` | compact summary |

8I rehearsal는 8H `paper_loop_input.<market>.<symbol>.json`을 입력으로 Date.md/store membership을 검증한 뒤, 기존 `ops/run_paper_once.py --no-write --json`만 subprocess로 호출한다.
**`PaperLoopRunner.run()`을 호출하지 않는다.** 현재 `PaperLoopRunner.run()`은 decision snapshot·broker submit·ledger NAV write가 가능하므로 no-write rehearsal에 사용하지 않는다.
**LLM·외부 API·KIS·OrderIntent 생성·ledger/fill/daily summary/postmortem 기록 없음.** ledger DB·decision DB는 rehearsal 전후로 없거나 byte-identical이어야 한다.

```bash
PYTHONPATH=src uv run python ops/rehearse_paper_loop_no_write.py \
  --paper-loop-input runtime/paper/YYYY-MM-DD/paper_loop/paper_loop_input.kr.SYNTH-KR-0001.json \
  --date-md runtime/research/YYYY-MM-DD/Date.md \
  --store runtime/research/YYYY-MM-DD/date_id_sources.sqlite3 \
  --ledger-db runtime/paper/ledger.sqlite3 \
  --decision-db runtime/paper/decisions.sqlite3 \
  --out-dir runtime/paper/YYYY-MM-DD/rehearsal \
  --no-write \
  --json
```

Controlled Day 1 paper walk-through는 Foundation 8B–8I chain을 **수동/file-based intake + manual LLM + validated JSON + 8I no-write rehearsal**까지 1회 검증하는 단계이며, **8I에서 종료**한다 (`docs/RUNBOOK.md` 참조). real API fetchers, 30-trading-day pilot start, KIS read-only `--run`은 **Controlled Day 1 이후 deferred**이다.

### Real Research Source Intake v1 (next stage — design)

Controlled Day 1 **PASS** 후 다음 증분은 **Real Research Source Intake v1** (design: [`docs/REAL_RESEARCH_SOURCE_INTAKE.md`](REAL_RESEARCH_SOURCE_INTAKE.md)).

| 항목 | 내용 |
|---|---|
| 목적 | read-only external fetch → immutable snapshot → `DateIdSourceRecord` → **기존 8B** 경로 |
| Scout | 변경 없음 — `Date.md` / `ScoutInput` only |
| 종료점 | 여전히 **8I no-write** ([G4](REAL_RESEARCH_SOURCE_INTAKE.md#mandatory-design-guards-g1g4)) |
| KIS | v1 **범위 밖** ([G3](REAL_RESEARCH_SOURCE_INTAKE.md#mandatory-design-guards-g1g4)) |
| 첫 구현 후보 | **FRED** (`FactType.MACRO`) — yfinance/DART는 follow-on |
| 구현 상태 | **1A–2B + 3A + 3A.1 + 3B1 + 3B2 + 3C1 + 3C2 + 3D1 + 3E1 + 3E2 + 3E3 + 3E4 + 3F1 + 3F2 + 3G1 + 3G2 + 3G3-0 + 3G3-1 + 3G3-2 + 3G3-3 + 3G3-4A + 3G3-4B + 3G3-5 + 3G3-6** (source-specific live endpoint adapter); **3G3-6+** live factor scoring / adapter hardening deferred |

- **Real sector pool → mapping chain** — operator-local path exists (3G1 selector + 3G2 helper + 3F1 generator). **3G3-1** fixture ranking and **3G3-2** ranked-mapping helper produce reviewable ranked JSON (advisory metadata only; not trading instruction). **3G3-3** discovery snapshot replay produces candidate pool TOML only. **3G3-4A/4B** fake-transport fetcher and operator HTTP live smoke produce immutable raw discovery snapshot (+ optional candidate pool). **3G3-5** maps source-specific fixture payloads to canonical discovery snapshots (local only). **3G3-6** fetches operator-supplied source-specific endpoints, stores immutable raw source snapshot, maps to canonical snapshot (+ optional candidate pool). Live factor scoring remains **deferred**. Operator approval required before universe generation or downstream smoke ([3G3-0 guardrails](REAL_RESEARCH_SOURCE_INTAKE.md#3g3-0-live-discoveryranking-guardrails-design-only)).

- **Controlled Day 1 walk-through** — Foundation 8B~8I ops chain을 **1회** 수동 검증하고 **8I no-write rehearsal에서 종료**. 30거래일 pilot **시작과 동일하지 않다**. runbook: `docs/RUNBOOK.md` § Controlled Day 1 paper walk-through.
- **Real API fetchers** — Controlled Day 1 **이후** deferred. FRED/DART/yfinance/news HTTP intake는 별도 구현·review 후.
- **30-trading-day paper pilot start** — real API fetchers **또는** repeatable manual intake discipline + explicit readiness decision 이후.
- **KIS read-only `--run`** — broker/write-mode paper loop **이후** deferred (`docs/TECH_DEBT.md` KIS endpoint/TR ID verification 참조).

### Evidence-based automation (8G~8I 내부 helper)

아래는 **미리 구현하지 않고**, manual pilot friction 관측 후 trigger 충족 시 검토한다.

| 후보 | Trigger condition |
|---|---|
| 8H PaperLoopInput assembler | 수동 조립 실수 **2회 이상** |
| optional orchestration beyond 8I | 하루 운영 **60분 초과가 3거래일 연속** |
| DailySummary writer helper | template 누락 **주 2회 이상** |
| Postmortem weekly template helper | tag summary 형식 오류 **2회 이상** |

**Evidence log:** friction 관측 시 `runtime/paper/YYYY-MM-DD/README.md` Manual notes에 기록.

---

## 참고

| 문서 | 용도 |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | ops entrypoint, acceptance, KIS smoke |
| [REAL_RESEARCH_SOURCE_INTAKE.md](REAL_RESEARCH_SOURCE_INTAKE.md) | post-Foundation real source intake v1 design |
| [DEBUG_EVENT_CODES.md](DEBUG_EVENT_CODES.md) | Debug event catalog |
| [POSTMORTEM_ERROR_TAGS.md](POSTMORTEM_ERROR_TAGS.md) | Postmortem tag catalog |
| [TECH_DEBT.md](TECH_DEBT.md) | P3 backlog, Foundation 후속 |
