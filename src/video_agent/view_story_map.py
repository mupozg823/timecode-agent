"""Read-only static Story Map HTML. # noqa: SIZE_OK

The exact two-locale table and embedded CSS/JavaScript assets account for the
size exception; renderer logic remains a single read-only projection.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final

from .corpus_projection import checkpoint_anchor, humanize_surface
from .story_projection import (
    GroundingState,
    StoryCheckpoint,
    StoryCut,
    StoryMap,
    StorySequence,
)
from .timestamps import fmt_ts_label


@unique
class StoryMapLocale(StrEnum):
    KOREAN = "ko"
    ENGLISH = "en"


_LABELS: Final[dict[StoryMapLocale, dict[str, str]]] = {
    StoryMapLocale.KOREAN: {
        "story_map": "스토리 맵", "memory": "메모리", "edit": "편집",
        "evidence": "근거", "rejected": "기각 대안", "duration": "채택 길이",
        "current": "현재 근거", "drifted": "근거 변경됨",
        "signal_only": "신호 근거", "unpinned": "리비전 미고정",
        "verified": "확인", "corrected": "정정", "hypothesized": "추정",
        "assembled": "조립됨", "boundary_verified": "경계 확인",
        "exported": "내보냄", "edit_order": "편집순", "excluded": "제외",
        "cut_start": "컷 시작",
        "invalid_duration": (
            "영상 길이가 유효하지 않아 비율 시간축을 표시할 수 없습니다"
        ),
    },
    StoryMapLocale.ENGLISH: {
        "story_map": "Story Map", "memory": "Memory", "edit": "Edit",
        "evidence": "Evidence", "rejected": "Rejected alternatives",
        "duration": "Selected duration", "current": "Current grounding",
        "drifted": "Evidence changed", "signal_only": "Signal grounding",
        "unpinned": "Revision unpinned", "verified": "Verified",
        "corrected": "Corrected", "hypothesized": "Hypothesized",
        "assembled": "Assembled", "boundary_verified": "Boundary verified",
        "exported": "Exported", "edit_order": "Edit order",
        "excluded": "Excluded", "cut_start": "Cut start",
        "invalid_duration": (
            "A proportional timeline is unavailable because the video "
            "duration is invalid"
        ),
    },
}

STORY_MAP_CSS: Final = (
    ".story-map{margin:0 0 18px;padding:14px;border:1px solid var(--border);"
    "border-radius:12px;background:var(--surface)}"
    ".story-scroll{overflow-x:auto;padding-bottom:6px;scrollbar-width:thin}"
    ".story-axis{position:relative;min-width:720px}"
    ".story-track{position:relative;height:30px;margin:4px 0;background:var(--bg);"
    "border-radius:6px}"
    ".story-span{position:absolute;left:var(--story-left);"
    "width:max(var(--story-width),3px);top:4px;height:22px;overflow:hidden;"
    "border:1px solid var(--border-strong);border-radius:5px;white-space:nowrap;"
    "text-overflow:ellipsis}"
    ".story-map button.story-span:focus-visible{outline:2px solid var(--accent);"
    "outline-offset:2px}"
    ".story-cut{border-style:solid}.story-rejected{border-style:dashed;opacity:.55}"
    ".story-decor{height:14px;background:none;margin:2px 0}"
    ".story-pin{position:absolute;left:var(--story-left);top:0;width:0;height:0;"
    "border-left:5px solid transparent;border-right:5px solid transparent;"
    "border-top:7px solid var(--text);transform:translateX(-50%)}"
    ".story-range{position:absolute;left:var(--story-left);"
    "width:max(var(--story-width),3px);bottom:1px;height:3px;"
    "background:var(--accent);opacity:.7;border-radius:2px}"
    ".story-excluded{position:absolute;left:var(--story-left);"
    "width:max(var(--story-width),3px);top:3px;height:8px;"
    "background:var(--border-strong);opacity:.4;border-radius:4px}"
    ".story-ribbons{margin:0 0 6px}"
    ".story-ribbon-row{position:relative;height:12px;margin:2px 0}"
    ".story-ribbon-name{position:absolute;left:0;top:-1px;font-size:12px;"
    "color:var(--muted);z-index:1}"
    ".story-ribbon{position:absolute;left:var(--story-left);"
    "width:max(var(--story-width),3px);top:4px;height:4px;border-radius:2px;"
    "background:var(--muted);opacity:.75}"
    ".story-ribbon-co{background:var(--accent)}"
    ".story-span.grounded,.story-ground.grounded,.story-pin.grounded,"
    ".story-range.grounded,.story-ribbon.grounded{"
    "outline:2px solid var(--accent)}"
    ".story-span.active{box-shadow:inset 0 0 0 2px var(--text)}"
    ".story-drifted{color:var(--warn);border-color:var(--warn)}"
    ".story-issue{margin:0 0 10px;padding:8px 10px;color:var(--warn)}"
    ".story-details{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:6px}"
    ".story-memory-row,.story-cut-row{display:grid;"
    "grid-template-columns:auto auto 1fr;gap:4px 10px;align-items:baseline;"
    "font-size:13px}.story-time{font-variant-numeric:tabular-nums;"
    # 색상은 스토리 맵 안으로 스코프 — .story-ground는 페이지의 체크포인트
    # 카드에도 훅으로 붙는데, 카드 본문까지 muted로 물들면 안 된다.
    "white-space:nowrap}.story-map .story-status,"
    ".story-map .story-ground{color:var(--muted)}"
    ".story-situation,.story-note,.story-grounds{grid-column:3;margin:0}"
    ".story-sequence{padding:10px 0;border-top:1px solid var(--border)}"
    ".story-sequence header{display:flex;flex-wrap:wrap;gap:6px 10px}"
    ".story-rejections{margin-top:8px}.story-rejections ul{padding-left:20px}"
    "@media(max-width:720px){.story-map{padding:10px}"
    ".story-memory-row,.story-cut-row{grid-template-columns:auto 1fr}"
    ".story-situation,.story-note,.story-grounds{grid-column:1/-1}}"
    "@media(prefers-reduced-motion:reduce){"
    ".story-map *{scroll-behavior:auto!important}}"
)

STORY_MAP_JS: Final = (
    "const storyCheckpoints=new Map();"
    "document.querySelectorAll('[data-checkpoint-id]').forEach(el=>{"
    "const id=el.dataset.checkpointId;"
    "if(!storyCheckpoints.has(id))storyCheckpoints.set(id,[]);"
    "storyCheckpoints.get(id).push(el);});"
    "const storyCuts=new Map();"
    "document.querySelectorAll('[data-story-cut-key]').forEach(el=>{"
    "const key=el.dataset.storyCutKey;"
    "if(!storyCuts.has(key))storyCuts.set(key,[]);storyCuts.get(key).push(el);});"
    "storyCuts.forEach(group=>{const refs=group.flatMap(el=>"
    "[...el.querySelectorAll('[data-grounding-id]')]);"
    "const setGrounding=on=>{group.forEach(el=>"
    "el.classList.toggle('grounded',on));refs.forEach(ref=>{"
    "ref.classList.toggle('grounded',on);"
    "(storyCheckpoints.get(ref.dataset.groundingId)||[]).forEach(el=>"
    "el.classList.toggle('grounded',on));});};group.forEach(el=>{"
    "el.addEventListener('pointerenter',()=>setGrounding(true));"
    "el.addEventListener('pointerleave',()=>setGrounding(false));"
    "el.addEventListener('focusin',()=>setGrounding(true));"
    "el.addEventListener('focusout',event=>{"
    "if(!group.some(node=>node.contains(event.relatedTarget)))"
    "setGrounding(false);});});});"
)


@dataclass(frozen=True, slots=True)
class _Context:
    duration: float | None
    media_available: bool
    labels: Mapping[str, str]
    checkpoint_spans: Mapping[str, tuple[float, float]]
    # 원장 텍스트(상황·신호)의 표면 순화 — 한국어 로케일 전용. 영어
    # 로케일에 한국어 대체어를 적용하면 혼합어 표면이 되고 원본 근거
    # 식별자가 사라진다(PR #94 리뷰).
    humanize: Callable[[str], str]


_GROUND_CLASS: Final[dict[GroundingState, str]] = {
    "current": "", "drifted": " story-drifted",
    "signal_only": "", "unpinned": "",
}


def _position(start: float, end: float, duration: float) -> tuple[float, float]:
    clipped_start = min(max(start, 0.0), duration)
    clipped_end = min(max(end, clipped_start), duration)
    return (
        clipped_start / duration * 100.0,
        (clipped_end - clipped_start) / duration * 100.0,
    )


def _span(start: float, end: float) -> str:
    return f"{fmt_ts_label(start)}–{fmt_ts_label(end)}"


def _style(start: float, end: float, duration: float) -> str:
    left, width = _position(start, end, duration)
    return f'style="--story-left:{left:.4f}%;--story-width:{width:.4f}%"'


def _status(context: _Context, code: str) -> str:
    return html.escape(context.labels.get(code, code))


def _marker(
    classes: str,
    attrs: str,
    label: str,
    span: tuple[float, float],
    context: _Context,
) -> str:
    """seek 마커의 단일 조립 지점 — 미디어가 없으면 정적 span.

    "죽은 seek 버튼 금지" 불변식은 여기 한 곳이다; 마커 종류가 늘 때마다
    재타이핑하지 않는다.
    """
    if context.media_available:
        return (
            f'<button type="button" class="{classes} seek" {attrs} '
            f'data-start="{span[0]:.3f}" data-end="{span[1]:.3f}">'
            f"{label}</button>"
        )
    return f'<span class="{classes}" {attrs}>{label}</span>'


def _left_style(t: float, duration: float) -> str:
    left = min(max(t, 0.0), duration) / duration * 100.0
    return f'style="--story-left:{left:.4f}%"'


def _pin(cut: StoryCut, key: str, context: _Context) -> str:
    """컷 시작점 = 결정 지점 — 증거(range)와 다른 문법으로 표시한다."""
    assert context.duration is not None
    aria = html.escape(
        f'{context.labels["cut_start"]} {fmt_ts_label(cut["start"])}'
    )
    return (f'<span class="story-pin" data-story-cut-key="{key}" '
            f'{_left_style(cut["start"], context.duration)} '
            f'aria-label="{aria}"></span>')


def _grounding_ranges(cut: StoryCut, key: str, context: _Context) -> str:
    """컷이 참조하는 체크포인트 구간 — 증거가 어디에 있는지의 range."""
    assert context.duration is not None
    parts = []
    for checkpoint_id in cut["checkpoint_ids"]:
        span = context.checkpoint_spans.get(checkpoint_id)
        if span is None:
            continue
        aria = html.escape(
            f'{context.labels["evidence"]} {checkpoint_id} '
            f'{_span(span[0], span[1])}'
        )
        parts.append(
            f'<span class="story-range" data-story-cut-key="{key}"'
            f' data-grounding-id="{checkpoint_anchor(checkpoint_id)}" '
            f'{_style(span[0], span[1], context.duration)} '
            f'aria-label="{aria}"></span>'
        )
    return "".join(parts)


def _excluded_bands(cuts: list[StoryCut], context: _Context) -> str:
    """어떤 채택 컷도 안 쓴 소스 구간 — 침묵이 아니라 명시된 사실."""
    assert context.duration is not None
    duration = context.duration
    clipped = sorted(
        (max(cut["start"], 0.0), min(cut["end"], duration))
        for cut in cuts
    )
    merged: list[list[float]] = []
    for start, end in clipped:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps, cursor = [], 0.0
    for start, end in merged:
        if start - cursor > 1e-9:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > 1e-9:
        gaps.append((cursor, duration))
    return "".join(
        f'<span class="story-excluded" {_style(start, end, duration)} '
        f'aria-label="{html.escape(context.labels["excluded"])} '
        f'{_span(start, end)}"></span>'
        for start, end in gaps
    )


def _provenance_marker(
    cut: StoryCut,
    identity: tuple[str, str],
    offsets: tuple[float, float],
    context: _Context,
) -> str:
    sequence_id, key = identity
    left, width = offsets
    aria = html.escape(
        f'{context.labels["edit_order"]} {sequence_id} {cut["order"]} '
        f'{_span(cut["start"], cut["end"])}'
    )
    style = f'style="--story-left:{left:.4f}%;--story-width:{width:.4f}%"'
    attrs = (
        f'data-story-kind="provenance-cut" data-story-cut-key="{key}" '
        f'{style} aria-label="{aria}"'
    )
    label = html.escape(cut["role"] or str(cut["order"]))
    return _marker(
        "story-span story-prov", attrs, label,
        (cut["start"], cut["end"]), context,
    )


def _checkpoint_marker(item: StoryCheckpoint, context: _Context) -> str:
    assert context.duration is not None
    span = _span(item["start"], item["end"])
    aria = html.escape(
        f'{context.labels["memory"]} {item["id"]} {span} '
        f'{context.labels.get(item["status"], item["status"])}'
    )
    attrs = (
        'data-story-kind="checkpoint" '
        f'data-checkpoint-id="{checkpoint_anchor(item["id"])}" '
        f'{_style(item["start"], item["end"], context.duration)} '
        f'aria-label="{aria}"'
    )
    return _marker(
        "story-span", attrs, html.escape(item["id"]),
        (item["start"], item["end"]), context,
    )


def _speaker_ribbons(
    items: list[StoryCheckpoint], context: _Context
) -> str:
    """화자≥2일 때만 붙는 추가 레이어 — 기존 메모리 트랙은 그대로 둔다.

    공동 등장 구간은 co 톤 + 같은 checkpoint 키로 기존 상호 하이라이트
    문법에 합류한다. 단화자에서 리본은 정보가 아니라 소음이다.
    """
    if context.duration is None:
        return ""
    speakers: dict[str, None] = {}
    for item in items:
        for speaker in item["speakers"]:
            speakers.setdefault(speaker, None)
    if len(speakers) < 2:
        return ""
    rows = []
    for speaker in speakers:
        segments = []
        for item in items:
            if speaker not in item["speakers"]:
                continue
            co = " story-ribbon-co" if len(item["speakers"]) > 1 else ""
            aria = html.escape(
                f'{speaker} {_span(item["start"], item["end"])}'
            )
            segments.append(
                f'<span class="story-ribbon{co}" '
                f'data-checkpoint-id="{checkpoint_anchor(item["id"])}" '
                f'{_style(item["start"], item["end"], context.duration)} '
                f'aria-label="{aria}"></span>'
            )
        name = html.escape(speaker)
        rows.append(
            f'<div class="story-ribbon-row" role="group" aria-label="{name}">'
            f'<span class="story-ribbon-name">{name}</span>'
            f'{"".join(segments)}</div>'
        )
    return (
        '<div class="story-ribbons"><div class="story-scroll" tabindex="0">'
        f'<div class="story-axis">{"".join(rows)}</div></div></div>'
    )


def _memory(items: list[StoryCheckpoint], context: _Context) -> str:
    parts = [f'<div class="story-memory"><h3>{context.labels["memory"]}</h3>']
    if context.duration is not None:
        parts.append(
            '<div class="story-scroll" tabindex="0"><div class="story-axis" '
            f'role="group" aria-label="{context.labels["memory"]}">'
        )
        for track in sorted({item["track"] for item in items}):
            markers = "".join(
                _checkpoint_marker(item, context)
                for item in items if item["track"] == track
            )
            parts.append(f'<div class="story-track">{markers}</div>')
        parts.append("</div></div>")
    parts.append('<ul class="story-details">')
    # 투영 계약: checkpoints는 이미 (start, end, id) 정렬로 온다 — 재정렬 불요.
    for item in items:
        item_id = html.escape(item["id"])
        speakers = " · ".join(html.escape(value) for value in item["speakers"])
        speakers = f" · {speakers}" if speakers else ""
        parts.append(
            '<li class="story-memory-row" '
            f'data-checkpoint-id="{checkpoint_anchor(item["id"])}">'
            f'<span class="story-time">{_span(item["start"], item["end"])}</span>'
            f'<span class="story-status">{_status(context, item["status"])}</span>'
            f'<strong>{item_id}</strong><p class="story-situation">'
            f'{html.escape(context.humanize(item["situation"]))}'
            f"{speakers}</p></li>"
        )
    parts.append("</ul></div>")
    return "".join(parts)


def _grounds(cut: StoryCut, context: _Context) -> str:
    refs = [
        f'<a class="story-ground" data-grounding-id="{checkpoint_anchor(item)}" '
        f'href="#{checkpoint_anchor(item)}">{html.escape(item)}</a>'
        for item in cut["checkpoint_ids"]
    ]
    refs.extend(
        f'<span class="story-ground">'
        f"{html.escape(context.humanize(item))}</span>"
        for item in cut["signals"]
    )
    return " · ".join(refs)


def _cut_marker(
    cut: StoryCut, identity: tuple[str, str], context: _Context
) -> str:
    assert context.duration is not None
    sequence_id, key = identity
    state, state_class = cut["grounding_state"], _GROUND_CLASS[cut["grounding_state"]]
    aria = html.escape(
        f'{context.labels["edit"]} {sequence_id} {cut["order"]} '
        f'{_span(cut["start"], cut["end"])} {context.labels[state]}'
    )
    attrs = (
        f'data-story-kind="cut" data-sequence-id="{html.escape(sequence_id)}" '
        f'data-grounding-state="{state}" data-story-cut-key="{key}" '
        f'{_style(cut["start"], cut["end"], context.duration)} aria-label="{aria}"'
    )
    label = html.escape(cut["role"] or str(cut["order"]))
    return _marker(
        f"story-span story-cut{state_class}", attrs, label,
        (cut["start"], cut["end"]), context,
    )


def _sequence(item: StorySequence, index: int, context: _Context) -> str:
    item_id = html.escape(item["id"])
    duration = fmt_ts_label(item["total_cut_duration"])
    parts = [
        f'<article class="story-sequence" data-sequence-id="{item_id}"><header>',
        f'<strong>{item_id}</strong><span class="story-status">'
        f'{_status(context, item["status"])}</span>',
        f'<span>{context.labels["duration"]} {duration}</span></header>',
        f'<p class="story-intent">{html.escape(item["intent"])}</p>',
    ]
    if context.duration is not None:
        markers = [
            _cut_marker(cut, (item["id"], f"{index}:{cut_index}"), context)
            for cut_index, cut in enumerate(item["cuts"])
        ]
        for rejected in item["rejected"]:
            aria = html.escape(
                f'{context.labels["rejected"]} '
                f'{_span(rejected["start"], rejected["end"])} {rejected["reason"]}'
            )
            markers.append(
                '<span class="story-span story-rejected" '
                f'{_style(rejected["start"], rejected["end"], context.duration)} '
                f'aria-label="{aria}">{context.labels["rejected"]}</span>'
            )
        decor = []
        for cut_index, cut in enumerate(item["cuts"]):
            key = f"{index}:{cut_index}"
            decor.append(_pin(cut, key, context))
            decor.append(_grounding_ranges(cut, key, context))
        decor.append(_excluded_bands(item["cuts"], context))
        prov_blocks, acc = [], 0.0
        total = item["total_cut_duration"]
        if total > 0:
            for cut_index, cut in enumerate(item["cuts"]):
                share = (cut["end"] - cut["start"]) / total
                prov_blocks.append(_provenance_marker(
                    cut,
                    (item["id"], f"{index}:{cut_index}"),
                    (acc * 100.0, share * 100.0),
                    context,
                ))
                acc += share
        prov_track = (
            '<div class="story-track story-provenance" role="group" '
            f'aria-label="{context.labels["edit_order"]}">'
            f'{"".join(prov_blocks)}</div>'
            if prov_blocks else ""
        )
        parts.append(
            '<div class="story-scroll" tabindex="0"><div class="story-axis">'
            f'<div class="story-track">{"".join(markers)}</div>'
            f'<div class="story-track story-decor">{"".join(decor)}</div>'
            f"{prov_track}</div></div>"
        )
    parts.append('<ol class="story-details">')
    for cut_index, cut in enumerate(item["cuts"]):
        state, grounds = cut["grounding_state"], _grounds(cut, context)
        role = f'<strong>{html.escape(cut["role"])}</strong>' if cut["role"] else ""
        note = (
            f'<p class="story-note">{html.escape(cut["note"])}</p>'
            if cut["note"] else ""
        )
        evidence = (
            f'<p class="story-grounds">{context.labels["evidence"]} {grounds}</p>'
            if grounds else ""
        )
        parts.append(
            f'<li class="story-cut-row{_GROUND_CLASS[state]}" '
            f'data-story-cut-key="{index}:{cut_index}" data-sequence-id="{item_id}" '
            f'data-grounding-state="{state}"><span class="story-time">'
            f'{_span(cut["start"], cut["end"])}</span><span class="story-status">'
            f'{context.labels[state]}</span>{role}{note}{evidence}</li>'
        )
    parts.append("</ol>")
    if item["rejected"]:
        rejected = "".join(
            f'<li class="story-rejected">{_span(row["start"], row["end"])} '
            f'— {html.escape(row["reason"])}</li>' for row in item["rejected"]
        )
        parts.append(
            f'<details class="story-rejections"><summary>{context.labels["rejected"]} '
            f'{len(item["rejected"])}</summary><ul>{rejected}</ul></details>'
        )
    parts.append("</article>")
    return "".join(parts)


def render_story_map(
    story: StoryMap,
    *,
    media_available: bool,
    locale: StoryMapLocale = StoryMapLocale.KOREAN,
) -> str:
    """Render one semantic, static Story Map without mutating its read model."""
    duration_issue = any(
        issue.get("code") == "invalid_duration" for issue in story["issues"]
    )
    if not story["checkpoints"] and not story["sequences"] and not duration_issue:
        return ""
    duration = (
        story["duration"]
        if story["duration"] > 0 and not duration_issue
        else None
    )
    context = _Context(
        duration,
        media_available,
        _LABELS[locale],
        {
            item["id"]: (item["start"], item["end"])
            for item in story["checkpoints"]
        },
        (humanize_surface
         if locale is StoryMapLocale.KOREAN
         else lambda text: text),
    )
    parts = [
        '<section class="story-map" aria-labelledby="story-map-title">',
        f'<h2 id="story-map-title">{context.labels["story_map"]}</h2>',
    ]
    if duration_issue:
        parts.append(
            f'<p class="story-issue" role="status">'
            f'{context.labels["invalid_duration"]}</p>'
        )
    if story["checkpoints"]:
        parts.append(_speaker_ribbons(story["checkpoints"], context))
        parts.append(_memory(story["checkpoints"], context))
    if story["sequences"]:
        parts.append(f'<div class="story-edit"><h3>{context.labels["edit"]}</h3>')
        parts.extend(
            _sequence(item, index, context)
            for index, item in enumerate(story["sequences"])
        )
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)
