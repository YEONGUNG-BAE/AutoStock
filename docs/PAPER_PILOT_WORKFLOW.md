# Paper Pilot Daily Workflow (Foundation 8A)

30거래일 paper pilot을 시작하기 **전**에, 하루 단위 paper 운용 절차와 산출물 convention을 고정한다.

> **이 문서는 workflow skeleton이다.** 자동 orchestration, collector, scheduler를 구현하지 않는다.  
> Foundation 8B/8C/8D는 며칠간 manual pilot을 돌린 뒤 **관측된 friction**이 있을 때만 evidence-based로 구현한다.

---

## 1. Status and scope

| 항목 | 현재 상태 |
|---|---|
| Automatic daily paper trading system | **아님** — manual + validated paper operation |
| LLM orchestration entrypoint (Scout→Allocator→Analysis 일괄) | **없음** |
| Production PaperLoopInput assembler | **없음** |
| Date.md 자동 생성기 | **없음** |
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
- Date.md 자동 생성기 / export helper는 **아직 없다**. pilot 초기에는 **수동 작성·갱신**한다.

### 파일 convention

| 파일 | 용도 |
|---|---|
| `date/Date.md` | 당일 prompt reference (수동) |
| `date/date_id_sources.jsonl` | store export snapshot (수동 export 또는 향후 helper) |

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

- Production PaperLoopInput assembler **없음**
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

## 12. Current gaps and evidence-based next steps

Foundation **8B / 8C / 8D**는 **미리 구현하지 않는다.**  
며칠간 manual pilot 후 가장 큰 friction을 확인하고, trigger condition을 만족할 때만 구현한다.

| ID | 후보 | Trigger condition |
|---|---|---|
| **8B** | manual JSON intake validator (raw → validated CLI) | 동일 validation copy/paste를 **3거래일 이상** 반복 |
| **8C** | validated PaperLoopInput assembler | PaperLoopInput **수동 조립 실수 2회 이상** |
| **8D** | optional LLM orchestration entrypoint | 하루 운영 시간이 **60분을 3거래일 연속** 초과 |
| — | Date.md export helper | Date.md ↔ store sync 실수 **2회 이상** |
| — | DailySummary writer helper | DailySummary template 누락 필드가 **주 2회 이상** |
| — | Postmortem weekly template helper | weekly close 시 tag summary 형식 오류 **2회 이상** |

**Evidence log:** friction 관측 시 `runtime/paper/YYYY-MM-DD/README.md`의 Manual notes에 기록 → 주간 review에서 8B/8C/8D 우선순위 결정.

---

## 참고

| 문서 | 용도 |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | ops entrypoint, acceptance, KIS smoke |
| [DEBUG_EVENT_CODES.md](DEBUG_EVENT_CODES.md) | Debug event catalog |
| [POSTMORTEM_ERROR_TAGS.md](POSTMORTEM_ERROR_TAGS.md) | Postmortem tag catalog |
| [TECH_DEBT.md](TECH_DEBT.md) | P3 backlog, Foundation 후속 |
