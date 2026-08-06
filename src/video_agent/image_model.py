"""Deterministic image/cause identity and event normalization."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Final

from .image_naming import (
    IMAGE_SUFFIXES as IMAGE_SUFFIXES,
)
from .image_naming import (
    _identity_image_path,
    _stable_id,
    cause_identity,
    filename_metadata,
    image_identity,
    infer_image_kind,
    normalize_image_path,
    resolve_image_path,
    strip_cell_midpoints,
)
from .image_types import ImageEvent, ImageMetadata, ImageRecord
from .revision import REVISION_FIELDS
from .temporal import canonicalize_span
from .workspace import Workspace

PROVENANCE_SCHEMA: Final = 1
_IMAGE_ID_RE = re.compile(r"^img-[0-9a-f]{16}$")
# 파일명이 실어 나르는 밀리초는 반올림된 값이라 미디어 끝을 아주 조금 넘을
# 수 있다. 그 폭까지만 끝에 맞춘다 — revision_backfill의 원장 클램프와 같은
# 정책이며, 그 이상은 반올림이 아니라 불일치이므로 그대로 거부한다.
_DERIVED_SPAN_CLAMP_SECONDS: Final = 5.0
_DERIVED_SPAN_CLAMP_RATIO: Final = 0.01
_KNOWN_INPUT_KEYS: Final = {
    "schema", "path", "file", "kind", "image_id", "cause_id",
    "cause_type", "edge_id", "reason", "source", "t", "span",
    "timestamps", "crop", "requested_t", "window", "role", "scores",
    "cells", "cols", "render_version", "metadata",
    "temporal_span", *REVISION_FIELDS,
}


def _infer_cause_type(raw: Mapping[str, object], kind: str) -> str:
    explicit = raw.get("cause_type")
    if isinstance(explicit, str) and explicit:
        return explicit
    if kind == "filmstrip":
        return "overview"
    if str(raw.get("reason") or "").startswith("sharp-gate"):
        return "sharpness-selection"
    return "capture"


def _event_context(event: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "path", "kind", "t", "span", "timestamps", "crop", "reason",
        "requested_t", "window", "role", "cols", "render_version",
    )
    return {key: event[key] for key in keys if key in event}


def _finite_number(value: object, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"image provenance {field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(
            f"image provenance {field} must be a finite number"
        ) from error
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"image provenance {field} must be >= {minimum}")
    return number


def _number_list(
    value: object, field: str, *, exact_length: int | None = None
) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"image provenance {field} must be a number list")
    if exact_length is not None and len(value) != exact_length:
        raise ValueError(
            f"image provenance {field} must contain {exact_length} numbers"
        )
    return [_finite_number(item, field) for item in value]


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"image provenance {field} must be a positive integer")
    return value


def _parse_metadata(
    raw: Mapping[str, object], image_path: str
) -> ImageMetadata:
    values = filename_metadata(image_path)
    if "t" in raw:
        values["t"] = _finite_number(raw["t"], "t")
    if "span" in raw:
        span = _number_list(raw["span"], "span", exact_length=2)
        if span[0] >= span[1]:
            raise ValueError("image provenance span must have start < end")
        values["span"] = span
    if "timestamps" in raw:
        values["timestamps"] = _number_list(raw["timestamps"], "timestamps")
    if "crop" in raw:
        crop = raw["crop"]
        if crop is not None and not isinstance(crop, str):
            raise ValueError("image provenance crop must be a string or null")
        values["crop"] = crop
    if "requested_t" in raw:
        values["requested_t"] = _finite_number(raw["requested_t"], "requested_t")
    if "window" in raw:
        values["window"] = _finite_number(raw["window"], "window")
    if "role" in raw:
        role = raw["role"]
        if not isinstance(role, str):
            raise ValueError("image provenance role must be a string")
        values["role"] = role
    if "scores" in raw:
        scores = raw["scores"]
        if not isinstance(scores, Mapping):
            raise ValueError("image provenance scores must be an object")
        parsed_scores: dict[str, float] = {}
        for key, score in scores.items():
            if not isinstance(key, str):
                raise ValueError("image provenance score keys must be strings")
            parsed_scores[key] = _finite_number(score, f"scores.{key}")
        values["scores"] = parsed_scores
    if "cells" in raw:
        values["cells"] = _positive_int(raw["cells"], "cells")
    if "cols" in raw:
        values["cols"] = _positive_int(raw["cols"], "cols")
    if "render_version" in raw:
        values["render_version"] = _positive_int(
            raw["render_version"], "render_version"
        )
    if "metadata" in raw:
        metadata = raw["metadata"]
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) for key in metadata
        ):
            raise ValueError("image provenance metadata must be an object")
        values["metadata"] = dict(metadata)
    if "timestamps" not in raw and ("span" in raw or "cells" in raw):
        # 명시 geometry가 파일명 값을 덮으면 파일명 유도 셀 중점은 무효 —
        # 최종 span·cells로 재계산하고, 재계산 불가면 제거한다.
        values.pop("timestamps", None)
        span = values.get("span")
        cells = values.get("cells")
        if span is not None and cells is not None:
            midpoints = strip_cell_midpoints(span[0], span[1], cells)
            if midpoints is not None:
                values["timestamps"] = midpoints
    return values


def _clamp_derived_span(ws: Workspace, span: object) -> object:
    """파일명에서 유도한 구간만 미디어 끝에 맞춘다.

    필름스트립은 자기 파일명에 구간 끝을 밀리초로 반올림해 적는다. 그래서
    영상 길이가 114.427937초일 때 이름은 `…_000114428`이 되고, 그 63마이크로초
    때문에 워크스페이스 전체가 결박되지 못한다. 원장이 명시한 구간은 손대지
    않는다 — 그쪽 불일치는 증거의 문제이지 이름의 문제가 아니다.
    """
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return span
    try:
        duration = float(ws.manifest.get("duration") or 0.0)
        start, end = float(span[0]), float(span[1])
    except (TypeError, ValueError):
        return span
    if duration <= 0 or end <= duration or start >= duration:
        return span
    ceiling = max(
        _DERIVED_SPAN_CLAMP_SECONDS, duration * _DERIVED_SPAN_CLAMP_RATIO
    )
    if end - duration > ceiling:
        return span
    return [start, duration]


def normalize_event(
    ws: Workspace,
    raw: Mapping[str, object],
    *,
    source: str = "canonical",
    preserve_persisted_id: bool = False,
) -> ImageEvent:
    path_value = raw.get("path", raw.get("file"))
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        raise ValueError("image provenance event requires path or file")
    logical_image_path = normalize_image_path(path_value)
    image_path = logical_image_path
    if source != "inventory":
        image_path, _ = resolve_image_path(ws, logical_image_path)
    raw_kind = raw.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) else infer_image_kind(image_path)
    values = _parse_metadata(raw, image_path)
    canonical_span = None
    if "span" in values:
        span_input = values["span"]
        # inventory는 디스크 스캔이라 raw의 구간도 파일명에서 풀어낸 값이다
        # (`load_image_records`가 `filename_metadata`를 그대로 펼쳐 넣는다).
        if "span" not in raw or source == "inventory":
            span_input = _clamp_derived_span(ws, span_input)
        projected, canonical_span = canonicalize_span(
            ws,
            span_input,
            raw.get("temporal_span"),
        )
        values["span"] = projected
    extra = {key: value for key, value in raw.items()
             if key not in _KNOWN_INPUT_KEYS}
    if extra:
        prior_metadata = values.get("metadata")
        values["metadata"] = {
            **(prior_metadata if isinstance(prior_metadata, dict) else {}),
            **extra,
        }
    reason_value = raw.get("reason")
    if reason_value is not None and not isinstance(reason_value, str):
        raise ValueError("image provenance reason must be a string or null")
    reason = reason_value
    cause_type = _infer_cause_type(raw, kind)
    expected_image_id = image_identity(ws, logical_image_path)
    persisted_image_id = raw.get("image_id")
    image_id = (
        persisted_image_id
        if preserve_persisted_id
        and isinstance(persisted_image_id, str)
        and _IMAGE_ID_RE.fullmatch(persisted_image_id)
        and persisted_image_id == expected_image_id
        else expected_image_id
    )
    explicit_cause = raw.get("cause_id")
    provisional = {
        **values,
        "path": image_path,
        "kind": kind,
        "reason": reason,
    }
    cause_context = _event_context(provisional)
    cause_context["path"] = _identity_image_path(ws, logical_image_path)
    cause_id = (
        explicit_cause
        if isinstance(explicit_cause, str) and explicit_cause.startswith("cause-")
        else cause_identity(ws, cause_type, cause_context)
    )
    event: ImageEvent = {
        "schema": PROVENANCE_SCHEMA,
        "path": image_path,
        "file": PurePosixPath(image_path).name,
        "kind": kind,
        "image_id": image_id,
        "cause_id": cause_id,
        "cause_type": cause_type,
        "edge_id": _stable_id("edge", [image_id, cause_id]),
        "reason": reason,
        "source": source,
    }
    if "t" in values:
        event["t"] = values["t"]
    if "span" in values:
        event["span"] = values["span"]
    if "timestamps" in values:
        event["timestamps"] = values["timestamps"]
    if "crop" in values:
        event["crop"] = values["crop"]
    if "requested_t" in values:
        event["requested_t"] = values["requested_t"]
    if "window" in values:
        event["window"] = values["window"]
    if "role" in values:
        event["role"] = values["role"]
    if "scores" in values:
        event["scores"] = values["scores"]
    if "cells" in values:
        event["cells"] = values["cells"]
    if "cols" in values:
        event["cols"] = values["cols"]
    if "render_version" in values:
        event["render_version"] = values["render_version"]
    if "metadata" in values:
        event["metadata"] = values["metadata"]
    if canonical_span is not None:
        event["temporal_span"] = canonical_span
    for field in REVISION_FIELDS:
        revision = raw.get(field)
        if isinstance(revision, str):
            event[field] = revision
    return event


def record_sort_key(record: ImageRecord | Mapping[str, object]) -> tuple[float, str]:
    path = str(record.get("path") or record.get("file") or "")
    span = record.get("span")
    if isinstance(span, list) and span and isinstance(span[0], (int, float)):
        return float(span[0]), path
    timestamp = record.get("t")
    return (
        float(timestamp) if isinstance(timestamp, (int, float)) else float("inf"),
        path,
    )
