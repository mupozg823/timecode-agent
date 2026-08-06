"""컷 조인 비트 정합 게이트 — 출력 타임라인 조인 vs 최근접 비트.

방법론(docs/research/2026-08-06-bgm-beat-sync-editing-methodology.md)의
편집 수락 기준 `p90 |offset| <= 40ms`를 결정적 게이트로 강제한다.
시퀀스 cuts는 소스 span의 순서 연결이므로 조인 시각은 누적 출력 길이다.
BGM은 출력 t=0 정렬을 가정한다(다르면 beats.json을 그 오프셋으로 다시
추출한다).

스냅 제안은 placement 계산일 뿐이다 — 이 모듈은 원장을 쓰지 않는다.
적용(selection)은 에이전트가 제안을 검토해 기존 시퀀스 흐름으로 새
리비전을 올리는 것이고, 비트 인덱스 재배치가 아닌 초 단위 미세조정으로
되돌아가는 것은 회귀다(방법론 역개선 루프 규약).
"""

from __future__ import annotations

import math
from typing import TypedDict

from .beats import BeatGrid, nearest_beat
from .workspace import Workspace

# 인지 임계에서 온 값이 아니라 편집 수락 실측 기준(방법론 문서) —
# 40ms는 24fps 한 프레임(41.7ms) 아래로 "비트 위에 있다"고 들리는 상한.
DEFAULT_GATE_MS = 40.0
# 스냅으로 컷 길이가 이보다 짧아지면 제안하지 않는다(무의미 컷 방지).
MIN_CUT_LEN_S = 0.05


class BeatJoin(TypedDict):
    join_after_order: int
    instant: float
    nearest_beat: float
    offset_ms: float


class BeatAlignmentReport(TypedDict):
    sequence: str
    bpm: float
    gate_ms: float
    join_count: int
    joins: list[BeatJoin]
    p90_ms: float | None
    max_ms: float | None
    passed: bool


class SnapCut(TypedDict):
    order: int
    span: list[float]
    end_delta_s: float
    snapped: bool
    reason: str | None


def sequence_cuts(ws: Workspace, sequence_id: str) -> list[dict]:
    from .sequence import load_sequences

    sequences = {seq["id"]: seq for seq in load_sequences(ws)}
    if sequence_id not in sequences:
        raise ValueError(
            f"unknown sequence id: {sequence_id}"
            f" (있는 것: {sorted(sequences)})"
        )
    return sequences[sequence_id]["cuts"]


def _join_instants(cuts: list[dict]) -> list[tuple[int, float]]:
    """(조인 직전 컷 order, 출력 타임라인 시각) — 내부 조인 n-1개."""
    instants: list[tuple[int, float]] = []
    elapsed = 0.0
    for cut in cuts[:-1]:
        start, end = (float(v) for v in cut["span"])
        elapsed += end - start
        instants.append((int(cut["order"]), elapsed))
    return instants


def _p90(values: list[float]) -> float:
    """nearest-rank 90분위 — 보간 없이 실측값 하나를 돌려준다."""
    ranked = sorted(values)
    return ranked[max(0, math.ceil(0.9 * len(ranked)) - 1)]


def beat_alignment_report(
    sequence_id: str,
    cuts: list[dict],
    grid: BeatGrid,
    *,
    gate_ms: float = DEFAULT_GATE_MS,
) -> BeatAlignmentReport:
    joins: list[BeatJoin] = []
    for order, instant in _join_instants(cuts):
        beat = nearest_beat(instant, grid["beat_times"])
        joins.append({
            "join_after_order": order,
            "instant": round(instant, 4),
            "nearest_beat": beat,
            "offset_ms": round((instant - beat) * 1000.0, 1),
        })
    offsets = [abs(join["offset_ms"]) for join in joins]
    p90_ms = round(_p90(offsets), 1) if offsets else None
    return {
        "sequence": sequence_id,
        "bpm": grid["bpm"],
        "gate_ms": gate_ms,
        "join_count": len(joins),
        "joins": joins,
        "p90_ms": p90_ms,
        "max_ms": round(max(offsets), 1) if offsets else None,
        "passed": p90_ms is None or p90_ms <= gate_ms,
    }


def snap_proposal(
    cuts: list[dict],
    grid: BeatGrid,
    *,
    duration: float,
    min_cut_len_s: float = MIN_CUT_LEN_S,
) -> list[SnapCut]:
    """조인이 비트에 앉도록 각 컷 꼬리(end)를 조정한 span 제안.

    왼쪽부터 처리하며 앞선 조정이 뒤 조인 시각을 옮긴 것을 반영해
    누적으로 다시 계산한다. 소스 꼬리가 늘거나 주는 것이므로 내용이
    바뀐다 — 제안을 적용하기 전 조인 병치 판독(boundary-eval)을 다시
    돌리는 것이 전제다.
    """
    proposal: list[SnapCut] = []
    elapsed = 0.0
    last = len(cuts) - 1
    for index, cut in enumerate(cuts):
        start, end = (float(v) for v in cut["span"])
        order = int(cut["order"])
        if index == last:
            proposal.append({
                "order": order, "span": [start, end],
                "end_delta_s": 0.0, "snapped": False, "reason": "tail-cut",
            })
            break
        instant = elapsed + (end - start)
        delta = nearest_beat(instant, grid["beat_times"]) - instant
        new_end = end + delta
        if new_end - start < min_cut_len_s or new_end > duration:
            proposal.append({
                "order": order, "span": [start, end],
                "end_delta_s": 0.0, "snapped": False,
                "reason": "span-bounds",
            })
            elapsed = instant
            continue
        proposal.append({
            "order": order, "span": [start, round(new_end, 4)],
            "end_delta_s": round(delta, 4), "snapped": True, "reason": None,
        })
        elapsed = instant + delta
    return proposal
