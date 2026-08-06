"""Corpus index: one-call vault build over every workspace under a root.

No-excuse size marker: ``# noqa: SIZE_OK``. Each generated file is replaced
atomically; the collection of projection files is not a multi-file transaction.

Self-contained md-first memory layer (independent of any external vault or
service): `va index` regenerates va-out/INDEX.md — the routing map of the
corpus (one line per workspace + an entity reverse index) — and refreshes
each workspace's <name>.md scene log so the tree opens as plain-markdown
vault (Obsidian-compatible, but nothing depends on Obsidian). The corpus is
a standalone vault: frontmatter carries `type: tca-*` discriminators so an
external knowledge vault's queries never pick these notes up.

Write-time gating philosophy: the index is rebuilt on demand by this
command, never by daemons.
"""

from __future__ import annotations

import json
import stat
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from .corpus_projection import (
    _NARRATIVE_TEMPLATE,
    NARRATIVE_END,
    NARRATIVE_START,
    READINESS_KO,
    WorkspaceMeta,
    escape_markdown_text,
    md_target,
    ws_meta,
)
from .fsio import write_text_atomic
from .image_index import export_image_index
from .image_provenance import load_image_records
from .projection_cache import (
    files_fingerprint,
    load_projection_cache,
    renderer_salt,
    save_projection_cache,
    workspace_fingerprint,
)
from .scene_log import export_md
from .search import corpus_root
from .sequence import export_edits_md, load_sequences
from .timestamps import fmt_ts_compact
from .wiki_audit import WIKI_INDEX_BYTE_LIMIT
from .workspace import Workspace
from .workspace_discovery import find_workspaces


class _IndexedWorkspaceMeta(WorkspaceMeta):
    images: int
    edits: int
    rel: str
    doc_stem: str


class _GraphColor(TypedDict):
    a: int
    rgb: int


class _GraphColorGroup(TypedDict):
    query: str
    color: _GraphColor


class _GraphPreset(TypedDict):
    showTags: bool
    showAttachments: bool
    hideUnresolved: bool
    showOrphans: bool
    colorGroups: list[_GraphColorGroup]
    showArrow: bool
    textFadeMultiplier: int
    nodeSizeMultiplier: int
    lineSizeMultiplier: int
    centerStrength: float
    repelStrength: int
    linkStrength: int
    linkDistance: int
    scale: int
    close: bool





def _ellipsize(text: str, limit: int = 40) -> str:
    """Cut at a word boundary with an ellipsis instead of mid-character."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,·—-(") + "…"


# Obsidian 그래프 표시 프리셋 — 첨부·고아·미해석·태그 노드를 끄고 그룹별
# 색을 준다. graph.json은 Obsidian이 UI 조작마다 덮어쓰는 휘발 파일이라
# 기본은 "없을 때만" 생성하고, 리셋은 --graph-reset 명시 요청 시에만.
_GROUP_PALETTE = [0xE0527A, 0x5285E0, 0xE08B52, 0x52C9E0,
                  0x9B59D0, 0x52B860, 0xC9B037]


def _graph_preset(groups: list[str]) -> _GraphPreset:
    color_groups: list[_GraphColorGroup] = [
        {"query": f'path:"{g}"',
         "color": {"a": 1, "rgb": _GROUP_PALETTE[i % len(_GROUP_PALETTE)]}}
        for i, g in enumerate(sorted(groups))
    ]
    color_groups.append(
        {"query": 'path:"wiki"', "color": {"a": 1, "rgb": 0x8A919D}})
    return {
        "showTags": False, "showAttachments": False,
        "hideUnresolved": True, "showOrphans": False,
        "colorGroups": color_groups, "showArrow": False,
        "textFadeMultiplier": 0, "nodeSizeMultiplier": 1,
        "lineSizeMultiplier": 1, "centerStrength": 0.52,
        "repelStrength": 10, "linkStrength": 1, "linkDistance": 250,
        "scale": 1, "close": False,
    }


def write_graph_preset(
    root: Path, groups: list[str], *, reset: bool = False
) -> bool:
    """볼트 그래프 프리셋을 생성한다(없을 때만; reset=True면 덮어쓴다)."""
    dest = root / ".obsidian" / "graph.json"
    if dest.exists() and not reset:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        dest,
        json.dumps(_graph_preset(groups), ensure_ascii=False, indent=2) + "\n",
    )
    return True


# 라우팅 인덱스 감사 상한 중 워크스페이스 표에 허용하는 몫 — 상한 자체는
# 감사(wiki_audit)가 소유하고 여기서는 비율만 정한다. 리터럴로 복제하면
# 상한을 올려도 접기 임계가 옛 값에 남는다. 이 선을 넘고 장르 그룹이 있으면
# 표를 그룹 요약으로 접는다.
_INDEX_TABLE_FOLD_BYTES = WIKI_INDEX_BYTE_LIMIT * 6 // 10


def _has_authored_narrative(path: Path) -> bool:
    """Return whether a generated scene log contains non-placeholder prose."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise OSError(
            f"unsafe scene-log source (not a regular file): {path}"
        )
    text = path.read_text(encoding="utf-8")
    _, start, remainder = text.partition(NARRATIVE_START)
    narrative, end, _ = remainder.partition(NARRATIVE_END)
    return bool(start and end) and narrative.strip() != _NARRATIVE_TEMPLATE.strip()


