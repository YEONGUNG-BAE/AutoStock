# Two-Loop Offline Rehearsal Contract (RTM-7c.3)

Deterministic **offline** integration rehearsal wiring slow-loop decision refresh and
fast-loop market monitor execution. No KIS network, production runtime DB, LLM runners,
daemon, or unattended pilot.

## Scope

| In scope | Out of scope |
|---|---|
| `DecisionRefreshScheduler` + scripted runner + `ActiveDecisionStore` writer | Real Scout / Allocator / Analysis runners |
| `MarketMonitor` + health adapter wiring + `FastLoopExecutionOrchestrator` reader | Production supervisor daemon |
| Shared SQLite active pointer (separate connections) | `runtime/` paths |
| Paper stack through `PaperBrokerAdapter` / `SQLiteLedger` / `SqliteTriggerJournal` | Throughput / latency validation |
| Fake clock + bounded phase interleaving | Wall-clock sleep (except `asyncio.sleep(0)`) |

## Fixed session (explicit schedule)

- Date: `2026-06-15` (`Asia/Seoul`)
- Window: PRE_OPEN `08:30`, OPEN `09:00`, CLOSE `15:30`, POST_CLOSE_END `16:00`
- Slots (caller-injected, no hidden defaults):
  - `s1` `09:30` — BUY, condition true at 70,000 KRW
  - `s2` `11:00` — HOLD (`plan=None`)
  - `s3` `13:00` — scripted runner failure (slot `FAILED`, prior active preserved)
  - `s4` `14:50` — BUY with false condition (`LAST_TRADE_PRICE <= 60000`)

Initial HOLD is direct-published at `08:50` PRE_OPEN (not a scheduler slot).

## Database boundaries

```text
scheduler_store = ActiveDecisionStore(path)   # slow-loop writer
fast_loop_store = ActiveDecisionStore(path)   # fast-loop reader (separate connection)
ledger / trigger journal → tmp_path only
```

## Wiring (composition / test layer)

```text
MarketMonitor.on_evidence(apply only) → MarketSupervisorAdapter → MarketHealthTracker
MarketMonitor.on_applied_update → FastLoopExecutionOrchestrator
```

Monitor transport `connect` evidence must **not** reset a manually seeded transport epoch.

## Expected outcomes (happy path)

- Runner calls: `4`; slot terminals: s1/s2/s4 `PUBLISHED`, s3 `FAILED`
- Paper fills: exactly **1** COMMITTED; position `57`, cash `96,010,000` KRW at 4% / 100M NAV
- Reconnect epoch sequence reset: per-stream. The pre-drop batch carries only a trade, so on
  reconnect only the **trade** stream is reset (new-epoch `sequence=1` trade APPLIED); the
  unobserved **quote** stream is *not* reset, so its new `sequence=1` quote is correctly rejected
  `OUT_OF_ORDER` against the retained stored sequence (no aggressive fail-open). No duplicate fill.
- Fast-loop process restart (new engine/coordinator/orchestrator, same DBs): re-armed engine
  re-fires, but the duplicate fill is blocked by **portfolio-state quantity resolution**
  (`TRIGGERED_ABORTED` / `no_executable_quantity` — position already at target weight), **not**
  journal dedup. The idempotency key embeds in-memory `activation_epoch`, which resets across
  restart → a *different* key → `reserve` succeeds and the journal records a new `ABORTED` row
  (final journal rows = COMMITTED 1 + ABORTED 1 = 2). Journal-based cross-process exactly-once
  (identical-key `SKIPPED_TERMINAL`) is proven separately in the F1b integration suite; persistent
  activation-epoch restore stays deferred (TECH_DEBT F1).
- Scheduler restart same day: duplicate slot execution `0`
- Session/health held phases: PRE_OPEN / WARMING / POST_CLOSE coordinator `0`

## Failure-path coverage (separate tests)

- Active store `PublicationError` → global terminal, coordinator `0`
- Health starvation (tight threshold) → gate not execution-ready
- Scheduler evidence sink failure → `FAILED_CLOSED`, published pointer not rolled back
- Fast-loop COMMITTED + evidence sink failure → returns COMMITTED, latches global terminal

## Activation

**Runtime activation: NO-GO.** Production calendar daemon composition (RTM-7c.4+) not started.
