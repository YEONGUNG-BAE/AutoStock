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

Counters cover actual observations only:

- transport connect/subscription/disconnect
- trade/quote frames and normalized updates
- decision publication slots
- health/session gate outcomes
- trigger evaluations and match/suppression outcomes
- execution requests, journal terminal outcomes, orders, fills
- lifecycle timestamps and resource close failures

Transport counters are derived from source lifecycle events, not from runtime
startup assumptions. Replay sources may emit an explicit synthetic replay
lifecycle; live KIS readiness comes from KIS websocket transport events. Offline
startup-only therefore reports `connected=0` and `subscription_acks=0`.

Single-owner counters: `connect_attempts` and `disconnects` are incremented only
by the source lifecycle (one increment per physical connection attempt/drop). The
monitor still emits connect/drop evidence rows for the timeline, but it does not
re-increment those counters — the lifecycle is the sole owner, so the completion
verdict reads a non-double-counted value.

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

Heartbeat values are read from `LatestMarketStateStore`, `MarketHealthTracker`,
the session provider, and the active decision store. Placeholder health strings
must not be emitted.

Admission-failure evidence isolation: evidence is owned by the lock owner only.
An admission failure (`invalid_input`, `runtime_lock_exists`) returns an
in-memory result and writes **zero** evidence rows — the evidence recorder is not
even opened. Evidence rows therefore exist only for runs that acquired the lock.

Evidence/summary ordering: the final evidence record (`stage="shutdown"`,
`event="finalized"`) is written and the recorder is flushed/closed **before** the
immutable summary is built and published. The persisted `summary.json` is
therefore byte-for-byte equal to the returned result. An evidence write failure
yields `FAIL/evidence_failed` and no PASS summary file.

Failure-stage principle: after a failed attended pilot, inspect the first failed
stage in evidence and run only the corresponding partial verification. Do not
expand scope to multi-symbol, daemonization, live orders, signing, or restart
automation in this lane.