def _ensure_scene_log_conflict_dir(ws: Workspace) -> Path:
    """Create the local conflict directory without following symlinks."""
    path = ws.root
    for component in ("conflicts", "scene-logs"):
        path = path / component
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise OSError(
                f"unsafe scene-log conflict directory (not a real directory): {path}"
            )
    return path


def _preserve_scene_log_conflict(source: Path, conflict_dir: Path) -> None:
    """Move authored prose into a unique regular file, never through a link."""
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise OSError(
            f"unsafe scene-log source (not a regular file): {source}"
        )
    source_bytes = source.read_bytes()
    destination = conflict_dir / source.name
    discriminator = 2
    while True:
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            source.replace(destination)
            return
        if not stat.S_ISREG(mode):
            raise OSError(
                f"unsafe scene-log conflict target (not a regular file): "
                f"{destination}"
            )
        if destination.read_bytes() == source_bytes:
            source.unlink()
            return
        destination = conflict_dir / (
            f"{source.stem}--{discriminator}{source.suffix}"
        )
        discriminator += 1


def build_index(
    roots: list[str] | None = None,
    *,
    graph_reset: bool = False,
    workspace_paths: Sequence[Path] | None = None,
    projection_root: Path | None = None,
) -> tuple[Path, int]:
    """Rebuild INDEX.md + per-workspace scene logs. Returns (path, ws count)."""
    ws_dirs = (
        list(workspace_paths)
        if workspace_paths is not None
        else find_workspaces(roots)
    )
    if not ws_dirs:
        raise ValueError("no workspaces found (looked in: "
                         + ", ".join(roots or ["./va-out"]) + ")")
    root = (
        Path(projection_root).resolve()
        if projection_root is not None
        else corpus_root(roots)
    )
    # 포맷 솔트 — 산출물을 결정하는 렌더러들의 소스 digest에서 자동 유도.
    index_salt = renderer_salt("index")
    cache = load_projection_cache(root)
    index_cache = cache.get("index")
    if not isinstance(index_cache, dict):
        index_cache = {}
    cache["index"] = index_cache

    rows: list[_IndexedWorkspaceMeta] = []
    for d in ws_dirs:
        ws = Workspace.load(d)
        image_records = load_image_records(ws)
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            rel = d.name
        sequences = load_sequences(ws)
        stem = ws.doc_stem
        # 행 구성과 scene-log 렌더가 같은 meta를 공유 — ws_meta는 전사
        # 적재·mode 추천·status 스냅샷을 포함해 워크스페이스당 두 번
        # 계산하기엔 비싸다.
        meta = ws_meta(ws)
        row: _IndexedWorkspaceMeta = {
            **meta,
            "images": len(image_records),
            "edits": len(sequences),
            "rel": rel,
            "doc_stem": stem,
        }
        depth = rel.count("/") + 1
        backlink = "../" * depth + "INDEX.md"
        # 파일명은 영상 제목에서 온다 — Obsidian은 파일명을 표시하므로
        # url-<hash> 류 기계 디렉터리명을 그대로 쓰면 vault를 못 읽는다.
        scene_log_path = ws.root / f"{stem}.md"
        image_index_path = ws.root / f"{stem}-images.md"
        edits_path = ws.root / f"{stem}-edits.md"
        current_outputs = {scene_log_path, image_index_path, edits_path}
        legacy_images = ws.root / "image-index.md"
        stale_scene_logs: list[Path] = []
        authored_stale: list[Path] = []
        preferred_scene_log: Path | None = None
        conflict_dir: Path | None = None
        if row["has_log"]:
            stale_scene_logs = [
                candidate
                for candidate in sorted(ws.root.glob("*.md"))
                if candidate != scene_log_path
                and "\ntype: tca-scene-log\n"
                in candidate.read_text(encoding="utf-8")[:200]
            ]
            authored_stale = [
                candidate
                for candidate in stale_scene_logs
                if _has_authored_narrative(candidate)
            ]
            canonical_authored = _has_authored_narrative(scene_log_path)
            replacement_candidates = (
                authored_stale
                if authored_stale
                else stale_scene_logs
                if not scene_log_path.is_file()
                else []
            )
            if not canonical_authored and replacement_candidates:
                # Filenames are persisted identity; unlike mtimes they survive
                # archive extraction and copies, so selection is reproducible.
                preferred_scene_log = max(
                    replacement_candidates,
                    key=lambda candidate: candidate.name,
                )
            conflicts_to_preserve = [
                candidate
                for candidate in authored_stale
                if candidate != preferred_scene_log
            ]
            if conflicts_to_preserve:
                # Preflight before any projection write or source move.
                conflict_dir = _ensure_scene_log_conflict_dir(ws)
        # 복원·복사로 돌아온 고아 투영물(tca-image-index/tca-edit-log,
        # 개명 전 stem 잔재)은 입력·산출물 지문 어느 쪽도 바꾸지 않는다 —
        # 스킵 전에 재빌드 경로와 같은 기준으로 스캔해야 영구 잔존이 없다.
        orphan_projections = False
        for candidate in sorted(ws.root.glob("*.md")):
            if candidate in current_outputs:
                continue
            head = candidate.read_text(encoding="utf-8")[:200]
            if (
                "\ntype: tca-image-index\n" in head
                or "\ntype: tca-edit-log\n" in head
            ):
                orphan_projections = True
                break
        if not orphan_projections and stem != d.name:
            for suffix in ("", "-images", "-edits"):
                stale = ws.root / f"{d.name}{suffix}.md"
                if stale in current_outputs or not stale.is_file():
                    continue
                if "\ntype: tca-" in stale.read_text(encoding="utf-8")[:200]:
                    orphan_projections = True
                    break
        # 입력 지문 + 산출물 상태 지문이 모두 일치하고 이관/정리 대상이
        # 없으면 이 워크스페이스의 쓰기 전체를 건너뛴다(투영은 결정적).
        # 산출물 지문이 필요한 이유: scene-log의 수기 서사는 다음 재투영의
        # 입력을 겸한다 — 바깥 편집이 있으면 반드시 다시 렌더해야 한다.
        fingerprint = workspace_fingerprint(d, salt=index_salt)
        output_paths = [scene_log_path, image_index_path, edits_path]
        cached_entry = index_cache.get(rel)
        if (
            isinstance(cached_entry, dict)
            and cached_entry.get("in") == fingerprint
            and cached_entry.get("out")
            == files_fingerprint(output_paths, salt=index_salt)
            and (not row["has_log"] or scene_log_path.is_file())
            and (image_index_path.is_file() if image_records else True)
            and (edits_path.is_file() if sequences
                 else not edits_path.is_file())
            and not stale_scene_logs
            and not orphan_projections
            and not legacy_images.is_file()
        ):
            rows.append(row)
            continue
        if image_records or image_index_path.is_file() or legacy_images.is_file():
            write_text_atomic(
                image_index_path,
                export_image_index(ws, image_records, index_backlink=backlink),
            )
        if row["has_log"]:
            if preferred_scene_log is not None:
                preferred_scene_log.replace(scene_log_path)
            write_text_atomic(
                scene_log_path,
                export_md(
                    ws, None, image_records=image_records,
                    index_backlink=backlink, meta=meta,
                ),
            )
            for stale_scene_log in stale_scene_logs:
                if not stale_scene_log.is_file():
                    continue
                if stale_scene_log in authored_stale:
                    if conflict_dir is None:
                        raise OSError(
                            "scene-log conflict directory was not preflighted"
                        )
                    _preserve_scene_log_conflict(stale_scene_log, conflict_dir)
                else:
                    stale_scene_log.unlink()
            legacy_scene_log = ws.root / "scene-log.md"
            if legacy_scene_log not in current_outputs:
                legacy_scene_log.unlink(missing_ok=True)
        if sequences:
            write_text_atomic(
                edits_path,
                export_edits_md(ws, index_backlink=backlink),
            )
        else:
            edits_path.unlink(missing_ok=True)
        if legacy_images not in current_outputs:
            legacy_images.unlink(missing_ok=True)
        for stale in sorted(ws.root.glob("*.md")):
            if stale in current_outputs:
                continue
            head = stale.read_text(encoding="utf-8")[:200]
            if (
                "\ntype: tca-image-index\n" in head
                or "\ntype: tca-edit-log\n" in head
            ):
                stale.unlink(missing_ok=True)
        # 개명 전 이름으로 남은 우리 투영물은 고아가 된다 — 우리가 쓴
        # 파일(tca-* frontmatter)임이 확인될 때만 지운다.
        if stem != d.name:
            for suffix in ("", "-images", "-edits"):
                stale = ws.root / f"{d.name}{suffix}.md"
                if stale in current_outputs or not stale.is_file():
                    continue
                head = stale.read_text(encoding="utf-8")[:200]
                if "\ntype: tca-" in head:
                    stale.unlink(missing_ok=True)
        index_cache[rel] = {
            "in": fingerprint,
            "out": files_fingerprint(output_paths, salt=index_salt),
        }
        rows.append(row)

    has_wiki = (root / "wiki" / "INDEX.md").is_file()
    lines = [
        "---",
        "type: tca-corpus-index",
        'aliases: ["코퍼스 인덱스"]',
        f"workspaces: {len(rows)}",
        "---",
        "",
        "# 코퍼스 인덱스",
        "",
        f"워크스페이스 {len(rows)}개 — `va index`로 재생성, "
        "`va search \"<텀>\"`으로 랭킹 검색.",
        "",
        *(["위키: [엔티티·태그·관계·대사](wiki/INDEX.md)", ""]
          if has_wiki else []),
        "| 영상 | 길이 | 유형 | 상태 | 구간 | 스틸 | 한 줄 |",
        "|---|---|---|---|---|---|---|",
    ]
    # 사람 표시 계층: 분석된 영상만 본 표에 — 체크포인트 0개 행("—" 잡음)은
    # 아래 별도 목록으로 물려 표를 읽을 수 있게 유지한다.
    analyzed = [r for r in rows if r["has_log"]]
    pending = [r for r in rows if not r["has_log"]]
    def _table_row(r: _IndexedWorkspaceMeta) -> str:
        shown = escape_markdown_text(r["title"] if r["title"] else r["name"])
        link = f"[{shown}]({md_target(f'{r['rel']}/{r['doc_stem']}.md')})"
        images = (
            f"[{r['images']}]"
            f"({md_target(f'{r['rel']}/{r['doc_stem']}-images.md')})"
            if r["images"] else "0"
        )
        return (
            f"| {link} | {fmt_ts_compact(r['duration'])} | {r['mode_head']} "
            f"| {READINESS_KO.get(r['readiness'], r['readiness'])} "
            f"| {r['cps']} "
            f"| {images} | {escape_markdown_text(_ellipsize(r['head']))} |")

    table = [(r, _table_row(r))
             for r in sorted(analyzed, key=lambda r: r["name"])]
    # 표는 편수에 비례해 커진다 — 인덱스를 여는 것 자체가 컨텍스트 비용이
    # 되기 전에 그룹 요약으로 접는다. 그룹이 없는 워크스페이스는 접지 않는다:
    # 접기가 도달 불가를 만들면 안 된다.
    grouped = [r for r, _ in table if "/" in r["rel"]]
    fold = bool(grouped) and (
        sum(len(line.encode("utf-8")) for _, line in table)
        > _INDEX_TABLE_FOLD_BYTES)
    lines += [line for r, line in table if not (fold and "/" in r["rel"])]
    if fold:
        lines += ["", f"그룹 소속 {len(grouped)}편은 아래 **그룹** 인덱스에 "
                      "있다 — 편수가 늘어도 이 인덱스는 커지지 않는다."]
    if pending:
        lines += ["", "## 미분석 (체크포인트 없음 — `va brief`로 착수)", ""]
        for r in sorted(pending, key=lambda r: r["name"]):
            shown = escape_markdown_text(
                r["title"] if r["title"] else r["name"]
            )
            images = (
                f", 이미지 [{r['images']}]"
                f"({md_target(f'{r['rel']}/{r['doc_stem']}-images.md')})"
                if r["images"] else ""
            )
            # signals-only 편집안은 장면 로그 없이도 존재 가능 — 인덱스에서
            # 직접 링크해 고아 페이지를 막는다.
            edits = (
                f", 편집안 [{r['edits']}]"
                f"({md_target(f'{r['rel']}/{r['doc_stem']}-edits.md')})"
                if r["edits"] else ""
            )
            lines.append(
                f"- {shown} — {fmt_ts_compact(r['duration'])}, "
                f"{r['mode_head']}{images}{edits}")

    # 역색인은 위키(엔티티 인덱스)가 정본 — 위키가 있으면 위임해 라우팅
    # 인덱스를 다이어트한다(중복 목록이 인덱스를 스펙으로 썩게 한다).
    if has_wiki:
        lines += ["", "엔티티·화자 찾기 → [위키 인덱스](wiki/INDEX.md)"]
    else:
        inverse: dict[str, list[_IndexedWorkspaceMeta]] = defaultdict(list)
        for r in rows:
            for lb in r["labels"]:
                inverse[lb].append(r)
        if inverse:
            lines += ["", "## 엔티티·화자 역색인", ""]
            for lb in sorted(inverse):
                targets = " · ".join(
                    f"[{escape_markdown_text(r['title'] or r['name'])}]"
                    f"({md_target(f'{r['rel']}/{r['doc_stem']}.md')})"
                    for r in sorted(inverse[lb], key=lambda row: row["rel"])
                )
                lines.append(
                    f"- **{escape_markdown_text(lb)}** → {targets}"
                )

    groups: dict[str, list[_IndexedWorkspaceMeta]] = defaultdict(list)
    for r in rows:
        if "/" in r["rel"]:
            groups[r["rel"].split("/", 1)[0]].append(r)
    if groups:
        lines += ["", "## 그룹", ""]
        for g in sorted(groups):
            members = groups[g]
            shown_group = escape_markdown_text(g)
            lines.append(
                f"- [{shown_group}]({md_target(f'{g}/{g}.md')})"
                f" — {len(members)}편")
            glines = [
                "---",
                "type: tca-group-index",
                "aliases: "
                + json.dumps([f"{g} 그룹 인덱스"], ensure_ascii=False),
                f"workspaces: {len(members)}",
                "---",
                "",
                f"# {shown_group}",
                "",
                "[← 코퍼스 인덱스](../INDEX.md)",
                "",
            ]
            for r in sorted(members, key=lambda r: r["name"]):
                shown = escape_markdown_text(
                    r["title"] if r["title"] else r["name"]
                )
                inner = r["rel"].split("/", 1)[1]
                status = READINESS_KO.get(r["readiness"], r["readiness"])
                head = (
                    escape_markdown_text(_ellipsize(r["head"], 60))
                    if r["head"]
                    else ""
                )
                if not r["has_log"]:
                    # 미분석(체크포인트 0) 멤버는 씬 로그가 없다 — 최상위
                    # 인덱스의 미분석 목록과 같은 계약으로, 없는 파일에
                    # 링크를 걸지 않는다.
                    glines.append(
                        f"- {shown} · 미분석 — `va brief`로 착수")
                    continue
                glines.append(
                    f"- [{shown}]({md_target(f'{inner}/{r['doc_stem']}.md')})"
                    f" · {status}"
                    + (f" — {head}" if head else ""))
            write_text_atomic(root / g / f"{g}.md", "\n".join(glines) + "\n")
    write_graph_preset(root, sorted(groups), reset=graph_reset)

    dest = root / "INDEX.md"
    write_text_atomic(dest, "\n".join(lines) + "\n")
    save_projection_cache(root, cache, live_keys={r["rel"] for r in rows})
    return dest, len(rows)
