# Attended One-Shot Activation Contract (RTM-7c.4f)

Design freeze for **attended one-shot** paper fast-loop runtime activation.
This lane defines the contract, pure stage model, and NO-GO safety proof only.

**Runtime activation: NO-GO.** No activation CLI, no `--run` implementation, no KIS
connection, no market stream, no monitor thread/process, no broker dispatch, no orders,
no operational DB writes, no approval input, no approval persistence.

Code model: `composition.attended_activation.AttendedActivationStage` (pure enum; no
side effects).

## Activation stage model

Stages are **logical** — not all are reachable today. The current codebase always
terminates at `ACTIVATION_NOT_IMPLEMENTED`.

```text
DISABLED
  → PRECHECK_MACHINE_PASS          (machine only; not approval)
  → RECEIPT_STRUCTURALLY_VALID     (schema + hash; not auth/freshness/approval)
  → WRITER_STOP_CONFIRMATION_REQUIRED   (manual; not machine-verified)
  → OPERATOR_APPROVAL_REQUIRED     (manual; not consumed or stored today)
  → ACTIVATION_NOT_IMPLEMENTED     (hard terminal — no caller exists)
```

| stage | enum value | decided by | today |
|-------|------------|------------|-------|
| Disabled | `disabled` | config / operator | default when `enabled=false` or tooling-only |
| Machine precheck pass | `precheck_machine_pass` | `precheck_runtime` | reachable; **not** activation |
| Receipt structurally valid | `receipt_structurally_valid` | `--verify-precheck-receipt` | reachable; **not** activation |
| Writer-stop confirmation | `writer_stop_confirmation_required` | Operator manual | required flag always `true`; **not consumed** |
| Operator approval | `operator_approval_required` | Operator manual | required flag always `true`; **not consumed** |
| Activation not implemented | `activation_not_implemented` | codebase invariant | **always terminal** |

### Hard invariant (current code)

