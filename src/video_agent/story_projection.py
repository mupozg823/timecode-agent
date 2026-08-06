"""Language-neutral, read-only Story Map projection."""

from __future__ import annotations

import heapq
import math
from copy import deepcopy
from typing import Final, Literal, TypedDict, assert_never

from .checkpoint_schema import CheckpointValue
from .corpus_projection import speaker_labels, string_values
from .sequence_grounding import revision_drift
from .sequence_store import (
    SequenceSourceState,
    load_sequence_read_snapshot,
)
from .workspace import Workspace

type GroundingState = Literal["current", "drifted", "signal_only", "unpinned"]
type StoryIssueSeverity = Literal["warning", "error"]


class StoryIssue(TypedDict, total=False):
    code: str
    severity: StoryIssueSeverity
    sequence_id: str
    checkpoint_id: str


class StoryCheckpoint(TypedDict):
    id: str
    start: float
    end: float
    status: str
    situation: str
    speakers: list[str]
    confidence: float | None
    visual_evidence: list[str]
    track: int


class StoryCut(TypedDict):
    order: int
    start: float
    end: float
    role: str
    note: str
    checkpoint_ids: list[str]
    signals: list[str]
    grounding_state: GroundingState


class StoryRejected(TypedDict):
    start: float
    end: float
    reason: str


class StorySequence(TypedDict):
    id: str
    status: str
    intent: str
    brief: dict[str, object]
    expected_effect: str
    total_cut_duration: float
    drifted_checkpoint_ids: list[str]
    human_overrides: list[str]
    cuts: list[StoryCut]
    rejected: list[StoryRejected]


class StoryMap(TypedDict):
    duration: float
    checkpoints: list[StoryCheckpoint]
    sequences: list[StorySequence]
    sequence_source_state: SequenceSourceState
    issues: list[StoryIssue]


_SOURCE_ISSUES: Final[dict[SequenceSourceState, StoryIssue]] = {
    SequenceSourceState.PRESENT_WITHOUT_VALID_RECORDS: {
        "code": "sequence_present_without_valid_records",
        "severity": "warning",
    },
    SequenceSourceState.UNREADABLE: {
        "code": "sequence_unreadable",
        "severity": "error",
    },
}


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _finite_span(value: CheckpointValue | None) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start = _finite_float(value[0])
    end = _finite_float(value[1])
    if start is None or end is None:
        return None
    return start, end


def _assign_tracks(items: list[StoryCheckpoint]) -> list[StoryCheckpoint]:
    active: list[tuple[float, int]] = []
    available: list[int] = []
    track_count = 0
    result: list[StoryCheckpoint] = []
    for item in sorted(
        items,
        key=lambda value: (value["start"], value["end"], value["id"]),
    ):
        while active and active[0][0] <= item["start"]:
            _end, track = heapq.heappop(active)
            heapq.heappush(available, track)
        if available:
            track = heapq.heappop(available)
        else:
            track = track_count
            track_count += 1
        projected: StoryCheckpoint = {**item, "track": track}
        result.append(projected)
        heapq.heappush(active, (item["end"], track))
    return result


def _grounding_state(
    checkpoint_ids: list[str],
    signals: list[str],
    pins: dict[str, object],
    drifted_ids: set[str],
) -> GroundingState:
    if not checkpoint_ids and signals:
        return "signal_only"
    if any(item in drifted_ids for item in checkpoint_ids):
        return "drifted"
    if any(item not in pins for item in checkpoint_ids):
        return "unpinned"
    return "current"


def _source_issue(state: SequenceSourceState) -> StoryIssue | None:
    match state:
        case SequenceSourceState.ABSENT | SequenceSourceState.READY:
            return None
        case SequenceSourceState.PRESENT_WITHOUT_VALID_RECORDS:
            return deepcopy(_SOURCE_ISSUES[state])
        case SequenceSourceState.UNREADABLE:
            return deepcopy(_SOURCE_ISSUES[state])
        case unreachable:
            assert_never(unreachable)


