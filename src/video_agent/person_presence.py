"""Provenance-bound person-presence checkpoint observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, assert_never

from .checkpoint_schema import (
    CheckpointObject,
    CheckpointValidationError,
    PersonPresenceState,
)
from .checkpoints import append_checkpoint
from .image_provenance import load_image_records, resolve_image_path
from .image_validation import (
    decodable_image_paths,
    image_record_verification_issues,
)
from .workspace import Workspace

_POINT_SPAN_HALF_WIDTH_SECONDS: Final = 0.5


@dataclass(frozen=True, slots=True)
class PersonPresenceJudgment:
    checkpoint_id: str
    frame_ref: str
    subject: str
    state: PersonPresenceState
    hypothesis: str


def record_person_presence(
    ws: Workspace,
    judgment: PersonPresenceJudgment,
) -> CheckpointObject:
    """Bind one interpreted full-frame capture to a typed checkpoint."""
    image_path, resolved = resolve_image_path(ws, judgment.frame_ref)
    records = {record["path"]: record for record in load_image_records(ws)}
    record = records.get(image_path)
    if record is None or not record["tracked"] or record["kind"] != "frame":
        raise CheckpointValidationError(
            "person_presence requires a provenance-tracked full frame"
        )
    if record.get("crop") is not None:
        raise CheckpointValidationError(
            "person_presence requires an uncropped full frame"
        )
    timestamp = record.get("t")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise CheckpointValidationError(
            "person_presence frame requires a finite point timestamp"
        )
    if resolved not in decodable_image_paths((resolved,)):
        raise CheckpointValidationError(
            "person_presence frame is unavailable or undecodable"
        )
    duration = float(ws.manifest["duration"])
    start = max(0.0, timestamp - _POINT_SPAN_HALF_WIDTH_SECONDS)
    end = min(duration, timestamp + _POINT_SPAN_HALF_WIDTH_SECONDS)
    if not start <= timestamp < end:
        raise CheckpointValidationError(
            "person_presence frame timestamp must be inside the video"
        )
    span = (start, end)
    if image_record_verification_issues(record, span):
        raise CheckpointValidationError(
            "person_presence frame does not support its checkpoint span"
        )
    match judgment.state:
        case PersonPresenceState.PRESENT | PersonPresenceState.ABSENT:
            status = "verified"
        case PersonPresenceState.UNCERTAIN:
            status = "hypothesized"
        case unreachable:
            assert_never(unreachable)
    checkpoint: CheckpointObject = {
        "id": judgment.checkpoint_id,
        "span": [start, end],
        "status": status,
        "hypothesis": judgment.hypothesis,
        "visual_observation": {
            "kind": "person_presence",
            "subject": judgment.subject,
            "state": judgment.state.value,
            "timestamp": float(timestamp),
        },
        "visual_evidence": [image_path],
    }
    return append_checkpoint(ws, checkpoint)
