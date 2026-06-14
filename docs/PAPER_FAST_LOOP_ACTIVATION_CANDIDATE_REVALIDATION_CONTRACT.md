# Activation Candidate Revalidation Contract (RTM-7c.4g)

Read-only **approval-time state revalidation** for the paper fast-loop activation
candidate. Compares a **verifier-VALID machine PASS** precheck receipt against the
**current** on-disk artifact state and **current** config binding.

**Runtime activation: NO-GO.** Mechanical revalidation PASS is **not** Operator
approval, writer-stop proof, receipt authenticity, freshness evaluation, or
activation authorization.

Code: `composition.activation_candidate_revalidation.revalidate_activation_candidate`
CLI: `ops/run_paper_fast_loop.py --revalidate-activation-candidate`

## What revalidation means

> The receipt's post-inspection artifact observation (`fingerprints_after`) still
> matches the current artifact state, and receipt `market` / `symbol` / `enabled`
> match the current config.

## What revalidation does **not** mean

- Operator approval input or consumption
- Writer-stop machine assertion (manual confirmation only)
- Receipt authenticity / signing / HMAC
- Freshness / TTL evaluation (`freshness_evaluated` is always `false`)
- Process ownership or concurrent-writer absence proof
- Runtime activation authorization
- Mutate-then-restore detection within the revalidation window

## Processing order (pure API)

1. Run `verify_runtime_precheck_receipt_payload` (existing verifier — no duplicate hash logic)
2. Verifier `INVALID` → `NO_GO` / `candidate_receipt_invalid`
3. Receipt must be machine PASS (`machine_outcome=pass`, `inspection_outcome=ok`, empty `reasons`) — else `candidate_receipt_not_pass`
4. Config binding (current `RuntimePaperFastLoopSettings` vs receipt fields)
5. Fingerprint current artifacts — **first pass** (`current_before`)
6. Fingerprint current artifacts — **second pass** (`current_after`)
7. `current_before == current_after == receipt.fingerprints_after` → mechanical `PASS`

No separate clock read. No SQLite connection (header-byte `user_version` only).

## Comparison target

Approval-time artifact comparison targets **`receipt.fingerprints_after`** only —
the post-inspection observation bound into the receipt. Production machine PASS
receipts also satisfy `fingerprints_before == fingerprints_after`, but revalidation
always compares current state to **`fingerprints_after`**.

## Two current fingerprint passes

Current on-disk state is fingerprinted twice in one revalidation call:

```text
current_before  →  current_after
```

- `candidate_current_artifact_drift:<artifact>` — `current_before != current_after`
  for that artifact (revalidation-window net drift; drift reason owns root cause)
- `candidate_receipt_artifact_mismatch:<artifact>` — stable current fingerprint
  (`current_before == current_after`) differs from `receipt.fingerprints_after`

Equality checks all seven normalized fingerprint fields per artifact (same as
precheck / receipt observation semantics).

Revalidation-window equality proves **net observable equality only** — not writer
absence, not mutate-then-restore detection.

## Config binding

Current settings (from config loader) vs receipt:

| check | failure reason |
|-------|----------------|
| `settings.enabled == false` | `candidate_config_disabled` |
| `receipt.market != settings.market` | `candidate_market_mismatch` |
| `receipt.symbol != settings.symbol` | `candidate_symbol_mismatch` |
| `receipt.enabled != settings.enabled` | `candidate_enabled_mismatch` |

CLI loads config with `load_settings(config_path, environ={})` — no `os.environ`
access; `${...}` substitution fails closed.

## Stable reason codes

One canonical reason per root cause. Artifact reasons follow canonical order:

```text
execution_inputs_snapshot
ledger
trigger_journal
active_decision_store
```

| reason | meaning |
|--------|---------|
| `candidate_receipt_invalid` | verifier `INVALID` |
| `candidate_receipt_not_pass` | structurally valid but not machine PASS |
| `candidate_config_disabled` | `settings.enabled == false` |
| `candidate_market_mismatch` | market ≠ current config |
| `candidate_symbol_mismatch` | symbol ≠ current config |
| `candidate_enabled_mismatch` | receipt `enabled` ≠ settings `enabled` |
| `candidate_current_artifact_drift:<artifact>` | current before/after differ |
| `candidate_receipt_artifact_mismatch:<artifact>` | stable current ≠ receipt after |
| `candidate_artifact_unreadable:<artifact>` | raw fingerprint read raised `OSError` |

Raw paths, SHA values, DB contents, exception types, and config secrets never appear
in reasons.

## Fail-closed artifact reads (H1)

Each artifact is fingerprinted by reading its bytes directly. A missing path is **not**
an error (it yields an all-`None` fingerprint); only a genuine read failure
(`FileNotFoundError` / `PermissionError` / generic `OSError`, including a TOCTOU
file-replaced-between-probe-and-open race) produces a stable
`candidate_artifact_unreadable:<artifact>` reason in canonical order. On any unreadable
artifact the partial fingerprints are discarded so no synthetic observation is treated
as healthy, and the call is a `NO_GO`. The raw exception type, message, and path are
never surfaced.

## Fixed activation posture (every path)

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
explicit_operator_approval_required = true
writers_stopped_manual_confirmation_required = true
freshness_evaluated = false
```

Mechanical `PASS` does **not** set `activation_authorized=true`.

## CLI contract

Mode: `--revalidate-activation-candidate` (mutually exclusive with other modes).

| input | rule |
|-------|------|
| receipt | stdin JSON only (strict bounded parser; 1 MiB) |
| config | `--config` (default `config/config.toml.example`) |
| envelope | none — raw receipt object only |
| file input | none |

Processing order in CLI: `--run` early refusal → `--verify-precheck-receipt` →
`--revalidate-activation-candidate` → other modes.

| outcome | exit code |
|---------|-----------|
| mechanical `PASS` | 0 |
| `NO_GO` | 1 |
| config / stdin / internal sanitized failure | 1 |

JSON fields include `credential_read`, `network_called`, `database_opened`,
`filesystem_written` — all `false` on success paths.

## Out of scope (this lane)

- Operator approval input or storage
- Approval receipt-hash binding
- Receipt signing / HMAC
- Freshness / TTL policy
- Activation token or activation CLI/caller
- `--run` implementation
- KIS / network / broker dispatch / orders
- Operational SQLite connection / write
- Schema migration / journal reconcile
- Process scan / kill / OS lock
- Daemon / scheduler / multi-symbol / production calendar

## RTM-7c.4j — verified snapshot core

`revalidate_verified_activation_candidate(*, settings, receipt, base_dir=None)` is the
snapshot-based core: it takes an immutable `VerifiedPrecheckReceipt`, reads only frozen
snapshot fields, and calls **no** verifier and never touches the raw payload dict. The
public `revalidate_activation_candidate` is now a raw-payload wrapper that builds a snapshot
once (`verify_and_snapshot_precheck_receipt`; non-VALID → `candidate_receipt_invalid`) and
delegates to the core. `fingerprints_after` is read straight off the snapshot — no re-parse.
The snapshot builder strict-clones the caller payload to a detached built-in JSON tree before
verify/extract (no `copy.deepcopy` / caller hooks); once clone completes, caller mutation
cannot affect the frozen observation. See `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md`.

## Related contracts

- `PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md` — receipt observation binding
- `PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md` — verifier semantics
- `PAPER_FAST_LOOP_VERIFIED_RECEIPT_SNAPSHOT_CONTRACT.md` — immutable snapshot consumed by the core
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — activation stage model
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root + CLI modes
