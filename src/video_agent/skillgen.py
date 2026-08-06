"""P2 스킬 컴파일러 — 승격 절차를 SKILL 초안으로 컴파일 (큐 #53).

절차 정본은 "전사 골격 + 시각 복원 토큰"의 결합이므로 실행 단계는 실지지
모달리티 visual_only/cross_modal 단계만 싣는다 — supported만으론 전사 단독
지지가 통과한다(PR #96 리뷰). 전문가 자작 텍스트 튜토리얼을 통째로
학습시켜도 실행 세부 층위에서 실패한다는 외부 재현(2026-08-04 Threads
소싱)이 이 게이트의 근거다. 초안은 등록 시점부터 known-weaknesses를
의무 포함하고(P3 강화 루프의 소비 목록), 사람 승인 전 활성 금지다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from .corpus_projection import md_target
from .fsio import write_text_atomic
from .timestamps import fmt_ts_label
from .wiki_procedures import (
    _VISUAL_GROUNDED,
    ProcedureMaturity,
    ProcedureStep,
    _code_span,
    _require_procedure_directory,
    _require_procedure_page,
    procedure_slug,
)

SKILL_DRAFT_MARKER: Final = "type: tca-skill-draft"


def render_skill_draft(
    task: str,
    steps: list[ProcedureStep],
    maturity: ProcedureMaturity,
    procedure_page: str,
    ws_link: Callable[..., str],
    ws_href: Callable[..., str],
) -> str:
    """근거 도달 단계들로 사람 승인 대기 상태의 SKILL 초안을 그린다."""
    grounded = [s for s in steps if s["grounded"] in _VISUAL_GROUNDED]
    spoken_only = [s for s in steps if s["grounded"] not in _VISUAL_GROUNDED]
    lines = [
        "---",
        SKILL_DRAFT_MARKER,
        "status: draft — 사람 승인 전(자동 활성 금지)",
        f"task: {task}",
        "---",
        "",
        f"# {task} (스킬 초안)",
        "",
        "> 위키 재생성 시 다시 만들어지는 파생 문서다. 승인·수정은 이",
        "> 파일이 아니라 승격한 사본에서 한다.",
        "",
        f"재등장 {maturity['workspace_count']}개 영상 · 실행 단계 "
        f"{len(grounded)}(시각 실지지만) · 전체 근거: "
        f"[절차 페이지]({md_target(f'../procedures/{procedure_page}')})",
        "",
    ]
    if maturity["tool_consensus"]:
        lines += [
            "적용 도메인(도구 합의): " + " ".join(
                _code_span(tool) for tool in maturity["tool_consensus"]
            ) + " — 취지 정합 게이트를 통과한 단일 취지 코퍼스다. "
            "이 범위 밖 도구로의 일반화는 근거가 없다.",
            "",
        ]
    if maturity["precision_warning"]:
        lines += [
            "⚠ 실행 정밀도 미보장 — 시각 확인 단계가 절반 미만인 절차다. "
            "이 초안만으로 실행 세부(메뉴 경로·수치·명령)를 신뢰하지 말 것.",
            "",
        ]
    lines += ["## 실행 단계", ""]
    for order, step in enumerate(grounded, start=1):
        href = ws_href(
            step["workspace_id"], up=2, checkpoint_id=step["checkpoint_id"]
        )
        span_label = (
            f"{fmt_ts_label(step['start'])}–{fmt_ts_label(step['end'])}"
        )
        tokens = step.get("exact_tokens") or ()
        token_suffix = (
            " — " + " ".join(_code_span(token) for token in tokens)
            if tokens else ""
        )
        lines.append(
            f"{order}. [{span_label}]({href}) {step['situation']}"
            f"{token_suffix}"
        )
    lines += ["", "## known-weaknesses", ""]
    if spoken_only:
        lines.append(
            f"- 발화 근거만 있는 단계 {len(spoken_only)}건은 실행 세부"
            "(메뉴 경로·수치·명령)가 미보장이다:"
        )
        for step in spoken_only:
            span_label = (
                f"{fmt_ts_label(step['start'])}–{fmt_ts_label(step['end'])}"
            )
            lines.append(f"  - {span_label} {step['situation']}")
    if maturity["precision_warning"]:
        lines.append(
            "- 실행 정밀도 미보장 경고를 승계한 절차다 — 시각 검증 예제가 "
            "더 쌓이기 전에는 골격 이상을 주장하지 않는다."
        )
    lines.append(
        "- 같은 절차의 새 영상이 들어오면 이 목록을 갱신하고 초안을 "
        "재컴파일한다(P3 강화 루프의 소비 목록)."
    )
    lines.append("")
    return "\n".join(lines)


def is_skill_draft(path: Path) -> bool:
    """생성 초안인가 — 수기·승인 사본(비소유)과 표식으로 구분한다."""
    text = path.read_text(encoding="utf-8")
    return SKILL_DRAFT_MARKER in text.splitlines()[:4]


def write_skill_drafts(
    wiki: Path,
    steps_by_task: Mapping[str, list[ProcedureStep]],
    maturity_by_task: Mapping[str, ProcedureMaturity],
    procedure_names: Mapping[str, str],
    ws_link: Callable[..., str],
    ws_href: Callable[..., str],
) -> dict[str, str]:
    """스킬 후보 절차만 초안으로 재생성하고 태스크→파일명을 돌려준다.

    엔티티·절차 페이지와 같은 수명주기 안전 계약을 미러한다: 심볼릭 링크
    루트 fail-closed, 수기(비소유) 파일명 선점, 슬러그 충돌 접미사,
    이번 빌드에 없는 소유 초안만 정리(PR #97 교훈).
    """
    candidates = {
        task: steps for task, steps in steps_by_task.items()
        if maturity_by_task.get(task, {}).get("skill_candidate")
    }
    skills = wiki / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    _require_procedure_directory(skills)
    owned: set[str] = set()
    unowned: set[str] = set()
    for page in skills.glob("*.md"):
        _require_procedure_page(page)
        (owned if is_skill_draft(page) else unowned).add(page.name)
    used = {name.casefold() for name in unowned}
    names: dict[str, str] = {}
    for task in sorted(candidates):
        base = procedure_slug(task)
        slug, suffix = base, 2
        while f"{slug}.md".casefold() in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(f"{slug}.md".casefold())
        names[task] = f"{slug}.md"
    for task, name in names.items():
        write_text_atomic(
            skills / name,
            render_skill_draft(
                task, candidates[task], maturity_by_task[task],
                procedure_names.get(task, f"{procedure_slug(task)}.md"),
                ws_link, ws_href,
            ),
        )
    current = set(names.values())
    for name in owned - current:
        (skills / name).unlink()
    return names
