# Controlled Day 1 Operator Checklist

## 1. Purpose

- **Controlled Day 1은 30-trading-day paper pilot 시작이 아니다.** pilot 일정·KIS read-only·write-mode paper loop 시작과 혼동하지 않는다.
- 이 문서는 Foundation **8B~8I** chain을 operator가 **1회 수동**으로 walk-through하기 **전**에 읽는 **사전 계획** 문서다. 실제 실행은 별도 human action이다.
- **종료 지점은 8I no-write rehearsal**이다. PASS 후에도 Scout/Allocator/Analysis 이후 단계로 **자동 진행하지 않는다**.
- 이 문서는 [`docs/RUNBOOK.md`](RUNBOOK.md)의 **Controlled Day 1 paper walk-through / Step-by-step command flow**를 **대체하지 않는다.** canonical shell command·경로·플래그는 RUNBOOK만 따른다. 여기서는 **실행 전 계획**, **중단(abort) 기준**, **증거(evidence) 수집**만 다룬다.
- **0A readiness-contract**(`tests/test_controlled_day1_readiness.py`)와 역할이 다르다. readiness 테스트는 docs-contract 정적 검증이며, 본 체크리스트는 **운영자 현장 절차**용이다.

**안전 경계 (문서 전역):**

- 실거래, broker 주문, KIS 호출, write-mode `ops/run_paper_once.py`, `PaperLoopRunner.run()` 경로를 **만들거나 호출하지 않는다.**
- external API fetch, secret·env 값 출력·문서화, checked-in fixture 편집, `runtime/` 산출물 git commit을 **하지 않는다.**
- ranking/factor는 advisory metadata로만 취급하며 buy/sell/hold/allocation/order/action을 **생성하지 않는다.**

---

## 2. Non-executable operator command plan

**실행 가능한 shell command block을 이 문서에 쓰지 않는다.** (`PYTHONPATH=...`, `uv run python ...` 전체 명령 재기재 금지.)

**Canonical command source:** [`docs/RUNBOOK.md`](RUNBOOK.md) — **## Controlled Day 1 paper walk-through** → **### Step-by-step command flow**

각 행은 copy-paste 명령이 아니라 operator 의도·RUNBOOK step·입출력 artifact·gate·abort만 정리한다. script 이름은 RUNBOOK 참조 수준으로만 언급한다.

