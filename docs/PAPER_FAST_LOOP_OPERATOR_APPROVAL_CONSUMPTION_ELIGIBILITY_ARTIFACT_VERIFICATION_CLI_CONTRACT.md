# Operator Approval Consumption Eligibility Artifact Verification CLI Contract (RTM-7c.4v)

Operator-facing **stdin-only, read-only** CLI mode that exposes the RTM-7c.4u eligibility-artifact
verifier API. It judges whether a **serialized** eligibility artifact is internally
schema·semantic·hash consistent and emits a sanitized JSON verdict.

**This is verification, not authentication or consumption.** No persistence, file output, actual
approval consumption, consumed marker, replay/nonce/idempotency, signing/HMAC, Operator identity
authentication, origin/provenance verification, TTL/freshness re-evaluation, activation
caller/token, or runtime activation. Runtime activation is constant NO-GO.

CLI: `ops/run_paper_fast_loop.py --verify-approval-consumption-eligibility-artifact --json`
API used (exactly once): `composition.operator_approval_consumption_eligibility_artifact_verifier.verify_operator_approval_consumption_eligibility_artifact_payload`

## Mode and arguments

Mutually exclusive with all other modes. Required:

```text
--verify-approval-consumption-eligibility-artifact
--json
```

Forbidden (→ `outcome=FAIL`, `eligibility_artifact_verification_argument_not_applicable`, exit 1):

```text
--config
--max-age-microseconds
--operator-approval-declared
--writers-stopped-manually-confirmed
--live-orders-forbidden-confirmed
```

Any other execution/verification mode combined with this flag → `outcome=FAIL`,
`eligibility_artifact_verification_mode_conflict`, exit 1. This artifact-specific conflict envelope
takes precedence over the approval-intent conflict envelope when both participate.

Missing `--json` → `outcome=FAIL`, `eligibility_artifact_verification_json_required`, exit 1.

`--run` takes precedence over every new argument: any combination including `--run` returns the
run-refused envelope (`outcome=NO_GO` / `live_run_not_implemented`) with **exit 2** before mode
resolution, stdin read, or any side effect.

### Applicability precedence

```text
--run (exit 2)  >  mode conflict  >  --json required  >  argument not applicable  >  stdin read
```

## Processing order

```text
1. argparse / --run early refusal (exit 2)
2. mode resolution
3. mode argument applicability (explicit --json + forbidden args)
4. bounded stdin read
5. strict JSON parse
6. verifier exactly once
7. JSON summary
```

In verify mode the CLI never calls `load_settings`, reads `os.environ`, reads the clock
(`datetime.now`/`time.time`), opens SQLite, constructs stores, runs precheck / evidence builder /
eligibility API / intent verifier, touches the broker, the network, or writes the filesystem.

## Stdin boundary

Reuses the bounded strict JSON parser (`parse_receipt_stdin_json`). Bound: 1 MiB; the CLI reads
`limit + 1` bytes. These stdin boundary failures are `outcome=FAIL` (verification not started) —
the parser never reaches the verifier:

```text
eligibility_artifact_input_empty
eligibility_artifact_input_not_utf8
eligibility_artifact_input_not_json
eligibility_artifact_input_too_large
eligibility_artifact_input_too_deep
eligibility_artifact_input_duplicate_key
eligibility_artifact_input_read_error
```

The parser only requires a syntactically valid JSON value. **The verifier — not the parser —
requires an exact object root**: a parsed JSON `list`/`string`/`null` reaches the verifier and is
rejected `INVALID` / `eligibility_artifact_not_object`.

Raw stdin, duplicate keys, numbers, paths, and exception text are never echoed.

## Outcome taxonomy

Three disjoint outcomes share one stable key set and constant posture:

| Outcome | Exit | Meaning | reason_codes | metadata |
|---------|------|---------|--------------|----------|
| FAIL | 1 | verification **not started** — CLI/argument/input boundary | one stable CLI/input reason | all `null` |
| INVALID | 1 | payload reached the verifier and was **rejected** | verifier stable reason code | only verifier-validated values |
| VALID | 0 | schema/semantic/hash **consistency only** | `[]` | verified exact lowercase hex64 |

