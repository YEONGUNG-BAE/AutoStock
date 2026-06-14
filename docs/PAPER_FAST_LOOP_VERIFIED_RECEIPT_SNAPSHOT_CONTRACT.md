# Verified Receipt Snapshot Contract (RTM-7c.4j)

Freezes a verifier-`VALID` precheck receipt payload into a **single immutable observation**
so that downstream stages read the *same* receipt instead of re-verifying and re-reading the
raw mutable `dict` independently. The snapshot is built **once** from the untrusted payload;
every retained field is copied to an immutable value at build time.

**Runtime activation: NO-GO.** A snapshot is an *observation* — not author authentication,
not a signature, not Operator approval, not a freshness/TTL verdict, and not activation
authorization. The snapshot carries the receipt's verified activation posture, which is a
constant NO-GO.

Code: `composition.verified_precheck_receipt.verify_and_snapshot_precheck_receipt`

## API

```text
composition.verified_precheck_receipt.verify_and_snapshot_precheck_receipt(payload)
  -> VerifiedPrecheckReceiptResult(outcome, reasons, receipt)
```

Input: untrusted JSON-decoded object. Output carries `outcome` (`valid` / `invalid`), stable
`reasons`, and (on VALID) the frozen `receipt: VerifiedPrecheckReceipt`. On any non-VALID
verification or post-VALID structural surprise it returns the single stable reason
`receipt_snapshot_invalid` and `receipt = None`. No raw key/value, exception, or path is
surfaced.

## Why a single snapshot

Before 4j, the final preflight passed the **same raw `receipt_payload` dict** to both the 4g
byte-state revalidation and the 4i receipt time observation. Each independently called the
verifier and re-read the raw mutable dict, so a cross-stage mutation could produce a *mixed
observation* — a hash read from one state and an age read from another. The snapshot closes
this: verify once, freeze once, and have every stage read frozen fields.

## Immutability guarantees

- `VerifiedPrecheckReceipt` is a `@dataclass(frozen=True)`.
- `checked_at` is a timezone-aware `datetime`; `checked_at_iso` is the exact original
  canonical string bound into the receipt hash.
- `fingerprints_before` / `fingerprints_after` are tuples of frozen `ArtifactFingerprint`
  (each `sidecar_suffixes` a tuple) — no mutable list/dict reference is retained.
- A later mutation of the raw payload dict (or its nested lists) cannot change the snapshot
  or its `receipt_sha256`.

## What it reuses (builds nothing new)

Reuses the existing `verify_runtime_precheck_receipt_payload` and the shared schema parse
helpers (`parse_fingerprint_list`, `strict_bool`). It builds **no** new canonical verifier,
hash, or JSON parser. The verifier returns only `outcome` / `schema_version` /
`receipt_sha256` / `reason_codes`, so the snapshot builder re-reads the already-verified dict
to copy the parsed fields.

## Verified cores

The downstream lanes expose snapshot-based cores that take a `VerifiedPrecheckReceipt` and
never call the verifier or read the raw payload:

- `revalidate_verified_activation_candidate(*, settings, receipt, base_dir=None)`
- `assess_verified_receipt_time(*, receipt, now)`

The raw-payload public wrappers (`revalidate_activation_candidate`, `assess_receipt_time`)
remain for backward compatibility: each builds a snapshot once and delegates to its core.

## What this lane does **not** do

- Select a receipt-age / TTL / max-age threshold or render a freshness verdict.
- Authenticate the receipt, sign it, or evaluate an HMAC.
- Consume an Operator approval or assert writer-stop.
- Read a clock, open a SQLite connection, touch the network/credentials, or authorize
  activation.

## No clock read

The module reads no clock of its own (no `datetime.now` / `time.time`); time relationships
are observed by the caller-`now` 4i lane, not here.