| Phase | Operator intent | Canonical RUNBOOK step reference | Required input artifacts | Expected local-only output artifacts | Gate before continuing | Abort trigger |
|---|---|---|---|---|---|---|
| Pre-walk-through regression gate | 회귀·acceptance baseline이 현재 문서화 값과 일치하는지 확인한 뒤 walk-through 시작 | Prerequisites — acceptance gate (`§2 Acceptance check`, `./ops/acceptance_check.sh`) | clean git working tree(의도된 docs 변경만); 로컬 pytest/acceptance 환경 | acceptance stdout/summary (baseline **`2591 passed`**, summary **`11 PASS, 0 WARN, 0 FAIL`**) | acceptance **PASS**; baseline mismatch 없음 | acceptance failure; baseline ≠ `2591 passed`; operator가 RUNBOOK 밖 command로 gate를 대체하려는 판단 |
| Runtime workspace preparation | `DAY` 확정, runtime 디렉터리·universe·portfolio·paper_loop context·operator-prepared research JSONL 준비 | **#### A. Prepare runtime directories**; Runtime directory convention; Prerequisites 표 | `DAY`; operator-prepared `runtime/research/${DAY}/research_sources.jsonl`; (없으면 example에서) universe/portfolio/context | `runtime/research/${DAY}/`, `runtime/paper/${DAY}/` 하위 scout/allocator/analysis/portfolio/paper_loop/rehearsal; `runtime/paper/universe.paper.toml`(또는 example 복사); portfolio_state·paper_loop_context JSON | 필수 경로·파일 존재; JSONL **synthesize 안 함** | required runtime input missing; missing JSONL을 script로 invent; RUNBOOK에 없는 준비 command 필요 판단 |
| 8B Research source intake | manual JSONL → store → Date.md (real fetcher 없음) | **#### B. 8B — Research source intake** (`ops/research_source_intake.py` via RUNBOOK 8B command) | `research_sources.jsonl` | `date_id_sources.sqlite3`, `Date.md` | **Gate:** exit 0, `date_id_sources.sqlite3` 및 `Date.md` 생성 | research JSONL shape/store/Date.md gate failure; acceptance/regression 이슈 재발 |
| 8C Date.md smoke | universe + store coverage로 Date.md smoke | **#### C. 8C — Date.md smoke** (`ops/run_date_md_smoke.py` via RUNBOOK 8C command) | universe TOML, `Date.md`, `date_id_sources.sqlite3` | stdout JSON (parseable) | **Gate:** exit 0, stdout JSON parseable; symbol coverage 요구 충족 | Date.md smoke failure; symbol coverage failure |
| 8D Scout packet/manual LLM handoff | Scout packet·prompt 생성 후 **수동** LLM paste·raw JSON 저장 | **#### D. 8D** — packet build + Manual LLM handoff (RUNBOOK 8D; LLM은 ops가 호출하지 않음) | Date.md, store, universe | `scout_input.json`, `scout_prompt.md`, suggested raw path (예: `scout_output.kr.raw.json`) | packet build exit 0; raw JSON 파일 **수동 저장 완료** | raw LLM output missing; automatic LLM orchestration 시도; RUNBOOK 외 handoff 경로 |
| 8E Scout raw JSON validation | Scout raw → validated JSON + summary | **#### D. 8E validation** (`ops/validate_scout_raw_json.py` via RUNBOOK) | raw JSON, `scout_input.json`, Date.md, store | `scout_output.validated.json`, validation logs/summary | **Gate:** `scout_output.validated.json` 생성 | validator failure; validated JSON manual edit 필요; Date-ID membership/staleness/citation failure |
| 8F Allocator packet/manual LLM handoff/validation | Allocator packet → manual LLM → validation (`--store` required) | **#### E. 8F** — packet, handoff, validation (`ops/build_allocator_manual_packet.py`, `ops/validate_allocator_raw_json.py` via RUNBOOK) | validated scout, scout validation summary, portfolio_state, Date.md, store, universe | `allocator_input.json`, `allocator_prompt.md`, `allocator_output.raw.json`, `allocator_output.validated.json`, validation summary | **Gate:** `allocator_output.validated.json` 생성 | raw LLM missing; validator failure; manual edit 필요; Date-ID failure; `--store` 누락 등 RUNBOOK 요구 위반 |
| 8G Analysis packet/manual LLM handoff/validation | Analysis packet(reason object schema) → manual LLM → per-symbol validation | **#### F. 8G** — packet, handoff, validation (`ops/build_analysis_manual_packet.py`, `ops/validate_analysis_raw_json.py` via RUNBOOK) | validated scout/allocator, summaries, portfolio, Date.md, store, universe, symbol | `analysis_input.*.json`, `analysis_prompt.*.md`, raw JSON, `analysis_output.*.validated.json`, validation summary | **Gate:** `analysis_output.kr.SYNTH-KR-0001.validated.json` 생성 (reason **object** 배열) | validator failure; reasons as strings; manual edit; Date-ID failure |
| 8H PaperLoopInput assembly | validated chain → PaperLoopInput 조립 (**실행 없음**) | **#### G. 8H** (`ops/assemble_paper_loop_input.py` via RUNBOOK); assembly log 확인 | validated scout/allocator/analysis, portfolio_state, paper_loop_context, Date.md, store | `paper_loop_input.*.json`, `paper_loop_input_assembly.*.txt`/summary | assembly exit 0; log에 8H guard lines 전부 존재 (§4 Evidence 참조) | assembly guard text missing; gate literal 불일치; execution/broker 징후 |
| 8I No-write rehearsal | DB before/after capture 후 validation-only rehearsal | **#### H. 8I** (`ops/rehearse_paper_loop_no_write.py --no-write` via RUNBOOK); rehearsal log·DB hash | `paper_loop_input.*.json`, Date.md, store, ledger/decision DB paths | rehearsal json/txt/summary; before/after DB ls/shasum 기록 | stdout `status == "ok"`; 8I guard lines 전부 (§4 Evidence); DB unchanged | 8I stdout/status가 validation-only가 아님; ledger/decision DB changed; write-mode·broker·KIS·PaperLoopRunner.run 징후 |
| Post-8I stop boundary | PASS여도 다음 설계·pilot·write-mode로 **진행하지 않음** | **### Next-step boundary (after Day 1)**; **### Success criteria** | 8I PASS 증거 세트 완료 | 없음 (새 write-path artifact **생성 안 함**) | §4 Evidence checklist 전부 체크; §5 boundary 확인·서명 | PASS 후 자동 다음 단계 시도; `git ls-files runtime` 비어 있지 않음; runtime commit |

