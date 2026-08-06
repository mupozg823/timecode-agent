"""Non-blocking evidence audit and semantic-promotion policy."""

from __future__ import annotations

from pathlib import Path

from .checkpoint_schema import (
    TERMINAL_STATUSES,
    CheckpointObject,
)
from .checkpoint_store import _load_checkpoint_entries
from .image_provenance import ImageRecord, load_image_records, resolve_image_path
from .image_validation import (
    decodable_image_paths,
    image_record_verification_issues,
)
from .transcript_evidence import (
    has_segment_support,
    load_transcript_segment_index,
)
from .verification_types import (
    CheckpointEntries,
    CheckpointVerificationLevels,
    PromotableEntry,
    TranscriptSegmentIndex,
    VerificationIssue,
    VerificationLevel,
    VerificationSnapshot,
    VisualEvidenceIssueCode,
)
from .verification_types import (
    VerificationAudit as VerificationAudit,
)
from .verification_types import (
    VerificationIssueCode as VerificationIssueCode,
)
from .workspace import Workspace


def _promotable_entries(ws: Workspace) -> list[PromotableEntry]:
    return _promotable_entries_from_entries(_load_checkpoint_entries(ws))


def _promotable_entries_from_entries(
    entries: CheckpointEntries,
) -> list[PromotableEntry]:
    return [
        (validated, checkpoint)
        for validated, checkpoint in entries
        if validated.status in TERMINAL_STATUSES
    ]


def promotable_checkpoints(ws: Workspace) -> list[CheckpointObject]:
    """Return terminal checkpoints whose declared support currently resolves."""
    return promotable_checkpoints_from_entries(
        ws, _load_checkpoint_entries(ws)
    )


def promotable_checkpoints_from_entries(
    ws: Workspace, entries: CheckpointEntries
) -> list[CheckpointObject]:
    """호출자가 이미 읽은 원장 스냅샷에서 promotability를 계산한다.

    원장을 재판독하면 동시 기입과 스냅샷이 갈라진다 — 낡은 표본이 새
    리비전의 결박으로 승격되는 경합을 여기서 차단한다.
    """
    promotable = _promotable_entries_from_entries(entries)
    snapshot = _verification_snapshot_from_entries(ws, promotable)
    return [
        checkpoint
        for validated, checkpoint in promotable
        if validated.checkpoint_id in snapshot.supported_checkpoint_ids
    ]


def _visual_evidence_refs(checkpoint: CheckpointObject) -> list[str]:
    raw = checkpoint.get("visual_evidence")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [ref for ref in raw if isinstance(ref, str) and ref.strip()]
    return []


def _has_legacy_support(checkpoint: CheckpointObject) -> bool:
    raw = checkpoint.get("evidence")
    return isinstance(raw, str) and bool(raw.strip())


def _has_correction_note(checkpoint: CheckpointObject) -> bool:
    for key in ("note", "correction_reason"):
        raw = checkpoint.get(key)
        if isinstance(raw, str) and raw.strip():
            return True
    return False


def _union_duration(spans: list[tuple[float, float]], duration: float) -> float:
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        bounded = [max(0.0, start), min(end, duration)]
        if merged and bounded[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bounded[1])
        else:
            merged.append(bounded)
    return sum(end - start for start, end in merged)


def _modality_level(
    visual: bool, transcript: bool
) -> VerificationLevel:
    if visual and transcript:
        return "cross_modal"
    if visual:
        return "visual_only"
    if transcript:
        return "transcript_only"
    return "unsupported"


def verification_audit(ws: Workspace) -> VerificationAudit:
    """Observe evidence quality without rejecting or rewriting legacy records."""
    return _verification_audit_from_entries(ws, _load_checkpoint_entries(ws))


def _verification_audit_from_entries(
    ws: Workspace, entries: CheckpointEntries
) -> VerificationAudit:
    return _verification_snapshot_from_entries(ws, entries).audit