Every path — precheck PASS, precheck NO_GO, receipt VALID, receipt INVALID,
malformed stdin, `--run` — must satisfy:

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
```

There is **no** code path that sets `activation_authorized=true`.

## Relationship to prior lanes

| prior gate | contract doc | what it is **not** |
|------------|--------------|-------------------|
| Machine precheck PASS | `PAPER_FAST_LOOP_RUNTIME_PRECHECK_CONTRACT.md` | Operator approval, writer-stop proof, activation |
| Receipt `receipt_sha256` | `PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md` | signature, freshness, approval |
| Receipt verifier VALID | `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md` | authentication, approval, activation |
| Activation candidate revalidation PASS | `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_REVALIDATION_CONTRACT.md` | approval, writer-stop, freshness, activation |
| Time-aware final preflight PASS | `PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FINAL_PREFLIGHT_CONTRACT.md` | receipt-age, freshness, approval, writer-stop, activation |
| Composition offline stack | `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` | live runtime caller |

## Future activation prerequisites

### Machine-verifiable (future lane must enforce)

- `[runtime.paper_fast_loop] enabled=true`
- Precheck machine `PASS` (`MachineCheckOutcome.PASS`)
- Inspection outcome `OK`; aggregate `reasons` empty
- Receipt verifier `VALID` on the operator-reviewed receipt object
- Receipt `market` / `symbol` match current config
- Approval-time fresh fingerprint comparison targets **`receipt.fingerprints_after`**
  (the post-inspection observation bound into the receipt)
- Machine `PASS` receipt requires **`fingerprints_before == fingerprints_after`**
  (canonical normalized payload equality on all seven fingerprint fields per artifact)
- Verifier `VALID` alone does **not** prove no-drift — only a receipt that also
  satisfies the shared observation semantic rules (`validate_observation_semantics`)
  carries that meaning
- Nonterminal journal row count `0`
- Database quiescence (no live `-wal`/`-shm`/`-journal` on configured DBs)

### Manual or policy-dependent (OPEN — no default values)

- All slow/fast-loop writers stopped (manual attestation only today)
- Explicit Operator approval (input mechanism **OPEN**)
- Receipt freshness / maximum age policy (**OPEN**)
- Binding of approval to receipt hash (**OPEN**)
- Process ownership and single-instance guarantee (**OPEN**)
- Activation epoch persistence policy (**OPEN**)
- Crash/restart recovery policy (**OPEN**)
- Calendar/session policy operational version (**OPEN**)

Unset policy items stay **OPEN**. RTM-7c.4g implements the machine-verifiable
**approval-time revalidation** step (current artifact state vs `receipt.fingerprints_after`
+ config binding) without consuming approval or authorizing activation. RTM-7c.4h adds
the **time-aware final preflight** step: 4g byte-state revalidation composed with a
fresh caller-time precheck, so a byte-identical snapshot / active-decision whose
validity window has since opened or closed is caught. The per-call
`fresh_precheck_executed` flag records whether that fresh precheck actually ran (false for
short-circuit NO_GOs that return before it). RTM-7c.4i adds a **policy-neutral receipt time
observation** between the 4g revalidation and the fresh precheck: it records the exact
`receipt_age_microseconds` and **fail-closes a future receipt** (`checked_at` after the
caller `now`, `candidate_receipt_time_in_future`, `fresh_precheck_executed=false`); the
per-call `receipt_age_evaluated` flag flips `true` once that comparison runs. It still does
**not** apply any receipt-age threshold, TTL, or freshness policy
(`freshness_policy_evaluated=false`) — age is observed, never thresholded. RTM-7c.4k adds a
**separate pure evaluator** (`evaluate_receipt_freshness`) that verdicts against an explicit
caller-supplied max-age when invoked — it is **not** wired into the final-preflight wrapper.
RTM-7c.4l adds API-only freshness-qualified preflight (`freshness_qualify_activation_candidate`)
with a required explicit `policy` argument — still not approval or activation. Receipt
freshness threshold selection, config/CLI binding, authenticity, and Operator approval binding
remain **OPEN**.

## Orphan sidecar observation limit

`composition.sqlite_inspector.fingerprint_artifact` probes sidecar suffixes only when
the **main** artifact path exists. When the main file is absent, the fingerprint
returns the absent canonical state:

```text
present=false
is_regular_file=false
sidecar_suffixes=[]
```

even if orphan `ledger.sqlite3-wal`, `-shm`, or `-journal` files exist on disk.
`sidecar_files(path)` **does** observe orphan sidecars independently, but that set is
**not** copied into the absent-main fingerprint.

Implications:

- Missing main artifact already yields machine `NO_GO` (`missing_database:<db>`) — there
  is no false activation PASS from orphan sidecars alone.
- Future activation must not treat absent-main + orphan-sidecar as quiescent without
  an explicit binding improvement (**OPEN**: orphan-sidecar state binding).

## Runtime activation surface inventory

Static classification of symbols that could be mistaken for live activation.
No secret/config/raw DB values listed.

### offline-only

| location | symbol / mode | notes |
|----------|---------------|-------|
| `ops/run_paper_fast_loop.py` | `--validate-only`, `--inspect-existing`, `--precheck-runtime`, `--verify-precheck-receipt`, `--revalidate-activation-candidate`, `--final-preflight-activation-candidate`, `--replay` | operator CLI; no live runtime |
| `src/composition/paper_fast_loop.py` | `build_paper_fast_loop_plan`, `inspect_paper_fast_loop`, `precheck_runtime`, `replay_offline` | composition root; replay uses temp dir |
| `src/composition/precheck_receipt_verifier.py` | `verify_runtime_precheck_receipt_payload` | stdin/API verification only |
| `tests/test_full_day_two_loop_rehearsal.py` | full-day rehearsal | fake clock; temp paths |
| `tests/test_paper_fast_loop_composition.py` | composition E2E | temp fixtures |

### read-only transport / smoke (manual `--run`; not fast-loop activation)

| location | symbol | notes |
|----------|--------|-------|
| `ops/run_kis_read_only_smoke.py` | `--run` | KIS HTTP read-only; explicit opt-in |
| `ops/run_kis_ws_readonly_smoke.py` | `--run` | KIS WS bounded smoke; confirmation env |
| `ops/run_kis_market_monitor.py` | bounded fake transport | not production daemon |
| `ops/rehearse_market_supervisor.py` | fixture monitor | rehearsal only |

### composition but no live caller

| location | symbol | notes |
|----------|--------|-------|
| `src/composition/paper_fast_loop.py` | `PaperExecutionCoordinator`, `_build_stack` | wired only inside `replay_offline` |
| `src/orchestration/fast_loop_execution.py` | `FastLoopExecutionOrchestrator` | library; no production daemon |
| `src/orchestration/decision_refresh_scheduler.py` | scheduler | tests/rehearsal only |
| `src/market_data/monitor.py` | `MarketMonitor` | smoke/rehearsal injectables |

### explicitly refused

| location | symbol | notes |
|----------|--------|-------|
| `ops/run_paper_fast_loop.py` | `--run` | exit **2** before `load_settings`; `live_run_not_implemented` |

### future OPEN

| item | notes |
|------|-------|
| Attended one-shot activation CLI / caller | **not implemented** |
| Fast-loop `--run` implementation | refused |
| Daemon / launchd / scheduler wiring for fast loop | not started |
| Approval input / persistence | not started |
| Receipt signing / HMAC | not started |
| Activation epoch store | not started |
| Orphan-sidecar fingerprint binding | not started |

**No new activation caller exists in this lane.** If one appears outside this
contract, treat it as a scope violation and report immediately.

## `--run` early refusal (`ops/run_paper_fast_loop.py`)

`--run` is evaluated immediately after mode resolution, **before**:

- `load_settings` (config / env / credential gates)
- any SQLite open
- any network / broker call
- any filesystem write under configured `runtime/` paths

JSON summary includes `activation_authorized=false`,
`runtime_activation_outcome="no_go"`, and side-effect attestations all `false`.

## Out of scope (this lane)

KIS websocket/HTTP/socket/DNS; credential/env secret read; activation CLI; `--run`
implementation; daemon/launchd/systemd/cron; scheduler supervision; broker dispatch;
orders/fills; operational DB write; schema migration; journal reconcile; receipt
signing/HMAC; approval input/persistence; activation token; TTL/freshness defaults;
OS lock/process scan/kill; multi-symbol; production calendar; throughput/latency
calibration; unattended pilot; persistent activation epoch.

## See also

- `docs/PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md`
- `docs/PAPER_FAST_LOOP_RUNTIME_PRECHECK_CONTRACT.md`
- `docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md`
- `docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md`
- `docs/PAPER_FAST_LOOP_RECEIPT_FRESHNESS_POLICY_CONTRACT.md` (RTM-7c.4k — explicit policy only; not composed into final-preflight wrapper)
- `docs/PAPER_FAST_LOOP_ACTIVATION_CANDIDATE_FRESHNESS_PREFLIGHT_CONTRACT.md` (RTM-7c.4l — API-only qualified preflight)
- `docs/TECH_DEBT.md` (OPEN items)
