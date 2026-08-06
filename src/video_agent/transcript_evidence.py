"""Transcript identity and temporal-overlap evidence validation."""

from __future__ import annotations

import math

from .checkpoint_schema import CheckpointObject, CheckpointValue
from .verification_types import SegmentIdKey, TranscriptSegmentIndex
from .workspace import Workspace, load_json


def _finite_float(value: CheckpointValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _segment_id_key(value: CheckpointValue) -> SegmentIdKey | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return ("number", str(value))
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        normalized = int(value) if value.is_integer() else value
        return ("number", str(normalized))
    if isinstance(value, str) and value.strip():
        return ("string", value.strip())
    return None


def _segment_span(value: CheckpointValue) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    start = _finite_float(value.get("start"))
    end = _finite_float(value.get("end"))
    if start is None or end is None or not 0 <= start < end:
        return None
    return (start, end)


def _spans_overlap(
    left: tuple[float, float], right: tuple[float, float]
) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def load_transcript_segment_index(ws: Workspace) -> TranscriptSegmentIndex:
    """Index valid transcript spans by stable segment identity."""
    return transcript_segment_index_from_value(load_json(ws.transcript_path))


def transcript_segment_index_from_value(
    raw: CheckpointValue,
) -> TranscriptSegmentIndex:
    """이미 읽어 둔 transcript JSON에서 인덱스를 만든다 — 한 스냅샷을 여러
    소비자가 공유해야 같은 상태 위에서 판정하고 판독도 1회로 남는다."""
    if not isinstance(raw, list):
        return {}

    index: TranscriptSegmentIndex = {}
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        span = _segment_span(item)
        segment_id = _segment_id_key(item.get("id", position))
        if span is None or segment_id is None:
            continue
        index.setdefault(segment_id, []).append(span)
    return index


def _indexed_segment_overlaps(
    segment_id: CheckpointValue,
    transcript_index: TranscriptSegmentIndex,
    checkpoint_span: tuple[float, float],
) -> bool:
    key = _segment_id_key(segment_id)
    if key is None:
        return False
    return any(
        _spans_overlap(segment_span, checkpoint_span)
        for segment_span in transcript_index.get(key, ())
    )


def has_segment_support(
    checkpoint: CheckpointObject,
    transcript_index: TranscriptSegmentIndex,
    checkpoint_span: tuple[float, float],
) -> bool:
    """Require every declared segment to exist and overlap the checkpoint."""
    raw = checkpoint.get("segments")
    if not isinstance(raw, (list, tuple)) or not raw:
        return False
    return all(
        _indexed_segment_overlaps(
            item.get("id") if isinstance(item, dict) else item,
            transcript_index,
            checkpoint_span,
        )
        for item in raw
    )
