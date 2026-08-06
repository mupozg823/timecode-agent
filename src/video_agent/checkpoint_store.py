"""POSIX-safe append-only checkpoint storage and validated index cache."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final
from weakref import WeakKeyDictionary

from .checkpoint_schema import (
    SPAN_END_EXCEEDS_DURATION,
    CheckpointObject,
    CheckpointValidationError,
    CheckpointValue,
    ValidatedCheckpoint,
    validate_checkpoint,
    validate_checkpoint_shape,
    validate_transition,
)
from .revision import (
    RevisionBindingError,
    bind_record_to_workspace,
    validate_record_revision,
)
from .temporal import TemporalSpanError, canonicalize_span
from .workspace import Workspace
from .workspace_lock import stable_workspace_lock

type _CheckpointSignature = tuple[int, int, int, int] | None
type _IndexedCheckpoint = tuple[ValidatedCheckpoint, CheckpointObject]
type _CheckpointCacheEntry = tuple[
    _CheckpointSignature,
    dict[str, _IndexedCheckpoint],
    int,
    bool,
]

# JSONL remains canonical; this projection vanishes with the Workspace object.
_CHECKPOINT_CACHE: WeakKeyDictionary[Workspace, _CheckpointCacheEntry] = (
    WeakKeyDictionary()
)

_LEGACY_SPAN_END_DRIFT_RATIO: Final = 0.005
_LEGACY_SPAN_END_DRIFT_FLOOR_SECONDS: Final = 0.05
_LEGACY_SPAN_END_DRIFT_CAP_SECONDS: Final = 3.0


def _legacy_span_end_is_rounding_drift(
    *, duration: float, end: float
) -> bool:
    tolerance = min(
        _LEGACY_SPAN_END_DRIFT_CAP_SECONDS,
        max(
            _LEGACY_SPAN_END_DRIFT_FLOOR_SECONDS,
            duration * _LEGACY_SPAN_END_DRIFT_RATIO,
        ),
    )
    return end - duration <= tolerance


@contextmanager
def _checkpoint_lock(ws: Workspace, *, exclusive: bool) -> Iterator[None]:
    with stable_workspace_lock(
        ws.root,
        ws.checkpoint_lock_path,
        exclusive=exclusive,
    ):
        yield


def _checkpoint_signature(ws: Workspace) -> _CheckpointSignature:
    try:
        stat = ws.checkpoints_path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


def _normalize_checkpoint_record(
    ws: Workspace,
    obj: CheckpointObject,
    *,
    writing: bool,
) -> CheckpointObject:
    normalized = (
        bind_record_to_workspace(ws, obj)
        if writing
        else dict(obj)
    )
    if not writing:
        validate_record_revision(ws, normalized)
    projected, canonical = canonicalize_span(
        ws,
        normalized.get("span"),
        normalized.get("temporal_span"),
    )
    normalized_span: list[CheckpointValue] = [projected[0], projected[1]]
    normalized["span"] = normalized_span
    if canonical is not None:
        canonical_value: dict[str, CheckpointValue] = {
            "stream_id": canonical["stream_id"],
            "start_pts": canonical["start_pts"],
            "end_pts_exclusive": canonical["end_pts_exclusive"],
            "time_base_num": canonical["time_base_num"],
            "time_base_den": canonical["time_base_den"],
            "timing_revision_id": canonical["timing_revision_id"],
        }
        normalized["temporal_span"] = canonical_value
    return normalized


def _apply_checkpoint_lines(
    ws: Workspace,
    lines: list[bytes],
    by_id: dict[str, _IndexedCheckpoint],
    *,
    first_lineno: int,
    history: list[CheckpointObject] | None = None,
) -> None:
    for offset, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
            if not isinstance(obj, dict):
                raise CheckpointValidationError(
                    "checkpoint line must contain a JSON object"
                )
            obj = _normalize_checkpoint_record(ws, obj, writing=False)
            try:
                checkpoint = validate_checkpoint(ws, obj)
            except CheckpointValidationError as error:
                if error.message != SPAN_END_EXCEEDS_DURATION:
                    raise
                span = obj.get("span")
                if not isinstance(span, (list, tuple)) or len(span) != 2:
                    raise
                start_value, end_value = span
                if (
                    isinstance(start_value, bool)
                    or not isinstance(start_value, (int, float))
                    or isinstance(end_value, bool)
                    or not isinstance(end_value, (int, float))
                ):
                    raise
                duration = float(ws.manifest["duration"])
                if not _legacy_span_end_is_rounding_drift(
                    duration=duration,
                    end=float(end_value),
                ):
                    raise
                repaired_span: list[CheckpointValue] = [
                    float(start_value),
                    duration,
                ]
                obj = {**obj, "span": repaired_span}
                checkpoint = validate_checkpoint(ws, obj)
            previous = by_id.get(checkpoint.checkpoint_id)
            validate_transition(previous[0] if previous else None, checkpoint)
            projected = _inherit_visual_evidence(
                previous[1] if previous else None, obj
            )
            by_id[checkpoint.checkpoint_id] = (checkpoint, projected)
            if history is not None:
                history.append(obj)
        except RevisionBindingError as error:
            print(
                "warning: skipping revision-incompatible checkpoints.jsonl "
                f"line {first_lineno + offset}: {error}",
                file=sys.stderr,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            CheckpointValidationError,
            TemporalSpanError,
        ):
            print(
                "warning: skipping corrupt checkpoints.jsonl line "
                f"{first_lineno + offset}",
                file=sys.stderr,
            )


def _inherit_visual_evidence(
    previous: CheckpointObject | None,
    current: CheckpointObject,
) -> CheckpointObject:
    """Omission keeps the prior relation; an explicit empty list clears it."""
    if (
        previous is not None
        and "visual_evidence" not in current
        and "visual_evidence" in previous
    ):
        return {**current, "visual_evidence": previous["visual_evidence"]}
    return current


def _refresh_checkpoint_cache(ws: Workspace) -> _CheckpointCacheEntry:
    signature = _checkpoint_signature(ws)
    cached = _CHECKPOINT_CACHE.get(ws)
    if cached is not None and cached[0] == signature:
        return cached
    if signature is None:
        refreshed: _CheckpointCacheEntry = (None, {}, 0, True)
        _CHECKPOINT_CACHE[ws] = refreshed
        return refreshed
    if (
        cached is not None
        and cached[0] is not None
        and cached[3]
        and cached[0][0] == signature[0]
        and cached[0][3] < signature[3]
    ):
        with ws.checkpoints_path.open("rb") as checkpoint_file:
            checkpoint_file.seek(cached[0][3])
            delta = checkpoint_file.read()
        delta_lines = delta.splitlines()
        by_id = cached[1].copy()
        _apply_checkpoint_lines(
            ws,
            delta_lines,
            by_id,
            first_lineno=cached[2] + 1,
        )
        refreshed = (
            signature,
            by_id,
            cached[2] + len(delta_lines),
            delta.endswith(b"\n"),
        )
        _CHECKPOINT_CACHE[ws] = refreshed
        return refreshed
    _load_checkpoints_unlocked(ws)
    return _CHECKPOINT_CACHE[ws]


def _latest_checkpoint(
    ws: Workspace, checkpoint_id: str
) -> ValidatedCheckpoint | None:
    indexed = _refresh_checkpoint_cache(ws)[1].get(checkpoint_id)
    return indexed[0] if indexed else None


def append_checkpoint(ws: Workspace, obj: CheckpointObject) -> CheckpointObject:
    validate_checkpoint_shape(obj)
    legacy_operation_lock = not ws.workspace_lock_path.is_file()
    with stable_workspace_lock(
        ws.root,
        ws.workspace_lock_path,
        exclusive=legacy_operation_lock,
        allow_reentrant_exclusive=legacy_operation_lock,
    ):
        normalized = _normalize_checkpoint_record(ws, obj, writing=True)
        checkpoint = validate_checkpoint(ws, normalized)
        with _checkpoint_lock(ws, exclusive=True):
            previous = (
                _latest_checkpoint(ws, checkpoint.checkpoint_id)
                if checkpoint.status == "hypothesized"
                else None
            )
            validate_transition(previous, checkpoint)
            signature = _checkpoint_signature(ws)
            cached = _CHECKPOINT_CACHE.get(ws)
            line_prefix = ""
            if signature is not None and signature[3] > 0:
                with ws.checkpoints_path.open("rb") as checkpoint_file:
                    checkpoint_file.seek(-1, 2)
                    if checkpoint_file.read(1) not in (b"\n", b"\r"):
                        line_prefix = "\n"

            with ws.checkpoints_path.open("a", encoding="utf-8") as checkpoint_file:
                checkpoint_file.write(
                    line_prefix + json.dumps(normalized, ensure_ascii=False) + "\n"
                )
            if cached is not None and cached[0] == signature:
                prior = cached[1].get(checkpoint.checkpoint_id)
                projected = _inherit_visual_evidence(
                    prior[1] if prior else None, normalized
                )
                cached[1][checkpoint.checkpoint_id] = (checkpoint, projected)
                _CHECKPOINT_CACHE[ws] = (
                    _checkpoint_signature(ws),
                    cached[1],
                    cached[2] + 1,
                    True,
                )
            else:
                _CHECKPOINT_CACHE.pop(ws, None)
    return normalized


def load_checkpoints(ws: Workspace) -> list[CheckpointObject]:
    """Return the latest valid record per id, ordered by span start."""
    with _checkpoint_lock(ws, exclusive=False):
        return _load_checkpoints_unlocked(ws)


def load_checkpoint_history(ws: Workspace) -> list[CheckpointObject]:
    """Return every valid append-only checkpoint state in ledger order."""
    with _checkpoint_lock(ws, exclusive=False):
        if not ws.checkpoints_path.is_file():
            return []
        contents = ws.checkpoints_path.read_bytes()
        history: list[CheckpointObject] = []
        _apply_checkpoint_lines(
            ws,
            contents.splitlines(),
            {},
            first_lineno=1,
            history=history,
        )
        return history


def load_checkpoint_history_with_entries(
    ws: Workspace,
) -> tuple[list[CheckpointObject], list[_IndexedCheckpoint]]:
    """한 번의 판독으로 전체 이력과 최신 상태를 함께 돌려준다.

    감사 경로가 이력(리플레이)과 스냅샷(상태)을 따로 읽으면 원장 1회 판독
    계약이 깨지고, 동시 기입 시 두 뷰가 서로 다른 원장을 본다.
    """
    with _checkpoint_lock(ws, exclusive=False):
        if not ws.checkpoints_path.is_file():
            _CHECKPOINT_CACHE[ws] = (None, {}, 0, True)
            return [], []
        contents = ws.checkpoints_path.read_bytes()
        lines = contents.splitlines()
        by_id: dict[str, _IndexedCheckpoint] = {}
        history: list[CheckpointObject] = []
        _apply_checkpoint_lines(
            ws, lines, by_id, first_lineno=1, history=history
        )
        ordered = sorted(by_id.values(), key=lambda item: item[0].span[0])
        _CHECKPOINT_CACHE[ws] = (
            _checkpoint_signature(ws),
            by_id,
            len(lines),
            not contents or contents.endswith(b"\n"),
        )
        return history, ordered


def _load_checkpoint_entries(ws: Workspace) -> list[_IndexedCheckpoint]:
    with _checkpoint_lock(ws, exclusive=False):
        return _load_checkpoint_entries_unlocked(ws)


def _load_checkpoints_unlocked(ws: Workspace) -> list[CheckpointObject]:
    return [obj for _, obj in _load_checkpoint_entries_unlocked(ws)]


def _load_checkpoint_entries_unlocked(ws: Workspace) -> list[_IndexedCheckpoint]:
    if not ws.checkpoints_path.is_file():
        _CHECKPOINT_CACHE[ws] = (None, {}, 0, True)
        return []
    contents = ws.checkpoints_path.read_bytes()
    lines = contents.splitlines()
    by_id: dict[str, _IndexedCheckpoint] = {}
    _apply_checkpoint_lines(ws, lines, by_id, first_lineno=1)
    ordered = sorted(by_id.values(), key=lambda item: item[0].span[0])
    _CHECKPOINT_CACHE[ws] = (
        _checkpoint_signature(ws),
        by_id,
        len(lines),
        not contents or contents.endswith(b"\n"),
    )
    return ordered
