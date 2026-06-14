# Precheck Receipt Verification Contract (RTM-7c.4e)

Strict **structural + hash** verification of an untrusted precheck receipt JSON object.
Verification is **stdin-only** via `--verify-precheck-receipt`; no config load, no env read,
no DB access, no filesystem write, no network, no clock read.

**Runtime activation: NO-GO.** `VALID` means only: supported schema, canonical field
structure, semantic consistency, and `receipt_sha256` matches a payload recomputation.
It is **not** author authentication, approval, freshness proof, writer-stop proof, or
runtime authorization.

## API

```text
composition.precheck_receipt_verifier.verify_runtime_precheck_receipt_payload(payload)
  -> RuntimePrecheckReceiptVerification
```

Input: untrusted JSON-decoded object (typically from stdin). Output carries
`outcome` (`valid` / `invalid`), optional `schema_version` / `receipt_sha256`, and
stable `reason_codes` — never the original receipt body.

**Duplicate object keys** are rejected at the CLI stdin JSON layer
(`composition.precheck_receipt_stdin_json`) — not by the verifier API, which receives
already-decoded objects. Default `json.loads` last-key-wins is **not** used for stdin.

Shared schema lives in `composition.precheck_receipt_schema` (builder + verifier
single source). Invariant: every receipt returned successfully by
`build_runtime_precheck_receipt()` must verify `VALID` after JSON dict conversion.

## Strict top-level schema (schema_version = 1)

Required fields (exact set — unknown or missing fields rejected):

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
receipt_sha256
```

Rules:

- No bool/int coercion (`type(x) is bool` / `type(x) is int` only).
- `receipt_sha256`: lowercase 64-char hex.
- `checked_at`: ISO string, timezone-aware (no TTL/expiry/freshness check).
- `market`: exactly `"KR"`.
- `symbol`: exactly six decimal digits.
- `machine_outcome`: `pass` | `no_go`.
- `inspection_outcome`: `ok` | `no_go`.
- `reasons`: `list[str]`, each nonblank; order is part of the hash.
- Activation posture (exact): `activation_authorized=false`,
  `runtime_activation_outcome="no_go"`, both manual flags `true`.

Semantic consistency (fail-closed):

- machine `pass` → inspection `ok` and empty `reasons`.
- inspection `no_go` → machine `no_go`.
- machine `no_go` → at least one reason.

## Fingerprint schema

Exactly four artifacts in order (before **and** after):

```text
execution_inputs_snapshot
ledger
trigger_journal
active_decision_store
```

Each fingerprint object — exact fields only:

```text
name
present
is_regular_file
size
sha256
user_version
sidecar_suffixes
```

Sidecar suffixes: subset of `-wal`, `-shm`, `-journal` in that canonical generator
order; no duplicates.

Consistency: absent → null size/hash/user_version; irregular present → null
size/hash/user_version; regular present → non-negative `size` + hex `sha256`;
JSON snapshot → `user_version` null.

## Hash verification

After structural + semantic validation:

1. Build canonical payload (all fields except `receipt_sha256`) using existing
   `decision.canonical_json.payload_sha256`.
2. Compare to stored `receipt_sha256`.
3. Mismatch → `receipt_hash_mismatch`.

## Stable reason codes

```text
receipt_not_object
receipt_unknown_field
receipt_missing_field
receipt_unsupported_schema
receipt_invalid_field
receipt_invalid_checked_at
receipt_invalid_market
receipt_invalid_symbol
receipt_invalid_outcome
receipt_invalid_activation_posture
receipt_invalid_fingerprint_count
receipt_invalid_fingerprint_order
receipt_invalid_fingerprint
receipt_semantic_mismatch
receipt_hash_mismatch
```

Input-layer CLI codes (stdin JSON parse + bound):

```text
receipt_input_empty
receipt_input_not_utf8
receipt_input_not_json
receipt_input_too_large
receipt_input_too_deep
receipt_input_duplicate_key
```

### Stdin JSON fail-closed (RTM-7c.4e safety closure)

Pathological stdin never escapes as an uncaught exception:

- oversized integer `ValueError` → `receipt_input_not_json`
- `RecursionError` (deep nesting) → `receipt_input_too_deep`
- duplicate object member (any nesting depth) → `receipt_input_duplicate_key`
- non-standard constants (`NaN`, `Infinity`, `-Infinity`) via `parse_constant` →
  `receipt_input_not_json`

Output never includes raw JSON, offending numbers, duplicate key names, nesting
content, tracebacks, or exception reprs. All invalid parse cases: exit `1`,
`outcome=INVALID`, activation fields false/no_go.

## CLI (`--verify-precheck-receipt`)

- Mutually exclusive with all other modes.
- Processed **before** `load_settings()` (no config path read).
- Stdin only — **receipt object root**, not the full precheck summary envelope.
  Operator must pass the nested `precheck_receipt` object from RTM-7c.4d JSON.
- Max stdin size: **1 MiB** — reader requests exactly `limit + 1` bytes; at exactly
  `limit` bytes (valid receipt JSON + trailing whitespace padding) parses and may
  verify `VALID`; at `limit + 1` → `receipt_input_too_large` without reading further.
- Exit `0` iff `VALID`, else `1`.
- JSON includes `activation_authorized=false`, `runtime_activation_outcome="no_go"`,
  and no-side-effect attestations (`credential_read`, `network_called`,
  `database_opened`, `filesystem_written` all false).

## Builder validation (shared schema)

`composition.precheck_receipt_schema` + `build_runtime_precheck_receipt` fail-closed
on malformed `checked_at`, invalid market/symbol, outcome semantic mismatch,
fingerprint count/order/names, and fingerprint field semantics
(`PrecheckReceiptError` with stable `reason_code` only — no raw values).

Canonical artifact order violations use `receipt_invalid_fingerprint_order` only
(`receipt_fingerprint_identity_mismatch` removed — unreachable once both lists must
match canonical order).

## Out of scope

Signing/HMAC, file input/persistence, envelope extraction, TTL/freshness, approval
input, activation token, runtime activation.

See also `docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md`.
