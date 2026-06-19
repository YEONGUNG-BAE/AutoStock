# Paper-Day Failure Triage Playbook

## Scope

Offline triage reference for the Monday 1-day attended paper diagnostic
(symbol `005930`, market `KR`). It interprets the four verdicts a run can
produce — `PASS`, `NO_GO`, `FAIL`, `NEEDS_REVIEW` — from `summary.json`,
`evidence.jsonl`, the captured `stdout-envelope.json`, and the rendered
`review-report.md`. It adds no new classification: the authoritative mechanical
verdict comes from `ops/validate_paper_day_summary.py` (`classify`) and is reused
verbatim by `ops/render_paper_day_report.py`. The authoritative PASS/NO_GO/FAIL
criteria live in `docs/PAPER_DAY_MONDAY_OPERATOR_PACKET.md`; this playbook tells
the Operator what each first-failure means and what to do (and not do) next.

This is a documentation/offline-tooling reference. It never runs live KIS, live
orders, a live paper diagnostic, a daemon, automatic restart, or runtime
activation, and it never prints a secret, a raw HTTP response, or a raw websocket
frame.

## Triage principle

```text
Always inspect the first failed stage.
Do not broaden scope after a failed run.
Do not retry blindly.
Do not change strategy.
Do not run live orders.
Do not activate runtime.
Use summary + evidence + stdout envelope + review-report together.
```

## First-failure rule

A run fails at exactly one place first. Read the **earliest** `failed_closed`
evidence row (its `stage` + `reason_code`) and the summary `stop_reason`; the
rendered report surfaces both in its **First failure** and **Verdict** sections.
Everything after the first failure is downstream noise — diagnose the first
failed stage, not the last log line. Do not widen scope, swap strategy, or rerun
until that first stage is understood and its cause is isolated.

## PASS

A clean PASS requires **every** clause below (the shared `is_clean_pass`
predicate plus the safety clauses):

```text
outcome = PASS
stop_reason = completed or startup_only (depending on the lane)
summary_publication_outcome = WRITTEN
cleanup_outcome = CLEAN
runtime_lock_fd_closed = true
runtime_lock_absent_confirmed = true
runtime_lock_release_reason_code = null
paper_only = true
activation_authorized = false
automatic_restart = false
real_order_adapter_constructed = false
sensitive_data_present = false in all evidence rows
tracked runtime = 0
```

Orders are **not** a PASS requirement:

```text
orders > 0 is not required for PASS.
zero paper orders can still be PASS if triggers/health/strategy conditions did
not require an order.
```

PASS is mechanical, not a go-live decision. The 1-day pilot stays NO-GO until a
Reviewer PASS, and live order / activation / automatic restart remain prohibited
regardless of the verdict.

## NO_GO taxonomy

`outcome = NO_GO` (exit 1): the run reached the lock owner and produced artifacts,
but a readiness/completion gate failed. Isolate the gate that failed **first**.

| stop_reason | meaning | check | do not | next |
| --- | --- | --- | --- | --- |
| `transport_not_ready` | source ended / readiness not reached before required startup readiness | first failed evidence stage, connect/disconnect counters | force PASS, retry outside session | confirm source lifecycle and readiness ordering |
| `subscription_rejected` | KIS rejected a channel subscription | `subscription_rejections`, `tr_id`/symbol/permission, first failed stage | print raw frame, retry blindly | verify subscription request shape and account permission |
| `health_not_ready` | source ran but readiness/health condition not satisfied | `normalized_*` counts, `health_hold`/`health_pass`, first failed stage | retry outside market session, change strategy, force PASS | inspect data availability and health gating evidence |
| `trade_not_observed` | no trade tick before the completion verdict | `normalized_trades`, market session timing | force PASS | confirm session liveness and subscription health |
| `quote_not_observed` | no quote before the completion verdict | `normalized_quotes`, market session timing | force PASS | confirm session liveness and subscription health |
| `trigger_not_evaluated` | trigger engine never evaluated | `trigger_evaluations`, upstream health/data gates | assume strategy bug first | confirm prerequisite health/data gates passed |
| `journal_uncertain` | journal/ledger consistency uncertain | journal/ledger evidence, `journal_committed` | retry blindly, delete journal | stop and review journal/ledger consistency |
| `reconcile_required` | reconcile flagged | journal/ledger evidence | retry blindly | stop and review journal/ledger consistency |
| `nonterminal_journal` | journal left non-terminal | `nonterminal_journal`, journal evidence | rerun over the same journal | review journal/ledger termination |
| `resource_close_failure` | cleanup/source lifecycle issue | cleanup_outcome, close timestamps, first failed stage | rerun blindly | review source/resource close lifecycle |
| `runtime_lock_exists` | duplicate runtime lock detected (admission refused, zero files written) | running processes, stale lock path | delete the lock blindly | confirm no other run is active per the runtime contract before clearing |

