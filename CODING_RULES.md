# 코딩 규칙 — LLM 기반 자동 주식 매매 프로젝트

## 구현 가능성 판단

현재 기획안은 **페이퍼 트레이딩 MVP 구현을 시작할 수 있는 수준**이다. 단, 라이브 자동 주문은 아직 금지한다. 먼저 스키마, Python 검증, 로그, 페이퍼 트레이딩 루프를 구현하고, 최소 1~2개월 이상 리플레이/페이퍼 데이터를 쌓은 뒤 라이브 전환 여부를 판단한다.

## 최종 실행 환경

- 최종 실행 환경은 사용자의 Mac/macOS다.
- 경로 처리는 `pathlib.Path`를 사용하고, Windows 전용 경로 또는 Linux-only 시스템 가정에 의존하지 않는다.
- 로컬 LLM/Ollama, 스케줄러, 파일 권한, 환경변수 처리는 macOS 기준으로 검증한다.
- CUDA 또는 Linux systemd 전제 코드는 작성하지 않는다. macOS 백그라운드 실행이 필요하면 별도 요청 전까지 launchd 구현도 하지 않는다.

## 핵심 원칙

### 1. LLM은 판단만 한다

LLM은 다음만 출력한다.

- 조사 요약
- 종목 매매 의도
- Allocator 판단
- Postmortem 오답노트

LLM은 다음을 하지 않는다.

- 주문 실행
- Python 검증 우회
- 보유 수량 직접 산정
- 증권사 API 직접 호출
- 시스템 룰 변경

### 2. Python은 검증과 실행만 한다

Python은 다음 책임을 가진다.

- `config.toml` 기반 런타임 모드 로딩
- paper/live 실행 모드 안전 게이트 검증
- LLM JSON 파싱
- Pydantic 스키마 검증
- Date-ID 존재 검증
- Date-ID stale 검증
- 비중·현금·금 룰 검증
- MDD 단계 룰과 긴급 트리거 검증
- 하드 필터 적용
- 주문 의도 생성
- 자체 PaperBrokerAdapter 기반 페이퍼 트레이딩 체결 시뮬레이션
- KIS live broker adapter와 내부 PaperBrokerAdapter 분리
- 로그 저장



### 2-1. 실행 모드를 먼저 확정한다

주문 검증은 `ExecutionMode`를 먼저 확정한 뒤 수행한다.

```text
NORMAL | REBALANCING | EMERGENCY_TRIGGER | MDD_KILLSWITCH | MANUAL
```

- `NORMAL`: 운용 하한 70%, 현금 10~30%, 자산군 소프트 밴드, Date-ID 검증을 정상 적용한다.
- `REBALANCING`: 자산군 조정 순서 때문에 현금/운용 비중이 일시적으로 깨질 수 있다.
- `EMERGENCY_TRIGGER`: 손상 포지션을 먼저 줄이기 위해 운용 하한 70%를 일시적으로 깰 수 있다.
- `MDD_KILLSWITCH`: LLM 없이 Python 룰베이스로 청산한다.
- 어떤 모드에서도 거래정지/주문 가능 여부, 브로커 API 안전장치, 라이브모드 게이트, 감사 로그 저장은 우회하지 않는다.

`bypass_llm=True`는 LLM 분석 및 LLM 환각 방어 계층을 우회한다는 의미이며, 브로커/거래소/라이브모드 안전장치를 우회한다는 의미가 아니다.

### 3. 프롬프트에 성과 목표를 직접 넣지 않는다

기획서의 수익률 목표는 평가 기준일 뿐이다. 조사, 분석, Allocator, Postmortem 프롬프트에는 목표 수익률을 직접 넣지 않는다.

### 4. 근거는 Date-ID로 추적한다

LLM이 투자 판단을 출력할 때는 반드시 `reasons[].date_id`를 포함한다. Date-ID가 없거나 Date.md에 존재하지 않으면 해당 출력은 실패로 본다.

### 5. 오류 응답은 일부 채택하지 않는다

Allocator 또는 분석 LLM 출력에서 명백한 환각, 스키마 오류, 룰 위반이 발생하면 해당 응답을 부분 채택하지 않는다. 특히 Allocator에서 금 목표 비중이 허용 범위 15~25%를 벗어나면 전체 응답을 폐기하고 `previous_targets`를 유지한다.

## 추천 프로젝트 구조

```text
src/
  domain/              # Pydantic models, enums, value objects
  data/                # analysis-data collectors, Date-ID, market/macro/disclosure/news adapters
  scout/               # Scout input/output orchestration
  llm/                 # Ollama client, prompt runner, JSON parser
  prompts/             # prompt templates
  allocator/           # allocator pipeline and validators
  analysis/            # KR/US stock analysis pipeline
  risk/                # hard filters, soft bands, MDD, slippage
  broker/              # BrokerAdapter, PaperBroker, KIS live adapter
  execution/           # order intent, fill simulator/orchestrator
  scheduler/           # KST + exchange calendar scheduler
  logs/                # DailySummary, Debug, Postmortem writers
  postmortem/          # weekly/monthly postmortem generation
  config/              # typed settings and config.toml loader
  tests/               # unit/integration/replay tests
```

## MVP 구현 순서

### Phase 0 — 런타임 설정

- `config.toml` 작성
- `src/config.py` typed loader 작성
- 기본값은 `paper` 모드
- live 모드는 `mode = "live"` + `allow_live_trading = true` + 환경변수 확인이 모두 필요

### Phase 1 — 스키마와 상태

- `Percent`, `DateId`, `DecisionId` 모델 작성
- `AllocatorDecision`, `AnalysisDecision`, `ScoutSummary` Pydantic 모델 작성
- `DailySummary`, `DebugEvent`, `PostmortemRecord`, `DecisionSnapshot` 저장 모델 작성

