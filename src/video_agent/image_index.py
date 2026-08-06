"""Obsidian-safe projection of image artifacts and their causal edges."""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Final, TypedDict
from urllib.parse import quote

from .checkpoint_selection import selected_checkpoints
from .checkpoints import load_checkpoints
from .corpus_projection import (
    escape_markdown_text,
    humanize_surface,
    md_target,
)
from .image_provenance import image_identity, load_image_records, resolve_image_path
from .image_types import ImageRecord
from .timestamps import fmt_ts
from .workspace import Workspace


def checkpoint_image_backlinks(ws: Workspace) -> dict[str, list[str]]:
    """Workspace-relative image path -> latest checkpoint IDs."""
    backlinks: dict[str, list[str]] = defaultdict(list)
    for checkpoint in load_checkpoints(ws):
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str):
            continue
        raw = checkpoint.get("visual_evidence") or []
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list):
            values = raw
        else:
            continue
        current: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                image_path, _ = resolve_image_path(ws, value)
            except ValueError:
                continue
            if image_path not in current:
                current.append(image_path)
        for image_path in current:
            for key in (image_path, image_identity(ws, image_path)):
                if checkpoint_id not in backlinks[key]:
                    backlinks[key].append(checkpoint_id)
    return dict(backlinks)


def image_record_map(
    records: list[ImageRecord],
) -> dict[str, ImageRecord]:
    return {
        key: record
        for record in records
        for key in (record["path"], record["image_id"])
    }


def preferred_reason(record: ImageRecord) -> str:
    """Latest non-empty reason across all preserved causes."""
    for cause in reversed(record.get("causes") or []):
        reason = cause.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    reason = record.get("reason")
    return reason if isinstance(reason, str) else ""


def capture_reasons(ws: Workspace) -> dict[str, str]:
    """Image path/basename -> latest explicit reason across preserved causes."""
    reasons: dict[str, str] = {}
    for record in load_image_records(ws):
        reason = preferred_reason(record)
        reasons[record["path"]] = reason
        reasons[record["file"]] = reason  # compatibility for older consumers
    return reasons


def image_counts(
    records: list[ImageRecord], backlinks: dict[str, list[str]] | None = None
) -> dict[str, int]:
    links = backlinks or {}
    return {
        "images": len(records),
        "available_images": sum(bool(record.get("exists")) for record in records),
        "tracked_images": sum(bool(record.get("tracked")) for record in records),
        "legacy_untracked_images": sum(
            not bool(record.get("tracked")) for record in records
        ),
        "checkpoint_linked_images": sum(
            record["path"] in links or record["image_id"] in links
            for record in records
        ),
        "causal_edges": sum(len(record.get("causes") or []) for record in records),
        "unknown_inventory_links": sum(
            not bool(record.get("causes")) for record in records
        ),
    }


def _table(value: object) -> str:
    return escape_markdown_text(value)


def _timing(record: ImageRecord) -> str:
    span = record.get("span")
    if (
        isinstance(span, list)
        and len(span) == 2
        and all(isinstance(value, (int, float)) for value in span)
    ):
        return f"{fmt_ts(float(span[0]))}–{fmt_ts(float(span[1]))}"
    t = record.get("t")
    return fmt_ts(float(t)) if isinstance(t, (int, float)) else "-"


_KIND_KO = {"filmstrip": "조망 시트", "frame": "스틸", "frame-crop": "부분 스틸"}


def _checkpoint_cell(ids: list[str], log_name: str) -> str:
    return " · ".join(
        f"[{_table(checkpoint_id)}]"
        f"({md_target(f'{log_name}#'
                      + quote(checkpoint_id, safe='-._~'))})"
        for checkpoint_id in ids
    ) or "-"


def _detail_lines(record: ImageRecord) -> list[str]:
    """Keep machine identifiers available without dominating the main table."""
    detail = {
        "image_id": record["image_id"],
        "path": record["path"],
        "kind": record["kind"],
        "causes": record.get("causes") or [],
    }
    detail_json = html.escape(
        json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True),
        quote=False,
    )
    return [
        f'<a id="{record["image_id"]}"></a>',
        "<details>",
        f"<summary>{_KIND_KO.get(record['kind'], record['kind'])} "
        f"{_timing(record)} 상세</summary>",
        "",
        "<pre><code>",
        detail_json,
        "</code></pre>",
        "",
        "</details>",
        "",
    ]


