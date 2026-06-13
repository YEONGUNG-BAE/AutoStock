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

- disconnected
- subscription incomplete
- heartbeat/pong timeout
- flapping (repeated short connects)
- monitor internal transport exit

### Market-data defects → `HOLD_EXECUTION_ONLY`

- quote starvation
- stale quote
- trade starvation (via quote path)
- warming (execution not ready, monitor kept)

**Starvation must never cancel the monitor or increment restart count.**

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

## Restart semantics

- `monitor_initial_starts`: first start per OPEN session epoch (not counted against restart budget)
- `monitor_restarts`: monitor task ended and restarted (counts against `max_restarts_in_window`)
- `max_restarts_in_window=2` → initial start + up to 2 restarts allowed; 3rd restart → `FAILED_CLOSED`
- Session close (OPEN→inactive) **resets** restart budget for the next OPEN epoch

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
`provisional_thresholds()` / `provisional_supervisor_policy()`:

| Field | Provisional value |
|-------|-------------------|
| `heartbeat_timeout_seconds` | 60 |
| `minimum_stable_uptime_seconds` | 300 |
| `reconnect_window_seconds` | 120 |
| `max_connects_in_window` | 3 |
| `flapping_min_uptime_seconds` | 30 |
| `flapping_min_market_events` | 1 |
| `quote_grace_seconds` | 30 |
| `quote_starvation_seconds` | 30 |
| `max_quote_age_seconds` | 60 |
| `max_restarts_in_window` | 2 |
| `restart_window_seconds` | 300 |
| `restart_backoff_seconds` | 1 |
| `poll_interval_seconds` | 1 |

Calibrate against live-smoke evidence before unattended pilot.

## Offline rehearsal CLI

```bash
PYTHONPATH=src python ops/rehearse_market_supervisor.py --fixture path/to/scenario.json
```

- Network-free, deterministic fake clock
- Evidence output only under `runtime/` (explicit path)
- JSON summary to stdout

## Full-day integration

`tests/test_session_day_simulation.py::test_full_day_supervisor_orchestration` exercises:

`ExplicitMarketScheduleProvider` + fake clock + scripted monitor + `MarketHealthTracker`
+ `MarketSupervisor` with typed action assertions.

Actual runtime / KIS live smoke / paper execution remain **inactive**.