def _project_sequence(sequence: dict, checkpoints_by_id: dict) -> StorySequence:
    drifted_checkpoint_ids = revision_drift(sequence, checkpoints_by_id)
    drifted_ids = set(drifted_checkpoint_ids)
    raw_pins = sequence.get("checkpoint_revisions")
    pins = dict(raw_pins) if isinstance(raw_pins, dict) else {}
    projected_cuts: list[StoryCut] = []
    for cut in sorted(sequence["cuts"], key=lambda value: value["order"]):
        span = _finite_span(cut["span"])
        assert span is not None
        checkpoint_ids = string_values(cut.get("checkpoint_ids"))
        signals = string_values(cut.get("signals"))
        role = cut.get("role")
        note = cut.get("note")
        projected_cuts.append(
            {
                "order": cut["order"],
                "start": span[0],
                "end": span[1],
                "role": role if isinstance(role, str) else "",
                "note": note if isinstance(note, str) else "",
                "checkpoint_ids": checkpoint_ids,
                "signals": signals,
                "grounding_state": _grounding_state(
                    checkpoint_ids, signals, pins, drifted_ids
                ),
            }
        )

    rejected: list[StoryRejected] = []
    for alternative in sequence.get("alternatives_rejected") or []:
        if not isinstance(alternative, dict):
            continue
        span = _finite_span(alternative.get("span"))
        reason = alternative.get("reason")
        if span is None or span[0] >= span[1] or not isinstance(reason, str):
            continue
        rejected.append({"start": span[0], "end": span[1], "reason": reason})

    raw_brief = sequence.get("brief")
    brief = deepcopy(raw_brief) if isinstance(raw_brief, dict) else {}
    expected_effect = sequence.get("expected_effect")
    return {
        "id": sequence["id"],
        "status": sequence["status"],
        "intent": sequence["intent"],
        "brief": brief,
        "expected_effect": (
            expected_effect if isinstance(expected_effect, str) else ""
        ),
        "total_cut_duration": sum(
            cut["end"] - cut["start"] for cut in projected_cuts
        ),
        "drifted_checkpoint_ids": drifted_checkpoint_ids,
        "human_overrides": string_values(sequence.get("human_overrides")),
        "cuts": projected_cuts,
        "rejected": rejected,
    }


def build_story_map(ws: Workspace) -> StoryMap:
    """Build one language-neutral Story Map without mutating either ledger.

    Contract: ``checkpoints`` come back sorted by (start, end, id) — downstream
    renderers rely on this order instead of re-sorting.
    """
    snapshot = load_sequence_read_snapshot(ws)
    duration = _finite_float(ws.manifest.get("duration"))
    if duration is None:
        duration = 0.0

    projected_checkpoints: list[StoryCheckpoint] = []
    for checkpoint in snapshot["checkpoints_by_id"].values():
        span = _finite_span(checkpoint.get("span"))
        assert span is not None
        checkpoint_id = checkpoint.get("id")
        status = checkpoint.get("status")
        situation = checkpoint.get("situation") or checkpoint.get("hypothesis")
        assert isinstance(checkpoint_id, str)
        assert isinstance(status, str)
        assert isinstance(situation, str)
        projected_checkpoints.append(
            {
                "id": checkpoint_id,
                "start": span[0],
                "end": span[1],
                "status": status,
                "situation": situation,
                "speakers": speaker_labels(checkpoint.get("speakers")),
                "confidence": _finite_float(checkpoint.get("confidence")),
                "visual_evidence": string_values(
                    checkpoint.get("visual_evidence")
                ),
                "track": 0,
            }
        )

    source_state = snapshot["sequence_source_state"]
    issues: list[StoryIssue] = []
    if (source_issue := _source_issue(source_state)) is not None:
        issues.append(source_issue)
    if duration <= 0:
        issues.append({"code": "invalid_duration", "severity": "error"})

    return {
        "duration": duration,
        "checkpoints": _assign_tracks(projected_checkpoints),
        "sequences": [
            _project_sequence(sequence, snapshot["checkpoints_by_id"])
            for sequence in sorted(
                snapshot["sequences"], key=lambda value: value["id"]
            )
        ],
        "sequence_source_state": source_state,
        "issues": issues,
    }