def export_image_index(
    ws: Workspace, records: list[ImageRecord] | None = None,
    *, index_backlink: str = "../INDEX.md",
) -> str:
    """Render links so Obsidian connects images without decoding them all."""
    catalog = load_image_records(ws) if records is None else records
    backlinks = checkpoint_image_backlinks(ws)
    counts = image_counts(catalog, backlinks)
    title = str(ws.manifest.get("title") or ws.root.name).strip()
    log_name = f"{ws.doc_stem}.md"
    contexts = {
        c["id"]: c
        for c in selected_checkpoints(ws, None)
    }
    lines = [
        "---",
        "type: tca-image-index",
        f"aliases: [{json.dumps(title + ' — 이미지 인덱스', ensure_ascii=False)}]",
        f"video: {json.dumps(ws.video.name, ensure_ascii=False)}",
        *(f"{key}: {value}" for key, value in counts.items()),
        "---",
        "",
        f"# 스틸·조망 목록: {ws.video.name}",
        "",
        f"[← 코퍼스 인덱스]({index_backlink})"
        + (f" · [장면 로그]({md_target(log_name)})" if contexts else ""),
        "",
        "영상에서 뽑은 스틸과 조망 시트를 장면 설명·확인 지식과 함께 "
        "정리한 목록입니다. 수집 provenance 원본은 상세에 보존됩니다.",
        "",
        "| 상세 | 종류 | 시각/구간 | 장면 설명 | 확인·재활용 지식 "
        "| 장면 구간 | 파일 |",
        "|---|---|---|---|---|---|---|",
    ]
    # 표에서 사람이 얻는 것은 "이 스틸이 무엇을 보여주는가"이고, 그 판독은
    # 연결된 체크포인트에서 온다. 촬영 사유만 남은 스틸은 장면 설명·확인
    # 지식·구간이 모두 빈 줄이 되어 표를 덮는다(실측 90행 중 81행).
    # 세 단으로 나눈다 — 판독된 것은 표에, 촬영 기록만 있는 것과 출처가 없는
    # 것은 접어서. 어느 쪽이든 시각과 파일 링크로 여전히 닿는다.
    def _read(record: ImageRecord) -> list[str]:
        return (backlinks.get(record["path"])
                or backlinks.get(record["image_id"]) or [])

    interpreted = [record for record in catalog if _read(record)]
    captured_only = [
        record for record in catalog
        if not _read(record) and record.get("causes")
    ]
    unsourced = [
        record for record in catalog
        if not _read(record) and not record.get("causes")
    ]
    for record in interpreted:
        image_id = record["image_id"]
        image_path = record["path"]
        checkpoint_ids = backlinks.get(
            image_path, backlinks.get(image_id, [])
        )
        file_cell = (
            f"[파일]({quote(image_path, safe='/-._~')})"
            if record.get("exists")
            else f"파일 없음: `{_table(image_path)}`"
        )
        # 사람 표시 계층: 이 이미지가 무엇을 찍었는가 — 연결된 체크포인트의
        # 검증 상황 요약(프레임 파일명은 캐시 기계 키라 불변)
        context = next(
            (contexts[i] for i in checkpoint_ids if i in contexts), {})
        content = humanize_surface(str(
            context.get("situation") or context.get("hypothesis") or ""
        ).strip())
        knowledge = humanize_surface(str(context.get("note") or "").strip())
        if len(content) > 46:
            content = content[:46].rstrip(" ,·—-(") + "…"
        lines.append(
            f"| [상세](#{image_id}) "
            f"| {_KIND_KO.get(record['kind'], record['kind'])} | {_timing(record)} "
            f"| {_table(content) if content else '-'} "
            f"| {_table(knowledge) if knowledge else '-'} "
            f"| {_checkpoint_cell(checkpoint_ids, log_name)} "
            f"| {file_cell} |"
        )
    def _folded(records: list[ImageRecord], summary: str) -> list[str]:
        if not records:
            return []
        block = ["", "<details>", f"<summary>{summary}</summary>", ""]
        for record in records:
            file_link = (
                f"[파일]({quote(record['path'], safe='/-._~')})"
                if record.get("exists")
                else f"파일 없음: `{_table(record['path'])}`"
            )
            block.append(
                f"- {_timing(record)} · "
                f"{_KIND_KO.get(record['kind'], record['kind'])} · {file_link}"
                f" · [상세](#{record['image_id']})"
            )
        return block + ["", "</details>"]

    lines += _folded(
        captured_only, f"촬영 기록만 있고 판독은 아직: {len(captured_only)}장")
    lines += _folded(
        unsourced, f"출처 기록 없음(초기 수집분): {len(unsourced)}장")
    # 상세는 전부 남긴다 — 출처가 없다는 사실(`"causes": []`)도 기계가 읽을
    # 수 있어야 하고, 이 블록은 접혀 있어 사람 표면을 차지하지 않는다.
    lines += ["", "## 상세 촬영 기록", ""]
    for record in catalog:
        lines.extend(_detail_lines(record))
    return "\n".join(lines) + "\n"