### Phase 2 — LLM 출력 검증

- JSON-only parser
- markdown fence 제거 방어
- enum 검증
- Date-ID 존재 검증
- Date-ID stale 검증
- `reasons[].date_id` 검증
- 실패 시 Debug.md 기술 이벤트 기록

### Phase 3 — Allocator

- Signal Summary → Cash Manager → Asset Allocator → Consistency Checker → Final JSON
- `cash_policy.cash_target_percent`: 전체 계좌 기준
- `target_weights`: 현금 제외 운용 자산 기준, KR/US/Gold 합계 100
- 금 평상시 18~22, 예외 15~25

### Phase 4 — 페이퍼 실행과 리플레이

- OrderIntent 모델 작성
- PaperBrokerAdapter 체결 시뮬레이션
- `paper_orders`, `paper_fills`, `paper_positions`, `paper_cash_ledger`, `paper_nav_snapshots` ledger 작성
- DecisionSnapshot 저장
- 같은 입력에서 같은 validation_result와 order_intent가 나오는 replay test 작성

### Phase 5 — Risk filters

- 단일 종목 누적 매수 원금 기준 5%
- 운용 부분 프로덕션 목표 70~90% (`NORMAL` 모드 하드 필터)
- `REBALANCING`/`EMERGENCY_TRIGGER`/`MDD_KILLSWITCH`에서는 운용 하한 일시 이탈 허용 후 복구 리뷰 기록
- 페이퍼 관찰 모드 하한은 명시적 config로 50%까지 완화 가능
- 현금 10~30%
- 자산군 소프트 밴드
- MDD 킬스위치 (-10%/50%, -15%/80%, -20%/95%)
- 방향성 슬리피지 (KR 0.5%, US 0.2%)
- 금 매매 빈도 (월 0~2회, 분기 4회 이하)
- 초기 MVP에서는 LLM 자기평가식 confidence를 하드필터로 사용하지 않음

### Phase 6 — 로그

- DailySummary 작성
- Debug.md 기술/운영 이벤트 작성
- 리플레이 가능한 이벤트 로그

### Phase 7 — Postmortem

- 국장/미장 WeeklyPostmortem 분리
- 국장/미장 MonthlyPostmortem 분리
- Top 3 Error Tags는 Postmortem 태그만 집계
- Debug.md는 Postmortem용 `error_tags`를 저장하지 않으며 Top 3에서 제외

### Phase 8 — KIS live read-only / tiny-live rehearsal

- KIS 모의투자 adapter는 MVP 기본 경로에서 제외
- live 계좌는 먼저 `allow_live_trading=false` 상태에서 read-only 조회만 검증
- access token, 잔고조회, 현재가/호가, ISA 계좌 조회를 먼저 검증
- 주문 endpoint 검증은 실전 전환 직전 극소액 수동 tiny-live에서만 수행
- ISA 계좌 smoke test 통과 전까지 ISA 자동 주문 금지

### Phase 9 — Emergency triggers

- STOCK_DROP, INDEX_CRASH, PORTFOLIO_LOSS, PROFIT_RUN
- 초기 parser/validator/paper broker/replay test 안정화 후 구현

## PR 체크리스트

- [ ] LLM 출력은 Pydantic으로 검증되는가?
- [ ] Date-ID가 Date.md에 존재하고 stale 검증을 통과하는가?
- [ ] LLM reasoning 원문을 검증 호출에 재주입하지 않는가?
- [ ] LLM 출력이 Python 검증을 우회하지 않는가?
- [ ] `ExecutionMode` 예외가 LLM 통제용 필터와 브로커/거래소 안전장치를 구분하는가?
- [ ] 금 비중 15~25 룰과 금 매매 빈도 룰을 검증하는가?
- [ ] `target_weights` 합계가 100인가?
- [ ] `cash_target_percent`는 전체 계좌 기준인가?
- [ ] Debug.md와 Postmortem 태그 집계가 분리되어 있고, Debug.md에는 Postmortem용 `error_tags`가 저장되지 않는가?
- [ ] 미국장 시간을 KST 하드코딩하지 않는가?
- [ ] MDD 단계 룰과 긴급 트리거 임계값이 config/domain 상수와 일치하는가?
- [ ] LLM 자기평가식 confidence를 MVP 하드필터로 사용하지 않는가?
- [ ] 분석 결과에서 broker를 직접 호출하지 않고 OrderIntent를 거치는가?
- [ ] DecisionSnapshot으로 replay 가능한 로그를 남기는가?
- [ ] KIS 모의투자 adapter가 MVP 기본 경로에서 제외되어 있고, 장기 paper performance ledger는 자체 PaperBroker인가?
- [ ] ISA 자동 주문 라우팅 전 balance/order/fill smoke test를 통과했는가?
- [ ] KIS/FRED/DART/news API 키와 계좌번호가 repo에 커밋되지 않는가?
- [ ] 라이브 주문 경로가 기본 비활성화되어 있는가?
- [ ] `main.py`가 trading mode를 하드코딩하지 않는가?
- [ ] `config.toml` 없거나 파싱 실패 시 live mode로 fallback하지 않는가?
- [ ] live mode는 `allow_live_trading` + 환경변수 확인 없이는 시작되지 않는가?


## Debug event code rule

Use `docs/DEBUG_EVENT_CODES.md` for human-only Debug.md event codes. These are not Postmortem `error_tags`, are not Top 3 Error Tag inputs, and must not be injected into runtime LLM prompts.

- When refactoring or rewriting existing code, preserve all existing validation rules and safety invariants. Do not silently weaken gates such as temperature=0, live trading gates, fail-closed config loading, or kis_mock exclusion. Report explicitly that existing safety invariants were preserved.
