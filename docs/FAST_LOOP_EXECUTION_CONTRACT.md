# Fast-Loop Paper Execution Contract (RTM-7c.2)

Offline library wiring only. **Runtime activation: NO-GO** until throughput/latency
validation on real KIS full-session data.

## Neutral post-apply hook

`MarketMonitor` accepts optional `on_applied_update: Callable[[AppliedMarketUpdate], None]`.
Invoked synchronously **after** `LatestMarketStateStore.apply` (and rolling `observe` for
APPLIED trades) and apply evidence emit, **before** the next source event.

Call conditions:

- `ApplyStatus.APPLIED`
- `event_type ∈ {TRADE, BEST_BID_ASK}`
- rolling observe succeeded for APPLIED trades (when `rolling_store` is configured)

Not invoked: heartbeat, duplicate, out_of_order, stream_mismatch, future_event, reset-only.

`AppliedMarketUpdate.applied_at` is the **exact same** `now` shared by latest apply and
rolling observe (no mid-tick clock re-read).

Hook exceptions → `MonitorInternalError("post_apply_hook failed")` → supervisor
**FAILED_CLOSED** (not transport restart).

## Synchronous serial execution boundary

```
APPLIED update → post-apply hook (sync) → orchestration complete → next event
```

No queue/drop/coalescing in v1. Correctness over throughput.

## FastLoopExecutionOrchestrator

Wiring path:

```
AppliedMarketUpdate
  → preflight / symbol halt check
  → ExecutionGateProvider (session OPEN + health execution-ready)
  → ActiveDecisionReader.read_active (atomic)
  → latest_store.peek(now=applied_at)
  → optional build_indicator_context (rolling plans)
  → ExecutionInputsProvider.resolve (executable plans)
  → PaperExecutionCoordinator.process(same applied_at)
```

Gate held → no active read, no coordinator, no broker/journal.

## Session and health gate

`ExecutionGateSnapshot` requires:

- `gate.market == update.market`
- `gate.evaluated_at == update.applied_at`
- `session.state == OPEN`
- `health.is_execution_ready` (transport HEALTHY ∧ market_data HEALTHY)

`HOLD_EXECUTION_ONLY` supervisor semantics are **not** relaxed here.

## Active decision

- `None` → `MISSING_ACTIVE_DECISION`
- `PublicationError` → global terminal `ACTIVE_DECISION_CORRUPT`
- identity / validity mismatch → fail-closed without coordinator
- validity uses plan `valid_from`/`expires_at` (same as TriggerEngine)

## HOLD replacement

Newer HOLD publication → `replace_bundle` disarms engine → subsequent APPLIED ticks
suppress with `HOLD_ACTION`; no new orders.

## Symbol halt

`CoordinatorStatus.UNCERTAIN` or `RECONCILE_REQUIRED` → halt `(market, symbol)` for
orchestrator instance lifetime. No auto-clear API.

## Evidence

`FastLoopExecutionEvidence` — no raw frames, credentials, accounts, full decision JSON,
or exception repr.

## Import boundaries

- `market_data/*` must not import `execution`, `orchestration`, `broker`, `ledger`
- `orchestration/fast_loop_execution.py` may import `market_data`, `execution`, `allocator`, `domain` only

## Not in this lane

- KIS `--run`, live orders, production runtime DB, scheduler daemon, unattended pilot
- Throughput/latency production validation