class ImageIndexConsistency(TypedDict):
    """원장이 아는 이미지와 인덱스 문서가 싣는 이미지의 3자 대조."""

    workspace: str
    doc_present: bool
    # 원장(captures.json + image-provenance.jsonl + frames/) 기준 진실
    catalog: int
    # 문서가 실제로 등재한 상세 앵커 수
    indexed: int
    # 문서 프론트매터가 스스로 선언한 수(`images:`)
    declared: int | None
    # 원장에 있는데 문서에 없는 것 — 사람이 찾아갈 수 있게 시각을 앞세운다
    missing: list[str]
    # 문서에만 남은 앵커 — 원장에서 사라졌는데 투영이 남은 경우
    orphaned: list[str]


# 목록만 자르는 표시 상한 — 총계(catalog/indexed)는 무절단.
_MISSING_DETAIL_CAP: Final = 20
_DOC_ANCHOR: Final = re.compile(r'<a id="(img-[0-9a-f]+)"></a>')
_DOC_DECLARED: Final = re.compile(r"^images: (\d+)$", re.MULTILINE)


def image_index_doc(ws: Workspace) -> Path:
    """투영이 쓰는 이미지 인덱스 문서 경로 — 문서 이름은 영상 제목에서 온다."""
    return ws.root / f"{ws.doc_stem}-images.md"


def image_index_consistency(
    ws: Workspace,
    records: list[ImageRecord] | None = None,
    *,
    workspace_id: str | None = None,
) -> ImageIndexConsistency:
    """원장과 발행된 인덱스가 같은 장 수를 말하는가.

    `va capture`는 원장에만 append하고 투영은 `va index`/`va view`가 다시
    렌더한다 — 그 사이에 촬영된 스틸은 원장에 정상 기록된 채로 사람이 읽는
    인덱스에서만 사라진다. 렌더러가 거르는 게 아니라 발행이 낡은 것이라,
    문서를 다시 읽지 않는 한 어느 표면도 이 차이를 말하지 않는다
    (2026-08-05 실측: blender 코퍼스 11개 중 5개 워크스페이스 불일치).

    상세 앵커(image_id)로 대조한다 — 표/접힘/미판독 어느 단으로 렌더됐든
    모든 아티팩트는 상세를 하나씩 갖기 때문에 단 구성 변화에 흔들리지 않는다.
    """
    catalog = load_image_records(ws) if records is None else records
    doc = image_index_doc(ws)
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError:
        text = None
    indexed_ids = set() if text is None else set(_DOC_ANCHOR.findall(text))
    declared_match = None if text is None else _DOC_DECLARED.search(text)
    by_id = {record["image_id"]: record for record in catalog}
    missing = [
        f"{_timing(record)} {record['path']}"
        for image_id, record in by_id.items()
        if image_id not in indexed_ids
    ]
    return {
        "workspace": workspace_id or ws.root.name,
        "doc_present": text is not None,
        "catalog": len(catalog),
        "indexed": len(indexed_ids),
        "declared": int(declared_match.group(1)) if declared_match else None,
        "missing": sorted(missing)[:_MISSING_DETAIL_CAP],
        "orphaned": sorted(indexed_ids - by_id.keys())[:_MISSING_DETAIL_CAP],
    }


def image_index_is_stale(row: ImageIndexConsistency) -> bool:
    """발행이 원장을 따라잡지 못했는가 — 게이트가 아니라 보고 판정."""
    if not row["doc_present"]:
        # 원장이 비어 있으면 문서가 없는 게 정상이다.
        return row["catalog"] > 0
    return (
        bool(row["missing"])
        or bool(row["orphaned"])
        or row["indexed"] != row["catalog"]
        or row["declared"] != row["catalog"]
    )
