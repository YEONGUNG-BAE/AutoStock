# Debug.md Event Code Catalog

This file defines the human-only event codes used in `memory/debug/Debug.md`.

## Scope

`Debug.md` is a technical/operational debugging log. These codes are for the developer/operator to inspect implementation, parser, broker, runtime, and policy-blocking issues.

These codes are **not** Postmortem `error_tags` and must **not** be used for Top 3 investment error tag aggregation.

## LLM prompt rule

Do **not** inject this file, `Debug.md`, or Debug event codes into runtime LLM prompts for Scout, Analysis, Allocator, or Postmortem.

Allowed runtime LLM feedback source:

- Weekly/Monthly Postmortem investment error tags only

Disallowed runtime LLM feedback sources:

- `Debug.md`
- `DEBUG_EVENT_CODES.md`
- `event_code`
- Debug event counts
- Debug event summaries

## Debug.md entry format

```md
## <event_id>
- timestamp_kst: 2026-05-05T08:20:00+09:00
- source: Allocator
- severity: HIGH
- event_code: ALLOCATOR_GOLD_TARGET_OUT_OF_RANGE
- detail: gold target 30% exceeded allowed 15~25% range
- action_taken: REJECTED_AND_FALLBACK
- fallback: previous_targets maintained
- related_file: memory/allocator/raw/2026-05-05-allocator-raw.json
- human_note: Optional developer note
```

## Severity

| severity | Meaning |
|---|---|
| `LOW` | Benign event or expected policy block. Monitor only. |
| `MEDIUM` | Operational issue or recoverable validation failure. Review soon. |
| `HIGH` | LLM output or execution issue caused rejection/fallback or blocked trading action. Review before relying on automation. |
| `CRITICAL` | Runtime, config, broker, or safety-gate issue that can invalidate the session. Stop or manually inspect. |

## Source values

| source | Meaning |
|---|---|
| `Scout` | Data gathering / Date-ID generation layer |
| `Analysis` | KR/US stock analysis LLM layer |
| `Allocator` | Cash/asset allocation LLM layer |
| `PythonValidator` | Pydantic/Python rule validation layer |
| `RiskFilter` | Hard filter, slippage, MDD, emergency-trigger layer |
| `Broker` | Live broker adapter layer |
| `PaperBroker` | Paper trading adapter / fill simulation layer |
| `Scheduler` | Calendar/time orchestration layer |
| `Config` | Runtime config / environment gate layer |
| `DataAdapter` | yfinance/FRED/KRX/news/API adapter layer |
| `Runtime` | Generic runtime exception or unexpected process error |

---

# Event codes

## 1. LLM output / schema events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `LLM_JSON_PARSE_ERROR` | HIGH | LLM response could not be parsed as JSON. | Reject response; keep previous safe state. |
| `LLM_SCHEMA_ERROR` | HIGH | Parsed JSON failed the required Pydantic/schema shape. | Reject response; inspect prompt/schema. |
| `LLM_ENUM_INVALID` | MEDIUM | Output used an enum value outside the allowlist. | Reject response or field; update prompt if recurring. |
| `LLM_FIELD_MISSING` | HIGH | Required output field was missing. | Reject response. |
| `LLM_FIELD_TYPE_INVALID` | HIGH | Output field had the wrong type. | Reject response. |
| `LLM_MARKDOWN_WRAPPER_ERROR` | MEDIUM | LLM wrapped JSON with markdown or extra prose despite JSON-only instruction. | Strip only if explicitly safe; otherwise reject. |

## 2. Date-ID / evidence events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `DATE_ID_MISSING` | HIGH | A required `reasons[].date_id` field is empty or absent. | Reject affected LLM decision. |
| `DATE_ID_NOT_FOUND` | HIGH | Date-ID was cited but not found in `Date.md`. | Reject affected LLM decision. |
| `DATE_ID_FORMAT_INVALID` | MEDIUM | Date-ID does not match expected `YYMMDD-N` format. | Reject or normalize only if unambiguous. |
| `EVIDENCE_CONTRADICTION` | HIGH | Cited evidence contradicts the decision direction. | Reject response; inspect Date.md and prompt. |
| `EVIDENCE_STALE` | MEDIUM | Evidence is outside allowed freshness window for the decision. | Reject or downgrade decision depending on rule. |

## 3. Allocator output events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `ALLOCATOR_CASH_TARGET_OUT_OF_RANGE` | HIGH | `cash_policy.cash_target_percent` outside 10~30% in non-MDD context. | Reject whole Allocator response; keep previous targets. |
| `ALLOCATOR_TARGET_SUM_INVALID` | HIGH | `target_weights.kr + us + gold != 100`. | Reject whole Allocator response; keep previous targets. |
| `ALLOCATOR_GOLD_TARGET_OUT_OF_RANGE` | HIGH | Gold target outside 15~25%. | Reject whole Allocator response; keep previous targets. |
| `ALLOCATOR_GOLD_EXCEPTION_REASON_MISSING` | MEDIUM | Gold target outside normal 18~22% but lacks valid risk-on/risk-off rationale. | Reject or flag for manual review. |
| `ALLOCATOR_POLICY_INCONSISTENT` | HIGH | `action`, `adjust_percent`, and target delta are mutually inconsistent. | Reject whole Allocator response. |
| `ALLOCATOR_REASON_CODE_INVALID` | MEDIUM | `reason_codes` contains a code outside the allowlist. | Reject response unless code set was explicitly expanded. |
| `ALLOCATOR_REJECTED_FALLBACK` | HIGH | Allocator response was rejected and previous targets were maintained. | Record final fallback event. |

