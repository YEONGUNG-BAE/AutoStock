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

### COMMITTED evidence sink failure (hardening)

When `on_evidence` raises after a `COMMITTED` coordinator result:

- current call returns `COMMITTED` (order not rolled back)
- `_global_terminal = True` immediately
- all subsequent updates → `GLOBAL_TERMINAL_FAIL_CLOSED`, coordinator 0

Non-`COMMITTED` sink failure → `EVIDENCE_SINK_ERROR` + global terminal (unchanged).

## Malformed update validation (hardening)

`handle_applied_update()` validates the full public boundary without raising:

- `AppliedMarketUpdate` type, `Market` enum, `MarketEventType` enum
- timezone-aware `applied_at`
- nonblank `str` symbol/provider/channel
- `int` sequence (not bool), `>= 0`

All malformed inputs → `MALFORMED_UPDATE`, gate/active/coordinator 0.

## Execution inputs binding (hardening)

`StaticExecutionInputsProvider.resolve()` additionally requires:

- `allocator_decision.universe == active.bundle.decision.universe`
- `allocator_decision.created_at <= now` (timezone-aware)

## Rolling orchestration (hardening)

When plan has rolling rules and `rolling_store` is configured:

```text
rule_required_windows → peek_history → build_indicator_context → coordinator
```

When `rolling_store is None`, orchestrator passes `indicators=None` and delegates
suppression to TriggerEngine (`MISSING_INDICATOR` etc.) — orchestrator does not
pre-fail.

Integration proof (RTM-7c.3 pre/rehearsal): `tests/test_fast_loop_execution_integration.py`
exercises real `PaperExecutionCoordinator` for rolling READY (COMMITTED once),
WARMING (`INDICATOR_WARMING`), and `rolling_store=None` (`MISSING_INDICATOR`).

## Coordinator reason mapping

`FastLoopExecutionResult.reason_code` prefers `CoordinatorResult.reason_code`; when
absent, maps `CoordinatorResult.trigger_reason.value` (e.g. `indicator_warming`,
`missing_indicator`, `hold_action`) so SUPPRESSED paths remain typed in evidence.

## Two-loop offline rehearsal

See `docs/TWO_LOOP_REHEARSAL_CONTRACT.md` for the deterministic `2026-06-15`
slow+fast rehearsal (explicit four slots, separate active-store connections,
one fill, duplicate prevention). Runtime activation remains **NO-GO**.

`orchestration` lazy-exports RTM-7c.1 store types + RTM-7c.2 gate/fast-loop types.
No eager imports; no circular dependency.

## Import boundaries

- `market_data/*` must not import `execution`, `orchestration`, `broker`, `ledger`
- `orchestration/fast_loop_execution.py` may import `market_data`, `execution`, `allocator`, `domain` only

## Not in this lane

- KIS `--run`, live orders, production runtime DB, scheduler daemon, unattended pilot
- Throughput/latency production validation
