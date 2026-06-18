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
`summary_publication_uncertain`). For `PUBLICATION_UNCERTAIN` or lock-release
uncertainty the operator must manually inspect/isolate the on-disk file.

Persisted summary vs returned envelope: the **persisted file** holds only the
immutable mechanical summary (`_build_summary` scalars). The **returned envelope**
is a superset that wraps that mechanical summary with cleanup/publication/lock
observations (`persisted_summary`, `summary_publication_outcome`,
`summary_publication_reason_codes`, `runtime_lock_fd_closed`,
`runtime_lock_unlinked`, `runtime_lock_absent_confirmed`,
`runtime_lock_identity_matched`, `runtime_lock_release_reason_code`,
`cleanup_outcome`). These envelope-only keys are **never written to the persisted
file**. `persisted_summary` carries the exact object serialized to disk **only
when `summary_publication_outcome == WRITTEN`** (byte-for-byte equality is
asserted only then); otherwise it is `null`. The persisted file may therefore
read `PASS` while the envelope's final `outcome` is downgraded — the persisted
mechanical observation and the operator verdict are distinct artifacts.

Clean-exit predicate (single owner `is_clean_pass`, shared by CLI and runtime):
a run is a clean `PASS` (exit `0`) only when **every** clause holds —
`outcome == PASS`, `summary_publication_outcome == WRITTEN`,
`runtime_lock_fd_closed == true`, `runtime_lock_absent_confirmed == true`,
`runtime_lock_release_reason_code is None`, and `cleanup_outcome == CLEAN`.
Any failing clause downgrades the returned `outcome` to `FAIL` with a stable
block reason while the persisted mechanical file is left untouched.

**Cleanup or operation fatal blocks PASS summary publish (Choice A):** when an
operation fatal (`MemoryError`, `KeyboardInterrupt`, `SystemExit`) or cleanup
fatal is pending, no summary file is written; lock release is still attempted;
the original fatal is re-raised after finalize.

Publication exception boundary: the publish step is wrapped so that **no
publisher exception can skip the lock release**. An ordinary exception escaping
the publisher (including its own `parent.mkdir`/serialization/directory-sync
paths) becomes a stable `NOT_WRITTEN`/`summary_publish_failed` result and marks
cleanup `INCOMPLETE`; a fatal is captured as a pending cleanup fatal
(`NOT_WRITTEN`/`operation_fatal`). Either way control falls through to the lock
release, which runs **exactly once**. The parent-directory fsync is a structured
`DirectorySyncResult` (open/fsync/close never let an `OSError` escape; a fatal is
carried in the result and re-raised by the caller), so a directory-sync failure
can no longer bypass lock release the way a raw `os.close` in a `finally` once
did.

Publisher-internal state preservation: the `NOT_WRITTEN` outer boundary above
applies only when the *whole* publish step never confirmed a destination. **Inside**
the publisher, once the hard link has landed (`destination_published`), no later
non-`OSError` exception may collapse the result back to `NOT_WRITTEN`. An ordinary
exception that escapes an inner step keeps whatever state was already established:
if the link landed, `_finalize()` recovers it as `PUBLISHED_INCOMPLETE`; if it had
not, the result is `PUBLICATION_UNCERTAIN` (never a false `NOT_WRITTEN`). Likewise
a post-link verification `lstat` that fails (`EIO`/`EACCES`) yields
`PUBLICATION_UNCERTAIN`, not `NOT_WRITTEN`. Publisher-internal fatal precedence is
explicit: an operation (body) fatal — including a directory-sync fatal — outranks a
cleanup (temp close/unlink) fatal, and **neither** may overwrite a confirmed
publication state; the structured `_finalize()` result owns the outcome and the
chosen fatal is re-raised only after finalize.

Fatal boundary consistency: every operation boundary distinguishes fatals from
ordinary failures with `except (MemoryError, KeyboardInterrupt, SystemExit):
raise` ahead of `except Exception:`. This holds uniformly at `recorder.open`,
the diagnostic-stack builder, the source factory (probe path), the summary
publisher's `parent.mkdir`, and the directory-sync helper — a `MemoryError` is
never absorbed as an ordinary failure at any of them.

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

Cleanup outcome: finalize computes a single `cleanup_outcome` over the whole
bounded cleanup (stack close, recorder close, summary publish, lock release):

- `FATAL`: any pending operation or cleanup fatal — no PASS, fatal re-raised.
- `INCOMPLETE`: an ordinary (non-fatal) cleanup failure — a stack/recorder close
  failure, an ordinary publisher exception, or a lock release that did not close
  the fd / produced a release reason. Forbids a clean PASS.
- `CLEAN`: every bounded cleanup step succeeded. A `PUBLISHED_INCOMPLETE` /
  `PUBLICATION_UNCERTAIN` publication is **not** an `INCOMPLETE` cleanup (the
  cleanup itself succeeded); the publication clause of the clean-exit predicate
  denies PASS independently.

