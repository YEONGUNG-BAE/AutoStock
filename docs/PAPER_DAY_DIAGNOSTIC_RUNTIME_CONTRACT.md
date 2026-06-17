# Paper-Day Diagnostic Runtime Contract

RTM-7c.5a/5b adds a bounded attended diagnostic composition for one paper-only
KR symbol.

Scope:

- market `KR`
- symbol `005930`
- universe `KR_LARGE`
- KIS market-data channels `H0STCNT0` trade and `H0STASP0` quote
- deterministic four-slot publisher at `09:30`, `11:00`, `13:00`, `14:50`
- paper broker, SQLite ledger, SQLite trigger journal
- single process, attended, bounded duration

This mode is not activation. It does not authorize runtime trading, does not
construct a real-order adapter, does not implement automatic restart, and does
not select an operational DB path implicitly. Existing
`ops/run_paper_fast_loop.py --run` remains `NO_GO` with exit `2`.

Runtime graph:

```text
MarketEventSource
-> MarketMonitor
-> LatestMarketStateStore
-> RollingTradeHistoryStore
-> MarketHealthTracker
-> SessionHealthExecutionGate
-> ActiveDecisionStore read
-> FastLoopExecutionOrchestrator
-> TriggerEngine
-> TriggerOrderBridge
-> PaperExecutionCoordinator
-> PaperBrokerAdapter
-> SqliteTriggerJournal
-> SQLiteLedger
```

The deterministic publisher is the only writer to `ActiveDecisionStore`. The
fast loop reads active decisions and never derives a `TriggerPlan` from decision
prose.

Lifecycle:

```text
validate                       (admission phase 1)
-> acquire create-new runtime lock   (admission phase 2)
-> open create-new evidence
-> resources open
-> source connect/subscription path only when required
-> bounded monitor run or startup probe
-> source stop
-> resource (stack) reverse close
-> post-close journal/quiescence inspection
-> completion verdict (if PASS)
-> final evidence record
-> evidence flush/close
-> build immutable summary
-> cleanup fatal/ordinary failure final judgment
-> summary create-new publish (only when no operation/cleanup fatal)
-> release lock                (last bounded cleanup)
```

Output ownership: validation and lock acquisition are the two admission phases.
Until the lock is held the runtime owns no output path, so **any admission
failure (`invalid_input`, `runtime_lock_exists`) returns an in-memory result and
writes zero files**: no DB open, no source factory call, no credential/env read,
no evidence, no summary, and no symlink target is created. Output files are
written by the lock owner only. State is tracked by explicit locals
(`validation_succeeded`, `lock_acquired`/`summary_path_owned`, `evidence_owned`,
`resources_opened`): validation fail -> write 0; lock not acquired -> write 0;
path ownership not confirmed -> write 0.

