"""Read-only answers derived from typed checkpoint observations."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Final

from . import ask_question
from .ask_locale import normalize_reply_locale
from .ask_types import (
    AskEnvelope,
    AskIntent,
    AskStatus,
    AskSubject,
    DepartureInterval,
    ObservationSample,
    UncertainInterval,
    UncertaintyReason,
    open_uncertainty,
)
from .checkpoint_schema import (
    CheckpointObject,
    PersonPresenceState,
    parse_person_presence_observation,
    validate_checkpoint_shape,
)
from .checkpoint_store import _load_checkpoint_entries
from .checkpoints import promotable_checkpoints_from_entries
from .image_provenance import load_image_records, resolve_image_path
from .image_validation import (
    decodable_image_paths,
    image_record_verification_issues,
)
from .workspace import Workspace

_FRAME_TIMESTAMP_TOLERANCE_SECONDS: Final = 0.001

def _visual_refs(checkpoint: CheckpointObject) -> tuple[str, ...]:
    raw = checkpoint.get("visual_evidence")
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(
            item for item in raw if isinstance(item, str) and item.strip()
        )
    return ()


def _visually_grounded_ids(
    ws: Workspace,
    checkpoints: list[CheckpointObject],
    promotable_ids: frozenset[str],
) -> frozenset[str]:
    records = {record["path"]: record for record in load_image_records(ws)}
    resolutions: dict[str, tuple[str, Path] | None] = {}
    paths = []
    for checkpoint in checkpoints:
        for ref in _visual_refs(checkpoint):
            try:
                image_path, resolved = resolve_image_path(ws, ref)
            except ValueError:
                resolutions[ref] = None
            else:
                resolutions[ref] = (image_path, resolved)
                paths.append(resolved)
    decodable = decodable_image_paths(tuple(paths))
    grounded: set[str] = set()
    for checkpoint in checkpoints:
        checkpoint_id = checkpoint.get("id")
        if (
            not isinstance(checkpoint_id, str)
            or checkpoint_id not in promotable_ids
        ):
            continue
        target_span = validate_checkpoint_shape(checkpoint).span
        raw_observation = checkpoint.get("visual_observation")
        if raw_observation is None:
            continue
        observation = parse_person_presence_observation(
            raw_observation,
            target_span,
        )
        for ref in _visual_refs(checkpoint):
            resolution = resolutions.get(ref)
            if resolution is None:
                continue
            image_path, resolved = resolution
            record = records.get(image_path)
            record_timestamp = record.get("t") if record is not None else None
            if (
                resolved in decodable
                and record is not None
                and record["kind"] == "frame"
                and isinstance(record_timestamp, (int, float))
                and not isinstance(record_timestamp, bool)
                and math.isfinite(record_timestamp)
                and abs(record_timestamp - observation.timestamp)
                <= _FRAME_TIMESTAMP_TOLERANCE_SECONDS
                and not image_record_verification_issues(
                    record,
                    target_span,
                )
            ):
                grounded.add(checkpoint_id)
                break
    return frozenset(grounded)


def _samples(ws: Workspace) -> list[ObservationSample]:
    # 원장은 한 번만 읽는다 — 샘플과 promotability가 다른 판독본을 보면
    # 동시 기입이 낡은 표본을 새 리비전의 결박으로 승격시킨다.
    entries = _load_checkpoint_entries(ws)
    checkpoints = [checkpoint for _, checkpoint in entries]
    promotable_ids = frozenset(
        checkpoint_id
        for checkpoint in promotable_checkpoints_from_entries(ws, entries)
        if isinstance((checkpoint_id := checkpoint.get("id")), str)
    )
    grounded_ids = _visually_grounded_ids(ws, checkpoints, promotable_ids)
    samples: list[ObservationSample] = []
    for checkpoint in checkpoints:
        raw = checkpoint.get("visual_observation")
        if raw is None:
            continue
        validated = validate_checkpoint_shape(checkpoint)
        span = validated.span
        observation = parse_person_presence_observation(raw, span)
        checkpoint_id = str(checkpoint["id"])
        if not ask_question.is_supported_subject(observation.subject):
            continue
        samples.append(
            ObservationSample(
                checkpoint_id=checkpoint_id,
                span=span,
                observation=observation,
                frame_refs=_visual_refs(checkpoint),
                visually_grounded=checkpoint_id in grounded_ids,
            )
        )
    return sorted(
        samples,
        key=lambda sample: (
            sample.observation.timestamp,
            sample.checkpoint_id,
        ),
    )


def _conflict_interval(
    previous: ObservationSample | None,
    group: list[ObservationSample],
) -> DepartureInterval | None:
    if (
        previous is None
        or previous.observation.state != "present"
        or not any(item.observation.state == "absent" for item in group)
    ):
        return None
    absent = next(
        item for item in group if item.observation.state == "absent"
    )
    return DepartureInterval(
        before=previous,
        after=absent,
        verified=False,
        reason=UncertaintyReason.CONFLICTING_STATE_AT_TIMESTAMP,
    )


def _departure_intervals(
    samples: list[ObservationSample],
) -> tuple[DepartureInterval, ...]:
    intervals: list[DepartureInterval] = []
    previous: ObservationSample | None = None
    cursor = 0
    while cursor < len(samples):
        timestamp = samples[cursor].observation.timestamp
        end = cursor + 1
        while (
            end < len(samples)
            and samples[end].observation.timestamp == timestamp
        ):
            end += 1
        group = samples[cursor:end]
        states = {item.observation.state for item in group}
        if len(states) > 1:
            conflict = _conflict_interval(previous, group)
            if conflict is not None:
                intervals.append(conflict)
            previous = None
        else:
            # 같은 상태 그룹의 대표는 결박된 표본 우선 — ID 사전순 첫
            # 표본이 미결박이면 실제 근거가 있는데도 uncertain으로 강등된다.
            current = next(
                (item for item in group if item.visually_grounded),
                group[0],
            )
            if (
                previous is not None
                and previous.observation.state == "present"
                and current.observation.state == "absent"
            ):
                grounded = (
                    previous.visually_grounded and current.visually_grounded
                )
                intervals.append(
                    DepartureInterval(
                        before=previous,
                        after=current,
                        verified=grounded,
                        reason=(
                            None
                            if grounded
                            else UncertaintyReason.ENDPOINT_EVIDENCE_MISSING
                        ),
                    )
                )
            previous = current
        cursor = end
    return tuple(intervals)


def _as_uncertain(interval: DepartureInterval) -> UncertainInterval:
    return UncertainInterval(
        start=interval.before.observation.timestamp,
        end=interval.after.observation.timestamp,
        before_refs=interval.before.frame_refs,
        after_refs=interval.after.frame_refs,
        reason=(
            interval.reason
            or UncertaintyReason.ENDPOINT_EVIDENCE_MISSING
        ),
    )


def answer_question(
    ws: Workspace,
    question: str,
    *,
    reply_locale: str = "auto",
) -> AskEnvelope:
    """Answer the supported presence question from the latest projection."""
    if support_error := ask_question.question_support_error(question):
        raise ask_question.UnsupportedAskQuestionError(support_error)
    samples = _samples(ws)
    intervals = _departure_intervals(samples)
    verified = tuple(item for item in intervals if item.verified)
    transition_uncertainty = tuple(
        _as_uncertain(item) for item in intervals if not item.verified
    )
    edge_uncertainty, follow_ups = open_uncertainty(
        samples,
        float(ws.manifest["duration"]),
    )
    uncertain = tuple(
        sorted(
            (*transition_uncertainty, *edge_uncertainty),
            key=lambda interval: (interval.start, interval.end),
        )
    )
    resolved = any(
        sample.observation.state is not PersonPresenceState.UNCERTAIN
        for sample in samples
    )
    status = AskStatus.UNOBSERVED if not resolved else AskStatus.PARTIAL
    return AskEnvelope(
        version=1,
        intent=AskIntent.PERSON_EXIT_COUNT,
        subject=AskSubject.SEATED_MAN,
        reply_locale=normalize_reply_locale(reply_locale, question),
        status=status,
        verified=verified,
        uncertain=uncertain,
        follow_up_timestamps=follow_ups,
    )