NO_GO is not success. Do not retry blindly; isolate the gate that failed first.

## FAIL taxonomy

`outcome = FAIL` (exit 1), or any hard safety violation (see
[Paper-only safety violations](#paper-only-safety-violations)).

| stop_reason | meaning | check | do not | next |
| --- | --- | --- | --- | --- |
| `source_config_gate_failed` | config/env gate failed before source start | `broker.kis_ws_read_only.enabled`, env vars present, symbol, `approval_base_url`/`websocket_url` | print secret values, retry until fixed | fix config/env, then retry |
| `source_approval_failed` | KIS approval key issuance failed | live vs. mock key/domain mismatch, approval URL, app permission, quote-contamination gotcha | print raw HTTP response or secret; immediately retry same settings | separate config/domain/account cause, then retry |
| `source_connect_failed` | websocket open/connect failed (before subscription stage) | `websocket_url`, DNS, TLS, network/firewall | print raw frame; immediately retry same settings | separate network/URL cause, then retry |
| `source_failed` | unclassified factory/consumer fallback (likely a bug) | evidence/summary, source code path | treat as expected | treat as a bug until classified into a subreason |
| `source_close_failed` | source close failed | cleanup_outcome, close evidence | retry in-process | treat the source as defective |
| `source_close_timeout` | source ignored cancellation; bounded at verdict level | cleanup_outcome, close timestamps | retry in-process | treat the source as defective; process isolation only |
| `evidence_failed` | evidence write failure (no PASS summary file) | filesystem/permissions on evidence path | assume the run is verifiable | inspect filesystem/permissions |
| `summary_failed` | summary publish failed | publication reason codes, destination dir | trust persisted bytes | manual artifact inspection |
| `summary_published_incomplete` | link landed but cleanup/fsync incomplete | publication reason codes | claim WRITTEN/byte-equality | manual artifact inspection (see Publication / envelope issues) |
| `summary_publication_uncertain` | persisted bytes cannot be trusted | publication reason codes | retry blindly; trust the file | manual artifact inspection |
| `runtime_lock_parent_unreadable` | lock parent dir unreadable | lock path permissions | delete the lock blindly | inspect lock-dir permissions per runtime contract |
| `runtime_lock_acquire_failed` | lock acquisition failed | lock path, other runs | delete the lock blindly | confirm no concurrent owner |
| `runtime_lock_acquire_uncertain` | lock acquisition uncertain | lock path, other runs | assume acquired/free | inspect lock state per runtime contract |
| `runtime_lock_release_failed` | lock release failed | lock residue, fd state | delete the lock blindly | inspect lock state per runtime contract |
| `runtime_lock_release_uncertain` | lock release uncertain | lock residue, fd state | immediately retry | inspect lock state before any retry |
| `runtime_lock_identity_mismatch` | replaced/foreign lock — never unlinked | lock identity/owner | unlink a foreign lock | investigate who owns the lock |
| `db_failed` | DB open/operation error | DB dir, sidecars, evidence/summary | rerun blindly | inspect DB state and review |
| `internal_runtime_error` | unexpected runtime error | evidence/summary, traceback location (not persisted) | treat as expected | inspect evidence/summary and review code |

KIS source subreasons (never print secret values, raw HTTP responses, or raw
frames):

```text
source_config_gate_failed:
  config/env issue. Check enabled/env/symbol/URLs. Do not print secrets.

source_approval_failed:
  approval key issue. Check live/mock mismatch, approval URL, app permission,
  quote contamination.
  Do not print raw HTTP response.
  Do not immediately retry with same settings.

source_connect_failed:
  websocket connect/open issue. Check websocket URL, DNS, TLS, firewall/network.
  Do not print raw websocket frame.
  Do not immediately retry with same settings.

source_failed:
  unclassified fallback. Treat as bug until classified.
```

## NEEDS_REVIEW taxonomy

`NEEDS_REVIEW` means the artifacts cannot mechanically confirm a clean PASS — most
often because the envelope-only cleanup/publication/lock fields are not available.

| cause | meaning | next |
| --- | --- | --- |
| missing `stdout-envelope.json` | `--envelope` not supplied | re-render with the captured envelope |
| empty `stdout-envelope.json` | envelope file is empty → treated as absent | re-capture the envelope from the correct run |
| malformed `stdout-envelope.json` | envelope JSON unparseable → treated as absent | re-capture the envelope from the correct run |
| envelope from wrong run | envelope contradicts the persisted summary | re-capture from the correct run |
| `missing_from_persisted_summary` | envelope-only fields absent from the merged view | supply the envelope; never invent the fields |
| validator pass blockers | one or more PASS clauses unmet | resolve the named blocker(s) |
| timeline malformed rows | evidence has unparseable rows (blocks PASS) | inspect/repair evidence capture |
| manual git/runtime hygiene missing | `git status --short` / `git ls-files runtime` not recorded | record both before claiming PASS |

```text
NEEDS_REVIEW is not PASS.
Do not infer envelope-only fields.
Do not hand-edit stdout-envelope.json.
Re-capture from the correct run or treat the run as unverifiable.
```

## Publication / envelope issues

The persisted `summary.json` holds only the mechanical summary; the
cleanup/publication/lock state lives **only** in the captured stdout envelope.
Interpret `summary_publication_outcome`:

```text
WRITTEN              -> byte equality / persisted summary claim allowed
PUBLISHED_INCOMPLETE -> destination likely exists but cleanup/fsync incomplete; PASS forbidden
PUBLICATION_UNCERTAIN-> persisted bytes cannot be trusted; PASS forbidden
NOT_WRITTEN          -> summary absent; PASS forbidden
```

Only `WRITTEN` permits trusting the persisted bytes (and the report's
byte-equality claim). The other three forbid PASS; do not hand-edit the envelope
or the summary to upgrade the state. Lock residue, fd-close failure, identity
mismatch, or uncertain release also forbids PASS, even under a fatal.

## Source readiness issues

Read the source readiness counters together before blaming the strategy:

```text
connect_attempts / connected     -> transport reached the endpoint at all
subscription_requests / acks     -> both trade + quote channels accepted
subscription_rejections          -> a rejected channel (subscription_rejected)
all_subscribed                   -> readiness reached
disconnects                      -> lifecycle closed
source_* reason counts           -> the classified source subreason, if any
```

A source error is never downgraded to `health_not_ready`: if a `source_*`
subreason is present, triage it as a FAIL source issue, not a NO_GO health gate.
A startup-only probe returns as soon as both subscription ACKs are observed; it
does not wait out the full duration for a market event.

## Journal / ledger issues

```text
nonterminal_journal > 0          -> journal left non-terminal; review termination
journal_uncertain (reason count) -> stop; review journal/ledger consistency
reconcile_required (reason count)-> stop; review journal/ledger consistency
journal_committed                -> committed terminal count
```

`journal_uncertain`, `reconcile_required`, and a stuck `nonterminal_journal` are
stop-and-review conditions. Do not rerun over the same journal/ledger before the
consistency question is resolved.

## Paper-only safety violations

Treat any of these as a **hard FAIL** regardless of `outcome`:

```text
any evidence row with sensitive_data_present = true
paper_only != true
activation_authorized = true
automatic_restart = true
real_order_adapter_constructed = true
```

These are immediate stop conditions: do not retry, do not continue, escalate. A
sensitive evidence row means a credential / raw HTTP response / raw frame may have
been captured — preserve the artifacts, do not print their contents, and escalate.

## Retry policy

```text
Safe to retry only after cause is isolated and fixed.
Never immediately retry:
  source_approval_failed
  source_connect_failed
  summary_publication_uncertain
  runtime_lock_release_uncertain
  journal_uncertain
  reconcile_required
  sensitive_data_present
```

Before any retry, confirm **all** of:

```text
fresh RUN_DIR
git status reviewed
git ls-files runtime empty
env length/strip check
no secret printing
operator attended
market session valid
```

The `env length/strip check` is the quote-contamination guard: compare
`APP_KEY` / `APP_SECRET` lengths and use a same-after-strip check; never print the
value. (Observed startup-3 failure: lengths `38`/`182` from copied quote
characters; after re-export `36`/`180` and startup-4 passed.)

## Escalation policy

Escalate (stop, preserve artifacts, hand to Reviewer) rather than retry when:

```text
any hard safety violation (sensitive_data_present / activation / auto-restart /
  real_order_adapter_constructed / paper_only != true)
summary_publication_uncertain or runtime_lock_release_uncertain
runtime_lock_identity_mismatch (foreign lock — never unlink)
journal_uncertain / reconcile_required / nonterminal_journal stuck
source_failed (unclassified — likely a bug)
source_close_failed / source_close_timeout (defective source)
repeated failure after one isolated-cause retry
```

When escalating, hand over `summary.json`, `evidence.jsonl`,
`stdout-envelope.json`, and `review-report.md`, and quote the **First failure**
section. Never paste secret values, raw HTTP responses, or raw frames.

## Operator handoff examples

Synthetic shapes (no secrets) live in
`docs/examples/paper_day_reports/README.md` and as renderable fixtures under
`tests/fixtures/paper_day_reports/`. For each non-PASS verdict, the handoff to the
Reviewer should read like:

```text
## Verdict
NO_GO (health_not_ready)
## First failure
stage: health
reason_code: health_not_ready
recorded_at: 2026-06-22T09:35:00+09:00
## Triage
NO_GO taxonomy -> health_not_ready: inspect data availability and health gating.
Do not retry outside the market session; do not change strategy; do not force PASS.
## Attachments
summary.json, evidence.jsonl, stdout-envelope.json, review-report.md
```

```text
## Verdict
FAIL (source_approval_failed)
## First failure
stage: source
reason_code: source_approval_failed
## Triage
FAIL taxonomy -> source_approval_failed: check live/mock mismatch, approval URL,
app permission, quote contamination. Do not print raw HTTP response. Do not
immediately retry with the same settings.
## Attachments
summary.json, evidence.jsonl, stdout-envelope.json, review-report.md
```

```text
## Verdict
NEEDS_REVIEW (missing_from_persisted_summary)
## Cause
stdout-envelope.json was not supplied / wrong run.
## Triage
Re-render with the captured envelope from the correct run. Do not infer
envelope-only fields; do not hand-edit stdout-envelope.json.
## Attachments
summary.json, evidence.jsonl, review-report.md (re-render with envelope)
```

The 1-day pilot remains **NO-GO** until a Reviewer PASS on the Monday attended
paper diagnostic. Live order / runtime activation / automatic restart remain
prohibited.