---

## 3. Abort criteria

아래 **하나라도** 해당하면 **즉시 중단**한다. 해당 step부터 RUNBOOK 절차에 따라 수정·재실행하기 **전에** 증거를 보존한다.

| Abort condition | What to preserve as evidence | What not to do |
|---|---|---|
| acceptance baseline mismatch 또는 acceptance failure | `acceptance_check.sh` stdout/stderr 전체; 요약 줄; 실행 시각 | baseline 숫자(2591)를 문서·스크립트에 임의 수정; gate 우회 |
| required runtime input artifact missing | `DAY`; `git status -uall --short`; 누락 경로 목록 | missing JSONL/raw LLM/placement **synthesize**; checked-in fixture로 대체 |
| research JSONL shape/store/Date.md gate failure | 8B stdout/stderr; JSONL 일부( secret 없이); validate-only 출력 | store/Date.md를 손으로 편집해 gate 통과 |
| Date.md smoke failure 또는 symbol coverage failure | 8C stdout JSON; universe TOML 경로; coverage 메시지 | `--require-symbol-coverage` 등 RUNBOOK 요구 **disable** |
| raw LLM output missing | 8D/8F/8G prompt path; handoff 시각; 빈/미생성 raw path | validator에 빈 raw 제출; 자동 LLM orchestration |
| validator failure | raw JSON; validator stdout/stderr; validation summary | validator **bypass**; validated JSON **manual edit** |
| validated JSON manual edit 필요 상황 | 실패 summary; raw vs validated diff(로컬만) | hand-edit validated; upstream 수정 없이 재검증만 반복 |
| Date-ID membership/staleness/citation validation failure | Date.md 발췌; store record id; citation 필드 | membership check off; stale evidence를 prompt에 유지 |
| PaperLoopInput assembly guard text missing | `paper_loop_input_assembly.*.txt` 전체(또는 head); assembly stdout | 8H를 PASS로 간주하고 8I 진행 |
| 8I stdout/status가 expected validation-only state가 아님 | 8I `--json` stdout; rehearsal txt; exit code | write-mode rehearsal; status를 ok로 오해 |
| ledger DB 또는 decision DB가 absent/unchanged invariant 위반 | before/after `ls -l`, `shasum -a 256` 출력 쌍 | DB 삭제 후 재실행으로 hash 맞추기; side effect 원인 미조사 재개 |
| `git ls-files runtime` 결과가 비어 있지 않음 | `git ls-files runtime` 출력; `git status -uall --short` | runtime artifact **commit**; `.gitignore` 우회 track |
| external API, KIS, broker, write-mode paper loop, `PaperLoopRunner.run()` 사용 징후 | 관련 log grep; command history(로컬); 8H/8I guard 부재 | “한 번만” live/read-only/write 호출; broker adapter 실험 |
| operator가 RUNBOOK에 없는 command를 보충 실행해야 한다고 판단 | 시도하려던 command 메모; RUNBOOK step 대비 gap | ad-hoc ops/src/script 실행; 본 문서에 없는 **새** 실행 경로 개설 |

**공통 보존:** 해당 phase stdout/stderr, ops `--json` stdout, 관련 `runtime/...` log/txt path, 실패 직전 `git status`.

**공통 금지:** secret 복사·문서화; `runtime/` commit; checked-in fixture·acceptance baseline 변경; validator bypass.

---

## 4. Evidence checklist

Controlled Day 1 **PASS** 판정 전 operator가 수집·확인할 증거다. 각 항목을 완료하면 `[x]`로 표시한다.

### Regression · environment

- [ ] acceptance gate 결과: baseline **`2591 passed`** 및 acceptance summary **`11 PASS, 0 WARN, 0 FAIL`** (RUNBOOK Prerequisites / §2)
- [ ] **`DAY`** 값 기록 및 RUNBOOK **Runtime directory convention** 경로와 일치
- [ ] `git ls-files runtime` — **empty output** (RUNBOOK Git safety)
- [ ] `git status -uall --short` — runtime tracked 파일 없음; docs만 의도적 변경

### 8B · 8C research layer

- [ ] `runtime/research/${DAY}/research_sources.jsonl` 존재 (operator-prepared)
- [ ] `runtime/research/${DAY}/date_id_sources.sqlite3` 존재
- [ ] `runtime/research/${DAY}/Date.md` 존재
- [ ] 8C Date.md smoke **PASS** (exit 0, stdout JSON parseable)
- [ ] 8C symbol coverage **PASS** (`--require-symbol-coverage` 충족)

