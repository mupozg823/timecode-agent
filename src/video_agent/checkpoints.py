"""Checkpoint facade and coverage/readiness projections."""

from __future__ import annotations

from typing import Final

from .checkpoint_schema import (
    STATUSES,
    CheckpointObject,
    CheckpointValidationError,
    ConvergenceMetrics,
    ConvergenceResult,
    CoverageMetrics,
    CoverageStatus,
    ValidatedCheckpoint,
)
from .checkpoint_store import (
    _load_checkpoint_entries,
    append_checkpoint,
    load_checkpoint_history,
    load_checkpoints,
)
from .verification import (
    promotable_checkpoints,
    promotable_checkpoints_from_entries,
    verification_audit,
)
from .workspace import Workspace

__all__ = [
    "CheckpointValidationError",
    "append_checkpoint",
    "convergence_gate",
    "coverage_status",
    "load_checkpoint_history",
    "load_checkpoints",
    "promotable_checkpoints",
    "promotable_checkpoints_from_entries",
    "verification_audit",
]

GAP_MIN_SECONDS: Final = 3.0
type _CheckpointEntries = list[tuple[ValidatedCheckpoint, CheckpointObject]]


def coverage_status(ws: Workspace) -> CoverageStatus:
    """Calculate coverage and legacy readiness from the latest ledger state."""
    return _coverage_status_from_entries(ws, _load_checkpoint_entries(ws))


def _coverage_status_from_entries(
    ws: Workspace, entries: _CheckpointEntries
) -> CoverageStatus:
    duration = float(ws.manifest["duration"])
    validated = [checkpoint for checkpoint, _ in entries]
    cps = [checkpoint for _, checkpoint in entries]
    counts = {status: 0 for status in STATUSES}
    for checkpoint in validated:
        counts[checkpoint.status] += 1

    def union(spans: list[list[float]]) -> list[list[float]]:
        merged: list[list[float]] = []
        for start, end in sorted(
            (max(0.0, start), min(float(end), duration))
            for start, end in spans
        ):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return merged

    covered = union(
        [[start, end] for start, end in (item.span for item in validated)]
    )
    verified = union(
        [
            [start, end]
            for item in validated
            if item.status in ("verified", "corrected")
            for start, end in (item.span,)
        ]
    )
    gaps: list[list[float]] = []
    cursor = 0.0
    for start, end in covered:
        if start - cursor > GAP_MIN_SECONDS:
            gaps.append([round(cursor, 3), round(start, 3)])
        cursor = max(cursor, end)
    if duration - cursor > GAP_MIN_SECONDS:
        gaps.append([round(cursor, 3), round(duration, 3)])

    confidences: list[float] = []
    for checkpoint in cps:
        raw_confidence = checkpoint.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (int, float)
        ):
            continue
        confidences.append(float(raw_confidence))
    status: CoverageMetrics = {
        "total_duration": duration,
        "counts": counts,
        "covered_ratio": (
            round(sum(end - start for start, end in covered) / duration, 4)
            if duration
            else 0.0
        ),
        "verified_ratio": (
            round(sum(end - start for start, end in verified) / duration, 4)
            if duration
            else 0.0
        ),
        "gaps": gaps,
        "mean_confidence": (
            round(sum(confidences) / len(confidences), 4)
            if confidences
            else None
        ),
    }
    return {**status, "readiness_legacy": convergence_gate(status)}


def convergence_gate(
    status: ConvergenceMetrics,
    *,
    terminal_support_complete: bool = True,
) -> ConvergenceResult:
    """Synthesize coverage evidence into a stable readiness recommendation."""
    covered = status["covered_ratio"]
    verified = status["verified_ratio"]
    confidence = (
        status["mean_confidence"]
        if status["mean_confidence"] is not None
        else 0.0
    )
    score = round(
        0.4 * covered + 0.4 * verified + 0.2 * min(confidence, 1.0),
        3,
    )
    if status["gaps"] or covered < 0.999:
        readiness = "cover_gaps"
        recommendation = (
            "커버 안 된 구간부터 체크포인트를 추가 (전 구간 커버가 선행 조건)"
        )
    elif verified < 0.6 or not terminal_support_complete:
        readiness = "verify_more"
        recommendation = (
            "검증 부족 — confidence<0.7·화자 전환·전사-무관 버스트 지점을 우선 캡처"
        )
    else:
        readiness = "converged"
        recommendation = "보조 기준 충족 — 인덱스만으로 자답해 확신 high면 종료"
    return {
        "status": readiness,
        "score": score,
        "breakdown": {
            "covered": covered,
            "verified": verified,
            "mean_confidence": confidence,
        },
        "recommendation": recommendation,
    }
