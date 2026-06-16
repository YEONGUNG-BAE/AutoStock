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
  `reason_codes` canonical nonempty tuple (primary operation reason, temp close failure, temp cleanup
  failure, parent-sync failure, in that order when present). Does **not** mean activation-authorized
  or durable consumption-ready.
- `NOT_WRITTEN`: destination was **not** published; digest `None`; `bytes_written None`; one or two
  stable reasons (pre-publish primary + optional temp close/cleanup failure)
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
eligibility_artifact_file_temp_close_failed
eligibility_artifact_file_temp_cleanup_failed
```

Canonical reason order (deduped, no duplicates):

- Pre-publish failure + temp close/cleanup failure:
  `(primary_reason, eligibility_artifact_file_temp_close_failed, eligibility_artifact_file_temp_cleanup_failed)`
- Post-publish temp cleanup failure only:
  `(eligibility_artifact_file_temp_cleanup_failed,)`
- Post-publish parent sync failure only:
  `(eligibility_artifact_file_sync_failed,)`
- Post-publish temp close + cleanup + parent sync failure:
  `(eligibility_artifact_file_temp_close_failed, eligibility_artifact_file_temp_cleanup_failed, eligibility_artifact_file_sync_failed)`
- Post-publish identity/publish defect + cleanup/sync failures: primary reason first, then cleanup,
  then sync (same dedupe rules). Temp close failure is ordered between primary and cleanup.

Processing order:

1. exact concrete `Path` type validation (`type(value) is type(Path())` only); invalid destination
   input returns `INVALID` and calls the encoder **0**
2. persistence encode + encode-result invariant validation (ordinary encoder exception, malformed
   encode result, or non-`CREATED` ⇒ `INVALID` / `invalid_snapshot`, filesystem access **0**;
   `MemoryError` / `KeyboardInterrupt` / `SystemExit` re-raised)
3. parent exists and is a directory (writer does **not** create directories)
4. destination create-new gate (`lstat`, no symlink follow on final component)
5. temp-name generation (`secrets.token_hex`); ordinary exception ⇒ `NOT_WRITTEN` /
   `temp_create_failed` with temp open/write/link/fsync calls **0**; fatal exception re-raised
6. same-directory temp create (`O_CREAT | O_EXCL`, `O_NOFOLLOW` when available, mode `0o600`)
7. complete byte write (short-write loop)
8. file `fsync` on temp fd
9. `fstat(temp_fd)` — temp identity capture (temp fd kept open until after publish)
10. atomic publish: `os.link(temp, destination)` (create-new — no overwrite)
11. `lstat(destination)` — dev/ino must match temp `fstat`; destination must be regular file
12. close temp fd exactly once; ordinary close failure makes close status uncertain and records
    `eligibility_artifact_file_temp_close_failed` (no same-integer retry, no no-leak claim). Temp
    `unlink` uses exactly two bounded attempts (failure recorded, not swallowed; successful retry
    produces no failure reason)
13. parent-directory `fsync` after every published path, including post-publish identity defects
    (failure ⇒ `PUBLISHED_INCOMPLETE`, destination unchanged)
14. result from explicit publication-state locals (`temp_created`, `temp_fd_open`,
    `temp_close_attempted`, `temp_close_complete`, `temp_cleanup_attempted`,
    `temp_cleanup_complete`, `destination_published`, `parent_sync_attempted`,
    `parent_sync_confirmed`, `primary_reasons`)

Core invariants:

- existing destination overwrite **0**
- destination already present ⇒ byte/stat change **0**
- symlink destination not followed
- temp stays in destination directory
- destination invisible until publish completes
- publish exposes complete canonical bytes only
- temp removed after success when possible; cleanup failure visible in reason tuple
- `WRITTEN` requires proven temp fd close, temp path cleanup, parent sync confirmed, and no primary
  reason
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
10. close; close ordinary failure ⇒ `INVALID` / `eligibility_artifact_file_read_failed` before
    decoder, close fatal re-raised
11. persistence decoder exactly once after close is confirmed; malformed decoder dependency result ⇒
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

On write failure **before** publish: destination unchanged; temp removed when possible. If temp was
never created, no cleanup reason is inferred. If temp close or cleanup itself fails after a primary
failure, reasons are returned in canonical order — lifecycle errors do **not** mask the root cause
(`NOT_WRITTEN`).

On write failure **after** publish: destination bytes remain; outcome `PUBLISHED_INCOMPLETE`; digest
and `bytes_written` preserved; temp close, temp cleanup, parent sync, and post-publish identity
defects appear in `reason_codes`.

The writer does not build a result before filesystem lifecycle cleanup is complete. Operation phase
records publication state, one central cleanup coordinator owns temp close, temp unlink, and parent
sync, and final result construction happens once from that final state. No failure reason is
inferred for an unattempted step. `sync_failed` is used for a real failed temp file `fsync` or a real
failed parent-directory `fsync`; an unattempted parent sync contributes no parent-sync reason.

Ordinary filesystem exceptions map to stable outcomes — no raw path/errno/exception in reason codes.
Post-publish ordinary exceptions preserve publication state and return `PUBLISHED_INCOMPLETE`, never
`NOT_WRITTEN`. If a temp-create helper creates a temp path then raises, the writer checks the
post-condition and unlinks any observed temp residue before returning `NOT_WRITTEN` /
`temp_create_failed`. If a publish helper creates the hard link then raises, the writer compares
destination `lstat` with temp `fstat`; a match is recovered as `destination_published=True`,
followed by temp cleanup and parent sync, and the result is `PUBLISHED_INCOMPLETE` /
`publish_failed`.

If `os.link` creates the hard link and then raises `OSError(EEXIST)`, dev/ino recovery takes
precedence over the errno: matching destination ⇒ `PUBLISHED_INCOMPLETE` /
`publish_failed` with digest/bytes retained, and **not** `destination_exists`. If a raced external
destination does not match the temp identity, the result remains `NOT_WRITTEN` /
`destination_exists`.

`MemoryError` / `KeyboardInterrupt` / `SystemExit` from the operation phase are re-raised as the
original fatal. When temp or destination state already exists, the writer runs the central cleanup
coordinator exactly once for that operation fatal. Cleanup has independent per-step fatal boundaries:
close fatal does not skip unlink or parent sync, unlink fatal does not skip parent sync, and
cleanup-time fatal exceptions do not replace an original operation fatal. If there is no operation
fatal and cleanup raises fatal, the first cleanup fatal is preserved while later cleanup steps still
run.

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

## RTM-7c.4x final writer-state and cleanup-truth closure

Closes the final writer-state review items without adding CLI, consumption, replay, signing, or
activation:

- Temp lifecycle state is explicit: `temp_created`, `temp_fd_open`, `temp_close_attempted`,
  `temp_close_complete`, `temp_cleanup_attempted`, `temp_cleanup_complete`,
  `destination_published`, `parent_sync_attempted`, `parent_sync_confirmed`, and
  `primary_reasons`.
- Temp create failure (`_open_exclusive_temp(...) -> None`) returns `NOT_WRITTEN` /
  `eligibility_artifact_file_temp_create_failed` only; `temp_cleanup_failed` is not inferred and no
  residue is expected.
- Temp fd close failures are visible as `eligibility_artifact_file_temp_close_failed`. A successful
  close is attempted exactly once. Ordinary close failure leaves close status uncertain, records
  `temp_close_failed`, and prevents `WRITTEN`; tests may manually close the fd afterward for fixture
  cleanup, but that is not a production safety proof.
- Temp unlink uses a fixed two-attempt retry. A transient first failure followed by success returns
  the final successful cleanup state; all failed attempts return `temp_cleanup_failed`.
- Published paths attempt parent-directory fsync even when a post-publish identity check fails.
  `parent_sync_attempted` and `parent_sync_confirmed` are separate; unattempted parent sync is not
  reported as failed.
- Ordinary exceptions after publish preserve `destination_published=True` and return
  `PUBLISHED_INCOMPLETE` with digest/bytes preserved; raw exception/path/errno is not surfaced.
- Result taxonomy is fixed: invalid snapshot, invalid destination type, and malformed dependency
  result are `INVALID`; missing parent, parent not directory, and existing destination are
  `NOT_WRITTEN`; complete publish is `WRITTEN`; published-but-incomplete lifecycle is
  `PUBLISHED_INCOMPLETE`.

## RTM-7c.4x resource-finalization and side-effect recovery closure

Closes the remaining resource-finalization review items without adding CLI, consumption, replay,
signing, authentication, or activation:

- **Encoder taxonomy and order:** path type validation precedes the encoder; ordinary encoder
  exceptions and malformed encode results are `INVALID` / `invalid_snapshot`, and filesystem
  publication calls remain **0**. Fatal encoder exceptions still propagate.
- **Fatal cleanup before re-raise:** operation-phase fatal exceptions preserve the original fatal
  type while running best-effort cleanup for any acquired temp path/fd and best-effort parent sync
  for any recovered or confirmed published destination.
- **Side-effect recovery:** temp-create and hard-link helpers are treated as side-effect boundaries.
  After an ordinary or fatal exception, observed temp residue is cleaned, and a destination whose
  dev/ino matches the temp identity is recovered as published rather than falsely reported
  `NOT_WRITTEN`.
- **Temp close truth:** temp fd close is one-shot. Ordinary close failure means lifecycle status is
  uncertain, forbids `WRITTEN`, and exposes `temp_close_failed`; the API does not claim handle-leak
  proof after such a failure.
- **Reader close truth:** the reader closes the fd before decode. A close failure returns `INVALID`
  / `read_failed` and calls the decoder **0**; close fatal exceptions propagate.
- **Parent-directory close truth:** directory fsync success is complete only when directory fd close
  also succeeds. Directory close failure after publish is `PUBLISHED_INCOMPLETE` / `sync_failed`.

## RTM-7c.4x fatal-cleanup single-pass closure

Closes the fatal-cleanup single-pass review items without adding CLI, consumption, replay, signing,
authentication, or activation:

- **Single cleanup owner:** operation fatal paths never run cleanup in an inner handler. Side-effect
  recovery records temp/publish state, then the outer fatal boundary calls one cleanup coordinator
  exactly once.
- **Exact cleanup bounds:** for one operation fatal, temp close is attempted at most once, temp
  unlink at most two times, and parent sync at most once. Link-fatal tests pin the exact counts for
  successful cleanup, close failure, unlink failure, and parent-sync failure.
- **Original fatal precedence:** recovery ordinary/fatal exceptions never replace the original
  operation fatal. Recovery failure still allows best-effort close/unlink/parent-sync to continue.
- **Per-step cleanup isolation:** cleanup close, unlink, and parent sync each have an independent
  fatal boundary. With an original operation fatal, cleanup fatal is suppressed in favor of the
  original; without an operation fatal, the first cleanup fatal is preserved after later cleanup
  steps run.
- **Temp-name boundary:** temp-name generation belongs to the temp lifecycle boundary. Ordinary
  `token_hex` failure returns `NOT_WRITTEN` / `temp_create_failed` with temp open/write/link/fsync
  calls **0**; fatal `token_hex` failure propagates with filesystem residue **0**.
- **Recovered `EEXIST`:** if `os.link` creates the destination and then raises `EEXIST`, matching
  dev/ino is classified as recovered publish (`PUBLISHED_INCOMPLETE` / `publish_failed`), not
  `destination_exists`; a mismatched external destination remains `NOT_WRITTEN` /
  `destination_exists`.

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
