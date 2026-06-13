# Offline Paper Fast-Loop Composition Contract (RTM-7c.4a)

Offline composition root + operator verification tooling only.
**Runtime activation: NO-GO.** `--run` is refused (`outcome=NO_GO`,
`reason_code=live_run_not_implemented`) before any credential read, network
socket, production-DB write, or runtime-directory creation.

This lane builds the offline wiring and the operator's read-only/replay tools.
It does NOT turn on a live runtime. Even when every gate is green, the change is
left for the operator to commit.

## Scope (hard boundaries)

- Single KR symbol, 6-digit code, `PAPER` account role, `KRW` currency.
- Network calls: **0**. KIS frames/transport: **0**. Credential reads: **0**.
- Production runtime DB writes: **0**. Runtime directory creation: **0**.
- Slow loop is manual/offline; a prepublished active decision is assumed.
- No scheduler process, no daemon, no calendar auto-exec, no migration, no
  journal reconcile.

## Package layout

`src/composition/` is the **only** place allowed to wire broker + ledger +
execution + orchestration into a runnable stack. `src/orchestration` purity is
preserved — orchestration never imports broker/ledger/execution directly; the
composition root does.

- `src/composition/sqlite_inspector.py` — strictly read-only SQLite inspection
  (`file:...?mode=ro` URI + `PRAGMA query_only = ON`). Never constructs
  `SQLiteLedger` / `SqliteTriggerJournal` / `ActiveDecisionStore` (their
  constructors create/migrate schema), never writes, never returns raw payloads,
  credentials, exception reprs, or tracebacks. Output is sanitized counts plus a
  small set of non-secret identifiers (`decision_id` / `plan_id`) and
  integer-quantity strings. None of the inspected DBs store credentials.
- `src/composition/paper_fast_loop.py` — the composition root. Three offline
  capabilities (below).
- `ops/run_paper_fast_loop.py` — operator CLI over the three capabilities plus
  the refused `--run`.

## Three offline capabilities

### `build_paper_fast_loop_plan` (validate-only, CLI default)

Loads + validates the on-disk execution-inputs snapshot and runs single-symbol
preflight against any *existing* ledger positions (read-only scan; the ledger
file is not created if absent). Returns `PaperFastLoopPlan` with outcome
`READY` / `NOT_READY` and sanitized reason codes:

- snapshot: `snapshot_file_missing`, `snapshot_not_yet_valid`,
  `snapshot_expired`, plus any typed `reason_code` surfaced by the snapshot
  loader (e.g. `snapshot_allocator_created_after`).
- position preflight: `unsupported_market`, `unsupported_account_role`,
  `unsupported_currency`, `foreign_position_present` (any position whose
  symbol ≠ the configured symbol).

No execution, no DB writes, no network.

### `inspect_paper_fast_loop` (read-only inspection)

Read-only summaries of the configured ledger / trigger journal /
active-decision-store via `sqlite_inspector`. Reports `missing_databases` for
files that do not exist. No writes, no schema creation, no network.

### `replay_offline` (deterministic offline replay)

Deterministic replay of the fast-loop execution stack against a built-in
normalized-event fixture, using **caller-provided temp paths only** — never the
configured `runtime/` paths. Raises `ValueError` for an unknown fixture or a
missing temp dir.

`AVAILABLE_REPLAY_FIXTURES = ("buy_fill", "hold_noop")`:

- `buy_fill` — BUY decision + `LAST_TRADE_PRICE <= 70000` plan → 1 `committed`
  result, final position quantity `"57"`, 1 terminal journal row.
- `hold_noop` — HOLD decision, no plan → no fill, no position.

Determinism anchors: KST date 2026-06-16, OPEN window, decision at 09:00, events
at 09:30, price 70000, threshold 70000, universe `KR_LARGE`, NAV 100M, target
weight 4% → 57 shares.

#### Replay health/quote preconditions (why the stack must be primed)

`is_execution_ready == is_healthy == transport HEALTHY AND market_data HEALTHY`:

1. Transport HEALTHY needs connected + all_subscribed **and**
   `since_connect >= minimum_stable_uptime_seconds`. Replay records
   connect/all_subscribed at the earlier decision time so uptime accrues.
2. Market-data HEALTHY (when the session expects quotes) needs a recorded QUOTE
   (`_last_quote_at`); a trade alone yields starvation. Replay records a
   `best_bid_ask` market event.
3. The coordinator must be able to price, so replay seeds a
   `NormalizedBestBidAsk` quote into the latest store before applying the trade
   (the quote is seeded, not routed as an applied update), avoiding
   `quote_unavailable` / `FAILED_CLOSED`.

## Wiring path (replay)

```
TriggerOrderBridge(journal, generator=OrderIntentGenerator(),
                   resolver=QuantityResolver(), broker=PaperBrokerAdapter,
                   ledger=SQLiteLedger)
  → PaperExecutionCoordinator(engine=TriggerEngine(), bridge,
        portfolio_context_service=PaperPortfolioContextService(
            ledger_source=ledger, market_state_source=adapter))
  → FastLoopExecutionOrchestrator(active_reader, latest_store, rolling_store,
        execution_gate=SessionHealthExecutionGate(calendar, tracker),
        execution_inputs_provider=StaticExecutionInputsProvider(...), coordinator)
```

All SQLite databases live under the caller-provided temp dir.

## CLI contract (`ops/run_paper_fast_loop.py`)

Four mutually-exclusive modes (default `--validate-only`):

| mode | exit code | notes |
|------|-----------|-------|
| `--validate-only` | 0 if `PASS` (READY), else 1 | snapshot + preflight |
| `--inspect-existing` | 0 | read-only summaries |
| `--replay FIXTURE` | 0 (1 on unknown fixture) | OS temp dir only |
| `--run` | **2** | **REFUSED** before any side effect |

- Mode collisions → exit 1, `reason_code` containing `mutually exclusive`.
- `--run` emits `outcome=NO_GO`, `reason_code=live_run_not_implemented`,
  `credential_read/network_called/production_db_touched/filesystem_written` all
  `false`, and returns a non-zero exit **before** loading settings, reading any
  credential, opening any socket, touching the production DB, or creating any
  path.
- `--json` emits a sanitized machine-readable summary. Credentials, raw frames,
  exception reprs, tracebacks, and DB dumps are never printed.

## Import boundary (guarded)

`tests/test_composition_import_guard.py` walks every `src/composition/*.py` and
`ops/run_paper_fast_loop.py` and asserts:

- **Forbidden, even at the root:** `socket`, `websocket`, `websockets`, `http`,
  `httpx`, `urllib`, `requests`, `data`, `llm` (network/transport/credential/
  live-data/LLM surfaces).
- **Allowed (composition IS the wiring root):** `broker`, `ledger`, `execution`,
  `orchestration`, `market_data`, `risk`, `paper_loop`, `domain`, `allocator`,
  `decision`, `analysis`, `config`, `composition`. Any first-party package
  outside this allowlist fails the guard so a new dependency gets reviewed.

### Documented deviation (Section-17)

GPT's original prompt wanted `execution` forbidden from the snapshot/composition
layer. The repo already allowlists `execution` for
`orchestration/fast_loop_execution.py`, and the keystone snapshot module imports
`execution` for `PaperPortfolioPolicy`; the composition root must construct the
real `PaperExecutionCoordinator`. Therefore `execution` is **allowed at the
composition boundary by design, not oversight**. The network/credential roots
above remain hard-forbidden regardless.
