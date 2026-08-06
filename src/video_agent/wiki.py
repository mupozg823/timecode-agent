"""Corpus wiki: deterministic aggregation of agent-labelled knowledge.

No-excuse size marker: ``# noqa: SIZE_OK``. This deterministic renderer
remains one projection surface; 2026-07-25 refactor decomposed the former
single build function into collect/write phase functions inside it.

`va wiki` compiles every workspace's checkpoints into an LLM-wiki layer
under va-out/wiki/: one page per entity/character (appearances, quotes,
relations), a genre-grouped tag reverse index, a relation ledger and a
corpus quote log. Extraction (who/what/why labels on checkpoints) is the
agent's selection job during the understanding loop; this module only
aggregates — placement/selection separation, applied to memory.

All plain markdown with `type: tca-wiki-*` frontmatter discriminators, so
the wiki stays isolated from any external knowledge vault's queries.

Karpathy LLM-wiki transposition (gist 442a6bf, 2026): three layers — raw
sources stay immutable (video + append-only checkpoints.jsonl), the wiki is
the LLM-maintained compounding artifact, SKILL.md is the schema. Entity
pages keep an agent-notes block (tca:notes markers) that survives
regeneration: the deterministic skeleton is rebuilt around it, the prose
inside it belongs to the agent (synthesis, contradiction flags). Queries go
index-first (INDEX.md -> page), never back to raw sources unless the wiki
can't answer.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .checkpoints import load_checkpoints
from .corpus_projection import (
    STATUS_KO,
    checkpoint_fragment,
    escape_markdown_text,
    humanize_surface,
    md_target,
    relation_triples,
    speaker_labels,
    string_values,
    tagfmt,
    ws_meta,
)
from .fsio import write_text_atomic
from .projection_cache import (
    directory_fingerprint,
    load_projection_cache,
    renderer_salt,
    save_projection_cache,
    workspace_fingerprint,
)
from .search import corpus_root
from .skillgen import write_skill_drafts
from .status import _workspace_status_snapshot
from .timestamps import fmt_ts_compact
from .wiki_procedures import (
    collect_procedure_steps,
    merge_procedure_steps,
    procedure_maturity,
    write_procedure_pages,
)
from .wiki_state import (
    ENTITY_RECALL_FILENAME,
    NOTES_END,
    NOTES_PLACEHOLDER,
    NOTES_START,
    demote_stale_entity_pages,
    existing_notes,
    plan_entity_pages,
)
from .workspace import Workspace
from .workspace_discovery import find_workspaces

# 사람 표시 계층: Obsidian 퀵스위처·검색·링크 자동완성이 파일명 대신
# 계층 이름으로 잡히도록 frontmatter aliases를 준다(파일명=기계 키 불변).
_LAYER_ALIAS = {
    "index": "위키 인덱스",
    "tags": "태그 역색인",
    "relations": "관계 원장",
    "quotes": "대사록",
}


def _fm(kind: str, extra: list[str]) -> list[str]:
    alias = _LAYER_ALIAS.get(kind)
    alias_line = [f'aliases: ["{alias}"]'] if alias else []
    return ["---", f"type: tca-wiki-{kind}", *alias_line, *extra, "---", ""]


ALIAS_JACCARD = 0.5   # 표기 변형 의심 임계 (측정 07-22: 정당 병합만 통과)

def _quotes_of(c: dict) -> list[dict]:
    out = []
    for q in c.get("quotes") or []:
        if isinstance(q, dict) and q.get("text"):
            out.append(q)
    return out


def _repair_archived_links(
    quarantine: Path, archived_target_of: dict[str, str]
) -> None:
    """강등 페이지의 동결 링크 보수 — 이동·개편 이전 경로를 현 구조로.

    강등은 페이지를 한 단계 깊이 옮기지만 본문 링크는 동결된다(실측:
    ../INDEX.md 57건 · 구 플랫 레이아웃 scene-log.md 63건). 에이전트
    노트 본문은 건드리지 않고 링크 대상만 결정적으로 재작성한다.
    """
    def ws_target(match: re.Match[str]) -> str:
        target = match.group("angle") or match.group("plain")
        legacy, separator, fragment = target.removeprefix("../../").partition("#")
        current = archived_target_of.get(legacy)
        if current is None:
            return match.group(0)
        suffix = f"#{fragment}" if separator else ""
        return f"]({md_target(f'../../../{current}{suffix}')})"

    for page in quarantine.glob("*.md"):
        text = page.read_text(encoding="utf-8")
        fixed = text.replace("](../INDEX.md)", "](../../INDEX.md)")
        fixed = re.sub(
            r"\]\((?:<(?P<angle>\.\./\.\./(?!\.\./)[^>\n]+)>|"
            r"(?P<plain>\.\./\.\./(?!\.\./)[^)\n]+))\)",
            ws_target,
            fixed,
        )
        if fixed != text:
            write_text_atomic(page, fixed)


@dataclass(frozen=True, slots=True)
class _Aggregate:
    """수집 단계의 의도적으로 가변인 라벨·관계 누산기."""

    appearances: dict[str, list[dict]] = field(
        default_factory=lambda: defaultdict(list))
    quotes_by_speaker: dict[str, list[dict]] = field(
        default_factory=lambda: defaultdict(list))
    relations: list[dict] = field(default_factory=list)
    quotes: list[dict] = field(default_factory=list)
    tag_hits: dict[str, list[dict]] = field(
        default_factory=lambda: defaultdict(list))
    modes: dict[str, str] = field(default_factory=dict)
    doc_path_of: dict[str, str] = field(default_factory=dict)
    display_name_of: dict[str, str] = field(default_factory=dict)
    archived_target_of: dict[str, str] = field(default_factory=dict)
    # 근거 게이트에서 탈락해 이 위키에 실리지 않은 종결 체크포인트 수.
    # 빠진 것을 세지 않으면 위키는 코퍼스 전부를 담은 것처럼 읽힌다.
    omitted_unsupported: dict[str, int] = field(default_factory=dict)
    # task-* 라벨의 절차 단계 — 근거 도달 체크포인트 한정 (P1, 큐 #52).
    procedure_steps: dict[str, list] = field(default_factory=dict)


def _collect(
    ws_dirs: list[Path], root: Path, include_hypotheses: bool
) -> _Aggregate:
    agg = _Aggregate()
    flat_archived_targets: dict[str, set[str]] = defaultdict(set)
    for d in ws_dirs:
        ws = Workspace.load(d)
        name = ws.root.name
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            rel = name
        workspace_id = rel
        doc_path = f"{rel}/{ws.doc_stem}"
        agg.doc_path_of[workspace_id] = doc_path
        current_target = f"{doc_path}.md"
        for legacy in (
            f"{rel}/{name}.md",
            f"{rel}/scene-log.md",
            current_target,
        ):
            agg.archived_target_of[legacy] = current_target
        for legacy in (
            f"{name}/{name}.md",
            f"{name}/scene-log.md",
            f"{name}/{ws.doc_stem}.md",
        ):
            flat_archived_targets[legacy].add(current_target)
        status_snapshot = _workspace_status_snapshot(ws)
        meta = ws_meta(ws, status_snapshot=status_snapshot)
        agg.modes[workspace_id] = meta["mode_head"]
        agg.display_name_of[workspace_id] = meta["title"] or name
        omitted = len(status_snapshot.terminal_checkpoints) - len(
            status_snapshot.supported_checkpoints
        )
        if omitted > 0:
            agg.omitted_unsupported[workspace_id] = omitted
        # 절차 단계는 include_hypotheses와 무관하게 근거 도달분만 —
        # 추정 단계가 절차 페이지에 실리면 "원장이 보장한 절차"가 깨진다.
        grounded_of = {
            levels.checkpoint_id: levels.grounded
            for levels in status_snapshot.verification.checkpoint_levels
        }
        merge_procedure_steps(
            agg.procedure_steps,
            collect_procedure_steps(
                workspace_id,
                status_snapshot.supported_checkpoints,
                grounded_of,
            ),
        )
        checkpoints = (
            load_checkpoints(ws)
            if include_hypotheses
            else list(status_snapshot.supported_checkpoints)
        )
        for c in checkpoints:
            raw_situation = c.get("situation")
            raw_hypothesis = c.get("hypothesis")
            # 위키 표도 사람이 읽는 표면이다 — 원장의 도구 어휘를 그대로
            # 내보내지 않는다(자르기 전에 바꿔야 길이 기준이 어긋나지 않는다).
            if isinstance(raw_situation, str) and raw_situation:
                situ = humanize_surface(raw_situation)[:80]
            elif isinstance(raw_hypothesis, str):
                situ = humanize_surface(raw_hypothesis)[:80]
            else:
                situ = ""
            row = {"ws": workspace_id, "cp": c["id"], "span": c["span"],
                   "situation": situ, "status": c["status"]}
            for lb in speaker_labels(c.get("speakers")):
                agg.appearances[lb].append({**row, "role": "화자"})
            for lb in speaker_labels(c.get("entities")):
                agg.appearances[lb].append({**row, "role": "엔티티"})
            for q in _quotes_of(c):
                q2 = {**q, "ws": workspace_id, "cp": c["id"]}
                agg.quotes.append(q2)
                if q.get("speaker"):
                    agg.quotes_by_speaker[q["speaker"]].append(q2)
            for r in relation_triples(c):
                agg.relations.append(
                    {**r, "ws": workspace_id, "cp": c["id"]})
            for t in string_values(c.get("tags")):
                if t.strip():
                    agg.tag_hits[tagfmt(t)].append(row)
    for legacy, targets in flat_archived_targets.items():
        if len(targets) == 1:
            agg.archived_target_of.setdefault(legacy, next(iter(targets)))
    return agg


def _ws_href(
    doc_path_of: dict[str, str],
    workspace_id: str,
    up: int = 2,
    checkpoint_id: str | None = None,
) -> str:
    fragment = (
        f"#{checkpoint_fragment(checkpoint_id)}"
        if checkpoint_id is not None
        else ""
    )
    target = (f"{'../' * up}"
              f"{doc_path_of.get(workspace_id, workspace_id)}.md{fragment}")
    return md_target(target)


def _ws_link(
    doc_path_of: dict[str, str],
    display_name_of: dict[str, str],
    workspace_id: str,
    up: int = 2,
    checkpoint_id: str | None = None,
) -> str:
    fragment = (
        f"#{checkpoint_fragment(checkpoint_id)}"
        if checkpoint_id is not None
        else ""
    )
    target = (f"{'../' * up}"
              f"{doc_path_of.get(workspace_id, workspace_id)}.md{fragment}")
    display_name = escape_markdown_text(
        display_name_of.get(workspace_id, workspace_id)
    )
    return f"[{display_name}]({md_target(target)})"


def _ent_link(
    slugs: dict[str, str], lb: str, prefix: str = "entities/"
) -> str:
    shown = escape_markdown_text(lb)
    if lb not in slugs:
        return f"**{shown}**"
    return f"[{shown}]({md_target(f'{prefix}{slugs[lb]}.md')})"


def _mermaid_label(value: str) -> str:
    """Render arbitrary ledger text inside one quoted Mermaid label."""
    visible = value.replace("\r\n", "\n").replace("\r", "\n")
    return (
        html.escape(visible, quote=True)
        .replace("\\", "&#92;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("\n", "&#10;")
    )


def _write_entity_pages(page_plan, slugs, agg: _Aggregate, ws_link) -> None:
    for lb, slug in slugs.items():
        rows = agg.appearances.get(lb, [])
        page_path = page_plan.paths[lb]
        shown = escape_markdown_text(lb)
        lines = _fm("entity", [f"entity: {json.dumps(lb, ensure_ascii=False)}",
                               f"appearances: {len(rows)}"])
        lines += [f"# {shown}", "", "[← 위키 인덱스](../INDEX.md)", "",
                  NOTES_START + existing_notes(page_plan.notes_sources[lb])
                  + NOTES_END, ""]
        if rows:
            lines += ["## 등장", "",
                      "| 영상 | 구간 | 역할 | 상태 | 상황 |", "|---|---|---|---|---|"]
            for r in sorted(rows, key=lambda r: (r["ws"], r["span"][0])):
                s, e = r["span"]
                situation = escape_markdown_text(r["situation"])
                lines.append(
                    f"| {ws_link(r['ws'])} | {fmt_ts_compact(s)}–{fmt_ts_compact(e)} "
                    f"| {r['role']} | {STATUS_KO.get(r['status'], r['status'])} "
                    f"| {situation} |")
        qs = agg.quotes_by_speaker.get(lb, [])
        if qs:
            lines += ["", "## 대사", ""]
            for q in sorted(qs, key=lambda q: (q["ws"], q.get("t") or 0)):
                t = f"{fmt_ts_compact(q['t'])} " if q.get("t") is not None else ""
                quote = escape_markdown_text(q["text"])
                lines.append(f"- {t}“{quote}” — {ws_link(q['ws'])}")
        rel = [r for r in agg.relations if lb in (r["s"], r["o"])]
        if rel:
            lines += ["", "## 관계", ""]
            for r in sorted(rel, key=lambda r: (r["ws"], r["cp"])):
                subject = escape_markdown_text(r["s"])
                predicate = escape_markdown_text(r["p"])
                object_ = escape_markdown_text(r["o"])
                checkpoint_id = escape_markdown_text(r["cp"])
                lines.append(
                    f"- **{subject}** –{predicate}→ **{object_}** "
                    f"({ws_link(r['ws'])} · {checkpoint_id})")
        write_text_atomic(page_path, "\n".join(lines) + "\n")


def _write_tags_page(wiki: Path, agg: _Aggregate, ws_link) -> None:
    # tag reverse index, grouped by the workspace's mode (genre proxy)
    lines = _fm("tags", [f"tags: {len(agg.tag_hits)}"])
    lines += ["# 태그 역색인", "", "[← 위키 인덱스](INDEX.md)", ""]
    by_mode: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for tag, rows in agg.tag_hits.items():
        for mode in sorted({agg.modes[r["ws"]] for r in rows}):
            by_mode[mode][tag] = [
                r for r in rows if agg.modes[r["ws"]] == mode]
    for mode in sorted(by_mode):
        lines += [f"## {escape_markdown_text(mode)}", ""]
        for tag in sorted(by_mode[mode]):
            hits = " · ".join(
                ws_link(r["ws"], up=1, checkpoint_id=r["cp"])
                for r in sorted(by_mode[mode][tag],
                                key=lambda r: (r["ws"], r["cp"])))
            lines.append(f"- #{escape_markdown_text(tag)} → {hits}")
        lines.append("")
    write_text_atomic(wiki / "tags.md", "\n".join(lines) + "\n")


def _write_relations_page(wiki: Path, agg: _Aggregate, ent_link, ws_link) -> None:
    # relation ledger (인과·상호작용 엣지 원장)
    lines = _fm("relations", [f"relations: {len(agg.relations)}"])
    lines += ["# 관계 원장", "", "[← 위키 인덱스](INDEX.md)", ""]
    for r in sorted(agg.relations, key=lambda r: (r["ws"], r["cp"])):
        predicate = escape_markdown_text(r["p"])
        checkpoint_id = escape_markdown_text(r["cp"])
        lines.append(
            f"- {ent_link(r['s'])} –**{predicate}**→ {ent_link(r['o'])} "
            f"({ws_link(r['ws'], up=1)} · {checkpoint_id})"
        )
    write_text_atomic(wiki / "relations.md", "\n".join(lines) + "\n")


def _write_quotes_page(wiki: Path, agg: _Aggregate, ws_link) -> None:
    # corpus quote log
    lines = _fm("quotes", [f"quotes: {len(agg.quotes)}"])
    lines += ["# 대사록", "", "[← 위키 인덱스](INDEX.md)", ""]
    for ws_name in sorted({q["ws"] for q in agg.quotes}):
        lines += [f"## {ws_link(ws_name, up=1)}", ""]
        for q in sorted((q for q in agg.quotes if q["ws"] == ws_name),
                        key=lambda q: q.get("t") or 0):
            t = f"{fmt_ts_compact(q['t'])} " if q.get("t") is not None else ""
            spk = (
                f"**{escape_markdown_text(q['speaker'])}**: "
                if q.get("speaker")
                else ""
            )
            quote = escape_markdown_text(q["text"])
            lines.append(f"- {t}{spk}“{quote}”")
        lines.append("")
    write_text_atomic(wiki / "quotes.md", "\n".join(lines) + "\n")


def _alias_groups(slugs: dict[str, str]) -> list[list[str]]:
    # near-duplicate labels (측정 2026-07-22: 표기 변형이 크로스 비디오
    # 인지율을 2/13로 떨어뜨림 — "2배 이벤트 배지"↔"2배 배지"). 후보 표면화만
    # 결정적으로 하고 병합 판단은 에이전트(selection) 몫.
    def _tokens(lb: str) -> frozenset[str]:
        return frozenset(re.sub(r"\([^)]*\)", " ", lb).split()) or frozenset([lb])

    groups: list[list[str]] = []
    labels_sorted = sorted(slugs)
    assigned: set[str] = set()
    for i, a in enumerate(labels_sorted):
        if a in assigned:
            continue
        ta = _tokens(a)
        group = [a]
        for b in labels_sorted[i + 1:]:
            if b in assigned:
                continue
            tb = _tokens(b)
            if len(ta & tb) / len(ta | tb) >= ALIAS_JACCARD:
                group.append(b)
                assigned.add(b)
        if len(group) > 1:
            assigned.update(group)
            groups.append(group)
    return groups


def _previous_slug_labels(wiki: Path) -> dict[str, str]:
    """직전 재생성이 남긴 슬러그→라벨 매핑."""
    try:
        payload = json.loads(
            (wiki / ENTITY_RECALL_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mapping = payload.get("slug_labels") if isinstance(payload, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {str(k): str(v) for k, v in mapping.items()}


def _record_entity_recall(
    wiki: Path, slug_labels: dict[str, str], previous: dict[str, str]
) -> list[str]:
    """슬러그 재배정을 감지해 남기고, 그 목록을 돌려준다.

    위험은 페이지가 사라지는 것이 아니다 — 재생성은 원장에 없는 페이지도
    보존한다. 실제 회귀는 같은 파일명이 **다른 대상**을 가리키게 되는
    것이다. 슬러그 배정이 디스크에 이미 있는 페이지 집합에 의존해 유입
    순서로 갈리기 때문이다(spec §5.3 실증). 외부 백링크와 보존 노트의
    귀속이 조용히 어긋난다.
    """
    reassigned = sorted(
        f"{slug}: {previous[slug]} → {label}"
        for slug, label in slug_labels.items()
        if slug in previous and previous[slug] != label
    )
    dropped = sorted(set(previous) - set(slug_labels))
    payload = {
        "slug_labels": slug_labels,
        "reassigned_count": len(reassigned),
        # 전량이 아니라 표본 — 목록이 길어져도 감사 표면이 무너지지 않는다.
        "reassigned_sample": reassigned[:20],
        "dropped_sample": dropped[:20],
    }
    try:
        write_text_atomic(
            wiki / ENTITY_RECALL_FILENAME,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    except OSError:
        pass
    return reassigned


def _split_durable(
    slugs: dict[str, str], agg: _Aggregate, page_plan
) -> tuple[list[str], list[str]]:
    # durable-only 등재 게이트: 재사용 신호(재등장·관계·대사·실서술)가 없는
    # 1회성은 후보로 분리 — 실측 1회성 93%가 지식 그래프를 역색인으로 희석
    related_labels = {n for r in agg.relations for n in (r["s"], r["o"])}

    def _durable(lb: str) -> bool:
        if len(agg.appearances.get(lb, [])) >= 2:
            return True
        if lb in agg.quotes_by_speaker or lb in related_labels:
            return True
        notes = existing_notes(page_plan.notes_sources[lb]).strip()
        return bool(notes) and notes != NOTES_PLACEHOLDER.strip()

    durable = [lb for lb in sorted(slugs) if _durable(lb)]
    candidates = [lb for lb in sorted(slugs) if lb not in set(durable)]
    return durable, candidates


def _write_index(
    wiki: Path, page_plan, slugs, agg: _Aggregate,
    counts: dict, alias_groups: list[list[str]],
    durable: list[str], candidates: list[str], ent_link,
    procedure_names: dict[str, str],
) -> Path:
    lines = _fm("index", [f"entities: {counts['entities']}"])
    lines += [
        "# 위키 인덱스", "",
        f"[← 코퍼스 인덱스](../INDEX.md) — `va wiki`로 재생성. "
        f"엔티티 {counts['entities']} · 태그 {counts['tags']} · "
        f"관계 {counts['relations']} · 대사 {counts['quotes']}", "",
        "- [태그 역색인](tags.md)", "- [관계 원장](relations.md)",
        "- [대사록](quotes.md)", "",
    ]
    # 이 위키가 담지 못한 것을 위키 자신이 밝힌다. 근거 미해소로 빠진 종결
    # 체크포인트를 세지 않으면, 빈 자리는 "그런 일이 없었다"로 읽힌다.
    omitted_total = sum(agg.omitted_unsupported.values())
    if omitted_total:
        worst = sorted(
            agg.omitted_unsupported.items(), key=lambda kv: (-kv[1], kv[0])
        )[:3]
        detail = " · ".join(f"{ws} {n}" for ws, n in worst)
        lines += [
            f"> **근거 미해소로 빠진 종결 체크포인트 {omitted_total}건** — "
            "이 위키는 근거가 지금 해소되는 주장만 싣는다. "
            f"많은 순: {detail}. "
            "`va audit`의 검증 수준(실지지)에서 전체를 본다.",
            "",
        ]
    # relation graph (DeepWiki-style architecture diagram, transposed)
    edges = sorted({(r["s"], r["p"], r["o"]) for r in agg.relations})
    MAX_EDGES = 60
    if edges:
        node_ids: dict[str, str] = {}
        for s, _, o in edges[:MAX_EDGES]:
            for n in (s, o):
                node_ids.setdefault(n, f"n{len(node_ids)}")
        lines += ["## 관계 그래프", "", "```mermaid", "graph LR"]
        for n, nid in node_ids.items():
            lines.append(f'    {nid}["{_mermaid_label(n)}"]')
        for s, p, o in edges[:MAX_EDGES]:
            label = _mermaid_label(p)
            lines.append(f'    {node_ids[s]} -->|"{label}"| {node_ids[o]}')
        lines += ["```", ""]
        if len(edges) > MAX_EDGES:
            lines += [f"(엣지 {len(edges)}개 중 {MAX_EDGES}개만 표시 — "
                      "전체는 [관계 원장](relations.md))", ""]
    if alias_groups:
        lines += ["## 통합 후보 (표기 변형 의심 — 병합 판단은 에이전트 몫)", ""]
        for g in alias_groups:
            lines.append("- " + " ≈ ".join(ent_link(lb) for lb in g)
                         + " — 같은 대상이면 정본 표기 하나로 라벨 통일")
        lines.append("")
    if agg.procedure_steps:
        # 절차는 "어떻게"의 지식 유형 — 인물·관계와 별도 축으로 노출한다.
        lines += ["## 절차", ""]
        for task in sorted(agg.procedure_steps):
            steps = agg.procedure_steps[task]
            workspaces = {step["workspace_id"] for step in steps}
            lines.append(
                f"- [{escape_markdown_text(task)}]"
                f"({md_target('procedures/' + procedure_names[task])})"
                f" (영상 {len(workspaces)} · 단계 {len(steps)})")
        lines.append("")
    lines += ["## 엔티티", ""]
    for lb in durable:
        n = len(agg.appearances.get(lb, []))
        lines.append(f"- {ent_link(lb)} ({n}회)")
    if candidates:
        lines += ["", "## 엔티티 후보 (1회성 — 재등장·관계·대사·서술이 "
                      "생기면 위로 승격)", ""]
        for lb in candidates:
            lines.append(f"- {ent_link(lb)} (1회)")
    demote_stale_entity_pages(page_plan, wiki)
    # 보관 후보도 인덱스에서 도달 가능해야 한다 — 링크 0인 격리는 그래프
    # 부유물이 되고 BFS 도달성(감사 A5 원칙)도 깨진다. 목록화가 곧 연결.
    quarantine = wiki / "hypotheses" / "entities"
    if quarantine.is_dir():
        _repair_archived_links(quarantine, agg.archived_target_of)
    archived = (
        sorted(quarantine.glob("*.md")) if quarantine.is_dir() else []
    )
    if archived:
        lines += ["", "## 보관 후보 (승격 게이트 탈락 — 재등장·관계·대사가 "
                      "생기면 복귀)", ""]
        for page in archived:
            lines.append(
                f"- [{page.stem}]"
                f"({md_target(f'hypotheses/entities/{page.name}')})")
    dest = wiki / "INDEX.md"
    write_text_atomic(dest, "\n".join(lines) + "\n")
    return dest


def build_wiki(
    roots: list[str] | None = None,
    *,
    include_hypotheses: bool = False,
    workspace_paths: Sequence[Path] | None = None,
    projection_root: Path | None = None,
) -> tuple[Path, dict]:
    """Aggregate promoted checkpoints into va-out/wiki/."""
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
    wiki = root / "wiki"
    ent_dir = wiki / "entities"
    ent_dir.mkdir(parents=True, exist_ok=True)

    # 위키는 엔티티가 여러 워크스페이스를 가로지르는 전역 집계라 부분
    # 재투영이 안 된다 — 대신 코퍼스 전체가 무변경이면 통째로 건너뛴다
    # (재개 세션의 흔한 경우). 솔트는 렌더러 소스 digest에서 자동 유도.
    wiki_salt = renderer_salt("wiki")
    fingerprint_entries = []
    for d in ws_dirs:
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            rel = d.name
        fingerprint_entries.append(
            f"{rel}={workspace_fingerprint(d, salt=wiki_salt)}"
        )
    corpus_fingerprint = hashlib.sha256(
        ("|".join(sorted(fingerprint_entries))
         + f"|hyp={include_hypotheses}").encode()
    ).hexdigest()[:16]
    cache = load_projection_cache(root)
    cached_wiki = cache.get("wiki")
    wiki_index = wiki / "INDEX.md"
    # 출력 지문까지 일치해야 스킵한다 — 위키 트리를 밖에서 건드렸다면
    # (심링크·중복 페이지·수기 노트 편집) 재빌드의 화해·fail-closed
    # 경로가 반드시 다시 돌아야 한다.
    if (
        isinstance(cached_wiki, dict)
        and cached_wiki.get("fp") == corpus_fingerprint
        and cached_wiki.get("out_fp")
        == directory_fingerprint(wiki, salt=wiki_salt)
        and isinstance(cached_wiki.get("counts"), dict)
        and wiki_index.is_file()
    ):
        return wiki_index, cached_wiki["counts"]

    # 재생성 직전의 슬러그→라벨 매핑. 같은 파일명이 다른 대상으로 넘어가면
    # 외부 백링크와 보존 노트의 귀속이 조용히 어긋난다(spec §5.3 실증).
    slug_labels_before = _previous_slug_labels(wiki)

    agg = _collect(ws_dirs, root, include_hypotheses)
    labels = set(agg.appearances) | set(agg.quotes_by_speaker)
    page_plan = plan_entity_pages(ent_dir, labels)
    slugs = {label: path.stem for label, path in page_plan.paths.items()}
    ws_link = partial(_ws_link, agg.doc_path_of, agg.display_name_of)
    ent_link = partial(_ent_link, slugs)

    _write_entity_pages(page_plan, slugs, agg, ws_link)
    _write_tags_page(wiki, agg, ws_link)
    _write_relations_page(wiki, agg, ent_link, ws_link)
    _write_quotes_page(wiki, agg, ws_link)
    procedure_names = write_procedure_pages(
        wiki, agg.procedure_steps, ws_link,
        partial(_ws_href, agg.doc_path_of),
    )
    skill_draft_names = write_skill_drafts(
        wiki, agg.procedure_steps,
        {row["task"]: row for row in procedure_maturity(agg.procedure_steps)},
        procedure_names, ws_link,
        partial(_ws_href, agg.doc_path_of),
    )

    alias_groups = _alias_groups(slugs)
    counts = {"entities": len(slugs), "tags": len(agg.tag_hits),
              "relations": len(agg.relations), "quotes": len(agg.quotes),
              "procedures": len(procedure_names),
              "skill_drafts": len(skill_draft_names),
              "alias_groups": len(alias_groups)}
    durable, candidates = _split_durable(slugs, agg, page_plan)
    dest = _write_index(wiki, page_plan, slugs, agg, counts,
                        alias_groups, durable, candidates, ent_link,
                        procedure_names)
    reassigned = _record_entity_recall(
        wiki,
        {slug: label for label, slug in slugs.items()},
        slug_labels_before,
    )
    counts["reassigned_entities"] = len(reassigned)
    cache["wiki"] = {
        "fp": corpus_fingerprint,
        "out_fp": directory_fingerprint(wiki, salt=wiki_salt),
        "counts": counts,
    }
    save_projection_cache(
        root, cache,
        live_keys={entry.split("=", 1)[0] for entry in fingerprint_entries},
    )
    return dest, counts