def _verification_snapshot_from_entries(
    ws: Workspace,
    entries: CheckpointEntries,
    *,
    transcript_index: TranscriptSegmentIndex | None = None,
) -> VerificationSnapshot:
    entries = _promotable_entries_from_entries(entries)
    issues: list[VerificationIssue] = []
    supported_spans: list[tuple[float, float]] = []
    checkpoint_levels: list[CheckpointVerificationLevels] = []
    supported_count = 0
    supported_checkpoint_ids: set[str] = set()
    records_by_path: dict[str, ImageRecord] | None = None
    resolved_visuals: dict[str, tuple[str, Path] | None] = {}

    all_visual_refs = dict.fromkeys(
        ref
        for _, checkpoint in entries
        for ref in _visual_evidence_refs(checkpoint)
    )
    for ref in all_visual_refs:
        try:
            resolved_visuals[ref] = resolve_image_path(ws, ref)
        except ValueError:
            resolved_visuals[ref] = None
    decodable_visuals = decodable_image_paths(
        tuple(
            resolution[1]
            for resolution in resolved_visuals.values()
            if resolution is not None
        )
    )

    for validated, checkpoint in entries:
        checkpoint_id = validated.checkpoint_id
        visual_refs = _visual_evidence_refs(checkpoint)
        visual_issues: list[tuple[str, VisualEvidenceIssueCode]] = []
        visual_support = False
        for ref in visual_refs:
            resolution = resolved_visuals[ref]
            if resolution is None:
                visual_issues.append((ref, "artifact_unavailable"))
                continue
            image_path, resolved = resolution
            if resolved in decodable_visuals:
                if records_by_path is None:
                    records_by_path = {r["path"]: r for r in load_image_records(ws)}
                record_issues = image_record_verification_issues(
                    records_by_path.get(image_path),
                    validated.span,
                )
                if record_issues:
                    visual_issues.extend((ref, code) for code in record_issues)
                else:
                    visual_support = True
            else:
                visual_issues.append((ref, "artifact_unavailable"))
        legacy_support = _has_legacy_support(checkpoint)
        segments = checkpoint.get("segments")
        transcript_support = False
        if isinstance(segments, (list, tuple)) and segments:
            if transcript_index is None:
                transcript_index = load_transcript_segment_index(ws)
            transcript_support = has_segment_support(
                checkpoint, transcript_index, validated.span
            )
        # Opaque evidence_refs remain useful provenance, but cannot establish
        # support until a typed resolver proves their target and time relation.
        structured_support = visual_support or transcript_support
        checkpoint_levels.append(
            CheckpointVerificationLevels(
                checkpoint_id=checkpoint_id,
                declared=_modality_level(
                    bool(checkpoint.get("visual_evidence")),
                    bool(segments),
                ),
                grounded=_modality_level(visual_support, transcript_support),
            )
        )
        supported = structured_support
        if supported:
            supported_count += 1
            supported_checkpoint_ids.add(checkpoint_id)
            supported_spans.append(validated.span)
        else:
            issues.append({"checkpoint_id": checkpoint_id, "code": "missing_support"})

        for ref, code in visual_issues:
            issues.append(
                {
                    "checkpoint_id": checkpoint_id,
                    "code": code,
                    "artifact": ref,
                }
            )
        # 자유형 evidence가 '유일한' 근거일 때만 이관 부채 — 실제로
        # 해석 가능한 시각·transcript segment를 소급 채운 기록만 해제된다.
        if legacy_support and not structured_support:
            issues.append(
                {"checkpoint_id": checkpoint_id, "code": "legacy_unstructured"}
            )
        if validated.status == "corrected" and not _has_correction_note(checkpoint):
            issues.append(
                {"checkpoint_id": checkpoint_id, "code": "correction_note_missing"}
            )

    duration = float(ws.manifest["duration"])
    supported_duration = _union_duration(supported_spans, duration)
    return VerificationSnapshot(
        audit={
            "terminal_count": len(entries),
            "supported_count": supported_count,
            "supported_verified_ratio": (
                round(supported_duration / duration, 4) if duration else 0.0
            ),
            "issues": issues,
        },
        checkpoint_levels=tuple(checkpoint_levels),
        supported_checkpoint_ids=frozenset(supported_checkpoint_ids),
    )