Summary publication is create-new and atomic: bytes are written to a
same-directory temp opened `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, fsynced, then
hard-linked to the create-new destination (link fails if the destination
exists). `Path.write_text()` overwrite is not used. A partial summary is never
visible. Publish outcomes are `WRITTEN`, `NOT_WRITTEN`, `PUBLISHED_INCOMPLETE`,
and `PUBLICATION_UNCERTAIN`:

- `WRITTEN`: destination publication confirmed, temp cleanup confirmed, parent
  directory sync confirmed.
- `PUBLISHED_INCOMPLETE`: destination publication confirmed, but temp cleanup
  and/or parent sync incomplete.
- `PUBLICATION_UNCERTAIN`: destination publication cannot be confirmed absent or
  present (e.g. post-link `lstat` `EIO`/`EACCES`).
- `NOT_WRITTEN`: destination absence confirmed.

Anything other than `WRITTEN` downgrades the returned run to `FAIL` with stable
reasons (`summary_failed`, `summary_published_incomplete`,
`summary_publication_uncertain`). **Persisted/returned byte equality is claimed
only when `summary_publication_outcome == WRITTEN`.** For
`PUBLICATION_UNCERTAIN` or lock-release uncertainty the operator must manually
inspect/isolate the on-disk file.

**Cleanup or operation fatal blocks PASS summary publish (Choice A):** when an
operation fatal (`MemoryError`, `KeyboardInterrupt`, `SystemExit`) or cleanup
fatal is pending, no summary file is written; lock release is still attempted;
the original fatal is re-raised after finalize.

Evidence/summary consistency: the final evidence record is written and the
evidence recorder is closed **before** the summary is built and published.
An evidence failure yields `FAIL/evidence_failed` and no PASS summary.

Fatal lifecycle ownership: a single outer owner runs cleanup in order
(source cancel/close → stack reverse-close → recorder close → cleanup fatal
judgment → summary publish if eligible → lock release) with lock release as the
**last bounded cleanup, always attempted** even under a fatal (`MemoryError`,
`SystemExit`). Resource-level close uses exact fatal preservation:
`MemoryError`/`KeyboardInterrupt`/`SystemExit` are never swallowed as ordinary
close failures; all resources are still attempted and the first cleanup fatal
is retained. Fatal identity is preserved (re-raised) with precedence:
operation > source cleanup > stack/resource cleanup > recorder cleanup > summary
publication > lock cleanup.

Lock release returns structured state (`runtime_lock_fd_closed`,
`runtime_lock_unlinked`, `runtime_lock_absent_confirmed`,
`runtime_lock_release_reason_code`). `unlink` `ENOENT` confirms absent;
`EACCES`/`EIO` yield `runtime_lock_release_uncertain`; other failures yield
`runtime_lock_release_failed`. Lock residue or uncertain release forbids PASS
return. No automatic stale-lock deletion after release failure.

Transport readiness counters are owned by source lifecycle events only:
connect attempts, connected state, subscription requests, ACKs, rejects, and
disconnects are never prefilled by the runtime body. Offline replay may emit an
explicit synthetic replay lifecycle (`source_kind=replay`); live KIS uses
`KisWsMarketEventSource` transport events (`source_kind=kis_live`).

Startup-only semantics:

- offline startup-only: validate, lock, open pilot DB resources, close resources,
  then write a post-close summary. It does not claim connected or subscription
  ACK readiness.
- live startup-only: validate, lock, open pilot DB resources, obtain KIS
  approval, connect websocket, observe trade and quote subscription ACKs, close
  source/resources, then write a post-close summary. It must not pass market
  events to the trigger engine or paper execution stack.

Live startup probe exception taxonomy (the probe classifies fully; a source
failure is never silently downgraded to `health_not_ready`):

```text
both subscription ACKs accepted        -> PASS/startup_only
subscription ACK rejected              -> NO_GO/subscription_rejected
consumer exhausts before readiness     -> NO_GO/transport_not_ready
consumer/source raises                 -> FAIL/source_failed
generator close raises (non-cancel)    -> FAIL/source_close_failed
generator close fatal                  -> fatal preserved (no PASS summary)
receive timeout without readiness      -> NO_GO/health_not_ready
```

The probe inspects the consumer task's exception explicitly rather than
suppressing it; suppressing a consumer exception and then reporting
`health_not_ready` is forbidden. After consumer cancel, `Task.exception()` is
read only when `task.done() and not task.cancelled()` (Python 3.11+ re-raises
`CancelledError` otherwise). Allowed: `asyncio.CancelledError` caused by explicit
consumer cancel. Failed: generator `finally`/`close` `RuntimeError` →
`source_close_failed`. Fatal: `MemoryError`/`KeyboardInterrupt`/`SystemExit` →
fatal precedence preserved.

Partial stack construction: `_close_partial_resources` returns structured
result; precedence is constructor operation fatal > cleanup fatal > constructor
ordinary error > cleanup ordinary errors.

Runtime summary outcome taxonomy:

```text
PASS: completed, startup_only
NO_GO: transport_not_ready, subscription_rejected, trade_not_observed,
       quote_not_observed, health_not_ready, trigger_not_evaluated,
       journal_uncertain, reconcile_required, nonterminal_journal,
       resource_close_failure, runtime_lock_exists
FAIL: invalid_input, source_failed, source_close_failed, evidence_failed,
      summary_failed, summary_published_incomplete, summary_publication_uncertain,
      runtime_lock_release_failed, runtime_lock_release_uncertain,
      db_failed, internal_runtime_error
```

CLI exit: clean `PASS` + `summary_publication_outcome == WRITTEN` +
`runtime_lock_absent_confirmed == true` → `0`; `NO_GO`/`FAIL`/`PUBLISHED_INCOMPLETE`/
`PUBLICATION_UNCERTAIN`/lock-release failure → `1`; fatal propagates per policy.
Existing `ops/run_paper_fast_loop.py --run` remains `NO_GO` with exit `2`.

Completion verdict: a `completed` market loop is only confirmed `PASS` after a
fixed-precedence re-check of the lifecycle counters. The verdict is evaluated
once, after resources close, and downgrades an otherwise-`PASS` run to `NO_GO`
with a stable reason. Precedence (first failing gate wins):

```text
1. nonterminal_journal                       (journal left non-terminal)
2. reconcile_required                         (critical journal state)
3. journal_uncertain                          (critical journal state)
4. resource_close_failure
5. subscription_rejected                      (transport)
6. transport_not_ready  (no all-subscribed)   (transport)
7. trade_not_observed                         (market data)
8. quote_not_observed                         (market data)
9. health_not_ready
10. trigger_not_evaluated
-> otherwise PASS
```

A `PASS` therefore requires that at least one trade tick and one quote were
observed, the subscription was accepted, health became ready, the trigger was
evaluated, and the journal was left fully terminal with no uncertain/reconcile
state.

Lock/path ownership: `db_dir` is explicit, evidence and summary outputs must not
overlap it, final symlink components are rejected, evidence is create-new, a
runtime lock is create-new/no-overwrite, existing DB sidecars are rejected, and
an existing non-empty pilot DB directory requires an explicit reuse policy.

Immediate stop: `UNCERTAIN` and `RECONCILE_REQUIRED` stop event intake after the
first occurrence and return `NO_GO`; later events must not reach orchestration.
Resource close failures are reflected in summary/return/exit. The summary is
written after resource close and includes `shutdown_completed_at`.

Actual KIS network execution remains Operator-only. Cursor/test work must use
replay or lifecycle-aware fakes. A 1-day pilot remains **NO-GO** until Reviewer
PASS.