## 4. Analysis output events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `ANALYSIS_ACTION_INVALID` | HIGH | Stock action outside `BUY/SELL/HOLD`. | Reject analysis response. |
| `ANALYSIS_WEIGHT_INVALID` | HIGH | Output weight is missing, negative, nonnumeric, or violates schema. | Reject or set HOLD depending on rule. |
| `ANALYSIS_REASONING_DATE_ID_MISSING` | HIGH | Required Date-ID evidence missing from reasoning. | Reject analysis response. |
| `ANALYSIS_ASSET_RANGE_VIOLATION` | MEDIUM | Suggested trade would move asset class outside Allocator allowed range. | Hold order; perform configured validation flow. |
| `ANALYSIS_SINGLE_NAME_BUY_CAP_EXCEEDED` | HIGH | Suggested buy exceeds cumulative buy-cost 5% cap. | Reject order intent. |

## 5. Gold policy / trade block events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `GOLD_TRADE_BLOCKED_MONTHLY_LIMIT` | LOW | Gold trade blocked because monthly soft limit was exceeded. | Do not trade gold; record event. |
| `GOLD_TRADE_BLOCKED_QUARTERLY_LIMIT` | LOW | Gold trade blocked because quarterly soft limit was exceeded. | Do not trade gold; record event. |
| `GOLD_MICRO_ADJUST_IGNORED` | LOW | Gold adjustment below 3% threshold was converted to MAINTAIN. | Record event; no gold order. |
| `GOLD_INCREASE_LIMIT_EXCEEDED` | MEDIUM | Gold increase exceeded configured +5% single-adjustment limit. | Reject or require validation depending on config. |
| `GOLD_DECREASE_LIMIT_EXCEEDED` | MEDIUM | Gold decrease exceeded configured -2% single-adjustment limit. | Reject or require validation depending on config. |

## 6. Order / broker / fill events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `BROKER_API_ERROR` | HIGH | Broker or paper broker API call failed. | Stop affected order flow; inspect adapter. |
| `ORDER_SUBMIT_ERROR` | HIGH | Order could not be submitted. | Record and do not assume position changed. |
| `ORDER_FILL_ERROR` | HIGH | Fill status is invalid, missing, or inconsistent. | Reconcile account state. |
| `PARTIAL_FILL_HANDLING_ERROR` | MEDIUM | Partial fill was not handled according to order policy. | Reconcile and patch order logic. |
| `PAPER_BROKER_SIM_ERROR` | HIGH | Paper broker simulation produced impossible or inconsistent state. | Stop paper session until fixed. |
| `PAPER_NAV_SNAPSHOT_ERROR` | HIGH | Paper NAV snapshot write failed after a paper fill. | Record event; reconcile paper ledger and NAV snapshot. |
| `SLIPPAGE_REJECTION` | LOW | Order was rejected by directional slippage rule. | Record as policy event; no trade. |
| `MARKET_CLOSED_ORDER_ATTEMPT` | MEDIUM | Order attempted outside valid market session. | Fix scheduler/calendar logic. |
| `UNTRADEABLE_SECURITY` | MEDIUM | Trading halt, limit-down lock, no liquidity, or non-tradeable asset. | Skip order and record. |

## 7. Risk filter / emergency event codes

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `MDD_LEVEL_1_TRIGGERED` | HIGH | Account MDD reached -10%; target cash 50%. | Execute Python-only MDD flow. |
| `MDD_LEVEL_2_TRIGGERED` | HIGH | Account MDD reached -15%; target cash 80%. | Execute Python-only MDD flow. |
| `MDD_LEVEL_3_TRIGGERED` | CRITICAL | Account MDD reached -20%; target cash 95%; halt after level 3. | Execute MDD flow and halt trading. |
| `MDD_COOLDOWN_ACTIVE` | LOW | MDD stage trigger suppressed by cooldown/same-day rule. | Record and skip duplicate stage action. |
| `EMERGENCY_TRIGGER_RATE_LIMITED` | LOW | Emergency trigger suppressed by cooldown/rate limit. | Record only. |
| `RISK_FILTER_ORDER_REJECTED` | MEDIUM | Python hard filter rejected an order intent. | Record reason; no order. |

## 8. Config / scheduler / runtime events

| event_code | severity | Definition | Typical action |
|---|---:|---|---|
| `CONFIG_LOAD_ERROR` | CRITICAL | `config.toml` missing, invalid, or failed typed validation. | Stop startup. |
| `LIVE_TRADING_GATE_FAILED` | CRITICAL | Live trading attempted without all explicit safety gates. | Stop startup. |
| `ENV_VAR_MISSING` | HIGH | Required environment variable missing. | Stop affected component. |
| `SCHEDULER_CALENDAR_ERROR` | HIGH | Market calendar or timezone calculation failed. | Stop scheduled action; inspect calendar code. |
| `SCHEDULER_MISFIRE` | MEDIUM | Scheduled job did not run at expected time. | Record and inspect scheduler. |
| `DATA_ADAPTER_ERROR` | HIGH | Data provider adapter failed or returned invalid data. | Skip affected analysis; inspect provider. |
| `RUNTIME_EXCEPTION` | HIGH | Unexpected Python exception. | Record stack summary and stop affected flow. |

---

# Maintenance rules

- Add new codes only when an event cannot be clearly represented by an existing code.
- Do not create investment-judgment tags here. Those belong to Weekly/Monthly Postmortem.
- Keep codes uppercase `SNAKE_CASE`.
- Prefer specific codes over generic `RUNTIME_EXCEPTION` when the failure mode is known.
- A Debug event may include one `event_code` only. Use `detail` and `human_note` for nuance.
- Do not aggregate these codes into runtime LLM prompt context.
