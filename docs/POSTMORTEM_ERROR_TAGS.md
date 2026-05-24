# Postmortem Error Tags Catalog

This file defines investment-decision mistake tags used only by Weekly/Monthly Postmortem.

## Scope

Postmortem `error_tags` are investment decision review tags.

These tags are not Debug.md event codes.

Allowed Top 3 Error Tags sources:
- WeeklyPostmortem tag summaries
- MonthlyPostmortem tag summaries

Disallowed Top 3 Error Tags sources:
- `Debug.md`
- `docs/DEBUG_EVENT_CODES.md`
- `DebugEvent.event_code`
- Debug event counts
- broker/API/runtime/fill technical failures

## Format

- Tags must start with `#`.
- Tags should use Korean snake-style labels, e.g. `#정보_과신`.
- Tags must not be blank.
- Tags must not contain whitespace.
- Tags must be explicitly listed in this catalog.
- Tags classify investment decision mistakes, not technical/runtime failures.

## Tags

| tag | definition | use_when |
|---|---|---|
| `#정보_과신` | 제한적이거나 불확실한 근거를 과도하게 확신해 판단한 경우 | 소수 근거, 약한 뉴스, 불완전한 지표를 강한 매수/매도 논리로 확대한 경우 |
| `#추격_매수` | 가격 상승 후 충분한 검증 없이 뒤늦게 매수한 경우 | 급등 이후 risk/reward 검토 없이 진입했거나 기준가 대비 불리한 구간에서 매수한 경우 |
| `#근거_해석_오류` | Date-ID 근거 자체는 존재했지만 의미를 잘못 해석한 경우 | 실적, 공시, 매크로, 수급 데이터를 반대로 읽었거나 중요도를 오판한 경우 |
| `#논리_일관성_부족` | 제시한 근거와 최종 행동이 일관되지 않은 경우 | bearish 근거가 우세한데 BUY를 냈거나, 리스크를 인정하면서 비중을 늘린 경우 |
| `#손절_지연` | 기존 thesis가 훼손됐는데도 축소/청산 판단이 지연된 경우 | 원래 매수 논리가 무너졌으나 HOLD를 반복하거나 손실 확대를 방치한 경우 |
| `#리밸런싱_지연` | 목표 비중 또는 위험 상태와 실제 포트폴리오 괴리가 장기간 방치된 경우 | Allocator 방향과 실제 비중 차이가 누적됐는데 조정 판단이 늦어진 경우 |
| `#현금_관리_오류` | 시장 환경 대비 현금 비중 판단이 부적절했던 경우 | 과도한 현금 보유로 기회를 놓쳤거나, 위험 국면에서 현금이 부족했던 경우 |
| `#금_비중_판단_오류` | 금 비중 조절 판단이 시장/포트폴리오 상황과 맞지 않았던 경우 | risk-off hedge가 필요한데 금을 줄였거나, 근거 없이 금을 과도하게 늘린 경우 |
| `#벤치마크_오판` | 절대 수익률만 보고 벤치마크 대비 성과를 잘못 평가한 경우 | 가격은 올랐지만 벤치마크를 크게 하회했거나, 하락했지만 상대적으로 방어한 상황을 오판한 경우 |
| `#과도한_보수성` | 근거 있는 기회에도 지나치게 보수적으로 판단한 경우 | risk/reward가 유리하고 룰을 통과했는데 반복적으로 기회를 회피한 경우 |

## Maintenance Rules

- Add new tags only for recurring investment decision mistake patterns.
- Do not add technical, parser, broker, API, fill, config, or runtime failure codes here.
- Do not copy values from `docs/DEBUG_EVENT_CODES.md`.
- Keep tag strings stable once used in stored Postmortem records.
- If a tag is renamed, add migration/backward-compatibility handling explicitly.
