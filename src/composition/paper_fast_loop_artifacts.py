"""Single source of truth for the paper fast-loop artifact specification (RTM-7c.4h / H2).

The precheck fingerprint loop, the revalidation fingerprint loop, the precheck-receipt
artifact-name tuple, and the final-preflight composition all derive their artifact name,
``PaperFastLoopPaths`` attribute, and SQLite-ness from this one tuple so the four artifacts
can never drift out of agreement across modules.

This is a dependency-free leaf module: it imports nothing from ``composition`` (or anywhere
else in the project), so wiring it into ``paper_fast_loop``, ``precheck_receipt_schema``, and
``activation_candidate_revalidation`` introduces no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PaperFastLoopArtifactSpec",
    "PAPER_FAST_LOOP_ARTIFACT_SPECS",
    "PAPER_FAST_LOOP_ARTIFACT_NAMES",
    "PAPER_FAST_LOOP_SQLITE_ARTIFACT_NAMES",
]


@dataclass(frozen=True)
class PaperFastLoopArtifactSpec:
    """One fast-loop artifact: its canonical ``name``, the ``PaperFastLoopPaths`` attribute
    holding its resolved path, and whether it is a SQLite database (``is_sqlite``) — the
    execution-inputs snapshot is JSON, the other three are SQLite."""

    name: str
    path_attr: str
    is_sqlite: bool


# Canonical order — every fingerprint loop, receipt artifact tuple, and test matrix follows it.
PAPER_FAST_LOOP_ARTIFACT_SPECS: tuple[PaperFastLoopArtifactSpec, ...] = (
    PaperFastLoopArtifactSpec(
        name="execution_inputs_snapshot", path_attr="snapshot_path", is_sqlite=False
    ),
    PaperFastLoopArtifactSpec(name="ledger", path_attr="ledger_path", is_sqlite=True),
    PaperFastLoopArtifactSpec(name="trigger_journal", path_attr="trigger_journal_path", is_sqlite=True),
    PaperFastLoopArtifactSpec(
        name="active_decision_store", path_attr="active_decision_store_path", is_sqlite=True
    ),
)

PAPER_FAST_LOOP_ARTIFACT_NAMES: tuple[str, ...] = tuple(
    spec.name for spec in PAPER_FAST_LOOP_ARTIFACT_SPECS
)

PAPER_FAST_LOOP_SQLITE_ARTIFACT_NAMES: frozenset[str] = frozenset(
    spec.name for spec in PAPER_FAST_LOOP_ARTIFACT_SPECS if spec.is_sqlite
)
