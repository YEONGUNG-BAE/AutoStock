# Paper-Day Internal Runtime Error Classification Gap

## Scope

This is an offline post-run classification note for sanitized Paper-Day evidence.
It is not live rerun authorization, not a PASS conversion, not parser-failure
evidence, and not safety-failure evidence.

Use this note only when reviewing already-persisted summary, evidence, envelope,
and report artifacts. It does not authorize live KIS, activation, full paper,
automatic restart, a daemon, live orders, raw frame inspection, raw payload
inspection, or secret-bearing diagnostics.

## 2026-06-29 Sanitized Case Summary

- HEAD: `4d2b76cc2cab1d6dec2275abbdb02e9c22dabf9b`
- run_id: `6ddbfa7a6ba64e4fbbb5a887b3c2b068`
- session_date: `2026-06-29`
- symbol: `005930`
- source_kind: `kis_live`
- formal verdict remains FAIL
- outcome: `FAIL`
- stop_reason: `internal_runtime_error`

The run produced about 3h live market-data success before failing closed. The
latest healthy heartbeat before failure was recorded at
`2026-06-29T12:07:40.205333+09:00` with `session_state=OPEN` and
`market_data_health=HEALTHY`.

In the final 2 minutes, sanitized evidence showed source/control-frame churn:

- `13` `source_error` drops
- all source drops carried `reason_subcode=malformed_control_after_ack`
- `14` `reconnect_stream_reset` events
- `16` health-held execution results

The tail lifecycle was:

1. final `drop` at `2026-06-29T12:08:55.346178+09:00`
2. `exhausted` at `2026-06-29T12:08:55.346493+09:00`
3. `failed_closed` at `2026-06-29T12:08:55.348674+09:00` with
   `reason_code=internal_runtime_error`
4. `finalized` at `2026-06-29T12:08:55.353442+09:00`

## Safety Invariant Summary

This case is safety-clean only because the persisted sanitized artifacts preserve
these invariants:

- `paper_only=true`
- `activation_authorized=false`
- `real_order_adapter_constructed=false`
- `automatic_restart=false`
- `nonterminal_journal=0`
- `cleanup_outcome=CLEAN`
- `summary_publication_outcome=WRITTEN`

If any of these invariants differ in a future run, do not apply this
safety-clean interpretation.

## Market-Data And Parser Summary

This case is not parser failure evidence. The market-data counters were
parser-clean:

- `quote_frames == normalized_quotes`
- `trade_frames == normalized_trades`
- `quote_frames + trade_frames == parse_success`

Do not treat `malformed_control_after_ack` source churn as a quote/trade parser
failure when quote and trade frames normalize 1:1.

## Historical Classification Gap

The 2026-06-29 run followed this mechanical path:

1. repeated sanitized KIS source drops with
   `reason_subcode=malformed_control_after_ack`
2. repeated reconnects with no market data in the final attempts
3. `MarketMonitor` emits `exhausted`
4. `MarketMonitor` raises `MonitorExhaustedError`
5. `attended_paper_day` only normalizes `_CriticalStop` exceptions from the
   monitor call
6. `MonitorExhaustedError` bubbles to the generic runtime handler
7. the run records `failed_closed` with `reason_code=internal_runtime_error`

That historical classification is a monitor-exhaustion classification gap: the
source/control-frame reconnect exhaustion is real, but the persisted terminal
reason for that already-completed run is generic. The historical run remains
`internal_runtime_error`; do not rewrite its artifacts and do not reinterpret it
as PASS.

## Current Normalized Classification

New fake/sanitized regression coverage now guards the attended Paper-Day monitor
execution boundary. When `MarketMonitor` raises `MonitorExhaustedError`, the run
still fails closed with `outcome=FAIL`, but the terminal reason is normalized to
`source_exhausted_after_reconnects` instead of generic `internal_runtime_error`.
If a sanitized source subcode was observed before exhaustion, the terminal
`failed_closed` evidence snapshot preserves it as `reason_subcode`, for example
`malformed_control_after_ack`.

Generic unexpected monitor exceptions are still classified as
`internal_runtime_error`. Reconnect behavior, reconnect attempt counts,
quote/trade parser behavior, market-data source behavior, activation behavior,
automatic restart behavior, and live order behavior are unchanged.

Future `source_exhausted_after_reconnects` terminal `failed_closed` snapshots now
carry sanitized diagnostics so post-run review can distinguish pre-readiness
source churn from post-readiness source churn without opening raw frames or
payloads:

- `reason_subcode`
- `source_drop_subcode_counts`
- `quote_readiness_reached`
- `trade_readiness_reached`
- `latest_session_state`
- `latest_market_data_health`
- `latest_heartbeat_at`
- `terminal_exhaustion_phase`
- `quote_frames`
- `normalized_quotes`
- `trade_frames`
- `normalized_trades`
- `parse_success`

`terminal_exhaustion_phase` is `pre_market_data_readiness` when neither quote nor
trade readiness was reached before exhaustion, `post_market_data_readiness` when
both were reached, and `unknown` for partial readiness. These fields are
diagnostic only: they do not change reconnect policy, parser behavior, source
behavior, outcome, or validator verdict.

The formal verdict remains FAIL unless a future policy and code change explicitly
changes classification. Do not retroactively convert this run to PASS.

## 2026-06-30 Diagnostics Validations

After the source exhaustion diagnostics were added, the separate
2026-06-30 1-hour validation run completed as a formal PASS:

- HEAD: `a0bbe4600e44a12295316b6b5feae9c83ef08bb6`
- RUN_LABEL: `paper-day-source-diagnostics-validation-01h-01`
- run_id: `0c6229f939944050a87061fe9735a832`
- source_kind: `kis_live`
- verdict PASS
- outcome: `PASS`
- stop_reason: `completed`
- source noise persisted: `malformed_control_after_ack=27`
- no terminal source exhaustion occurred

The later 2026-06-30 rest-of-session validation also completed as a formal PASS:

- RUN_LABEL: `paper-day-source-diagnostics-validation-rest-of-session-01`
- run_id: `479aea40b15c41cf92dc5067ab704da8`
- source_kind: `kis_live`
- verdict PASS
- outcome: `PASS`
- stop_reason completed
- latest heartbeat: `OPEN` / `HEALTHY`
- source noise persisted: `malformed_control_after_ack=626`
- source noise persisted: `source_iterator_unknown_after_ack=1`
- reconnect_stream_reset=1251
- no terminal source exhaustion occurred

Both 2026-06-30 PASS runs are separate from the 2026-06-29 historical failures.
Those failures remain formal FAIL and must not be converted to PASS. The
diagnostics code did not change reconnect behavior, parser behavior, source
behavior, order behavior, activation behavior, daemon behavior, or automatic
restart behavior; it only made post-run classification and terminal diagnostics
more explicit.

## Next Action

- No immediate live rerun required solely to confirm this diagnosis.
- Keep fake/sanitized regression coverage in place if classification behavior
  changes later.
- Do not proceed to full paper solely from this FAIL.
- Keep treating the existing run as formal FAIL, safety-clean, parser-clean, and
  envelope-clean based only on the sanitized artifacts and same-run envelope
  identity.
