# LLM 자동 주식 매매 프로젝트 — Cursor Rules 패키지

이 패키지는 `auto_trading_report_final_v9_no_kis_mock.md` 기준으로 구현을 시작하기 위한 코딩 규칙과 Cursor Project Rules를 분리한 것이다.

## 설치

프로젝트 루트에 아래 구조를 복사한다.

```text
.cursor/rules/*.mdc
docs/DEBUG_EVENT_CODES.md
CODING_RULES.md
```

Cursor는 공식적으로 프로젝트 규칙을 `.cursor/rules` 디렉터리의 `.mdc` 파일로 관리한다. 이 패키지는 단일 `.cursorrules`가 아니라 여러 개의 `.mdc` 규칙으로 나눠, 작업할 때 필요한 규칙만 컨텍스트에 올리도록 설계했다.

## 사용 원칙

- 대부분의 규칙은 `alwaysApply: false`다.
- 기본으로 항상 적용되는 것은 `00-rule-index.mdc`뿐이다.
- 작업 전 Cursor 채팅에서 필요한 규칙을 직접 언급한다.

예:

```text
@05-allocator.mdc 기준으로 Allocator pydantic schema 작성해줘.
@06-risk-filters-and-orders.mdc 기준으로 주문 전 검증 레이어 만들어줘.
@08-logs-debug-postmortem.mdc 기준으로 WeeklyPostmortem 템플릿 생성해줘.
```

## 규칙 파일 맵

| 파일 | 목적 |
|---|---|
| `00-rule-index.mdc` | 최소 공통 규칙과 규칙 선택 안내 |
| `01-architecture-boundaries.mdc` | LLM/Python/Execution 경계 |
| `02-domain-models-and-state.mdc` | 도메인 모델, 비중 기준, 상태 저장 |
| `03-llm-json-schema-and-prompts.mdc` | LLM JSON 출력, 프롬프트, 검증 |
| `04-scout-date-id-and-data.mdc` | Scout, Date-ID, 데이터 수집 및 stale 검증 |
| `05-allocator.mdc` | Allocator, cash_policy, asset_policies, 금 룰 |
| `06-risk-filters-and-orders.mdc` | 하드 필터, 주문, 슬리피지, MDD |
| `07-scheduler-and-market-calendar.mdc` | 스케줄러, KST/미국장 캘린더 |
| `08-logs-debug-postmortem.mdc` | DailySummary, Debug.md, Postmortem, 태그 |
| `09-testing-paper-trading.mdc` | 테스트, 페이퍼 트레이딩, 리플레이 |
| `10-python-style.mdc` | Python 스타일, 타입, Pydantic, 로깅 |
| `11-runtime-config-and-mode.mdc` | config.toml, paper/live 모드, 안전 게이트 |
| `12-emergency-triggers.mdc` | 긴급 트리거, PROFIT_RUN, MDD 이벤트 우선순위 |
| `13-macos-runtime-environment.mdc` | 최종 실행 환경(macOS/Mac), Ollama 기준선 검증 |
| `14-broker-api-and-paper-broker.mdc` | KIS API, 계좌 라우팅, 자체 PaperBroker ledger, KIS live read-only/tiny-live 단계 |
| `99-forbidden-patterns.mdc` | 금지 패턴, PR 전 점검 |
| `docs/DEBUG_EVENT_CODES.md` | 사람 전용 Debug.md 이벤트 코드 목록. 런타임 LLM 프롬프트에 주입 금지 |


## Debug event codes

`docs/DEBUG_EVENT_CODES.md` defines the human-only event codes for `memory/debug/Debug.md`.

Important:

- These codes are for developer/operator debugging only.
- Do not inject `Debug.md`, `DEBUG_EVENT_CODES.md`, event codes, event counts, or event summaries into runtime LLM prompts.
- Top 3 Error Tags must be aggregated only from Weekly/Monthly Postmortem investment error tags.

## 구현 우선순위

1. Config loader와 도메인 모델
2. LLM JSON 스키마와 validator
3. Date-ID 저장, 존재 검증, stale 검증
4. PaperBrokerAdapter와 내부 paper ledger, replay 가능한 DecisionSnapshot/event log
5. Allocator parser/validator
6. Risk filter와 OrderIntent, ExecutionMode별 검증 정책
7. Scheduler
8. Weekly/Monthly Postmortem
9. KIS live read-only / tiny-live rehearsal
10. Emergency triggers

긴급 트리거는 초기부터 붙이지 않는다. parser/validator/replay test가 먼저 안정화된 뒤 구현한다.

라이브 주문은 이 패키지의 범위 밖이다. 초기 구현은 반드시 페이퍼 트레이딩 전용으로 시작한다.



## ExecutionMode update

Risk validation must distinguish regular LLM-driven orders from Python-owned exceptional flows:

```text
NORMAL | REBALANCING | EMERGENCY_TRIGGER | MDD_KILLSWITCH | MANUAL
```

`bypass_llm=True` means Python skips LLM analysis and LLM-control filters. It does not skip broker/exchange/order-availability checks, live-mode gates, or audit logs.

## Runtime mode

Use `project_scaffold/config/config.toml.example` as the starting point.
Trading mode must be selected through typed config, not by editing `main.py`.

```toml
[trading]
mode = "paper"
allow_live_trading = false
```

Live mode requires an explicit config flag and environment confirmation. See `11-runtime-config-and-mode.mdc`.


## Broker/API/PaperBroker update

- Broker API는 KIS Developers 단일 채택을 기본값으로 한다.
- 분석 데이터는 FRED, DART/OpenDART, yfinance, edgartools/SEC, 뉴스 API를 별도 계층으로 둔다.
- 장기 페이퍼 트레이딩은 내부 `PaperBrokerAdapter`와 자체 ledger가 기준이다.
- KIS 모의투자 adapter는 MVP 기본 경로에서 제외한다. KIS API 검증은 live read-only와 극소액 수동 tiny-live 단계로 진행한다.
- ISA 계좌는 smoke test 통과 전까지 자동 주문 라우팅 대상이 아니다.
