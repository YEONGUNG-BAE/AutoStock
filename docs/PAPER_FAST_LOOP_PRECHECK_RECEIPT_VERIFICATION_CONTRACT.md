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
receipt_invalid_outcome
receipt_invalid_activation_posture
receipt_invalid_fingerprint_count
receipt_invalid_fingerprint_order
receipt_invalid_fingerprint
receipt_fingerprint_identity_mismatch
receipt_semantic_mismatch
receipt_hash_mismatch
```

Input-layer CLI codes (stdin bound):

```text
receipt_input_empty
receipt_input_not_utf8
receipt_input_not_json
receipt_input_too_large
```

Errors never echo raw input values, paths, or hash payloads.

## CLI (`--verify-precheck-receipt`)

- Mutually exclusive with all other modes.
- Processed **before** `load_settings()` (no config path read).
- Stdin only — **receipt object root**, not the full precheck summary envelope.
  Operator must pass the nested `precheck_receipt` object from RTM-7c.4d JSON.
- Max stdin size: **1 MiB** (`limit + 1` byte probe; oversize →
  `receipt_input_too_large`).
- Exit `0` iff `VALID`, else `1`.
- JSON includes `activation_authorized=false`, `runtime_activation_outcome="no_go"`,
  and no-side-effect attestations (`credential_read`, `network_called`,
  `database_opened`, `filesystem_written` all false).

## Builder validation (RTM-7c.4e carry-over)

`build_runtime_precheck_receipt` fail-closed on malformed `checked_at`, wrong
fingerprint count/order/names, or before/after name-sequence mismatch
(`PrecheckReceiptError` with stable `reason_code` only).

## Out of scope

Signing/HMAC, file input/persistence, envelope extraction, TTL/freshness, approval
input, activation token, runtime activation.

See also `docs/PAPER_FAST_LOOP_PRECHECK_RECEIPT_CONTRACT.md`.
