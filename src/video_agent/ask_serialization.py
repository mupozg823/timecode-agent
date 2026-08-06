"""Compact agent projection for deterministic ask envelopes."""

from __future__ import annotations

from typing import Literal, TypedDict

from .ask_types import AskEnvelope, DepartureInterval


class VerifiedIntervalPayload(TypedDict):
    from_ms: int
    to_ms: int
    evidence: list[str]


class UncertainIntervalPayload(TypedDict):
    from_ms: int
    to_ms: int
    code: str


class AskEnvelopePayload(TypedDict):
    v: Literal[1]
    intent: str
    subject: str
    reply_locale: str
    status: str
    count: int
    verified: list[VerifiedIntervalPayload]
    uncertain: list[UncertainIntervalPayload]
    next_ms: list[int]


def _milliseconds(seconds: float) -> int:
    return round(seconds * 1000)


def _verified_evidence(interval: DepartureInterval) -> list[str]:
    return list(
        dict.fromkeys(
            (*interval.before.frame_refs, *interval.after.frame_refs)
        )
    )


def envelope_to_payload(report: AskEnvelope) -> AskEnvelopePayload:
    """Project one envelope without localized prose or source duplication."""
    return {
        "v": report.version,
        "intent": report.intent.value,
        "subject": report.subject.value,
        "reply_locale": report.reply_locale,
        "status": report.status.value,
        "count": report.count,
        "verified": [
            {
                "from_ms": _milliseconds(
                    interval.before.observation.timestamp
                ),
                "to_ms": _milliseconds(
                    interval.after.observation.timestamp
                ),
                "evidence": _verified_evidence(interval),
            }
            for interval in report.verified
        ],
        "uncertain": [
            {
                "from_ms": _milliseconds(interval.start),
                "to_ms": _milliseconds(interval.end),
                "code": interval.reason.value,
            }
            for interval in report.uncertain
        ],
        "next_ms": [
            _milliseconds(timestamp)
            for timestamp in report.follow_up_timestamps
        ],
    }
