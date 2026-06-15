# Operator Approval Consumption Eligibility Artifact File Contract (RTM-7c.4x)

Atomic **create-new** file publish and read-only file verification for the RTM-7c.4w canonical
persistence payload. This lane introduces **actual file I/O** with an explicit caller-provided path
only — no automatic ``runtime/`` selection, no CLI, no consumption, no replay/signing, no
activation authorization.

**Runtime activation: NO-GO.** Read `VALID` is persistence-payload schema·semantic·hash
**consistency** only — never authenticity, provenance, approval consumption, or activation
authorization.

Code:

- `composition.operator_approval_consumption_eligibility_artifact_file.write_verified_operator_approval_consumption_eligibility_artifact_create_new`
- `composition.operator_approval_consumption_eligibility_artifact_file.read_operator_approval_consumption_eligibility_artifact_file`
- reuses RTM-7c.4w `encode_verified_operator_approval_consumption_eligibility_artifact` (writer) and
  `decode_operator_approval_consumption_eligibility_artifact_payload` (reader)

## Writer contract

`write(*, snapshot, destination) -> EligibilityArtifactFileWriteResult`

```
outcome: WRITTEN | PUBLISHED_INCOMPLETE | NOT_WRITTEN | INVALID
reason_codes: tuple[str, ...]
eligibility_artifact_sha256: str | None
bytes_written: int | None
```

Invariants:

- `WRITTEN`: `reason_codes == ()`, digest lowercase hex64, `bytes_written > 0`, destination
  published, temp cleanup complete, parent directory sync confirmed
- `PUBLISHED_INCOMPLETE`: destination published, digest lowercase hex64, `bytes_written > 0`,
  `reason_codes` canonical nonempty tuple (may contain **two** stable reasons — see cleanup/sync
  ordering below). Does **not** mean activation-authorized or durable consumption-ready.
- `NOT_WRITTEN`: destination was **not** published; digest `None`; `bytes_written None`; one or two
  stable reasons (pre-publish primary + optional temp cleanup failure)
- `INVALID`: invalid input/snapshot; filesystem publication **not attempted**; digest `None`;
  `bytes_written None`; exactly one stable reason

**Post-publish invariant:** once destination is published, the writer **never** returns
`NOT_WRITTEN`. Parent-directory `fsync` failure and temp cleanup failure after successful `link` leave
the complete destination bytes in place — no automatic unlink/rollback.

Stable writer reasons:

```
eligibility_artifact_file_invalid_input
eligibility_artifact_file_invalid_snapshot
eligibility_artifact_file_parent_missing
eligibility_artifact_file_parent_not_directory
eligibility_artifact_file_destination_exists
eligibility_artifact_file_destination_not_regular
eligibility_artifact_file_temp_create_failed
eligibility_artifact_file_write_failed
eligibility_artifact_file_publish_failed
eligibility_artifact_file_sync_failed
eligibility_artifact_file_temp_cleanup_failed
```

Canonical reason order (deduped, no duplicates):

- Pre-publish failure + temp cleanup failure:
  `(primary_reason, eligibility_artifact_file_temp_cleanup_failed)`
- Post-publish temp cleanup failure only:
  `(eligibility_artifact_file_temp_cleanup_failed,)`
- Post-publish parent sync failure only:
  `(eligibility_artifact_file_sync_failed,)`
- Post-publish temp cleanup + parent sync failure:
  `(eligibility_artifact_file_temp_cleanup_failed, eligibility_artifact_file_sync_failed)`
- Post-publish identity/publish defect + cleanup/sync failures: primary reason first, then cleanup,
  then sync (same dedupe rules)

Processing order:

1. persistence encode + encode-result invariant validation (malformed encode result or non-`CREATED`
   ⇒ `INVALID` / `invalid_snapshot`, filesystem access **0**)
