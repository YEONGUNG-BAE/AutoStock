# Ephemeral Precheck State Receipt Contract (RTM-7c.4d)

Read-only operator precheck **only**. This lane adds an **ephemeral receipt** that
binds the mechanical precheck outcome to the exact observed artifact fingerprints,
reason set, market/symbol, and observation timestamp. The receipt exists **only** in
stdout / return objects — it is **never** written to disk.

**Runtime activation: NO-GO.** A receipt on a machine `PASS` is **not** runtime
authorization, **not** Operator approval, and **not** writer-stop confirmation.

## Receipt schema (`RuntimePrecheckReceipt`)

| field | type | notes |
|-------|------|-------|
| `schema_version` | int | currently `1` (`PRECHECK_RECEIPT_SCHEMA_VERSION`) |
| `checked_at` | str | ISO string from the timezone-aware `now` passed to `precheck_runtime` (no extra clock read) |
| `market` | str | from settings |
| `symbol` | str | from settings |
| `enabled` | bool | from settings |
| `machine_outcome` | str | `pass` / `no_go` |
| `inspection_outcome` | str | `ok` / `no_go` |
| `reasons` | tuple[str, …] | aggregate precheck reasons |
| `fingerprints_before` | tuple[ArtifactFingerprint, …] | fixed `_PRECHECK_ARTIFACTS` order |
| `fingerprints_after` | tuple[ArtifactFingerprint, …] | same order |
| `activation_authorized` | bool | always `false` |
| `runtime_activation_outcome` | str | always `"no_go"` |
| `explicit_operator_approval_required` | bool | always `true` |
| `writers_stopped_manual_confirmation_required` | bool | always `true` |
| `receipt_sha256` | str | canonical hash of the payload below |

`RuntimePrecheckResult.receipt` always carries a receipt for both `PASS` and `NO_GO`.
The four activation fields on the outer result and on the receipt are identical (single
builder source: `_precheck_activation_posture()`).

## Canonical hash payload

Computed with existing `decision.canonical_json.canonical_json_dumps` +
`decision.canonical_json.payload_sha256`. No duplicate canonicalizer.

Fields included (in sorted-key JSON after canonicalization):

```text
schema_version
checked_at
market
symbol
enabled
machine_outcome
inspection_outcome
reasons
fingerprints_before
fingerprints_after
activation_authorized
runtime_activation_outcome
explicit_operator_approval_required
writers_stopped_manual_confirmation_required
```

`receipt_sha256` itself is **not** part of the hash input.

### Fingerprint sub-payload (per artifact, fixed order)

```text
name
present
is_regular_file
size
sha256
user_version
sidecar_suffixes
```

Artifact order follows `_PRECHECK_ARTIFACTS`:

1. `execution_inputs_snapshot`
2. `ledger`
3. `trigger_journal`
4. `active_decision_store`

### Fingerprint semantics (shared with RTM-7c.4e verifier)

Strict types: `present`/`is_regular_file` require `type(x) is bool` (int-as-bool
rejected). Absent canonical: `present=false`, `is_regular_file=false`, all other
fields null/empty sidecar. Builder validation uses the same
`validate_fingerprint_semantics` as the verifier; builder success → verifier VALID
after JSON dict conversion.

## Explicit exclusions from receipt payload

Never included in the hash or CLI JSON receipt:

- absolute path
- config file contents / config path
- snapshot raw JSON / active bundle JSON
- DB rows
- credential or env name / value
- traceback / exception message
- git HEAD / hostname / username

## Security and non-security properties

**What `receipt_sha256` is:**

- A deterministic identifier for one canonical observation snapshot
- Useful to detect accidental alteration or payload mismatch when comparing two
  operator-reviewed outputs
- Binds machine outcome to exact observed fingerprints and reasons

**What `receipt_sha256` is not:**

- An electronic signature, MAC, or authenticated attestation
- Operator approval or runtime authorization
- A freshness proof or writer-stop proof
- Concurrent-writer absence proof
- Mutate-then-restore detection
- A guarantee of future state

Anyone can construct a payload and recompute the hash; the receipt provides **no**
trust-anchor or identity authentication.

## Out of scope (this lane)

- signing key / HMAC / public-key signature
- receipt file persistence
- approval input flags / activation flags
- TTL / expiry thresholds
- persisted activation epoch

Verification (RTM-7c.4e): ``composition.precheck_receipt_verifier`` or CLI
``--verify-precheck-receipt`` (stdin-only receipt object). See
``docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_VERIFICATION_CONTRACT.md``.

## CLI (`--precheck-runtime --json`)

Top-level fields from RTM-7c.4c remain for compatibility. Added nested object:

```text
precheck_receipt
```

Contains all sanitized receipt fields including `receipt_sha256`. Hash generation
failure is fail-closed via existing top-level `precheck error: <ExceptionType>`
sanitization (no raw exception text).

See also `docs/PAPER_FAST_LOOP_RUNTIME_PRECHECK_CONTRACT.md` for the underlying
precheck semantics and quiescence limits.
