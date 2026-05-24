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
### Secrets / Config / GitHub Hygiene

- 실제 API key, token, account number, app secret, certificate, private key는 절대 Git에 커밋하지 않는다.
- runtime config는 `config/config.toml`에 두고, 이 파일은 Git에서 ignore한다.
- repository에는 `config/config.toml.example`만 커밋한다.
- `.env`, `.env.*`, `secrets/`, `*.pem`, `*.key`, `*.crt`는 Git에서 ignore한다.
- API key 값 자체를 config에 저장하지 않는다. config에는 환경변수 이름만 저장한다.
  - 예: `fred_api_key_env = "FRED_API_KEY"`
  - 예: `kis_app_secret_env = "KIS_LIVE_APP_SECRET"`
- 실제 환경변수 값 존재 여부 검증은 해당 adapter를 실제 read-only로 실행하는 Phase에서 수행한다.
- unit test에서는 실제 API key, 실제 환경변수, 실제 외부 network를 사용하지 않는다.
- 테스트에는 `TEST_FRED_API_KEY` 같은 가짜 환경변수 이름 문자열만 사용한다.
- runtime artifacts는 Git에 커밋하지 않는다.
  - `*.db`
  - `*.sqlite`
  - `*.sqlite3`
  - `logs/`
  - `memory/`
  - `/data/`
- macOS/Python cache도 Git에 커밋하지 않는다.
  - `.DS_Store`
  - `__MACOSX/`
  - `__pycache__/`
  - `.pytest_cache/`
  - `.venv/`
  - `.uv-cache/`
- `src/data/`는 source code directory이므로 ignore하지 않는다. runtime data directory를 ignore할 때는 `/data/`처럼 root anchored rule을 사용한다.


## MVP 구현 순서

> **Phase numbering policy**
>
> 본 프로젝트의 Phase 번호는 이제 실제 구현 이력 기준으로 고정한다.
> 이전 페이즈는 이미 완료된 구현 단위로 간주하며 되돌리거나 재해석하지 않는다.
>
> Cursor 작업 지시에서 "Phase N"이라고 하면 아래 canonical phase numbering을 따른다.
> legacy phase numbering과 충돌하면 아래 목록을 우선한다.

### Phase 0 — Runtime Config / Live Gate [DONE]

- `config.toml` 기반 typed settings loader
- environment variable substitution
- `trading.mode = paper | live`
- `broker.adapter = paper | kis_live`
- 기본값은 paper mode
- `config.toml` 부재 시 fail-closed
- live mode는 다음 조건을 모두 만족해야만 허용
  - `trading.mode = live`
  - `broker.adapter = kis_live`
  - `allow_live_trading = true`
  - `LIVE_TRADING_CONFIRM` 확인 문구 일치
  - live credentials 존재
- `kis_mock` / `KIS_MOCK_*` 제외

### Phase 1 — Ollama JSON Harness / RunManifest [DONE]

- Ollama HTTP client
- JSON-only runner
- Pydantic validation runner
- raw response / parsed JSON / validation result 분리
- parse error / validation error / Ollama API error / Ollama client error 구분
- markdown/code-fence output은 sanitizer 없이 실패 처리
- `temperature = 0`만 허용
- Ollama request payload에 `think` 명시
- `seed = 42` 기본
- RunManifest
- smoke bench script
- unit test에서 실제 Ollama/network 호출 금지

### Phase 2 — Core Domain Models [DONE]

- `Money`
- `MarketPrice`
- `OrderIntent`
- `OrderResult`
- `Fill`
- `Position`
- `CashSnapshot`
- `PortfolioSnapshot`
- `NavSnapshot`
- `ExecutionMode`는 config enum 재사용
- Decimal 기반 금액/수량 처리
- finite Decimal 검증
- price/quantity/commission/tax validation

### Phase 2.1 — Domain Validation Hardening [DONE]

- 모든 domain datetime은 timezone-aware 강제
- blank string strip + reject
- `OrderIntent.time_in_force = DAY`
- `quantity`와 `target_weight_percent`는 정확히 하나만 허용
- `MARKET` 주문은 `limit_price = None`
- `LIMIT` 주문은 `limit_price > 0` 필수

### Phase 3 — PaperBrokerAdapter / SQLiteLedger / Paper Ledger [DONE]

- `BrokerAdapter` Protocol
- `PaperBrokerAdapter`
- `SQLiteLedger`
- `order_intents`
- `order_results`
- `fills`
- `current_cash`
- `current_positions`
- append-only `paper_cash_ledger`
- `nav_snapshots` 저장
- SQLite serialization rule
  - Decimal → string
  - datetime → ISO 8601 string
  - enum → value string
- 복원 시 domain model validation 재사용
- duplicate `order_id` 중복 체결 금지
- rejected/pending order는 fill/cash/position side effect 금지
- `target_weight_percent` 주문은 broker 단계에서 sizing하지 않고 reject
- `OrderIntent`와 `MarketPrice`의 symbol/market/currency mismatch reject
- LIMIT fill policy
  - MARKET: `fill_price = market_price.price`
  - BUY LIMIT: `market_price.price <= limit_price`일 때 체결, `fill_price = market_price.price`
  - SELL LIMIT: `market_price.price >= limit_price`일 때 체결, `fill_price = market_price.price`
  - Phase 3 단순 모델에는 bid/ask/orderbook이 없으므로 LIMIT과 MARKET의 차이는 PENDING 게이팅뿐