### 8D · 8E Scout

- [ ] Scout packet: `scout_input.json`, `scout_prompt.md` 존재
- [ ] Scout raw output 존재 (예: `scout_output.kr.raw.json`)
- [ ] Scout validated: `scout_output.validated.json` 존재
- [ ] Scout validation summary/log 존재

### 8F Allocator

- [ ] Allocator packet: `allocator_input.json`, `allocator_prompt.md` 존재
- [ ] Allocator raw output 존재
- [ ] `allocator_output.validated.json` 존재
- [ ] Allocator validation summary 존재

### 8G Analysis

- [ ] Analysis packet: `analysis_input.*.json`, `analysis_prompt.*.md` 존재
- [ ] Analysis raw output 존재
- [ ] `analysis_output.kr.SYNTH-KR-0001.validated.json` 존재
- [ ] Analysis validation summary 존재
- [ ] reasons가 **object 배열**임을 validation summary로 확인 (string 배열 아님)

### 8H PaperLoopInput assembly

- [ ] `paper_loop_input.*.json` 및 assembly txt/summary 존재
- [ ] assembly log에 아래 guard lines **verbatim** (RUNBOOK 8H Gate — 표현·대소문자·구두점 변경 금지):
  - [ ] `PaperLoopInput model validation: PASS`
  - [ ] `execution: NOT RUN`
  - [ ] `order generation: NOT RUN`
  - [ ] `broker: NOT CALLED`
  - [ ] `KIS: NOT CALLED`

### 8I No-write rehearsal

- [ ] rehearsal stdout JSON: `status == "ok"`
- [ ] rehearsal stdout JSON: `run_paper_once_status == "VALIDATION_ONLY"`
- [ ] rehearsal txt/log에 아래 guard lines **verbatim** (RUNBOOK 8I Gate — paraphrase 금지):
  - [ ] `run_paper_once --no-write: PASS`
  - [ ] `ledger_db unchanged: PASS`
  - [ ] `decision_db unchanged: PASS`
  - [ ] `PaperLoopRunner.run: NOT CALLED`
  - [ ] `PaperBroker: NOT CALLED`
  - [ ] `KIS: NOT CALLED`
  - [ ] `Order generation: NOT RUN`
  - [ ] `Execution artifacts: NOT CREATED`
- [ ] before/after **ledger DB** evidence: absent 또는 byte/hash-identical (`ls -l`, `shasum -a 256` 쌍)
- [ ] before/after **decision DB** evidence: absent 또는 byte/hash-identical (`ls -l`, `shasum -a 256` 쌍)

### Chain closure · stop acknowledgement

- [ ] external API / KIS / broker / write-mode paper loop **미사용** (RUNBOOK Success criteria)
- [ ] final **“stop after 8I”** acknowledgement — operator가 Post-8I boundary(§5)를 읽고 다음 단계 자동 진행하지 않음을 명시적으로 확인

---

## 5. Post-8I boundary

Controlled Day 1 **PASS 후에도** 아래는 **자동으로 진행하지 않는다.** 다음 설계·구현은 **별도 Claude 검증** 및 **사용자 명시 요청** 후에만 다룬다.

| Do not auto-start after PASS | Notes |
|---|---|
| real API fetcher implementation | FRED/DART/yfinance/news HTTP ops — design/PR 별도 |
| 30-trading-day paper pilot start | Controlled Day 1 ≠ pilot start |
| KIS read-only run | credentials·`--run` 별도 readiness |
| write-mode `ops/run_paper_once.py` | validation-only 8I와 구분 |
| `PaperLoopRunner.run()` | no-write rehearsal에서 NOT CALLED 유지 |
| broker order submission | 실거래·모의 주문 경로 아님 |
| ledger/decision DB writes | 8I invariant: unchanged |
| fills, NAV snapshots, daily summary, postmortem | write-mode paper loop 산출물 |

**다음 설계 참고(자동 진행 아님):** RUNBOOK **### Next-step boundary (after Day 1)** — Real Research Source Intake v1 등은 별도 PR·readiness decision 이후. walk-through 종료점은 여전히 **8I no-write**이다.

**Operator 최종 확인 문구 (서명·타임스탬프 권장):**

> Controlled Day 1 PASS. 8I no-write rehearsal까지 완료했으며, §5 항목으로 자동 진행하지 않는다. Canonical commands는 RUNBOOK Step-by-step만 사용했고, runtime 산출물은 commit하지 않는다.
