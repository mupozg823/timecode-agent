"""Read-only HTML detail projection of Story Map edit decisions."""

from __future__ import annotations

import html

from .corpus_projection import checkpoint_anchor, humanize_surface
from .sequence import SEQ_STATUS_KO
from .sequence_store import SequenceSourceState
from .story_projection import (
    StoryCut,
    StoryMap,
    StoryRejected,
    StorySequence,
)
from .timestamps import fmt_ts_label


def _span_label(start: float, end: float) -> str:
    return html.escape(f"{fmt_ts_label(start)}–{fmt_ts_label(end)}")


def _cut_html(cut: StoryCut, index: int) -> str:
    role = html.escape(cut["role"])
    note = html.escape(cut["note"])
    # 원장은 checkpoint_ids "또는" signals 중 하나만 있어도 유효한 근거로
    # 받는다(sequence_schema: 근거 없는 컷 금지). 체크포인트 앵커만 그리면
    # signals로만 선 컷이 근거 없는 컷처럼 보인다 — 원장이 보장한 것과 반대다.
    grounds = [
        f'<a href="#{checkpoint_anchor(cid)}">{html.escape(cid)}</a>'
        for cid in cut["checkpoint_ids"]
    ]
    grounds += [
        f'<span class="signal">'
        f"{html.escape(humanize_surface(signal))}</span>"
        for signal in cut["signals"]
    ]
    role_html = f' <span class="role">{role}</span>' if role else ""
    note_html = f'<p class="note">{note}</p>' if note else ""
    meta_html = (
        f'<p class="meta">근거 {" · ".join(grounds)}</p>' if grounds else ""
    )
    return (
        f'<li class="cut" data-cut="{index}"'
        f' data-grounding-state="{cut["grounding_state"]}"'
        f' data-start="{cut["start"]:.3f}" data-end="{cut["end"]:.3f}">'
        f'<span class="cut-span">{_span_label(cut["start"], cut["end"])}'
        f"</span>{role_html}{note_html}{meta_html}</li>"
    )


def _rejected_html(rejected: list[StoryRejected]) -> str:
    if not rejected:
        return ""
    rows = [
        f"<li>{_span_label(item['start'], item['end'])} — "
        f"{html.escape(item['reason'])}</li>"
        for item in rejected
    ]
    return ('<details class="rejected"><summary>기각된 대안 '
            f'{len(rows)}</summary><ul>{"".join(rows)}</ul></details>')


# 납품 사양 키는 원장 스키마(snake_case)가 아니라 사람 말로 보여준다.
# 모르는 키는 원문 유지 — 조용한 탈락이 없어야 사양 전체가 보인다.
_BRIEF_KEY_KO = {"platform": "플랫폼", "aspect": "화면비",
                 "target_len_s": "목표 길이", "genre": "장르",
                 "format": "포맷"}


def _brief_entry(key: str, value: object) -> str:
    label = _BRIEF_KEY_KO.get(key, key)
    shown = f"{value}초" if key == "target_len_s" else str(value)
    return f"{html.escape(label)} {html.escape(shown)}"


def _brief_html(brief: dict[str, object]) -> str:
    """납품 사양(플랫폼·화면비·목표 길이) — 컷을 어디로 내보낼지의 전제다."""
    parts = " · ".join(
        _brief_entry(key, value)
        for key, value in brief.items()
        if value is not None
    )
    return f'<p class="brief">{parts}</p>' if parts else ""


def _card_html(seq: StorySequence) -> str:
    status_ko = html.escape(SEQ_STATUS_KO.get(seq["status"], seq["status"]))
    duration = fmt_ts_label(seq["total_cut_duration"])
    head = (
        '<div class="seq-head">'
        f'<strong>{html.escape(seq["id"])}</strong>'
        f'<span class="badge">{status_ko}</span>'
        f'<span class="duration">채택 길이 {duration}</span></div>'
    )
    effect = html.escape(seq["expected_effect"])
    effect_html = f'<p class="effect">{effect}</p>' if effect else ""
    # 드리프트는 원장이 이미 아는 사실이다 — 표면이 감추면 낡은 근거로
    # 승격을 정당화하게 된다.
    drift_html = (
        '<p class="notice">근거 변경됨: '
        + " · ".join(
            html.escape(item) for item in seq["drifted_checkpoint_ids"]
        )
        + "</p>"
        if seq["drifted_checkpoint_ids"]
        else ""
    )
    override_html = (
        '<details class="overrides"><summary>사람 수정 이력 '
        f'{len(seq["human_overrides"])}</summary><ul>'
        + "".join(
            f"<li>{html.escape(item)}</li>"
            for item in seq["human_overrides"]
        )
        + "</ul></details>"
        if seq["human_overrides"]
        else ""
    )
    cut_html = "".join(
        _cut_html(cut, index) for index, cut in enumerate(seq["cuts"])
    )
    return (
        '<article class="seq">'
        f"{head}"
        f'<p class="intent">{html.escape(seq["intent"])}</p>'
        f'{_brief_html(seq["brief"])}{effect_html}{drift_html}'
        f'<ol class="cuts">{cut_html}</ol>'
        f"{override_html}"
        f'{_rejected_html(seq["rejected"])}'
        "</article>"
    )


def render_sequence_section(story: StoryMap) -> str:
    """편집 결정 섹션. 소스 상태를 구분해 그린다.

    "편집 결정이 없다"와 "원장을 못 읽었다"는 다른 사실이다 — 부재는
    빈 문자열, 나머지는 명시적 공지로 남긴다.
    """
    state = story["sequence_source_state"]
    if state is SequenceSourceState.ABSENT:
        return ""
    if state is SequenceSourceState.UNREADABLE:
        return (
            '<section class="seqs"><h2>편집 결정</h2>'
            '<p class="notice">편집 원장을 읽지 못했습니다</p></section>'
        )
    if not story["sequences"]:
        return (
            '<section class="seqs"><h2>편집 결정</h2>'
            '<p class="notice">원장은 있지만 표시 가능한 편집 결정이 없습니다'
            "</p></section>"
        )
    cards = "".join(_card_html(seq) for seq in story["sequences"])
    return ('<section class="seqs"><h2>편집 결정 '
            f'{len(story["sequences"])}</h2>{cards}</section>')
