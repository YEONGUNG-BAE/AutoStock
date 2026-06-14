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
verification, clone failure, or post-VALID structural surprise it returns the single stable
reason `receipt_snapshot_invalid` and `receipt = None`. No raw key/value, exception, or path
is surfaced.

## Why a single snapshot

Before 4j, the final preflight passed the **same raw `receipt_payload` dict** to both the 4g
byte-state revalidation and the 4i receipt time observation. Each independently called the
verifier and re-read the raw mutable dict, so a cross-stage mutation could produce a *mixed
observation* — a hash read from one state and an age read from another. The snapshot closes
this: verify once, freeze once, and have every stage read frozen fields.

## Atomic verify-and-snapshot (verify/copy TOCTOU closure)

Processing order:

```text
caller payload
  → strict built-in JSON-tree clone (_clone_receipt_payload / _clone_json_tree)
  → private detached built-in tree
  → detached tree verifier (verify_runtime_precheck_receipt_payload)
  → same detached tree immutable snapshot (_snapshot_from_verified_payload)
```

The verifier argument object and snapshot extraction object are **identical** detached
built-in trees with identity separate from the caller input (top-level and every mutable
nested container). The caller-owned payload is **never** passed directly to the verifier and
is **not** re-read after clone. Once the strict detached clone completes, subsequent caller
mutation cannot affect verifier or snapshot observation.

**Concurrency contract (precise):** this lane does **not** claim point-in-time atomic snapshot
across concurrent caller mutation during clone; it does **not** use locks or thread
synchronization. The guarantee is post-clone isolation: verifier and snapshot extraction share
the same detached built-in tree, which must pass schema/semantic/hash validation.

## Strict detached JSON-tree clone (RTM-7c.4j closure)

Clone uses `_clone_json_tree` — **not** `copy.deepcopy` and **not** caller-defined
`__deepcopy__` hooks.

**Allowed exact built-in types only** (`isinstance` is **not** used — `type(value) is …`):

```text
type(value) is dict
type(value) is list
type(value) is str
type(value) is int
type(value) is bool
value is None
```

Custom container/scalar subclasses are rejected at clone time. Dict keys must be exact `str`.
Each dict/list node is a new built-in container; caller references are never retained.
Repeated non-cyclic references are cloned independently at each position (JSON has no alias
semantics).

**Fail-closed clone paths** (all → `receipt_snapshot_invalid`, verifier call count `0`, no raw
exception/value/path leak):

- Top-level or nested custom container/scalar subclass
- Cyclic dict/list structures (not valid JSON trees)
- Excessive nesting (`RecursionError` during clone)
- Non-JSON scalar types (including `float` — receipt schema uses integers only)

Clone failure (including the above) is fail-closed with no raw exception, key, value, or path
leak.

## Immutability guarantees

- `VerifiedPrecheckReceipt` is a `@dataclass(frozen=True)`.
- `checked_at` is a timezone-aware `datetime`; `checked_at_iso` is the exact verified source
  string bound into the receipt hash (the verifier validates a parseable timezone-aware ISO
  string; it does not canonicalize datetime strings — different ISO forms may denote the same
  instant).
- `fingerprints_before` / `fingerprints_after` are tuples of frozen `ArtifactFingerprint`
  (each `sidecar_suffixes` a tuple) — no mutable list/dict reference is retained.
- A later mutation of the raw payload dict (or its nested lists) cannot change the snapshot
  or its `receipt_sha256`.

## What it reuses (builds nothing new)

Reuses the existing `verify_runtime_precheck_receipt_payload` and the shared schema parse
helpers (`parse_fingerprint_list`, `strict_bool`). It builds **no** new canonical verifier,
hash, or JSON parser. Detached copy uses strict built-in JSON-tree clone only.

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
