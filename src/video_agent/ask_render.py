"""Localized human rendering for deterministic checkpoint answers."""

from __future__ import annotations

from typing import assert_never

from .ask_locale import HumanLocale, parse_human_locale
from .ask_types import AskEnvelope, UncertaintyReason


def _refs_ko(refs: tuple[str, ...]) -> str:
    return ", ".join(refs) or "(프레임 참조 없음)"


def _refs_en(refs: tuple[str, ...]) -> str:
    return ", ".join(refs) or "(no frame reference)"


def _reason_ko(reason: UncertaintyReason) -> str:
    match reason:
        case UncertaintyReason.NO_PRESENCE_OBSERVATIONS:
            return "사람 존재 관측 없음"
        case UncertaintyReason.PRESENCE_UNRESOLVED:
            return "선택한 프레임의 사람 존재 여부를 판독하지 못함"
        case UncertaintyReason.UNOBSERVED_PREFIX:
            return "첫 관측 이전 미관측"
        case UncertaintyReason.UNOBSERVED_SUFFIX:
            return "마지막 관측 이후 미관측"
        case UncertaintyReason.CONFLICTING_STATE_AT_TIMESTAMP:
            return "동일 시각 상태 충돌"
        case UncertaintyReason.ENDPOINT_EVIDENCE_MISSING:
            return "끝점 근거 미충족"
        case unreachable:
            assert_never(unreachable)


def _reason_en(reason: UncertaintyReason) -> str:
    match reason:
        case UncertaintyReason.NO_PRESENCE_OBSERVATIONS:
            return "no person-presence observations"
        case UncertaintyReason.PRESENCE_UNRESOLVED:
            return "person presence could not be resolved from selected frames"
        case UncertaintyReason.UNOBSERVED_PREFIX:
            return "unobserved before the first sample"
        case UncertaintyReason.UNOBSERVED_SUFFIX:
            return "unobserved after the last sample"
        case UncertaintyReason.CONFLICTING_STATE_AT_TIMESTAMP:
            return "conflicting states at the same timestamp"
        case UncertaintyReason.ENDPOINT_EVIDENCE_MISSING:
            return "endpoint evidence requirements were not met"
        case unreachable:
            assert_never(unreachable)


def _render_ko(report: AskEnvelope) -> str:
    count = len(report.verified)
    if count:
        lines = [f"검증된 이탈 횟수: {count}회"]
    else:
        lines = [
            "검증된 이탈 횟수: 0회 "
            "(이탈 없음이 증명된 것이 아니라 확인되지 않음)"
        ]
    for interval in report.verified:
        lines.append(
            "검증 구간: "
            f"{interval.before.observation.timestamp:.3f}s → "
            f"{interval.after.observation.timestamp:.3f}s | "
            f"이전 프레임: {_refs_ko(interval.before.frame_refs)} | "
            f"이후 프레임: {_refs_ko(interval.after.frame_refs)}"
        )
    if report.uncertain:
        for interval in report.uncertain:
            lines.append(
                "불확실 구간: "
                f"{interval.start:.3f}s → {interval.end:.3f}s | "
                f"이전 프레임: {_refs_ko(interval.before_refs)} | "
                f"이후 프레임: {_refs_ko(interval.after_refs)} | "
                f"사유: {_reason_ko(interval.reason)}"
            )
    else:
        lines.append("불확실 구간: 없음")
    if report.follow_up_timestamps:
        formatted = ", ".join(
            f"{timestamp:.3f}s" for timestamp in report.follow_up_timestamps
        )
        lines.append(f"후속 확인 시각: {formatted}")
    return "\n".join(lines)


def _render_en(report: AskEnvelope) -> str:
    count = report.count
    if count:
        lines = [f"Verified screen departures: {count}"]
    else:
        lines = [
            "Verified screen departures: 0 "
            "(not confirmed; this does not prove none occurred)"
        ]
    for interval in report.verified:
        lines.append(
            "Verified interval: "
            f"{interval.before.observation.timestamp:.3f}s → "
            f"{interval.after.observation.timestamp:.3f}s | "
            f"before: {_refs_en(interval.before.frame_refs)} | "
            f"after: {_refs_en(interval.after.frame_refs)}"
        )
    if report.uncertain:
        for interval in report.uncertain:
            lines.append(
                "Uncertain interval: "
                f"{interval.start:.3f}s → {interval.end:.3f}s | "
                f"before: {_refs_en(interval.before_refs)} | "
                f"after: {_refs_en(interval.after_refs)} | "
                f"reason: {_reason_en(interval.reason)}"
            )
    else:
        lines.append("Uncertain intervals: none")
    if report.follow_up_timestamps:
        formatted = ", ".join(
            f"{timestamp:.3f}s" for timestamp in report.follow_up_timestamps
        )
        lines.append(f"Suggested follow-up timestamps: {formatted}")
    return "\n".join(lines)


def render_report(report: AskEnvelope) -> str:
    """Render one localized human answer from the canonical envelope."""
    match parse_human_locale(report.reply_locale):
        case HumanLocale.KOREAN:
            return _render_ko(report)
        case HumanLocale.ENGLISH:
            return _render_en(report)
        case unreachable:
            assert_never(unreachable)