2. exact concrete `Path` type validation (`type(value) is type(Path())` only)
3. parent exists and is a directory (writer does **not** create directories)
4. destination create-new gate (`lstat`, no symlink follow on final component)
5. same-directory temp create (`O_CREAT | O_EXCL`, `O_NOFOLLOW` when available, mode `0o600`)
6. complete byte write (short-write loop)
7. file `fsync` on temp fd
8. `fstat(temp_fd)` — temp identity capture (temp fd kept open until after publish)
9. atomic publish: `os.link(temp, destination)` (create-new — no overwrite)
10. `lstat(destination)` — dev/ino must match temp `fstat`; destination must be regular file
11. close temp fd; temp `unlink` (failure recorded, not swallowed)
12. parent-directory `fsync` (failure ⇒ `PUBLISHED_INCOMPLETE`, destination unchanged)
13. result from explicit publication-state locals (`destination_published`, `temp_cleanup_complete`,
    `parent_sync_confirmed`, `primary_reasons`)

Core invariants:

- existing destination overwrite **0**
- destination already present ⇒ byte/stat change **0**
- symlink destination not followed
- temp stays in destination directory
- destination invisible until publish completes
- publish exposes complete canonical bytes only
- temp removed after success when possible; cleanup failure visible in reason tuple
- no automatic ``runtime/`` path selection
- published destination never reported `NOT_WRITTEN`

## Reader contract

`read(*, source) -> EligibilityArtifactFileReadResult`

```
outcome: VALID | INVALID
reason_codes: tuple[str, ...]
snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None
```

Stable reader reasons:

```
eligibility_artifact_file_invalid_input
eligibility_artifact_file_missing
eligibility_artifact_file_not_regular
eligibility_artifact_file_too_large
eligibility_artifact_file_read_failed
```

Payload decoder reasons are preserved verbatim when applicable (e.g.
`eligibility_persistence_payload_not_canonical`, `eligibility_artifact_hash_mismatch`).

Processing:

1. exact concrete `Path` type validation
2. `lstat` / no symlink on source — capture `st_dev`, `st_ino`
3. regular-file gate; size bound (1 MiB)
4. read-only open with `O_NOFOLLOW` when available
5. `fstat` after open — `st_dev`/`st_ino` must match `lstat` (regular-file replacement between
   `lstat` and `open` ⇒ fail-closed)
6. `fstat` must still be regular file; size bound re-check
7. read exactly `fstat_before.st_size` bytes (short read ⇒ fail-closed)
8. 1-byte EOF probe — must be `b""` (extra trailing byte / growth ⇒ fail-closed)
9. `fstat_after` — dev/ino/size unchanged vs `fstat_before`
10. close
11. persistence decoder exactly once; malformed decoder dependency result ⇒
    `eligibility_artifact_file_read_failed` (never `VALID`)

Reader creates/modifies/deletes **nothing** — no sidecar/temp/reconcile files.

## Path and symlink policy

- Input must be **exact** concrete `pathlib.Path` (`type(value) is type(Path())`). `str`, path-like
  objects, user `Path` subclasses (including subclasses whose `__name__ == "PosixPath"` or that
  override `__fspath__` / `parent`) are rejected (`eligibility_artifact_file_invalid_input`).
- Absolute and relative paths are both allowed.
- Final path component: `lstat` without follow — symlinks ⇒ `destination_not_regular` (writer) /
  `not_regular` (reader).
- Parent directory symlinks to an existing directory are **allowed** (documented limitation: no
  claim that every path component is symlink-free).
- Reader TOCTOU: `lstat → open → fstat_before → bounded read → EOF probe → fstat_after` minimizes
  race window but does **not** claim process-wide locking or concurrent atomic snapshot. Writer
  concurrent read: pre-publish missing; post-publish complete file; temp files are not reader
  targets. Hostile shared-directory races (concurrent mutation between probes) are not fully excluded
  — limitation documented, not claimed closed.

## Failure cleanup

On write failure **before** publish: destination unchanged; temp removed when possible. If temp
cleanup itself fails after a primary failure, both reasons are returned in canonical order — cleanup
error does **not** mask the root cause (`NOT_WRITTEN`).

On write failure **after** publish: destination bytes remain; outcome `PUBLISHED_INCOMPLETE`; digest
and `bytes_written` preserved; temp cleanup and/or parent sync failures appear in `reason_codes`.

Ordinary filesystem exceptions map to stable outcomes — no raw path/errno/exception in reason codes.
`MemoryError` / `KeyboardInterrupt` / `SystemExit` re-raised.

