"""legacy 워크스페이스를 리비전 결박으로 백필한다.

ADR-0005는 결박 없는 워크스페이스를 새 증거에 대해 읽기 전용으로 못박았다.
의도한 안전장치지만, 리비전 체계 이전에 쌓인 코퍼스 전체가 그 상태로 남아
이해 루프가 통째로 멈췄다(2026-07-28 실측: 40/40 미결박).

백필은 그 동결을 푸는 유일한 경로다. 동시에 되돌릴 수 없는 데이터 변경이므로
설계의 무게는 성공이 아니라 실패 쪽에 있다:

- **결박은 주장이지 증명이 아니다.** ingest 시점 봉인이 없으므로, 원본을 다시
  측정해 기록된 관측과 일치할 때만 진행하고 `revision_origin: "backfilled"`로
  표시한다. 사후에 붙인 결박을 ingest 봉인처럼 읽히게 두지 않는다.
- **기록은 한 줄도 잃지 않는다.** 결박되는 순간 리비전 필드 없는 줄은 전부
  거부되고 리더가 건너뛴다 — 나이브 승격이 체크포인트 8→0을 만든 기전이다.
  그래서 원장을 재결박하고, 정본 로더로 사후 검증해 하나라도 유실되면 전량
  복원한다.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .checkpoints import load_checkpoints
from .fsio import write_text_atomic
from .image_provenance import load_provenance_events
from .probe import probe
from .revision import current_revision_bindings, publish_workspace_revisions
from .revision_types import REVISION_FIELDS
from .sequence import load_sequences
from .workspace import Workspace
from .workspace_lock import stable_workspace_lock


class BackfillRefused(RuntimeError):
    """전제나 사후조건이 깨졌다 — 워크스페이스는 손대기 전 상태 그대로다."""


_BACKUP_DIRNAME: Final = ".tca-rebind-backup"
# URL ingest가 원본을 워크스페이스 안에 내려받는 자리(ingest_acquire.py).
_SELF_CONTAINED_SOURCE: Final = "source.mp4"
_LEDGER_NAMES: Final = (
    "checkpoints.jsonl",
    "sequences.jsonl",
    "image-provenance.jsonl",
)
# 재측정 일치 허용오차. 컨테이너 반올림은 통과시키고 다른 파일은 걸러낸다.
_PROBE_TOLERANCE: Final = {
    "duration": 0.05,
    "fps": 0.01,
    "width": 0.0,
    "height": 0.0,
}
# 반올림으로 미디어 끝을 넘긴 span만 클램프한다. 그 이상은 증거 수정이다.
_CLAMP_MAX_SECONDS: Final = 5.0
_CLAMP_MAX_RATIO: Final = 0.01


def _source_candidate(ws: Workspace) -> tuple[Path, bool]:
    """원본 파일을 찾는다 — 기록된 절대경로가 낡았을 수 있다.

    실측(2026-07-28): 프로젝트가 ~/video-agent → ~/timecode-agent로 옮겨지고
    코퍼스가 하위 폴더로 재편되면서, 원본을 워크스페이스 안에 그대로 갖고
    있는 13편이 "원본 없음"으로 보였다. `<workspace>/source.mp4`는 URL
    ingest가 직접 쓰는 자리이므로(ingest_acquire.py) 추측이 아니라 규약이다.

    되찾은 파일을 그냥 믿지는 않는다 — 재측정이 기록된 관측과 일치할 때만
    통과한다. 같은 자리의 다른 영상은 여기서 걸린다.
    """
    video = ws.manifest.get("video")
    if isinstance(video, str) and Path(video).is_file():
        return Path(video), False
    relocated = ws.root / _SELF_CONTAINED_SOURCE
    if relocated.is_file():
        return relocated, True
    raise BackfillRefused(
        f"원본 파일이 없다: {video!r} — 워크스페이스 안에도 "
        f"{_SELF_CONTAINED_SOURCE}가 없다. 재측정 없이 결박하면 증거를 "
        "사실이 아닌 것에 묶게 된다"
    )


def _fresh_probe(ws: Workspace) -> tuple[dict[str, Any], Path, bool]:
    """원본을 다시 측정한다 — 없거나 어긋나면 거기서 멈춘다."""
    video, relocated = _source_candidate(ws)
    try:
        measured = probe(video)
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 백필을 막아야 한다
        raise BackfillRefused(f"원본 재측정 실패: {error}") from error
    drifted = []
    for field, tolerance in _PROBE_TOLERANCE.items():
        recorded, now = ws.manifest.get(field), measured.get(field)
        if recorded is None or now is None:
            continue
        try:
            if abs(float(recorded) - float(now)) > tolerance:
                drifted.append(f"{field} {recorded}→{now}")
        except (TypeError, ValueError):
            drifted.append(f"{field} {recorded!r}→{now!r}")
    if drifted:
        where = f" ({video})" if relocated else ""
        raise BackfillRefused(
            f"원본 재측정이 기록된 관측과 어긋난다{where} "
            f"({', '.join(drifted)}) — 같은 파일이 아니다"
        )
    return measured, video, relocated


def _clamp_ceiling(duration: float) -> float:
    return max(_CLAMP_MAX_SECONDS, duration * _CLAMP_MAX_RATIO)


def _clamp_span(
    span: object, *, duration: float, where: str
) -> tuple[list[float] | None, bool]:
    """미디어 끝을 반올림으로 넘긴 span만 끝에 맞춘다."""
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None, False
    try:
        start, end = float(span[0]), float(span[1])
    except (TypeError, ValueError):
        return None, False
    if end <= duration:
        return [start, end], False
    overshoot = end - duration
    if overshoot > _clamp_ceiling(duration):
        raise BackfillRefused(
            f"{where}: span이 미디어 끝을 {overshoot:.3f}초 넘는다 "
            f"({end} > {duration}) — 반올림이 아니라 불일치다"
        )
    if start >= duration:
        raise BackfillRefused(
            f"{where}: span 시작이 미디어 끝을 넘는다 ({start} >= {duration})"
        )
    return [start, duration], True


def _rebind_records(
    records: list[dict | str], *, bindings: Mapping[str, str], duration: float,
    ledger: str,
) -> tuple[list[dict | str], int]:
    """모든 줄에 새 결박을 찍고, 넘친 span을 끝에 맞춘다.

    파싱되지 않는 줄은 그대로 통과시킨다 — 손댈 수 없는 줄이지 버릴 줄이
    아니다. 줄 수는 들어온 만큼 그대로 나간다.
    """
    rebound: list[dict | str] = []
    clamped = 0
    for index, record in enumerate(records, 1):
        if isinstance(record, str):
            rebound.append(record)
            continue
        updated = dict(record)
        # 낡은 시간축 캐노니컬은 새 timing revision과 맞지 않는다.
        updated.pop("temporal_span", None)
        for field, value in bindings.items():
            updated[field] = value
        where = f"{ledger}:{index} id={record.get('id')!r}"
        span, was_clamped = _clamp_span(
            record.get("span"), duration=duration, where=where)
        if span is not None:
            updated["span"] = span
            clamped += int(was_clamped)
        cuts = record.get("cuts")
        if isinstance(cuts, list):
            new_cuts = []
            for cut_index, cut in enumerate(cuts, 1):
                if not isinstance(cut, dict):
                    new_cuts.append(cut)
                    continue
                new_cut = dict(cut)
                new_cut.pop("temporal_span", None)
                cut_span, cut_clamped = _clamp_span(
                    cut.get("span"), duration=duration,
                    where=f"{where} cut {cut_index}")
                if cut_span is not None:
                    new_cut["span"] = cut_span
                    clamped += int(cut_clamped)
                new_cuts.append(new_cut)
            updated["cuts"] = new_cuts
        rebound.append(updated)
    return rebound, clamped


def _read_ledger(path: Path) -> list[dict | str]:
    """모든 줄을 읽는다 — 파싱되지 않는 줄은 원문 그대로 들고 간다.

    건너뛰면 재기록 때 사라진다. 정본 로더도 그 줄을 건너뛰므로 id 기반
    사후조건은 그 삭제를 볼 수 없고, 성공하면 백업까지 지워진다 — 즉
    조용한 영구 손실이다. 백필이 고칠 줄이 아니라고 해서 버릴 줄도 아니다.
    """
    if not path.is_file():
        return []
    records: list[dict | str] = []
    for raw in path.read_text("utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            records.append(stripped)
            continue
        records.append(record if isinstance(record, dict) else stripped)
    return records


def _loaded_ids(ws: Workspace) -> dict[str, list[str]]:
    """정본 로더가 지금 실제로 보는 것 — 사후조건의 유일한 기준."""
    events = load_provenance_events(ws)
    return {
        "checkpoints": sorted(
            str(item.get("id")) for item in load_checkpoints(ws)),
        "sequences": sorted(
            str(item.get("id")) for item in load_sequences(ws)),
        "image-provenance": sorted(
            str(item.get("edge_id")) for item in events),
    }


def _backup(ws: Workspace) -> Path:
    backup = ws.root / _BACKUP_DIRNAME
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    shutil.copy2(ws.manifest_path, backup / ws.manifest_path.name)
    for name in _LEDGER_NAMES:
        path = ws.root / name
        if path.is_file():
            shutil.copy2(path, backup / name)
    return backup


def _restore(ws: Workspace, backup: Path) -> None:
    shutil.copy2(backup / ws.manifest_path.name, ws.manifest_path)
    for name in _LEDGER_NAMES:
        saved = backup / name
        target = ws.root / name
        if saved.is_file():
            shutil.copy2(saved, target)
        elif target.is_file():
            target.unlink()


def _apply_backfill(
    ws: Workspace, *, measured: dict[str, Any], source: Path,
    relocated: bool, duration: float,
) -> dict[str, Any]:
    """배타 리스를 쥔 채로 백필 전 과정을 수행한다."""
    before = _loaded_ids(ws)
    ledgers = {
        name: _read_ledger(ws.root / name)
        for name in _LEDGER_NAMES
        if (ws.root / name).is_file()
    }
    lines_before = {name: len(records) for name, records in ledgers.items()}

    backup = _backup(ws)
    try:
        if relocated:
            # 발행은 `ws.video`를 해시한다 — manifest_updates보다 먼저 읽으므로
            # 되찾은 경로를 여기서 기입해야 낡은 경로를 해시하지 않는다.
            # 이후 명령들도 이 값을 쓰므로 워크스페이스 전체가 다시 열린다.
            manifest = ws.manifest
            manifest["video"] = str(source)
            ws.save_manifest(manifest)
            ws = Workspace(ws.root)
        bindings = publish_workspace_revisions(
            ws,
            manifest_updates={
                "timing": measured["timing"],
                "source_stat": measured["source_stat"],
                "revision_origin": "backfilled",
            },
            # 재측정한 그 파일이 결박되는 파일이어야 한다. probe와 해시 사이에
            # 원본이 교체되면 낡은 timing을 새 바이트에 묶게 된다.
            expected_source_stat=measured["source_stat"],
            allow_legacy_backfill=True,
        )
        stamped: dict[str, str] = {
            "source_revision_id": bindings["source_revision_id"],
            "transcript_revision_id": bindings["transcript_revision_id"],
            "timing_revision_id": bindings["timing_revision_id"],
        }
        clamped_total = 0
        for name, records in ledgers.items():
            rebound, clamped = _rebind_records(
                records,
                bindings=stamped,
                duration=duration,
                ledger=name,
            )
            clamped_total += clamped
            write_text_atomic(
                ws.root / name,
                "".join(
                    (record if isinstance(record, str)
                     else json.dumps(record, ensure_ascii=False)) + "\n"
                    for record in rebound
                ),
            )
        reloaded = Workspace(ws.root)
        after = _loaded_ids(reloaded)
        lost = {
            key: sorted(set(before[key]) - set(after[key]))
            for key in before
            if set(before[key]) - set(after[key])
        }
        if lost:
            detail = " · ".join(
                f"{key} {len(ids)}건 {ids[:5]}" for key, ids in lost.items())
            raise BackfillRefused(f"백필 후 기록 유실: {detail}")
        # id는 정본 로더가 보는 것만 센다. 로더가 건너뛰는 줄(깨진 줄)이
        # 사라져도 위 검사는 조용하므로 줄 수를 따로 못 박는다.
        shrunk = [
            f"{name} {lines_before[name]}→{len(_read_ledger(ws.root / name))}"
            for name in ledgers
            if len(_read_ledger(ws.root / name)) < lines_before[name]
        ]
        if shrunk:
            raise BackfillRefused(f"백필 후 원장 줄 유실: {' · '.join(shrunk)}")
    except BaseException:
        _restore(ws, backup)
        shutil.rmtree(backup, ignore_errors=True)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    return {
        "workspace": ws.root.name,
        "eligible": True,
        "applied": True,
        "records": {key: len(value) for key, value in after.items()},
        "clamped_spans": clamped_total,
        "relocated_source": relocated,
    }


def backfill_workspace(ws: Workspace, *, apply: bool = False) -> dict[str, Any]:
    """legacy 워크스페이스 하나를 결박한다. `apply=False`면 아무것도 쓰지 않는다."""
    if current_revision_bindings(ws) is not None:
        raise BackfillRefused("이미 결박된 워크스페이스다 — 백필할 것이 없다")
    if ws.manifest.get("revision_draft") is True:
        raise BackfillRefused(
            "초안 워크스페이스는 첫 기입 때 스스로 결박한다 — 백필 대상이 아니다"
        )

    measured, source, relocated = _fresh_probe(ws)
    duration = float(ws.manifest["duration"])
    if apply:
        # 발행과 원장 재기록은 하나의 사건이어야 한다. 발행 안쪽의 배타 락은
        # 발행이 끝나면 풀리므로, 그 사이에 들어온 append는 미리 읽어둔 원장
        # 스냅샷에 덮여 사라진다. 읽기·백업·발행·재기록·검증·정리를 전부
        # 한 리스 안에서 한다.
        with stable_workspace_lock(
            ws.root,
            ws.workspace_lock_path,
            exclusive=True,
            allow_reentrant_exclusive=True,
        ):
            return _apply_backfill(
                ws,
                measured=measured,
                source=source,
                relocated=relocated,
                duration=duration,
            )

    ledgers = {
        name: _read_ledger(ws.root / name)
        for name in _LEDGER_NAMES
        if (ws.root / name).is_file()
    }
    # dry-run도 클램프 한도를 넘는 span은 지금 알려야 한다.
    clamped = 0
    for name, records in ledgers.items():
        _, count = _rebind_records(
            records,
            bindings=dict.fromkeys(REVISION_FIELDS, ""),
            duration=duration,
            ledger=name,
        )
        clamped += count
    return {
        "workspace": ws.root.name,
        "eligible": True,
        "applied": False,
        "records": {
            key: len(value) for key, value in _loaded_ids(ws).items()},
        "clamped_spans": clamped,
        "relocated_source": relocated,
    }