Stable key set (every outcome): `outcome`, `mode`, `schema_version`,
`approval_intent_schema_version`, `approval_intent_sha256`, `candidate_evidence_schema_version`,
`candidate_evidence_sha256`, `eligibility_artifact_sha256`, `reason_codes`, plus the constant
posture below. `reason_codes` is always a list — there is no singular `reason_code` key on this
envelope. FAIL emits all metadata digests/schemas as `null`; INVALID malformed digests are not
echoed.

Every path emits the constant posture:

```text
activation_authorized = false
runtime_activation_outcome = "no_go"
artifact_authenticated = false
artifact_persisted = false
approval_consumed = false
replay_prevented = false
```

## Verification semantics: consistency, not authenticity

```text
VALID = schema/semantic/hash consistency
VALID != authenticity/provenance
```

| Category | Example | Verdict |
|----------|---------|---------|
| A — semantic-invalid | bad schema/market/symbol/timestamp/posture/digest hex | `INVALID` + reason |
| B — semantic-valid change + stale digest | valid change, stored digest not recomputed | `INVALID` / `eligibility_artifact_hash_mismatch` |
| C — semantic-valid change + recomputed digest | valid change, digest recomputed over it | `VALID` (consistency only) |

The CLI never presents Category C as authentication, provenance, or (without warning)
consumption-ready. VALID is an observation of internal consistency.

## Exception contract

A verifier raising an ordinary `Exception` (defensive — the verifier itself returns INVALID rather
than raising) → exit 1 / `INVALID` / `eligibility_artifact_invalid_field`, no raw exception text.
Because the payload had already reached the verifier, this is an INVALID (verifier-domain) outcome,
not a FAIL boundary outcome. `MemoryError`, `KeyboardInterrupt`, and `SystemExit` are re-raised.

## Single execution (per VALID call)

```text
stdin read = 1
JSON parse = 1
artifact verifier = 1
settings/env/clock/DB/fs write/builder/eligibility/intent/evidence/network/broker = 0
```

## Carry-over (RTM-7c.4v) — validated-content emission

The 4t builder now hashes and constructs the artifact from the **validated content snapshot**
(`content.validated`) returned by the shared content owner, never the raw caller locals — one
identical observation source for validation, hashing, and construction. Builder output (13 fields)
and the `eligibility_artifact_sha256` digest remain byte-equivalent to the pre-change result.

## Still OPEN (unchanged posture)

Artifact persistence/file output, actual approval consumption, consumed marker,
replay/nonce/idempotency, signing/HMAC, Operator identity authentication, origin/provenance
verification, intent/evidence lookup, TTL/freshness re-evaluation, activation caller/token,
`--run`, KIS/network, broker/order, operational DB write, daemon/scheduler, unattended pilot.

## Related contracts

- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4u verifier + snapshot API
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4t builder + shared owners
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_CONTRACT.md` — RTM-7c.4s eligibility preflight
- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_CONTRACT.md` — RTM-7c.4w canonical persistence-payload encode/decode (also carries the 4w hardening of this mode: malformed verifier result fails closed, early-failure call counts)
- `PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md` — attended activation posture inventory
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root

## RTM-7c.4w carry-over hardening

The verifier call and the result validation/summary now run inside one defensive boundary.
`_verify_eligibility_artifact_summary` delegates to the shared composition helper
`validate_operator_approval_consumption_eligibility_artifact_verification_invariants` (same rules as
the persistence encoder) **before** reading any metadata. A malformed verifier return (None / object
/ dict / subclass / wrong outcome / property-raising / invariant violation) fails closed to exit 1 /
`INVALID` / `eligibility_artifact_invalid_field` / null metadata with no raw object/value/property
leak; fatal `MemoryError`/`KeyboardInterrupt`/`SystemExit` re-raise. Early-failure call counts are
asserted: applicability/mode FAIL reads stdin `0` and the verifier `0`; a stdin-boundary FAIL reads
stdin `1` / verifier `0`; an artifact INVALID/VALID calls the verifier `1`.

**4w closure:** persistence encoder/decoder share the same invariant helpers; decoder additionally
requires exact canonical input bytes (`eligibility_persistence_payload_not_canonical` when
representation differs). Parser failures stay `eligibility_persistence_payload_not_json` (or mapped
parser reason) — never misclassified as verifier failure.
