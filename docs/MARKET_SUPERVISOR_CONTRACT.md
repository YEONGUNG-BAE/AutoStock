# Market Supervisor Contract (RTM-7b)

## Scope

This document covers the **offline** session/health/supervisor layer under `src/market_data`
plus the concrete evidence adapter in `src/data/market_supervisor_adapter.py` and the
offline rehearsal CLI `ops/rehearse_market_supervisor.py`.

**Not in scope (this lane):**
- Real KIS `--run` / socket / DNS / HTTP
- Broker / ledger / paper execution wiring
- Production KRX holiday calendar provider
- Scheduler / launchd / unattended pilot
- Runtime default activation

## Responsibility boundaries

| Layer | Owns |
|-------|------|
| `KisWsMarketEventSource` | Single connection lifecycle (connect→subscribe→ACK→yield→disconnect). **No internal reconnect.** |
| `MarketMonitor` | Reconnect/backoff/heartbeat-timeout **within one `run()`** |
| `MarketSupervisor` | Process-level monitor start/stop/restart **between `run()` calls**, calendar gating, typed actions |

**MarketMonitor reconnect ≠ Supervisor restart.** Monitor reconnects inside a live `run()`.
Supervisor cancels the monitor task and schedules a new `run()` on transport failure.

## Calendar policy

### Missing schedule (safe wait)

When `ExplicitMarketScheduleProvider` has no entry for a weekday:

- `session.state = UNKNOWN`
- `calendar_reason = CALENDAR_MISSING`
- `action = WAIT_FOR_CALENDAR`
- `monitor_initial_starts = 0` (no monitor)
- Supervisor stays inactive; next tick re-queries calendar

Weekends without schedule entries → `CLOSED` (allowed).

### Provider contract failure (fail-closed)

Only hard failures become `FAILED_CLOSED`:

- Provider raises `MarketSessionError` or unexpected exception
- Malformed `SessionWindow` ordering
- Timezone-naive instant

**Missing schedule alone never permanently fails the supervisor.**

## Transport vs market-data actions

### Transport defects → `RESTART_TRANSPORT`

- subscription grace exceeded (connected but not `all_subscribed`)
- heartbeat/pong timeout (current epoch only)
- completed-epoch flapping (short unstable epochs in rolling window)
- monitor transport exit / exhaustion / internal failure

`RESTART_TRANSPORT` is armed once per transport failure epoch and cleared on the next
`connected` signal. Repeated UNHEALTHY ticks while already armed do **not** schedule
additional restarts.

### Market-data defects → `HOLD_EXECUTION_ONLY`

- quote starvation
- stale quote
- trade starvation (via quote path)
- warming (execution not ready, monitor kept)

**Starvation must never cancel the monitor or increment restart count.**

## Subscription ACK semantics

KIS concrete events map as follows:

| Concrete event | Adapter effect |
|----------------|----------------|
| `connected` | neutral `connected` — starts new connection epoch |
| `all_subscribed` | neutral `all_subscribed` — sets tracker `all_subscribed=true` |
| `disconnect` | neutral `disconnect` |
| `ping_received` | neutral `ping_received` |
| `pong_sent` | neutral `pong_sent` |
| `subscription_sent` | informational only (no tracker state change) |
| `ack` | informational only (per-subscription ACK, **not** completion) |
| `subscribed` | informational only (per-subscription evidence) |
| `unsubscribe_sent` | informational only |
| unknown | `AdapterError` (fail-closed) |

### Subscription grace

After `connected` with `all_subscribed=false`:

- `elapsed <= subscription_grace_seconds` → transport `WARMING`, `execution_ready=false`, **no restart**
- `elapsed > subscription_grace_seconds` → transport `UNHEALTHY`, `RESTART_TRANSPORT` allowed

`WARMING` is never healthy or execution-ready.

## Connection epoch isolation

A new `connected` event resets:

- `connected_at`, `all_subscribed=false`
- `last_pong_at=None`
- `market_events_in_epoch=0`
- subscription grace timer

Previous-epoch pong/heartbeat values are **not** reused. Delayed events with
`at < epoch_connected_at` are rejected as `OUT_OF_ORDER`.

## Completed-epoch flapping

Flapping is judged from **completed** `ConnectionEpochResult` records (bounded deque, max 64):

```text
stable epoch = uptime >= flapping_min_uptime_seconds
               AND market_event_count >= flapping_min_market_events
short unstable = NOT stable
```

