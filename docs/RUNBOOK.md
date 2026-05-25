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

- 10개 check 모두 `[PASS]`
- Summary: `10 PASS, 0 WARN, 0 FAIL`
- exit code `0`

**pytest baseline:** `872 passed` (acceptance check 내부 Check 1)

**실패 시:** 다음 운용 단계(Ollama smoke, Date.md 갱신, PaperLoop one-shot 등)로 **진행하지 않는다**. FAIL 원인을 해결한 뒤 acceptance check를 재실행한다.

WARN은 exit code 1을 만들지 않지만, pytest baseline mismatch(`872 passed` 미포함)는 baseline drift 가능성이 있으므로 원인을 확인한다.

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

- **Date.md 자동 생성기는 이번 단계에서 구현하지 않는다.**
- Date.md 작성/갱신은 **우선 수동 운영 절차**로 둔다.
- Date-ID stale validation은 **Python validation layer**가 담당한다.
- Date-ID가 없는 LLM 판단은 **부분 채택하지 않는다** — Allocator/Analysis 출력 전체를 폐기하고 `previous_targets` 또는 안전 상태를 유지한다.

### 수동 갱신 절차 (개요)

1. 당일 수집한 근거(뉴스, 공시, 매크로 등)를 `SQLiteDateIdSourceStore`에 기록한다.
2. 해당 record를 기반으로 `Date.md`에 export 항목을 수동 추가/갱신한다.
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

**입력:** `PaperLoopInput` JSON은 upstream Layer A에서 생성/검증되어야 한다. sample 생성기는 후속 작업에서 별도 구현한다. 구조 참고는 `tests/test_paper_loop_runner.py`만 본다.

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

**Collector가 아님:** 이 script는 ledger/log/postmortem/emergency store를 자동으로 읽지 않는다. `PaperReviewInput` JSON은 후속 collector 또는 수동 export 절차로 준비한다. sample generator는 후속 작업에서 별도 구현한다. 구조 참고는 `tests/test_paper_review_*.py`만 본다.

**기본 실행:** input 검증 + in-memory report 생성 + text summary 출력. 파일 write 없음.

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

> **이번 단계에서 KIS smoke script는 구현하지 않는다.** 아래는 수동 운용 절차만 기록한다.

### P3 backlog

- KIS endpoint / TR ID는 **공식 문서 대조 전까지 P3 backlog**다 (`docs/TECH_DEBT.md` 참조).

### read-only smoke 절차

1. `config/config.toml`에서 live gates가 **비활성** 상태인지 확인한다 (`trading.mode=paper`, `allow_live_trading=false`).
2. KIS credentials는 **환경변수**로만 제공 — repo에 commit하지 않는다.
3. **`broker.kis_read_only.enabled=true`는 자동 실행 trigger가 아니다** — 명시적 수동 호출만 허용.
4. 확인 대상: **balance / positions / current price / orderbook** (read-only).
5. **order endpoint 호출 금지.**
6. **secrets / account numbers commit 금지.**

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
| `CODING_RULES.md` | Phase numbering, 핵심 원칙 |
| `docs/DEBUG_EVENT_CODES.md` | Debug event catalog (human-only) |
| `docs/POSTMORTEM_ERROR_TAGS.md` | Postmortem error tag catalog |
| `docs/TECH_DEBT.md` | P2/P3 backlog |
| `.cursor/rules/08-logs-debug-postmortem.mdc` | Log/Postmortem 규칙 |
| `.cursor/rules/11-runtime-config-and-mode.mdc` | Runtime config / live gate |
| `.cursor/rules/14-broker-api-and-paper-broker.mdc` | Broker / PaperBroker 규칙 |