Lock acquire is partial-side-effect-safe: after the `O_EXCL` open, any failure
of `fstat`/`write` rolls back via `_abort_partial_acquire`, which returns a
structured `RuntimeLockAcquireCleanupResult` (`fd_closed`, `lock_unlinked`,
`lock_absent_confirmed`, `identity_matched`, `reason_code`, `fatal`) rather than a
bare bool — the caller owns fatal precedence and reason selection. The fd is
closed and the inode is unlinked **only when its `(st_dev, st_ino)` still matches
the one we created**. A fatal during the write body is re-raised after rollback
(operation fatal outranks any rollback fatal); a rollback fatal is otherwise
re-raised by the caller. An ordinary failure yields `runtime_lock_acquire_failed`
**only when the rollback is fully confirmed** (fd closed *and* lock absent), else
`runtime_lock_acquire_uncertain`. In particular an fd-close `OSError` during
rollback makes the acquire `runtime_lock_acquire_uncertain` **even when the unlink
succeeds** — a closed-but-unconfirmed fd is never reported as a clean rollback.
The fd and identity are recorded only on a fully successful acquire, so a failed
acquire leaves no stale lock and no leaked fd.

The lock-parent `mkdir` is part of the same stable admission taxonomy — a raw
`OSError`/`RuntimeError` never escapes it. No lock fd is open on this path, so
there is nothing to roll back. A `PermissionError` or `OSError(EACCES/EIO)` →
`runtime_lock_parent_unreadable`; any other `OSError` → `runtime_lock_acquire_failed`;
a non-`OSError` `Exception` → `runtime_lock_acquire_uncertain`; a fatal
(`MemoryError`/`KeyboardInterrupt`/`SystemExit`) is re-raised unchanged.

Lock release returns structured state (`runtime_lock_fd_closed`,
`runtime_lock_unlinked`, `runtime_lock_absent_confirmed`,
`runtime_lock_identity_matched`, `runtime_lock_release_reason_code`). It is
identity-safe: it `lstat`s the path and unlinks **only** when the inode still
matches the acquired identity. A replaced/foreign inode is left intact and
reported `runtime_lock_identity_mismatch` (never unlinked). `unlink` `ENOENT`
confirms absent; `EACCES`/`EIO` yield `runtime_lock_release_uncertain`; other
failures yield `runtime_lock_release_failed`. fd-close and unlink are observed
independently: an fd-close `OSError` sets `runtime_lock_fd_closed == false` and a
release reason even when the subsequent unlink succeeds. An fd-close **fatal**
(`MemoryError`/`KeyboardInterrupt`/`SystemExit`) is likewise captured into the
result (`runtime_lock_fd_closed == false`) rather than thrown from `release()`,
and the unlink is **still attempted** afterward; later release fatals never
overwrite the first (`fatal = fatal or exc`). A fatal during release is therefore
never raised from `release()` — it is returned in the result and re-raised by the
outer owner only after the higher-precedence body/source/stack/recorder/publisher
fatals, so a lock-release fatal can never replace the original failure path. Lock
residue, fd-close failure, identity mismatch, or uncertain release forbids PASS
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
consumer ignores cancel (no close)     -> FAIL/source_close_timeout
generator close fatal                  -> fatal preserved (no PASS summary)
receive timeout without readiness      -> NO_GO/health_not_ready
```

Bounded cancellation (scope — Option B, reduced contract): after the consumer is
cancelled, the cleanup *await* is bounded by
`asyncio.wait_for(asyncio.shield(consumer), PROBE_CLEANUP_TIMEOUT_SECONDS)`, so a
consumer that ignores `CancelledError` cannot hang the *probe verdict* — the
timeout yields `FAIL/source_close_timeout`. The boundedness guarantee is
**verdict-level, not task-termination-level**: a source that genuinely refuses
`CancelledError` leaves a real pending task that the in-process event loop cannot
force-terminate, and `asyncio.run()` loop shutdown would itself block on it. The
contract therefore guarantees in-process termination **only for
cancellation-compliant sources**. The real `KisWsMarketEventSource` is
cancellation-compliant — it re-raises `CancelledError` after a `finally` that runs
`_safe_unsubscribe` + `_safe_close` (proven by
`tests/test_kis_ws_source.py::test_cancellation_cleans_up_and_reraises`). An
all-cancel-ignoring source is honestly out of scope for in-process bounding; only
process isolation (a hard subprocess timeout) bounds it, demonstrated by
`tests/test_attended_paper_day.py::test_all_cancel_ignoring_source_is_not_bounded_without_process_isolation`.

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
FAIL: invalid_input, source_failed, source_close_failed, source_close_timeout,
      evidence_failed, summary_failed, summary_published_incomplete,
      summary_publication_uncertain, runtime_lock_parent_unreadable,
      runtime_lock_acquire_failed,
      runtime_lock_acquire_uncertain, runtime_lock_release_failed,
      runtime_lock_release_uncertain, runtime_lock_identity_mismatch,
      db_failed, internal_runtime_error
```

CLI exit: exit `0` only on the shared `is_clean_pass` predicate (`PASS` +
`summary_publication_outcome == WRITTEN` + `runtime_lock_fd_closed == true` +
`runtime_lock_absent_confirmed == true` + `runtime_lock_release_reason_code is
None` + `cleanup_outcome == CLEAN`). `NO_GO`/`FAIL`/`PUBLISHED_INCOMPLETE`/
`PUBLICATION_UNCERTAIN`/any lock fd-close, identity, or release failure → `1`;
fatal propagates per policy. Existing `ops/run_paper_fast_loop.py --run` remains
`NO_GO` with exit `2`.

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
written after resource close and includes `resource_close_completed_at` (stamped
when the resource stack finishes closing, not a whole-process shutdown time — the
lock release and summary publish run after this stamp is serialized).

Actual KIS network execution remains Operator-only. Cursor/test work must use
replay or lifecycle-aware fakes. A 1-day pilot remains **NO-GO** until Reviewer
PASS.
