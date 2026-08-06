"""Single-snapshot workspace readiness projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

from .checkpoint_schema import (
    CheckpointObject,
    CheckpointValue,
    ConvergenceMetrics,
    ConvergenceResult,
    CoverageStatus,
)
from .checkpoint_store import _load_checkpoint_entries
from .checkpoints import _coverage_status_from_entries, convergence_gate
from .transcript_evidence import transcript_segment_index_from_value
from .transcript_segments import TranscriptValue, normalize_transcript_segments
from .verification import (
    _promotable_entries_from_entries,
    _verification_snapshot_from_entries,
)
from .verification_types import (
    CheckpointEntries,
    VerificationAudit,
    VerificationSnapshot,
)
from .verify_priority import (
    STATUS_QUEUE_CAP,
    VerifyPriorityItem,
    verify_priority_from_entries,
)
from .workspace import Workspace, load_json


class ReadinessV2(ConvergenceResult):
    """Grounded readiness that identifies its evidence basis."""

    version: Literal[2]
    verified_basis: Literal["supported_verified_ratio"]


class WorkspaceStatus(CoverageStatus):
    """Coverage, evidence audit, and readiness from one ledger snapshot.

    `readiness` is the canonical grounded gate (CONTEXT.md 용어 정본);
    the coverage-only legacy gate rides along as `readiness_legacy`.
    """

    verification_audit: VerificationAudit
    readiness: ReadinessV2
    verify_queue: list[VerifyPriorityItem]


@dataclass(frozen=True, slots=True)
class _WorkspaceStatusSnapshot:
    status: WorkspaceStatus
    verification: VerificationSnapshot
    # 같은 원장 읽기에서 나온 터미널 체크포인트 — 서사 감사처럼 본문
    # 텍스트가 필요한 소비자가 원장을 다시 읽지 않게 한다.
    terminal_checkpoints: tuple[CheckpointObject, ...]
    supported_checkpoints: tuple[CheckpointObject, ...]
    # 전체 원장 — 관계 후보처럼 터미널 여부와 무관하게 모든 체크포인트를
    # 훑어야 하는 소비자가 원장을 다시 읽지 않게 한다.
    checkpoints: tuple[CheckpointObject, ...]


def _workspace_status_snapshot(ws: Workspace) -> _WorkspaceStatusSnapshot:
    """Project public status and internal modalities from one ledger read."""
    return _workspace_status_snapshot_from_entries(
        ws, _load_checkpoint_entries(ws)
    )


class _Unread:
    """전사 미제공 표지 — load_json의 정당한 None(부재/손상)과 구분한다."""


_UNREAD: Final = _Unread()


def _workspace_status_snapshot_from_entries(
    ws: Workspace,
    entries: CheckpointEntries,
    *,
    transcript_value: TranscriptValue | _Unread = _UNREAD,
) -> _WorkspaceStatusSnapshot:
    """호출자가 이미 읽은 원장(과 선택적 전사) 스냅샷에서 상태를 투영한다.

    전사는 한 번만 읽어 두 소비자(근거 인덱스·검증 우선순위)에 나눠 준다
    — 따로 읽으면 동시 기입 시 서로 다른 전사 위에서 판정한다.
    """
    raw_transcript: TranscriptValue = (
        load_json(ws.transcript_path)
        if isinstance(transcript_value, _Unread)
        else transcript_value
    )
    coverage = _coverage_status_from_entries(ws, entries)
    verification = _verification_snapshot_from_entries(
        ws,
        entries,
        # 두 재귀 JSON 별칭(TranscriptValue↔CheckpointValue)은 구조 동형이나
        # list 불변성 탓에 상호 대입 불가 — 경계에서 한 번만 cast한다.
        transcript_index=transcript_segment_index_from_value(
            cast(CheckpointValue, raw_transcript)
        ),
    )
    terminal_entries = _promotable_entries_from_entries(entries)
    audit = verification.audit
    metrics: ConvergenceMetrics = {
        "covered_ratio": coverage["covered_ratio"],
        "verified_ratio": audit["supported_verified_ratio"],
        "gaps": coverage["gaps"],
        "mean_confidence": coverage["mean_confidence"],
    }
    grounded = convergence_gate(
        metrics,
        terminal_support_complete=(
            audit["supported_count"] == audit["terminal_count"]
        ),
    )
    readiness: ReadinessV2 = {
        **grounded,
        "version": 2,
        "verified_basis": "supported_verified_ratio",
    }
    status: WorkspaceStatus = {
        **coverage,
        "verification_audit": audit,
        "readiness": readiness,
        "verify_queue": verify_priority_from_entries(
            ws,
            entries,
            top=STATUS_QUEUE_CAP,
            segments=normalize_transcript_segments(raw_transcript),
        ),
    }
    return _WorkspaceStatusSnapshot(
        status=status,
        verification=verification,
        terminal_checkpoints=tuple(checkpoint for _, checkpoint in terminal_entries),
        supported_checkpoints=tuple(
            checkpoint
            for validated, checkpoint in terminal_entries
            if validated.checkpoint_id in verification.supported_checkpoint_ids
        ),
        checkpoints=tuple(checkpoint for _, checkpoint in entries),
    )


def workspace_status(ws: Workspace) -> WorkspaceStatus:
    """Project coverage and evidence-grounded readiness from one ledger read."""
    return _workspace_status_snapshot(ws).status