- `slippage = Money.zero(currency)`
- cash mutation public path는 `apply_cash_change()` 하나로 제한
- post-hardening (Phase 3 cleanup, no new features)
  - `apply_cash_change()` validates delta/balance consistency
  - cash mutation public path is restricted to `apply_cash_change()` (`_upsert_cash`, `_append_cash_ledger_entry` are internal)
  - insufficient position SELL does not call fee calculator
  - cash ledger delta sum must match `current_cash`
  - transaction rollback preserves `current_cash` and `paper_cash_ledger` consistency
- 현재 테스트 baseline: `125 passed`

### Phase 4 — Decision Schema / DecisionSnapshot / Replay Foundation [DONE]

목표: LLM 투자 판단과 이후 replay 가능한 실행 흐름의 기반 스키마를 만든다.

구현 대상:

- value objects
  - `Percent`
  - `DateId`
  - `DecisionId`
- evidence/reference model
  - `SourceRef` 또는 `EvidenceRef`
  - `reason`
  - `date_id`
  - optional source metadata
- validation result model
  - schema validation result
  - Date-ID validation result
  - stale validation result
  - rule validation result
- `DecisionSnapshot`
  - `decision_id`
  - `created_at`
  - `schema_name`
  - `raw_payload`
  - `normalized_payload`
  - `validation_result`
  - optional `order_intent_ids`
  - replay metadata
- DecisionSnapshot persistence
- deterministic replay foundation
  - same input → same normalized payload
  - same input → same validation_result
  - same valid decision → same generated intent skeleton, if implemented later
- DebugEvent skeleton, only if needed for validation failure recording

금지:

- 외부 API 호출 금지
- FRED/DART/yfinance 실제 adapter 구현 금지
- Allocator 판단 로직 구현 금지
- Analysis 4역할 판단 로직 구현 금지
- RiskFilter 구현 금지
- OrderIntent 생성 로직 구현 금지
- Scheduler 구현 금지
- KIS 구현 금지

### Phase 5 — Date-ID Store / Evidence Source Layer [DONE]

목표: LLM 판단 근거를 Date-ID로 추적하고 stale 여부를 검증할 수 있는 저장/검증 계층을 만든다.

구현 대상:

- Date-ID record model
- Date-ID storage
- source timestamp
- fact type
  - PRICE
  - FLOW
  - FX
  - NEWS
  - DISCLOSURE
  - MACRO
  - MANUAL
- allowed staleness policy
- Date-ID existence validation
- Date-ID stale validation
- `reasons[].date_id` validation
- Date-ID validation failure는 `ValidationResult` / `ValidationIssue`로 표현한다.
- Debug.md writer 연결은 Phase 12에서 구현한다.
  
금지:

- 실제 FRED/DART/yfinance/news API 호출 금지
- Allocator 구현 금지
- Analysis 구현 금지
- RiskFilter 구현 금지

### Phase 6 — Data API Read-only Adapters [DONE]

목표: 외부 데이터를 read-only로 가져오되, unit test에서는 전부 fake client로 격리한다.

구현 대상:

- yfinance read-only adapter
- FRED read-only adapter
- DART read-only adapter
- adapter protocol
- fake clients for unit tests
- fetched data → Date-ID source record 변환
- source timestamp 저장
- stale validation과 연결

금지:

- unit test에서 실제 외부 네트워크 호출 금지
- news API는 이 Phase 이후로 보류 가능
- trading decision 구현 금지
- broker execution과 직접 연결 금지

### Phase 7 — Scout Input Builder / ScoutSummary Schema [DONE]

목표: Date-ID source layer의 데이터를 Scout 입력으로 조립하고 ScoutSummary JSON schema를 검증한다.

구현 대상:

- Scout input builder
- ScoutSummary schema
- positive / negative / neutral factor structure
- `summary_one_liner`
- all reasons must cite Date-ID
- Date-ID existence/stale validation integration
- JSON validation failure handling

금지:

- 실제 종목 매매 판단 금지
- Allocator 구현 금지
- RiskFilter 구현 금지
- OrderIntent 생성 금지

### Phase 8 — Allocator Decision Schema + Validator [DONE]

목표: 자산군 배분 판단 JSON schema와 Python validator를 만든다.

구현 대상:

- `AllocatorDecision` (final target 중심 schema)
- Signal Summary
- Cash Manager
- Asset Allocator
- Consistency Checker
- `cash_policy.cash_target_percent`
  - 전체 계좌 기준 **최종** 현금 목표
- `target_weights`
  - 현금 제외 운용 자산 기준 **최종** KR/US/Gold 목표
  - KR/US/Gold 합계 100