When `short_epochs_in_window >= flapping_max_short_epochs` within
`flapping_window_seconds` → transport `FLAPPING` (evaluated before WARMING).

## Restart semantics

Two counters are maintained separately:

| Field | Meaning |
|-------|---------|
| `monitor_initial_starts` | First start per OPEN session epoch (free from restart budget) |
| `monitor_restarts` | Lifetime total restarts (never decreases) |
| `restarts_in_current_window` | Rolling-window count (`len(_restart_times)` after trim) |

Policy:

```text
trim entries < now - restart_window_seconds
allowed = len(_restart_times) < max_restarts_in_window
```

On restart: `_total_restarts += 1`, `_restart_times.append(now)`.

Session close (OPEN→inactive) clears `_restart_times` only; lifetime total is preserved.

## Sticky `FAILED_CLOSED`

Factory failure or evidence-sink failure during normal lifecycle:

- monitor task not created / no restart scheduled
- supervisor enters terminal `FAILED_CLOSED`
- subsequent ticks do **not** auto-recover
- requires a new supervisor instance

During cancellation cleanup, evidence-sink errors are suppressed and `CancelledError`
is re-propagated.

## Monitor exit classification

| `MonitorExitReason` | Restart? |
|---------------------|----------|
| `TRANSPORT_EXIT` / `TRANSPORT_EXHAUSTED` / `INTERNAL_FAILURE` | yes (transport reason) |
| `CANCELLED` / `SESSION_CLOSED` | no |
| running monitor + market `STARVED` | **no** (`HOLD_EXECUTION_ONLY`) |

## Evidence adapter

```
KIS source / Monitor concrete evidence
            ↓
src/data/market_supervisor_adapter.py
            ↓
neutral signal (kind/event_type + at)
            ↓
MarketHealthTracker / MarketSupervisor
```

Import guard:
- `src/market_data/*` must **not** import `data.*`
- Adapter must **not** import broker/ledger/execution
- Unknown concrete events → `AdapterError` (fail-closed)

## Health strictness

```text
is_healthy = transport == HEALTHY AND market_data == HEALTHY
is_execution_ready = is_healthy (conservative)
```

`WARMING`, `UNKNOWN`, `STARVED`, `STALE`, `INVALID`, `NOT_EXPECTED` are not execution-ready.

Timestamp policy (`record_*`):
- naive datetime → rejected
- unknown kind/type → `UNKNOWN_KIND`, state unchanged
- `at > now` → `FUTURE`, state unchanged
- out-of-order per stream → `OUT_OF_ORDER`, state unchanged

## Provisional thresholds

All thresholds are **caller-supplied** in production. Test/CLI helpers use
`provisional_thresholds()` / `provisional_supervisor_policy()`.
Values are **provisional** — calibrate after Monday live-smoke evidence.

| Field | Provisional value |
|-------|-------------------|
| `subscription_grace_seconds` | 30 |
| `heartbeat_timeout_seconds` | 60 |
| `minimum_stable_uptime_seconds` | 300 |
| `flapping_window_seconds` | 120 |
| `flapping_max_short_epochs` | 3 |
| `flapping_min_uptime_seconds` | 30 |
| `flapping_min_market_events` | 1 |
| `quote_grace_seconds` | 30 |
| `quote_starvation_seconds` | 30 |
| `max_quote_age_seconds` | 60 |
| `max_restarts_in_window` | 2 |
| `restart_window_seconds` | 300 |
| `restart_backoff_seconds` | 1 |
| `poll_interval_seconds` | 1 |

## Offline rehearsal CLI

```bash
PYTHONPATH=src python ops/rehearse_market_supervisor.py --fixture path/to/scenario.json
```

- Network-free, deterministic fake clock
- Evidence path validated via `Path.is_relative_to(runtime_root)` (no `startswith("runtime")`)
- Rejects `runtime_evil/`, `../runtime/`, and symlink escape attempts
- Transition summary counts actual previous→current state changes
- Scripted `long_running` monitor fixture support
- JSON summary to stdout (includes health/action sequences when configured)

## Full-day integration

`tests/test_session_day_simulation.py::test_full_day_long_running_exact_counts` exercises:

`ExplicitMarketScheduleProvider` + fake clock + **long-running** scripted monitor +
`MarketHealthTracker` + `MarketSupervisor` with exact start/restart/cancel/action assertions.

Actual runtime / KIS live smoke / paper execution remain **inactive**.
