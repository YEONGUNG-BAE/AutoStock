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
outcome: WRITTEN | NOT_WRITTEN | INVALID
reason_codes: tuple[str, ...]
eligibility_artifact_sha256: str | None
bytes_written: int | None
```

Invariants:

- `WRITTEN`: `reason_codes == ()`, digest lowercase hex64, `bytes_written > 0`
- `NOT_WRITTEN` / `INVALID`: `len(reason_codes) == 1`, digest `None`, `bytes_written None`

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
```

Processing order:

1. persistence encode (encode `INVALID` ⇒ filesystem access **0**)
2. exact path type validation
3. parent exists and is a directory (writer does **not** create directories)
4. destination create-new gate (`lstat`, no symlink follow on final component)
5. same-directory temp create (`O_CREAT | O_EXCL`, `O_NOFOLLOW` when available, mode `0o600`)
6. complete byte write (short-write loop)
7. file `fsync`
8. atomic publish: `os.link(temp, destination)` (create-new — no overwrite)
9. temp `unlink`
10. parent-directory `fsync`
11. result

Core invariants:

- existing destination overwrite **0**
- destination already present ⇒ byte/stat change **0**
- symlink destination not followed
- temp stays in destination directory
- destination invisible until publish completes
- publish exposes complete canonical bytes only
- temp removed after success and after normal failure paths
- no automatic ``runtime/`` path selection

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

1. exact path type validation
2. `lstat` / no symlink on source
3. regular-file gate
4. read-only open with `O_NOFOLLOW` when available
5. `fstat` after open — must still be regular file
6. size bound (1 MiB, same as persistence decoder)
7. bounded complete read; stat size vs bytes read mismatch ⇒ fail-closed
8. close
9. persistence decoder exactly once

Reader creates/modifies/deletes **nothing** — no sidecar/temp/reconcile files.

## Path and symlink policy

- Input must be `pathlib.Path` or the OS concrete implementation (`PosixPath` / `WindowsPath`).
  `str`, path-like objects, and user-defined `Path` subclasses are rejected
  (`eligibility_artifact_file_invalid_input`).
- Absolute and relative paths are both allowed.
- Final path component: `lstat` without follow — symlinks ⇒ `destination_not_regular` (writer) /
  `not_regular` (reader).
- Parent directory symlinks to an existing directory are **allowed** (documented limitation: no
  claim that every path component is symlink-free).
- Reader TOCTOU: `lstat → open → fstat` minimizes race window but does **not** claim process-wide
  locking or concurrent atomic snapshot. Writer concurrent read: pre-publish missing; post-publish
  complete file; temp files are not reader targets.

## Failure cleanup

On write failure before publish: destination unchanged; temp removed when possible. If temp cleanup
itself fails after a primary failure, the primary stable reason is still returned (cleanup error
does not mask the root cause). Parent-directory `fsync` failure after successful `link` leaves the
complete destination file in place (documented: sync failure is reported, bytes already published).

Ordinary filesystem exceptions map to stable `NOT_WRITTEN` / `INVALID` — no raw path/errno/exception
in reason codes. `MemoryError` / `KeyboardInterrupt` / `SystemExit` re-raised.

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

## Out of scope (deferred)

Operator CLI for file paths; automatic path selection; actual approval consumption; consumed
marker; replay/nonce/idempotency; signing/HMAC; Operator identity authentication; provenance
verification; intent/evidence lookup; TTL/freshness re-evaluation; activation caller/token; `--run`;
KIS/network; broker/order; operational DB write; daemon/scheduler.

## Related contracts

- `PAPER_FAST_LOOP_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_CONTRACT.md` — RTM-7c.4w canonical bytes
- `PAPER_FAST_LOOP_VERIFIED_OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_CONTRACT.md` — RTM-7c.4u verified snapshot + shared invariants
- `PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md` — composition root (no new CLI mode in 4x)