## Dependency result invariants

Writer validates `type(result) is EligibilityArtifactPersistencePayloadResult` via shared helper
`validate_eligibility_artifact_persistence_payload_encode_result_invariants` before filesystem
access. Malformed encode results (None/object/dict/subclass/wrong-outcome/CREATED+reasons/null or
wrong bytes/empty or over-limit bytes/null or malformed digest/INVALID+bytes/digest/property-raising)
⇒ `INVALID` / `eligibility_artifact_file_invalid_snapshot`, filesystem access **0**.

Reader validates `type(result) is EligibilityArtifactPersistencePayloadVerification` via
`validate_eligibility_artifact_persistence_payload_decode_result_invariants`. Malformed decoder
results ⇒ `INVALID` / `eligibility_artifact_file_read_failed`, `snapshot=None`.

## Isolation

Writer (normal path): persistence encoder `1`; filesystem only; clock/config/env/DB/network/broker
`0`.

Reader (normal path): persistence decoder `1`; filesystem read-only; clock/config/env/DB/network/
broker `0`.

Forbidden: upstream eligibility rerun, intent/evidence verifier rerun, precheck, runtime activation,
background thread/process, automatic ``runtime/`` paths.

## Deterministic round-trip

```
verified snapshot
→ writer WRITTEN
→ reader VALID
→ 13-field equality
→ file bytes == encoder bytes
→ mode 0o600
→ digest equality
→ second write same destination → destination_exists (no overwrite)
→ reader does not mutate source bytes/stat
```

## Carry-over hardening (4x on 4w)

- **H1 schema constants:** shared
  `validate_operator_approval_consumption_eligibility_artifact_verification_invariants` now requires
  VALID metadata schema versions to equal the owning module constants exactly:
  `OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION`,
  `OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION`, `ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION`.
- **H2 alternate JSON escape:** semantically identical payloads using `\u00XX` escapes for ASCII
  fields decode `INVALID` / `eligibility_persistence_payload_not_canonical`.

## RTM-7c.4x closure — published-state, cleanup, and read-integrity

Closes independent-review findings on the committed 4x file lane without adding CLI, consumption,
replay, signing, or activation:

- **P1-A post-publish result semantics:** new outcome `PUBLISHED_INCOMPLETE` for published
  destination with incomplete durability bookkeeping (temp cleanup and/or parent sync failure,
  publish identity defect). Eliminates the contradiction where parent fsync failure after successful
  `link` returned `NOT_WRITTEN` while destination existed.
- **P1-B temp cleanup accounting:** cleanup failures are never silently swallowed; pre-publish
  cleanup failure preserves primary reason + `temp_cleanup_failed`; post-publish cleanup failure
  preserves digest/bytes with `PUBLISHED_INCOMPLETE`.
- **Dependency result invariants:** shared pure helpers exported from persistence payload module;
  writer/reader fail-closed on malformed encoder/decoder dependency results.
- **Exact Path contract:** `type(value) is type(Path())` only — class-name spoof and `Path`
  subclasses rejected.
- **Reader identity and complete-read:** `lstat`/`fstat` dev/ino match; bounded read +
  mandatory 1-byte EOF probe; post-read `fstat` size/identity unchanged; decoder called exactly once
  only after stable complete read.

**Still OPEN (unchanged posture):** file-path CLI, automatic path selection, actual approval
consumption, consumed marker, replay/nonce/idempotency, signing/HMAC, Operator identity
authentication, origin/provenance verification, intent/evidence lookup, TTL/freshness
re-evaluation, activation caller/token, `--run`, KIS/network, broker/order, operational DB write,
daemon/scheduler, default runtime activation.

## Out of scope (deferred)

Operator CLI for file paths; automatic path selection; actual approval consumption; consumed
marker; replay/nonce/idempotency; signing/HMAC; Operator identity authentication; provenance
verification; intent/evidence lookup; TTL/freshness re-evaluation; activation caller/token; `--run`;
KIS/network; broker/order; operational DB write; daemon/scheduler.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_CONTRACT.md` — RTM-7c.4w canonical bytes
- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4u verified snapshot + shared invariants
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root (no new CLI mode in 4x)