- gold rule
  - `gold_policy_mode=normal`: 18~22
  - `gold_policy_mode=exception`: 15~25
- all reasons require Date-ID
- invalid Allocator output은 부분 채택하지 않고 전체 폐기

스키마에 포함하지 않음 (Phase 8):

- `action` / `adjust_percent`
- `asset_policies` per-bucket action fields
- `reason_codes`

Phase 10에서 Python이 처리:

- 현재 실제 비중과 target의 차이 계산
- rebalance delta
- OrderIntent 생성

금지:

- 실제 주문 생성 금지
- PaperBroker 실행 금지
- KIS 호출 금지

### Phase 9 — Analysis Decision Schema + Validator [NEXT]

목표: 종목 분석 4역할 JSON schema와 validator를 만든다.

구현 대상:

- `AnalysisDecision`
- Bear perspective
- Bull perspective
- Risk Manager evaluation
- Fund Manager decision
- `summary_one_liner`
- action: BUY / SELL / HOLD
- weight percent
- Date-ID required for reasoning
- validation failure handling

금지:

- Analysis 결과로 직접 broker 호출 금지
- OrderIntent 생성은 Phase 10에서 수행
- RiskFilter 구현 금지

### Phase 10 — RiskFilter + OrderIntent Generation

목표: validated Allocator/Analysis output을 Python hard filter로 검증하고 `OrderIntent`를 생성한다.

구현 대상:

- single position cumulative principal cap: 5%
- invested percent rule
  - NORMAL: production target 70~90%
  - paper observation mode lower bound can be configured down to 50%
- cash band: 10~30%
- asset class soft band
- MDD killswitch thresholds
  - -10% → target cash 50%
  - -15% → target cash 80%
  - -20% → target cash 95%
- directional slippage
  - KR 0.5%
  - US 0.2%
- gold trade frequency
  - monthly 0~2
  - quarterly <= 4
- LLM self-reported confidence must not be used as MVP hard filter
- validated decision → OrderIntent generation

금지:

- KIS live 주문 금지
- live mode 우회 금지
- PaperBroker 외부 가격 조회 금지

### Phase 11 — Paper E2E Loop

목표: LLM 판단부터 PaperBroker 체결까지 replay 가능한 paper loop를 연결한다.

구현 대상:

- ScoutSummary / AllocatorDecision / AnalysisDecision loading
- validation result
- DecisionSnapshot 저장
- RiskFilter
- OrderIntent generation
- PaperBrokerAdapter submit_order
- order/fill/cash/position/nav persistence
- replay test
  - same input → same validation_result
  - same input → same OrderIntent
  - same PaperBroker input → same order/fill/cash/position effect

금지:

- KIS live 주문 금지
- external API calls in unit tests 금지

### Phase 12 — Logs / DailySummary / Debug Events

목표: 운영 및 기술 이벤트를 replay 가능한 로그로 남긴다.

구현 대상:

- DailySummary
- DebugEvent
- Debug.md writer
- technical event vs operational event separation
- replayable event log
- Debug event codes
- Debug.md event codes are not Postmortem `error_tags`

금지:

- Debug.md event code를 runtime LLM prompt에 주입 금지
- Debug.md event code를 Postmortem Top 3 Error Tags에 사용 금지

### Phase 13 — Postmortem

목표: 국장/미장 weekly/monthly postmortem을 분리하고 error_tags를 관리한다.

구현 대상:

- WeeklyPostmortem KR
- WeeklyPostmortem US
- MonthlyPostmortem KR
- MonthlyPostmortem US
- Postmortem `error_tags`
- Top 3 Error Tags는 Postmortem 태그만 집계
- Debug.md는 Postmortem용 `error_tags`를 저장하지 않으며 Top 3에서 제외

### Phase 14 — KIS live read-only / Tiny-live Rehearsal

목표: 실전 계좌는 read-only 검증부터 시작하고, 주문 endpoint는 극소액 수동 tiny-live 직전에만 검증한다.

구현 대상:

- KIS live read-only adapter
- access token
- balance inquiry
- current price inquiry
- orderbook inquiry
- ISA account support check
- `allow_live_trading=false` 상태에서 read-only 검증
- tiny-live manual rehearsal

금지:

- KIS mock adapter 재도입 금지
- KIS 모의투자를 장기 paper ledger로 사용 금지
- ISA smoke test 통과 전 ISA 자동 주문 금지
- live order endpoint 자동 호출 금지

### Phase 15 — Emergency Triggers

목표: parser/validator/paper broker/replay 안정화 후 긴급 트리거를 구현한다.

구현 대상:

- STOCK_DROP
- INDEX_CRASH
- PORTFOLIO_LOSS
- PROFIT_RUN
- TriggerPayload schema
- throttling rule
- emergency Scout context
- MDD_KILLSWITCH는 Python 룰베이스

### Phase 16 — Long Paper Trading Review / Parameter Review

목표: 장기 paper data를 기반으로 파라미터를 검토한다.

구현 대상:

- 3~6개월 paper result review
- MDD threshold review
- execution model review
- asset band review
- Allocator tolerance review

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
