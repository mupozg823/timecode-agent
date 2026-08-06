"""Typed values for deterministic checkpoint questions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Literal, NewType

from .checkpoint_schema import PersonPresenceObservation, PersonPresenceState
from .image_naming import strip_cell_midpoints

ReplyLocale = NewType("ReplyLocale", str)


@unique
class AskIntent(StrEnum):
    PERSON_EXIT_COUNT = "person_exit_count"


@unique
class AskSubject(StrEnum):
    SEATED_MAN = "seated_man"


@unique
class AskStatus(StrEnum):
    PARTIAL = "partial"
    UNOBSERVED = "unobserved"


@unique
class UncertaintyReason(StrEnum):
    NO_PRESENCE_OBSERVATIONS = "NO_PRESENCE_OBSERVATIONS"
    PRESENCE_UNRESOLVED = "PRESENCE_UNRESOLVED"
    UNOBSERVED_PREFIX = "UNOBSERVED_PREFIX"
    UNOBSERVED_SUFFIX = "UNOBSERVED_SUFFIX"
    CONFLICTING_STATE_AT_TIMESTAMP = "CONFLICTING_STATE_AT_TIMESTAMP"
    ENDPOINT_EVIDENCE_MISSING = "ENDPOINT_EVIDENCE_MISSING"


@dataclass(frozen=True, slots=True)
class ObservationSample:
    checkpoint_id: str
    span: tuple[float, float]
    observation: PersonPresenceObservation
    frame_refs: tuple[str, ...]
    visually_grounded: bool


@dataclass(frozen=True, slots=True)
class DepartureInterval:
    before: ObservationSample
    after: ObservationSample
    verified: bool
    reason: UncertaintyReason | None


@dataclass(frozen=True, slots=True)
class UncertainInterval:
    start: float
    end: float
    before_refs: tuple[str, ...]
    after_refs: tuple[str, ...]
    reason: UncertaintyReason


@dataclass(frozen=True, slots=True)
class AskEnvelope:
    version: Literal[1]
    intent: AskIntent
    subject: AskSubject
    reply_locale: ReplyLocale
    status: AskStatus
    verified: tuple[DepartureInterval, ...]
    uncertain: tuple[UncertainInterval, ...]
    follow_up_timestamps: tuple[float, ...]

    @property
    def count(self) -> int:
        return len(self.verified)


def open_uncertainty(
    samples: list[ObservationSample],
    duration: float,
) -> tuple[tuple[UncertainInterval, ...], tuple[float, ...]]:
    """Model unobserved video edges and their deterministic follow-ups."""
    if not samples:
        whole = UncertainInterval(
            start=0.0,
            end=duration,
            before_refs=(),
            after_refs=(),
            reason=UncertaintyReason.NO_PRESENCE_OBSERVATIONS,
        )
        uniform_follow_ups = tuple(
            strip_cell_midpoints(0.0, duration, 4) or ()
        )
        return (whole,), uniform_follow_ups
    resolved = [
        sample
        for sample in samples
        if sample.observation.state is not PersonPresenceState.UNCERTAIN
    ]
    if not resolved:
        whole = UncertainInterval(
            start=0.0,
            end=duration,
            before_refs=(),
            after_refs=(),
            reason=UncertaintyReason.PRESENCE_UNRESOLVED,
        )
        return (whole,), tuple(
            sample.observation.timestamp for sample in samples
        )
    uncertain: list[UncertainInterval] = []
    follow_ups: list[float] = []
    first = resolved[0]
    if first.observation.timestamp > 0.0:
        uncertain.append(
            UncertainInterval(
                start=0.0,
                end=first.observation.timestamp,
                before_refs=(),
                after_refs=first.frame_refs,
                reason=UncertaintyReason.UNOBSERVED_PREFIX,
            )
        )
        follow_ups.append(first.observation.timestamp / 2)
    last = resolved[-1]
    if last.observation.timestamp < duration:
        uncertain.append(
            UncertainInterval(
                start=last.observation.timestamp,
                end=duration,
                before_refs=last.frame_refs,
                after_refs=(),
                reason=UncertaintyReason.UNOBSERVED_SUFFIX,
            )
        )
        follow_ups.append((last.observation.timestamp + duration) / 2)
    cursor = 0
    while cursor < len(samples):
        if samples[cursor].observation.state is not PersonPresenceState.UNCERTAIN:
            cursor += 1
            continue
        run_start = cursor
        while (
            cursor < len(samples)
            and samples[cursor].observation.state is PersonPresenceState.UNCERTAIN
        ):
            follow_ups.append(samples[cursor].observation.timestamp)
            cursor += 1
        before = samples[run_start - 1] if run_start > 0 else None
        after = samples[cursor] if cursor < len(samples) else None
        start = before.observation.timestamp if before is not None else 0.0
        end = after.observation.timestamp if after is not None else duration
        if start < end:
            uncertain.append(
                UncertainInterval(
                    start=start,
                    end=end,
                    before_refs=before.frame_refs if before is not None else (),
                    after_refs=after.frame_refs if after is not None else (),
                    reason=UncertaintyReason.PRESENCE_UNRESOLVED,
                )
            )
    by_bounds = {
        (interval.start, interval.end): interval for interval in uncertain
    }
    return tuple(by_bounds.values()), tuple(sorted(dict.fromkeys(follow_ups)))
