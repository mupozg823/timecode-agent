"""POSIX-safe append-only storage for edit-decision sequences."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum, unique
from pathlib import Path
from typing import TypedDict

from .checkpoint_schema import CheckpointObject
from .revision import (
    RevisionBindingError,
    bind_record_to_workspace,
    validate_record_revision,
)
from .sequence_grounding import (
    GroundingSnapshot,
    checkpoint_grounding_snapshot,
    pin_terminal_revisions,
    validate_terminal_grounding,
)
from .sequence_schema import (
    SEQUENCE_SCHEMA_VERSION,
    SequenceValidationError,
    validate_sequence_object,
)
from .temporal import TemporalSpanError, canonicalize_span
from .workspace import Workspace
from .workspace_lock import stable_workspace_lock


@unique
class SequenceSourceState(StrEnum):
    ABSENT = "absent"
    READY = "ready"
    PRESENT_WITHOUT_VALID_RECORDS = "present_without_valid_records"
    UNREADABLE = "unreadable"


class SequenceReadSnapshot(TypedDict):
    sequences: list[dict]
    checkpoints_by_id: dict[str, CheckpointObject]
    sequence_source_state: SequenceSourceState


def sequences_path(ws: Workspace) -> Path:
    return ws.root / "sequences.jsonl"


@contextmanager
def _sequence_lock(ws: Workspace, *, exclusive: bool) -> Iterator[None]:
    with stable_workspace_lock(
        ws.root,
        ws.sequence_lock_path,
        exclusive=exclusive,
    ):
        yield


def _validate_sequence(
    ws: Workspace,
    obj: object,
    *,
    prior: dict | None,
    snapshot: GroundingSnapshot,
    legacy_compat: bool = False,
    replay: bool = False,
) -> dict:
    checkpoints_by_id, unsupported_ids = snapshot
    obj = _normalize_sequence_record(ws, obj, writing=not replay)
    sequence = validate_sequence_object(
        ws,
        obj,
        prior=prior,
        known_checkpoints=set(checkpoints_by_id),
        legacy_compat=legacy_compat,
    )
    # 근거 게이트·리비전 고정은 쓰기 시점의 것이다. replay에서 리비전이
    # 고정된 라인은 쓰기 게이트를 이미 통과한 역사적 사실이라 현재
    # 체크포인트 상태로 재판정하지 않는다 — 재판정하면 드리프트가 심할수록
    # 라인이 corrupt 취급으로 소실되어 드리프트 경고 대상 자체가 사라진다.
    # 무핀 레거시 라인만 종전대로 현재 근거를 재검증한다.
    if not replay:
        validate_terminal_grounding(
            sequence, checkpoints_by_id, unsupported_ids
        )
        pin_terminal_revisions(sequence, checkpoints_by_id, prior=prior)
    elif not isinstance(sequence.get("checkpoint_revisions"), dict):
        validate_terminal_grounding(
            sequence, checkpoints_by_id, unsupported_ids
        )
    return sequence


def _normalize_sequence_record(
    ws: Workspace,
    obj: object,
    *,
    writing: bool,
) -> object:
    if not isinstance(obj, dict):
        return obj
    normalized = (
        bind_record_to_workspace(ws, obj)
        if writing
        else dict(obj)
    )
    if not writing:
        validate_record_revision(ws, normalized)
    cuts = normalized.get("cuts")
    if isinstance(cuts, list):
        normalized["cuts"] = [
            _normalize_sequence_span(ws, cut)
            if isinstance(cut, dict)
            else cut
            for cut in cuts
        ]
    alternatives = normalized.get("alternatives_rejected")
    if isinstance(alternatives, list):
        normalized["alternatives_rejected"] = [
            _normalize_sequence_span(ws, alternative)
            if isinstance(alternative, dict)
            else alternative
            for alternative in alternatives
        ]
    return normalized


def _normalize_sequence_span(ws: Workspace, value: dict) -> dict:
    if "span" not in value:
        return dict(value)
    normalized = dict(value)
    try:
        projected, canonical = canonicalize_span(
            ws,
            normalized.get("span"),
            normalized.get("temporal_span"),
        )
    except TemporalSpanError as error:
        raise SequenceValidationError(str(error)) from None
    normalized["span"] = projected
    if canonical is not None:
        normalized["temporal_span"] = canonical
    return normalized


def _prior_for(obj: object, latest: list[dict]) -> dict | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
        return None
    return {sequence["id"]: sequence for sequence in latest}.get(obj["id"])


def validate_sequence(ws: Workspace, obj: object) -> dict:
    """Validate without writing against one locked cross-ledger snapshot."""
    with _sequence_lock(ws, exclusive=False):
        with checkpoint_grounding_snapshot(ws) as snapshot:
            latest = _load_sequences_unlocked(ws, snapshot)
            return _validate_sequence(
                ws,
                obj,
                prior=_prior_for(obj, latest),
                snapshot=snapshot,
            )


def append_sequence(ws: Workspace, obj: object) -> dict:
    """Validate and append while sequence and checkpoint snapshots are locked."""
    legacy_operation_lock = not ws.workspace_lock_path.is_file()
    with stable_workspace_lock(
        ws.root,
        ws.workspace_lock_path,
        exclusive=legacy_operation_lock,
        allow_reentrant_exclusive=legacy_operation_lock,
    ):
        with _sequence_lock(ws, exclusive=True):
            with checkpoint_grounding_snapshot(ws) as snapshot:
                latest = _load_sequences_unlocked(ws, snapshot)
                sequence = _validate_sequence(
                    ws,
                    obj,
                    prior=_prior_for(obj, latest),
                    snapshot=snapshot,
                )
                path = sequences_path(ws)
                prefix = "\n" if _missing_trailing_newline(path) else ""
                with path.open("a", encoding="utf-8") as ledger:
                    ledger.write(
                        prefix + json.dumps(sequence, ensure_ascii=False) + "\n"
                    )
                return sequence


def _missing_trailing_newline(path: Path) -> bool:
    """직전 기록이 개행 없이 끝났으면 새 레코드 앞에 개행이 필요하다."""
    if not (path.is_file() and path.stat().st_size):
        return False
    with path.open("rb") as ledger:
        ledger.seek(-1, 2)
        return ledger.read(1) not in (b"\n", b"\r")


def _load_sequences_unlocked(
    ws: Workspace, snapshot: GroundingSnapshot
) -> list[dict]:
    path = sequences_path(ws)
    if not path.is_file():
        return []
    latest: dict[str, dict] = {}
    for line_number, line in enumerate(path.read_bytes().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
            if not isinstance(obj, dict):
                raise SequenceValidationError(
                    "sequence line must contain a JSON object"
                )
            raw_id = obj.get("id")
            prior = latest.get(raw_id) if isinstance(raw_id, str) else None
            legacy_compat = (
                obj.get("schema_version") is None
                and not (
                    prior
                    and prior.get("schema_version")
                    == SEQUENCE_SCHEMA_VERSION
                )
            )
            sequence = _validate_sequence(
                ws,
                obj,
                prior=prior,
                snapshot=snapshot,
                legacy_compat=legacy_compat,
                replay=True,
            )
            latest[sequence["id"]] = sequence
        except RevisionBindingError as error:
            print(
                "warning: skipping revision-incompatible sequences.jsonl line "
                f"{line_number}: {error}",
                file=sys.stderr,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            SequenceValidationError,
            TemporalSpanError,
        ):
            print(
                f"warning: skipping corrupt sequences.jsonl line {line_number}",
                file=sys.stderr,
            )
    return list(latest.values())


def load_sequence_read_snapshot(ws: Workspace) -> SequenceReadSnapshot:
    """Read sequence decisions and their checkpoint grounding atomically."""
    with _sequence_lock(ws, exclusive=False):
        with checkpoint_grounding_snapshot(ws) as grounding:
            checkpoints_by_id, _unsupported_ids = grounding
            path = sequences_path(ws)
            try:
                source_stat = path.stat()
            except FileNotFoundError:
                sequences: list[dict] = []
                source_state = SequenceSourceState.ABSENT
            except OSError:
                sequences = []
                source_state = SequenceSourceState.UNREADABLE
            else:
                if not stat.S_ISREG(source_stat.st_mode):
                    sequences = []
                    source_state = SequenceSourceState.UNREADABLE
                else:
                    try:
                        sequences = _load_sequences_unlocked(ws, grounding)
                    except OSError:
                        sequences = []
                        source_state = SequenceSourceState.UNREADABLE
                    else:
                        source_state = (
                            SequenceSourceState.READY
                            if sequences
                            else SequenceSourceState.PRESENT_WITHOUT_VALID_RECORDS
                        )
            return {
                "sequences": sequences,
                "checkpoints_by_id": dict(checkpoints_by_id),
                "sequence_source_state": source_state,
            }


def load_sequences(ws: Workspace) -> list[dict]:
    """Return latest valid revisions under sequence and checkpoint locks."""
    with _sequence_lock(ws, exclusive=False):
        with checkpoint_grounding_snapshot(ws) as snapshot:
            return _load_sequences_unlocked(ws, snapshot)
