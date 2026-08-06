"""Content-derived workspace revisions and append-only ledger binding."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar
from weakref import WeakKeyDictionary

from .fsio import write_text_atomic
from .probe import source_stat_fingerprint
from .revision_types import (
    REVISION_FIELDS,
    RevisionBindings,
    RevisionRecord,
    RevisionValue,
)
from .revision_types import (
    RevisionBindingError as RevisionBindingError,
)
from .workspace_lifecycle import (
    _material_names,
)
from .workspace_lifecycle import (
    _write_building_manifest as _write_building_manifest,
)
from .workspace_lifecycle import (
    assert_ingest_target_unused as assert_ingest_target_unused,
)
from .workspace_lifecycle import (
    begin_ingest as begin_ingest,
)
from .workspace_lock import stable_workspace_lock

if TYPE_CHECKING:
    from .workspace import Workspace

type _FileSignature = tuple[int, int, int, int, int] | tuple[str] | None

# 판독 불가(stat 실패가 부재가 아닌 경우)의 서명 — 부재(None)와 구분해야
# 부재 수용이 남긴 receipt가 판독 불가 소스까지 무검증 통과시키지 않는다.
_UNREADABLE_SIGNATURE: Final[tuple[str]] = ("unreadable",)
type RevisionInputSignature = tuple[
    _FileSignature,
    _FileSignature,
    _FileSignature,
]
_RecordValue = TypeVar("_RecordValue")
_TRANSCRIPT_DEPENDENT_EVIDENCE: Final = (
    "checkpoints.jsonl",
    "sequences.jsonl",
    "image-provenance.jsonl",
    "corrections.jsonl",
    "captures.json",
)
_TRANSCRIPT_DEPENDENT_DIRECTORIES: Final = ("wiki", "conflicts")
_REVISION_CACHE: WeakKeyDictionary[
    Workspace, tuple[RevisionInputSignature, RevisionBindings]
] = WeakKeyDictionary()


def _copy_bindings(bindings: RevisionBindings) -> RevisionBindings:
    return {
        "source_revision_id": bindings["source_revision_id"],
        "transcript_revision_id": bindings["transcript_revision_id"],
        "timing_revision_id": bindings["timing_revision_id"],
    }


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_hash(value: RevisionValue) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _stable_file_hash(path: Path) -> tuple[str, dict[str, int]]:
    try:
        before = source_stat_fingerprint(path.stat())
        digest = _file_hash(path)
        after = source_stat_fingerprint(path.stat())
    except OSError as error:
        raise RevisionBindingError(
            f"revision source is unavailable: {path}"
        ) from error
    if before != after:
        raise RevisionBindingError(
            f"revision source changed while hashing: {path}"
        )
    return digest, after


def _json_file_hash(path: Path) -> str:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RevisionBindingError(
            f"revision source is unavailable: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise RevisionBindingError(
            f"revision source is invalid JSON: {path}"
        ) from error
    return _canonical_hash(parsed)


def _timing_revision(manifest: Mapping[str, RevisionValue]) -> str:
    return _canonical_hash(
        {
            "duration": manifest.get("duration"),
            "width": manifest.get("width"),
            "height": manifest.get("height"),
            "fps": manifest.get("fps"),
            "timing": manifest.get("timing"),
        }
    )


def _source_revision(
    source_content_hash: str,
    transcript_revision_id: str,
    timing_revision_id: str,
) -> str:
    return _canonical_hash(
        {
            "source_content_hash": source_content_hash,
            "transcript_revision_id": transcript_revision_id,
            "timing_revision_id": timing_revision_id,
        }
    )


_BINDING_RECEIPT_FILENAME: Final = ".tca-revision-receipt.json"


def _normalized_signature(value: object) -> object:
    """JSON은 튜플을 배열로 만든다 — 비교 전에 같은 모양으로 되돌린다."""
    if isinstance(value, list):
        return tuple(_normalized_signature(item) for item in value)
    return value


def _read_binding_receipt(
    ws: Workspace, signature: RevisionInputSignature
) -> RevisionBindings | None:
    """직전에 증명된 결박을 되읽는다 — 입력 서명이 그대로일 때만.

    결박 확인은 원본 전체를 해시한다. 프로세스 안에서는 메모리 캐시가
    한 번으로 줄이지만, CLI는 명령마다 새 프로세스다. 실측(2026-07-28):
    결박된 워크스페이스 27개 코퍼스에서 `va audit` 4.47초 중 4.14초가
    이 해시였다 — 코퍼스가 전부 legacy일 때는 조기 반환돼 보이지 않던
    비용이다. `.tca-decoded-cfr.json`과 같은 방식으로 증명을 남긴다.
    """
    try:
        raw = json.loads(
            (ws.root / _BINDING_RECEIPT_FILENAME).read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if _normalized_signature(raw.get("signature")) != signature:
        return None
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict):
        return None
    if not all(isinstance(bindings.get(field), str) for field in REVISION_FIELDS):
        return None
    return {
        "source_revision_id": bindings["source_revision_id"],
        "transcript_revision_id": bindings["transcript_revision_id"],
        "timing_revision_id": bindings["timing_revision_id"],
    }


def _store_binding_receipt(
    ws: Workspace, signature: RevisionInputSignature, bindings: RevisionBindings
) -> None:
    """증명을 남긴다 — 실패해도 조용히 넘어간다. 계산은 캐시가 아니라 원장이다."""
    try:
        write_text_atomic(
            ws.root / _BINDING_RECEIPT_FILENAME,
            json.dumps(
                {"signature": signature, "bindings": bindings},
                ensure_ascii=False,
            ),
        )
    except OSError:
        pass


def _file_signature(path: Path) -> _FileSignature:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE_SIGNATURE
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
    )


def revision_input_signature(ws: Workspace) -> RevisionInputSignature:
    """Fingerprint files that determine the active workspace revision."""
    return (
        _file_signature(ws.manifest_path),
        _file_signature(ws.transcript_path),
        _file_signature(ws.video),
    )


def seal_ingest_source(ws: Workspace) -> str:
    """Bind one building ingest to the exact bytes observed before ASR."""
    manifest: RevisionRecord = ws.manifest
    if manifest.get("ingest_state") != "building":
        raise RevisionBindingError("ingest source can only be sealed while building")
    source_content_hash, source_stat = _stable_file_hash(ws.video)
    if manifest.get("source_stat") != source_stat:
        raise RevisionBindingError(
            "ingest source changed after probe; retry in the same incomplete "
            "workspace"
        )
    manifest["ingest_source_content_hash"] = source_content_hash
    ws.save_manifest(manifest)
    return source_content_hash


def assert_transcript_revision_update_allowed(
    ws: Workspace,
) -> RevisionBindings | None:
    """Keep transcript enrichment from silently invalidating durable evidence."""
    bindings = current_revision_bindings(ws)
    blocking = _material_names(
        ws.root,
        _TRANSCRIPT_DEPENDENT_EVIDENCE,
        _TRANSCRIPT_DEPENDENT_DIRECTORIES,
    )
    if blocking:
        raise RevisionBindingError(
            "run diarize before recording transcript-dependent evidence "
            f"({', '.join(blocking)}); use a fresh revision workspace to "
            "change an evidenced transcript"
        )
    return bindings


def publish_workspace_revisions(
    ws: Workspace,
    *,
    manifest_updates: Mapping[str, RevisionValue] | None = None,
    expected_source_stat: Mapping[str, int] | None = None,
    allow_legacy_backfill: bool = False,
) -> RevisionBindings:
    """Publish one revision while excluding other workspace-wide transitions.

    `allow_legacy_backfill` is the single door through which a markerless
    legacy workspace may be bound after the fact. It stays closed by default:
    binding without an ingest-time seal is an assertion, not a proof, and only
    `revision_backfill` — which re-measures the source and rolls back on any
    record loss — is entitled to make it.
    """
    with stable_workspace_lock(
        ws.root,
        ws.workspace_lock_path,
        exclusive=True,
        allow_reentrant_exclusive=True,
    ):
        with stable_workspace_lock(
            ws.root,
            ws.manifest_lock_path,
            exclusive=True,
            allow_reentrant_exclusive=True,
        ):
            return _publish_workspace_revisions_locked(
                ws,
                manifest_updates=manifest_updates,
                expected_source_stat=expected_source_stat,
                allow_legacy_backfill=allow_legacy_backfill,
            )


def _publish_workspace_revisions_locked(
    ws: Workspace,
    *,
    manifest_updates: Mapping[str, RevisionValue] | None = None,
    expected_source_stat: Mapping[str, int] | None = None,
    allow_legacy_backfill: bool = False,
) -> RevisionBindings:
    """Hash and publish while the operation and manifest locks are held."""
    manifest: RevisionRecord = ws.manifest
    present = [field for field in REVISION_FIELDS if field in manifest]
    has_source_hash = "source_content_hash" in manifest
    fully_bound = (
        len(present) == len(REVISION_FIELDS)
        and all(isinstance(manifest.get(field), str) for field in REVISION_FIELDS)
        and isinstance(manifest.get("source_content_hash"), str)
    )
    if present and not fully_bound:
        raise RevisionBindingError("workspace revision binding is incomplete")
    if has_source_hash and not fully_bound:
        raise RevisionBindingError("workspace source content hash is incomplete")
    if not (
        manifest.get("ingest_state") == "building"
        or manifest.get("revision_draft") is True
        or fully_bound
        or allow_legacy_backfill
    ):
        raise RevisionBindingError(
            "legacy workspace is read-only for revision publication; ingest "
            "into a fresh revision-bound workspace"
        )
    source_content_hash, source_stat = _stable_file_hash(ws.video)
    ingest_state = manifest.get("ingest_state")
    sealed_hash = manifest.get("ingest_source_content_hash")
    prior_hash = manifest.get("source_content_hash")
    if expected_source_stat is not None and source_stat != dict(
        expected_source_stat
    ):
        raise RevisionBindingError(
            "workspace source changed during revision publication"
        )
    if ingest_state == "building":
        if (
            sealed_hash != source_content_hash
            or manifest.get("source_stat") != source_stat
        ):
            raise RevisionBindingError(
                "ingest source changed after its pre-transcription seal"
            )
    elif isinstance(prior_hash, str):
        if prior_hash != source_content_hash:
            raise RevisionBindingError(
                "workspace source content revision mismatch"
            )
    elif allow_legacy_backfill:
        # 백필은 봉인된 stat이 없다 — 그래서 호출자가 직접 재측정하고,
        # 재측정이 기록된 관측과 어긋나면 여기 오기 전에 멈춘다.
        pass
    elif manifest.get("source_stat") != source_stat:
        raise RevisionBindingError(
            "workspace source changed after probe"
        )
    if manifest_updates is not None:
        manifest.update(manifest_updates)
    transcript_revision_id = _json_file_hash(ws.transcript_path)
    timing_revision_id = _timing_revision(manifest)
    bindings: RevisionBindings = {
        "source_revision_id": _source_revision(
            source_content_hash,
            transcript_revision_id,
            timing_revision_id,
        ),
        "transcript_revision_id": transcript_revision_id,
        "timing_revision_id": timing_revision_id,
    }
    manifest["source_revision_id"] = bindings["source_revision_id"]
    manifest["transcript_revision_id"] = bindings["transcript_revision_id"]
    manifest["timing_revision_id"] = bindings["timing_revision_id"]
    manifest["source_content_hash"] = source_content_hash
    manifest["revision_schema_version"] = 1
    manifest.pop("ingest_source_content_hash", None)
    manifest.pop("revision_draft", None)
    if ingest_state == "building":
        manifest["ingest_state"] = "ready"
    ws.save_manifest(manifest)
    _REVISION_CACHE.pop(ws, None)
    return bindings


def current_revision_bindings(ws: Workspace) -> RevisionBindings | None:
    """Return verified active bindings, or None for a legacy workspace."""
    manifest: RevisionRecord = ws.manifest
    present = [field for field in REVISION_FIELDS if field in manifest]
    if not present:
        return None
    if len(present) != len(REVISION_FIELDS):
        raise RevisionBindingError("workspace revision binding is incomplete")

    signature = revision_input_signature(ws)
    cached = _REVISION_CACHE.get(ws)
    if cached is not None and cached[0] == signature:
        return _copy_bindings(cached[1])

    receipt = _read_binding_receipt(ws, signature)
    if receipt is not None and all(
        manifest.get(field) == value for field, value in receipt.items()
    ):
        _REVISION_CACHE[ws] = (signature, receipt)
        return _copy_bindings(receipt)

    source_content_hash = manifest.get("source_content_hash")
    if not (
        isinstance(source_content_hash, str)
        and source_content_hash.startswith("sha256:")
    ):
        raise RevisionBindingError("workspace source content hash is missing")
    try:
        actual_source_hash, _source_stat = _stable_file_hash(ws.video)
    except RevisionBindingError as error:
        # 소스 부재는 드리프트가 아니다 — ingest가 남긴 seal이 권위다.
        # gc 미디어 퍼지나 외부 이동 뒤에도 원장은 읽히고 텍스트 분석은
        # 이어져야 한다(legacy의 "재측정 불가 시 거부"는 seal이 없어서다,
        # ADR-0006). 파일이 다시 나타나면 재해싱 검증이 그대로 재개된다.
        # 부재 판별은 예외 원인으로만 한다 — Path.exists()는 EACCES를
        # 삼켜 판독 불가를 부재로 오인한다.
        if not isinstance(error.__cause__, FileNotFoundError):
            raise
        actual_source_hash = None
    if (
        actual_source_hash is not None
        and actual_source_hash != source_content_hash
    ):
        raise RevisionBindingError("workspace source content revision mismatch")

    transcript_revision_id = _json_file_hash(ws.transcript_path)
    timing_revision_id = _timing_revision(manifest)
    source_revision_id = _source_revision(
        source_content_hash,
        transcript_revision_id,
        timing_revision_id,
    )
    expected: RevisionBindings = {
        "source_revision_id": source_revision_id,
        "transcript_revision_id": transcript_revision_id,
        "timing_revision_id": timing_revision_id,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise RevisionBindingError(f"workspace {field} mismatch")
    _REVISION_CACHE[ws] = (signature, expected)
    _store_binding_receipt(ws, signature, expected)
    return _copy_bindings(expected)


def bind_record_to_workspace(
    ws: Workspace, record: Mapping[str, _RecordValue]
) -> dict[str, _RecordValue | str]:
    """Stamp a new record, rejecting caller-supplied revision drift."""
    bound: dict[str, _RecordValue | str] = {
        key: value for key, value in record.items()
    }
    with stable_workspace_lock(
        ws.root,
        ws.workspace_lock_path,
        exclusive=False,
    ):
        bindings = current_revision_bindings(ws)
        if bindings is None:
            with stable_workspace_lock(
                ws.root,
                ws.manifest_lock_path,
                exclusive=True,
            ):
                bindings = current_revision_bindings(ws)
                if bindings is None:
                    if ws.manifest.get("revision_draft") is not True:
                        raise RevisionBindingError(
                            "legacy workspace is read-only for new evidence; "
                            "ingest into a fresh revision-bound workspace before "
                            "appending records"
                        )
                    if not ws.transcript_path.is_file():
                        write_text_atomic(ws.transcript_path, "[]")
                    bindings = _publish_workspace_revisions_locked(ws)
    for field, value in (
        ("source_revision_id", bindings["source_revision_id"]),
        ("transcript_revision_id", bindings["transcript_revision_id"]),
        ("timing_revision_id", bindings["timing_revision_id"]),
    ):
        supplied = bound.get(field)
        if supplied is not None and supplied != value:
            raise RevisionBindingError(f"record {field} mismatch")
        bound[field] = value
    return bound


def validate_record_revision(
    ws: Workspace, record: Mapping[str, _RecordValue]
) -> None:
    """Reject stale or unbound records in a revision-aware workspace."""
    bindings = current_revision_bindings(ws)
    if bindings is None:
        return
    missing = [field for field in REVISION_FIELDS if field not in record]
    if missing:
        raise RevisionBindingError(
            f"record revision binding is missing: {', '.join(missing)}"
        )
    for field, value in bindings.items():
        if record.get(field) != value:
            raise RevisionBindingError(f"record {field} mismatch")


def receipt_revision_fields(ws: Workspace) -> dict[str, str]:
    bindings = current_revision_bindings(ws)
    if bindings is None:
        return {}
    source_content_hash = ws.manifest.get("source_content_hash")
    if not isinstance(source_content_hash, str):
        raise RevisionBindingError("workspace source content hash is missing")
    return {
        "source_revision_id": bindings["source_revision_id"],
        "transcript_revision_id": bindings["transcript_revision_id"],
        "timing_revision_id": bindings["timing_revision_id"],
        "source_content_hash": source_content_hash,
    }
