"""Persisted checkpoint schema and transition validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict, TypeGuard

from .workspace import Workspace

type CheckpointValue = (
    str
    | int
    | float
    | bool
    | None
    | list[CheckpointValue]
    | tuple[CheckpointValue, ...]
    | dict[str, CheckpointValue]
)
type CheckpointObject = dict[str, CheckpointValue]

STATUSES: Final = ("hypothesized", "verified", "corrected")
TERMINAL_STATUSES: Final = frozenset(("verified", "corrected"))
SPAN_END_EXCEEDS_DURATION: Final = "'span' end must not exceed video duration"


class ConvergenceResult(TypedDict):
    status: str
    score: float
    breakdown: dict[str, float]
    recommendation: str


class ConvergenceMetrics(TypedDict):
    covered_ratio: float
    verified_ratio: float
    gaps: list[list[float]]
    mean_confidence: float | None


class CoverageMetrics(ConvergenceMetrics):
    total_duration: float
    counts: dict[str, int]


class CoverageStatus(CoverageMetrics):
    readiness_legacy: ConvergenceResult


class CheckpointValidationError(ValueError):
    """A checkpoint record does not satisfy the persisted schema."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class ValidatedCheckpoint:
    checkpoint_id: str
    span: tuple[float, float]
    status: str


class PersonPresenceState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class PersonPresenceObservation:
    subject: str
    state: PersonPresenceState
    timestamp: float


def _is_finite_number(value: CheckpointValue) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_person_presence_observation(
    value: CheckpointValue,
    span: tuple[float, float],
) -> PersonPresenceObservation:
    """Parse one supplied visual observation at the checkpoint boundary."""
    if not isinstance(value, dict):
        raise CheckpointValidationError(
            "'visual_observation' must be a person_presence object"
        )
    kind = value.get("kind")
    if kind != "person_presence":
        raise CheckpointValidationError(
            "'visual_observation.kind' must be 'person_presence'"
        )
    subject = value.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise CheckpointValidationError(
            "'visual_observation.subject' must be non-empty"
        )
    raw_state = value.get("state")
    try:
        state = PersonPresenceState(raw_state)
    except (TypeError, ValueError):
        raise CheckpointValidationError(
            "'visual_observation.state' must be 'present', 'absent', or "
            "'uncertain'"
        ) from None
    timestamp = value.get("timestamp")
    if not _is_finite_number(timestamp):
        raise CheckpointValidationError(
            "'visual_observation.timestamp' must be a finite number"
        )
    parsed_timestamp = float(timestamp)
    if not span[0] <= parsed_timestamp < span[1]:
        raise CheckpointValidationError(
            "'visual_observation.timestamp' must be inside checkpoint span"
        )
    return PersonPresenceObservation(
        subject=subject.strip(),
        state=state,
        timestamp=parsed_timestamp,
    )


def validate_checkpoint(
    ws: Workspace, obj: CheckpointObject
) -> ValidatedCheckpoint:
    checkpoint = validate_checkpoint_shape(obj)
    if checkpoint.span[1] > float(ws.manifest["duration"]):
        raise CheckpointValidationError(SPAN_END_EXCEEDS_DURATION)
    return checkpoint


def validate_checkpoint_shape(obj: CheckpointObject) -> ValidatedCheckpoint:
    """Validate workspace-independent fields before reading workspace state."""
    checkpoint_id = obj.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        raise CheckpointValidationError("checkpoint requires non-empty 'id'")

    status = obj.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise CheckpointValidationError(f"'status' must be one of {STATUSES}")

    hypothesis = obj.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise CheckpointValidationError("checkpoint requires non-empty 'hypothesis'")

    span = obj.get("span")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        raise CheckpointValidationError(
            "'span' must be [start, end] seconds with finite numbers"
        )
    start_value, end_value = span
    if not _is_finite_number(start_value) or not _is_finite_number(end_value):
        raise CheckpointValidationError(
            "'span' must be [start, end] seconds with finite numbers"
        )
    start, end = float(start_value), float(end_value)
    if not 0 <= start < end:
        raise CheckpointValidationError(
            "'span' must be [start, end] seconds with 0 <= start < end"
        )
    if "confidence" in obj:
        raw_confidence = obj["confidence"]
        if not _is_finite_number(raw_confidence) or not 0 <= raw_confidence <= 1:
            raise CheckpointValidationError(
                "'confidence' must be a finite number between 0 and 1"
            )
    if "exact_tokens" in obj:
        raw_tokens = obj["exact_tokens"]
        if not isinstance(raw_tokens, (list, tuple)) or not all(
            isinstance(token, str) and token.strip() for token in raw_tokens
        ):
            raise CheckpointValidationError(
                "'exact_tokens' must be a list of non-empty strings"
            )
    if "visual_observation" in obj:
        parse_person_presence_observation(obj["visual_observation"], (start, end))

    return ValidatedCheckpoint(
        checkpoint_id=checkpoint_id,
        span=(start, end),
        status=status,
    )


def validate_transition(
    previous: ValidatedCheckpoint | None, current: ValidatedCheckpoint
) -> None:
    if (
        previous is not None
        and previous.status in TERMINAL_STATUSES
        and current.status == "hypothesized"
    ):
        raise CheckpointValidationError(
            "invalid checkpoint status transition: "
            f"{previous.status} -> {current.status}"
        )
