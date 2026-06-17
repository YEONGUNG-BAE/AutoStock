# Paper-Day Diagnostic Evidence Contract

Evidence is append-only JSONL. It is diagnostic only and is not approval,
authentication, replay prevention, or activation evidence.

Required fields:

```text
schema_version
run_id
session_date
recorded_at
stage
event
market
symbol
reason_code
counter_delta
snapshot
sensitive_data_present
```

`sensitive_data_present` must be `false`. Evidence must not contain credentials,
raw websocket frames, raw config text, DB dumps, traceback reprs, or LLM
prompt/response payloads.

Counters cover:

- transport connect/subscription/disconnect
- trade/quote frames and normalized updates
- decision publication slots
- health/session gate outcomes
- trigger evaluations and match/suppression outcomes
- execution requests, journal terminal outcomes, orders, fills
- lifecycle timestamps and resource close failures

Heartbeat records include:

```text
connected
subscriptions_ready
last_trade_age_ms
last_quote_age_ms
transport_health
market_data_health
session_state
active_decision_id
trigger_evaluations
execution_requests
committed_orders
nonterminal_journal
```

Failure-stage principle: after a failed attended pilot, inspect the first failed
stage in evidence and run only the corresponding partial verification. Do not
expand scope to multi-symbol, daemonization, live orders, signing, or restart
automation in this lane.

