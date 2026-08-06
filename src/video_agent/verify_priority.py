"""Deterministic visual-verification priority and trigger-alignment audit.

CRAFT(arXiv 2605.19075)의 "확신이 낮을 때만 시각 검증으로 escalate"를 추가
모델 의존 없이 치환한다: 점수는 원장·전사의 기존 신호만으로 언제든 재계산
가능한 파생값이므로 원장에 쓰지 않는다 — 감사 가능성은 결정적 재계산과
리플레이(trigger_alignment)가 제공한다 (future-queue #49 v1).
"""

from __future__ import annotations

from typing import Final, TypedDict

from .checkpoint_schema import TERMINAL_STATUSES, CheckpointObject
from .checkpoint_store import _load_checkpoint_entries, load_checkpoint_history
from .transcript_segments import TranscriptSegment, load_transcript_segments
from .verification_types import CheckpointEntries
from .workspace import Workspace

# 가중치는 convergence_gate 권고문("confidence<0.7·화자 전환·전사-무관 버스트
# 지점을 우선 캡처")의 서술 우선순위를 수치화한 것 — 자기 확신 결여가 1순위,
# 전사로 접지할 수 없는 구간이 2순위다.
_WEIGHTS: Final = {
    "self_doubt": 0.4,
    "transcript_silence": 0.25,
    "asr_weakness": 0.2,
    "speaker_shift": 0.15,
}
_LOW_ASR_CONF: Final = 0.6      # brief의 low-conf 표기와 같은 경계
_UNSTATED_DOUBT: Final = 0.5    # 확신 미기재 = 정량화 안 함 → 중간 의심
ALIGNMENT_TOP: Final = 3
STATUS_QUEUE_CAP: Final = 5

type _Candidate = tuple[str, tuple[float, float], float | None]


class VerifyPriorityItem(TypedDict):
    id: str
    span: list[float]
    score: float
    signals: dict[str, float]


class TriggerAlignment(TypedDict):
    verify_transitions: int
    aligned_top3: int
    alignment_ratio: float | None


def _confidence(record: CheckpointObject) -> float | None:
    raw = record.get("confidence")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _span(record: CheckpointObject) -> tuple[float, float] | None:
    raw = record.get("span")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    start, end = raw
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return None
    if isinstance(end, bool) or not isinstance(end, (int, float)):
        return None
    return float(start), float(end)


def _segment_low_conf(segment: TranscriptSegment) -> bool:
    conf = segment.get("conf")
    return (
        not isinstance(conf, bool)
        and isinstance(conf, (int, float))
        and conf < _LOW_ASR_CONF
    )


def _signals(
    span: tuple[float, float],
    confidence: float | None,
    segments: list[TranscriptSegment],
) -> dict[str, float]:
    overlapping = [
        segment
        for segment in segments
        if segment["start"] < span[1] and span[0] < segment["end"]
    ]
    if overlapping:
        transcript_silence = 0.0
        asr_weakness = sum(
            1 for segment in overlapping if _segment_low_conf(segment)
        ) / len(overlapping)
        speakers = {
            str(segment.get("speaker"))
            for segment in overlapping
            if segment.get("speaker")
        }
        speaker_shift = 1.0 if len(speakers) >= 2 else 0.0
    else:
        transcript_silence = 1.0
        asr_weakness = 0.0
        speaker_shift = 0.0
    self_doubt = (
        _UNSTATED_DOUBT if confidence is None else 1.0 - confidence
    )
    return {
        "self_doubt": round(self_doubt, 4),
        "transcript_silence": transcript_silence,
        "asr_weakness": round(asr_weakness, 4),
        "speaker_shift": speaker_shift,
    }


def _score_candidates(
    candidates: list[_Candidate], segments: list[TranscriptSegment]
) -> list[VerifyPriorityItem]:
    items: list[VerifyPriorityItem] = []
    for checkpoint_id, span, confidence in candidates:
        signals = _signals(span, confidence, segments)
        score = round(
            sum(_WEIGHTS[name] * value for name, value in signals.items()),
            3,
        )
        items.append(
            {
                "id": checkpoint_id,
                "span": [span[0], span[1]],
                "score": score,
                "signals": signals,
            }
        )
    items.sort(key=lambda item: (-item["score"], item["span"][0], item["id"]))
    return items


def verify_priority_from_entries(
    ws: Workspace,
    entries: CheckpointEntries,
    *,
    top: int | None = None,
    segments: list[TranscriptSegment] | None = None,
) -> list[VerifyPriorityItem]:
    """호출자가 이미 읽은 원장/전사 스냅샷에서 검증 우선순위를 계산한다."""
    candidates: list[_Candidate] = [
        (validated.checkpoint_id, validated.span, _confidence(checkpoint))
        for validated, checkpoint in entries
        if validated.status == "hypothesized"
    ]
    if not candidates:
        return []
    if segments is None:
        segments = load_transcript_segments(ws.transcript_path)
    items = _score_candidates(candidates, segments)
    return items if top is None else items[:top]


def verify_priority_queue(
    ws: Workspace, *, top: int | None = None
) -> list[VerifyPriorityItem]:
    """Rank open hypotheses by how much visual verification they warrant."""
    return verify_priority_from_entries(
        ws, _load_checkpoint_entries(ws), top=top
    )


def trigger_alignment(ws: Workspace) -> TriggerAlignment:
    """검증 전이가 당시 우선순위 top-3을 겨눴는지 원장 리플레이로 계측한다."""
    return trigger_alignment_from_history(ws, load_checkpoint_history(ws))


def trigger_alignment_from_history(
    ws: Workspace,
    history: list[CheckpointObject],
    *,
    segments: list[TranscriptSegment] | None = None,
) -> TriggerAlignment:
    """이미 읽어 둔 원장 이력(과 선택적 전사 스냅샷)에서 트리거 정렬을 계측한다.

    가설 없이 곧장 verified로 적힌 기록은 게이팅 결정이 아니므로 세지
    않는다. 근사 한계: 전사는 현재 리비전 기준 — 결박 워크스페이스는 전사
    리비전이 봉인되어 있어 전이 시점과 실질 차이가 없다.
    """
    if segments is None:
        segments = load_transcript_segments(ws.transcript_path)
    latest: dict[str, tuple[str, tuple[float, float], float | None]] = {}
    transitions = 0
    aligned = 0
    for record in history:
        checkpoint_id = record.get("id")
        status = record.get("status")
        span = _span(record)
        if (
            not isinstance(checkpoint_id, str)
            or not isinstance(status, str)
            or span is None
        ):
            continue
        previous = latest.get(checkpoint_id)
        if (
            previous is not None
            and previous[0] == "hypothesized"
            and status in TERMINAL_STATUSES
        ):
            open_hypotheses = [
                (open_id, open_span, open_confidence)
                for open_id, (open_status, open_span, open_confidence)
                in latest.items()
                if open_status == "hypothesized"
            ]
            ranked = _score_candidates(open_hypotheses, segments)
            transitions += 1
            if checkpoint_id in {
                item["id"] for item in ranked[:ALIGNMENT_TOP]
            }:
                aligned += 1
        latest[checkpoint_id] = (status, span, _confidence(record))
    return {
        "verify_transitions": transitions,
        "aligned_top3": aligned,
        "alignment_ratio": (
            round(aligned / transitions, 4) if transitions else None
        ),
    }
