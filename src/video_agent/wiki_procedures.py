"""절차 지식 계층 — task-* 라벨 체크포인트의 위키 절차 페이지와 성숙도.

지식→스킬 트랙 P1(future-queue #52). 절차 정본은 "전사 골격 + 시각 복원
토큰"의 결합(docs/eval/2026-08-03-blender-procedure-extraction-bench.md)
이므로 단계는 근거 도달(supported) 체크포인트에서만 모으고, 시각 실지지
비율을 병기해 P2 skillgen 소스 게이트(grounded visual_only/cross_modal
한정)의 판단 수치를 미리 드러낸다. 재등장 임계는 "독립 출처 상호 확증
2편 + 1"의 초기 휴리스틱 — 파일럿 실데이터로 캘리브레이션한다.
"""

from __future__ import annotations

import math
import re
import stat
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final, TypedDict

from .checkpoint_schema import CheckpointObject
from .corpus_projection import humanize_surface, string_values
from .fsio import write_text_atomic
from .timestamps import fmt_ts_label

TASK_TAG_PREFIX: Final = "task-"
TOOL_TAG_PREFIX: Final = "tool-"
SKILL_CANDIDATE_MIN_WORKSPACES: Final = 3
# 시각 확인 단계가 이 비율 미만이면 "실행 정밀도 미보장" — 골격 위주
# 절차의 스킬화는 실행 세부에서 실패한다(P0 벤치 층위 분리의 외부 재현,
# 2026-08-04 Threads 소싱). 초기 휴리스틱, 파일럿 데이터로 캘리브레이션.
EXECUTION_PRECISION_MIN_VISUAL_RATIO: Final = 0.5
# 취지(도메인) 정합 게이트 — 서로 다른 취지의 소스가 한 절차에 섞이면
# 실행 수치가 조용히 오염된다(단일 소스 규약: 값은 단일 취지 코퍼스에서만,
# 타 소스는 기법만 — 애니메 헤드 실사용 실패 원장의 "혼입" 계열 일반화).
# 신호는 결정적으로 tool-* 태그에서 읽는다: 도구 태그를 가진 지지
# 워크스페이스의 엄격 과반이 공유하는 도구가 합의이고, 도구 태그가 있는데
# 합의와 전혀 겹치지 않는 워크스페이스가 취지 이탈이다. 태그가 아예 없는
# 워크스페이스는 이탈의 증거가 없으므로 세지 않는다(모르는 것을 위반으로
# 세지 않는다 — evidence_of와 같은 기본값 방향).

# 근거 종류를 기계 감사 가능한 열거로 못박는다 — 추정이 문제가 아니라
# "추정인 줄 모르는 것"이 문제다(GREYBOX 설계 §11.7 provenance 필수 열거의
# 이식). 새 추론은 하지 않는다: 값은 실지지 모달리티에서만 결정적으로 도출.
EVIDENCE_VISUAL_VERIFIED: Final = "visual_verified"
EVIDENCE_SPEECH_ONLY: Final = "speech_only"
EVIDENCE_AGENT_ESTIMATED: Final = "agent_estimated"
EVIDENCE_CODES: Final = (
    EVIDENCE_VISUAL_VERIFIED,
    EVIDENCE_SPEECH_ONLY,
    EVIDENCE_AGENT_ESTIMATED,
)
_EVIDENCE_BY_GROUNDED: Final = {
    "visual_only": EVIDENCE_VISUAL_VERIFIED,
    "cross_modal": EVIDENCE_VISUAL_VERIFIED,
    "transcript_only": EVIDENCE_SPEECH_ONLY,
}
# 시각 실지지 판정은 evidence 표와 같은 원본에서 파생한다 — 두 표가 갈라지면
# 페이지의 열거와 성숙도 비율이 조용히 다른 말을 한다.
_VISUAL_GROUNDED: Final = frozenset(
    grounded for grounded, evidence in _EVIDENCE_BY_GROUNDED.items()
    if evidence == EVIDENCE_VISUAL_VERIFIED
)
# 사람 표면 표기 — 시각으로 실지지된 단계와 전사 근거 단계를 구분한다.
_GROUNDED_KO: Final = {
    "visual_only": "시각 확인",
    "cross_modal": "시각 확인",
    "transcript_only": "발화 근거",
}
EVIDENCE_KO: Final = {
    EVIDENCE_VISUAL_VERIFIED: "시각 확증",
    EVIDENCE_SPEECH_ONLY: "발화 단독",
    EVIDENCE_AGENT_ESTIMATED: "에이전트 추정",
}


def evidence_of(grounded: str) -> str:
    """실지지 모달리티 → evidence 열거.

    표에 없는 값(unsupported·미상)은 추정으로 내려앉는다 — 모르는 근거를
    근거로 세지 않는 쪽이 기본값이어야 한다.
    """
    return _EVIDENCE_BY_GROUNDED.get(grounded, EVIDENCE_AGENT_ESTIMATED)


class ProcedureStep(TypedDict):
    workspace_id: str
    checkpoint_id: str
    start: float
    end: float
    situation: str
    grounded: str
    evidence: str
    tools: list[str]
    exact_tokens: list[str]


class ProcedureMaturity(TypedDict):
    task: str
    workspace_count: int
    step_count: int
    visually_grounded_ratio: float
    tool_consensus: list[str]
    offdomain_workspaces: list[str]
    intent_coherent: bool
    skill_candidate: bool
    precision_warning: bool


def _step_span(checkpoint: CheckpointObject) -> tuple[float, float] | None:
    raw = checkpoint.get("span")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    start, end = raw
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return None
    if isinstance(end, bool) or not isinstance(end, (int, float)):
        return None
    return float(start), float(end)


def collect_procedure_steps(
    workspace_id: str,
    checkpoints: Iterable[CheckpointObject],
    grounded_of: Mapping[str, str],
) -> dict[str, list[ProcedureStep]]:
    """한 워크스페이스의 task-* 체크포인트를 태스크별·시간순 단계로 모은다.

    호출자는 근거 도달(supported) 체크포인트만 넘겨야 한다 — 추정 단계가
    절차 페이지에 실리면 "원장이 보장한 절차"라는 계약이 깨진다.
    """
    grouped: dict[str, list[ProcedureStep]] = {}
    for checkpoint in checkpoints:
        checkpoint_id = checkpoint.get("id")
        span = _step_span(checkpoint)
        if not isinstance(checkpoint_id, str) or span is None:
            continue
        tags = string_values(checkpoint.get("tags"))
        tasks = [t[len(TASK_TAG_PREFIX):] for t in tags
                 if t.startswith(TASK_TAG_PREFIX) and len(t) > len(TASK_TAG_PREFIX)]
        if not tasks:
            continue
        tools = [t[len(TOOL_TAG_PREFIX):] for t in tags
                 if t.startswith(TOOL_TAG_PREFIX) and len(t) > len(TOOL_TAG_PREFIX)]
        raw_situation = checkpoint.get("situation")
        raw_hypothesis = checkpoint.get("hypothesis")
        if isinstance(raw_situation, str) and raw_situation:
            situation = humanize_surface(raw_situation)
        elif isinstance(raw_hypothesis, str):
            situation = humanize_surface(raw_hypothesis)
        else:
            situation = ""
        grounded = str(grounded_of.get(checkpoint_id, "unsupported"))
        step: ProcedureStep = {
            "workspace_id": workspace_id,
            "checkpoint_id": checkpoint_id,
            "start": span[0],
            "end": span[1],
            "situation": situation,
            "grounded": grounded,
            "evidence": evidence_of(grounded),
            "tools": tools,
            "exact_tokens": [
                token for token in string_values(
                    checkpoint.get("exact_tokens")
                ) if token.strip()
            ],
        }
        for task in tasks:
            grouped.setdefault(task, []).append(step)
    for steps in grouped.values():
        steps.sort(key=lambda s: (s["workspace_id"], s["start"],
                                  s["checkpoint_id"]))
    return grouped


def merge_procedure_steps(
    into: dict[str, list[ProcedureStep]],
    part: dict[str, list[ProcedureStep]],
) -> None:
    for task, steps in part.items():
        into.setdefault(task, []).extend(steps)


def tool_coherence(
    steps: Iterable[ProcedureStep],
) -> tuple[list[str], list[str]]:
    """(도구 합의, 취지 이탈 워크스페이스) — 결정적 취지 신호.

    합의 = 도구 태그 보유 워크스페이스의 엄격 과반이 공유하는 도구.
    태그 보유 2편 이상인데 합의가 비면 공유 핵이 없는 갈라진 코퍼스이므로
    전체가 이탈이다.
    """
    tools_by_ws: dict[str, set[str]] = {}
    for step in steps:
        tools_by_ws.setdefault(step["workspace_id"], set()).update(
            step["tools"]
        )
    tagged = {ws: tools for ws, tools in tools_by_ws.items() if tools}
    if not tagged:
        return [], []
    threshold = math.floor(len(tagged) / 2) + 1
    counts: Counter[str] = Counter()
    for tools in tagged.values():
        counts.update(tools)
    consensus = sorted(t for t, n in counts.items() if n >= threshold)
    if not consensus:
        return [], sorted(tagged) if len(tagged) >= 2 else []
    offdomain = sorted(
        ws for ws, tools in tagged.items()
        if not tools.intersection(consensus)
    )
    return consensus, offdomain


def procedure_maturity(
    steps_by_task: Mapping[str, list[ProcedureStep]],
) -> list[ProcedureMaturity]:
    """태스크별 성숙도 — "많이 쌓였다"를 감이 아니라 숫자로 판정한다."""
    rows: list[ProcedureMaturity] = []
    for task in sorted(steps_by_task):
        steps = steps_by_task[task]
        workspaces = {step["workspace_id"] for step in steps}
        visual = sum(
            1 for step in steps if step["grounded"] in _VISUAL_GROUNDED
        )
        consensus, offdomain = tool_coherence(steps)
        rows.append({
            "task": task,
            "workspace_count": len(workspaces),
            "step_count": len(steps),
            "visually_grounded_ratio": (
                round(visual / len(steps), 4) if steps else 0.0
            ),
            "tool_consensus": consensus,
            "offdomain_workspaces": offdomain,
            "intent_coherent": not offdomain,
            "skill_candidate": (
                len(workspaces) >= SKILL_CANDIDATE_MIN_WORKSPACES
                and not offdomain
            ),
            "precision_warning": _precision_warning(visual, len(steps)),
        })
    return rows


def route_verdict(row: ProcedureMaturity) -> str:
    """스킬 라우팅 판정 — 게이트 미달 사유를 한 단어로.

    영상을 넣은 뒤 "이 코퍼스가 어느 스킬을 어디까지 전진시켰나"에
    답하는 표면(va skillgen --route)의 판정 열이다.
    """
    if row["skill_candidate"]:
        return "draft"
    if not row["intent_coherent"]:
        return "blocked-intent"
    return (
        f"needs-workspaces({row['workspace_count']}"
        f"/{SKILL_CANDIDATE_MIN_WORKSPACES})"
    )


def _precision_warning(visual: int, step_count: int) -> bool:
    """시각 확인 단계가 절반 미만인가 — 반올림 전 원값으로 판정한다."""
    return step_count > 0 and (
        visual < step_count * EXECUTION_PRECISION_MIN_VISUAL_RATIO
    )


class ProcedureEvidenceRow(TypedDict):
    task: str
    step_count: int
    counts: dict[str, int]
    ratios: dict[str, float]


class ProcedureEvidenceAudit(TypedDict):
    """스텝 evidence 분포 — 게이트가 아니라 보고 축이다.

    agent_estimated가 있다는 사실 자체는 실패가 아니다. 실패는 그것이
    보이지 않는 것이므로 임계도 종료코드도 걸지 않는다.
    """

    step_count: int
    counts: dict[str, int]
    ratios: dict[str, float]
    by_task: list[ProcedureEvidenceRow]


def _evidence_counts(
    steps: Iterable[ProcedureStep],
) -> tuple[dict[str, int], dict[str, float]]:
    counts: dict[str, int] = dict.fromkeys(EVIDENCE_CODES, 0)
    for step in steps:
        code = step["evidence"]
        counts[code] = counts.get(code, 0) + 1
    total = sum(counts.values())
    ratios = {
        code: (round(count / total, 4) if total else 0.0)
        for code, count in counts.items()
    }
    return counts, ratios


def procedure_evidence_audit(
    steps_by_task: Mapping[str, list[ProcedureStep]],
) -> ProcedureEvidenceAudit:
    """페이지(태스크)별·전체 evidence 분포를 센다."""
    rows: list[ProcedureEvidenceRow] = []
    for task in sorted(steps_by_task):
        counts, ratios = _evidence_counts(steps_by_task[task])
        rows.append({
            "task": task,
            "step_count": sum(counts.values()),
            "counts": counts,
            "ratios": ratios,
        })
    counts, ratios = _evidence_counts(
        step for task in sorted(steps_by_task) for step in steps_by_task[task]
    )
    return {
        "step_count": sum(counts.values()),
        "counts": counts,
        "ratios": ratios,
        "by_task": rows,
    }


class UnverifiedTokenRow(TypedDict):
    task: str
    workspace_id: str
    checkpoint_id: str
    tokens: list[str]


class UnverifiedTokenAudit(TypedDict):
    """시각 미확정 토큰 — 발화 단독 근거 단계에 실린 exact_tokens.

    `exact_tokens`라는 이름은 "화면에서 확정한 문자열"로 읽힌다. 그러나
    evidence=speech_only 단계에서 그 토큰의 근거는 들린 말뿐이다 — 실측
    사례(cp-020)에서 토큰 `Multiply`의 유일한 프레임 근거는 정작 `Mix`가
    하이라이트된 순간이었다. 이름이 시각 확정을 암시하는 동안 사람도
    다음 파이프라인도 그것을 확정으로 읽는다.

    분포와 마찬가지로 게이트가 아니라 보고 축이다 — 발화 근거 토큰이
    있다는 사실이 아니라 그것이 구분되지 않는 것이 실패다.
    """

    token_count: int
    step_count: int
    rows: list[UnverifiedTokenRow]


# 목록만 자르는 표시 상한 — 총계(token_count/step_count)는 무절단.
_UNVERIFIED_TOKEN_DETAIL_CAP: Final = 20


def unverified_token_audit(
    steps_by_task: Mapping[str, list[ProcedureStep]],
) -> UnverifiedTokenAudit:
    """발화 단독 근거 단계가 실은 exact_tokens를 태스크·시간순으로 센다."""
    rows: list[UnverifiedTokenRow] = []
    token_count = 0
    for task in sorted(steps_by_task):
        for step in steps_by_task[task]:
            tokens = step.get("exact_tokens") or []
            if step["evidence"] != EVIDENCE_SPEECH_ONLY or not tokens:
                continue
            token_count += len(tokens)
            rows.append({
                "task": task,
                "workspace_id": step["workspace_id"],
                "checkpoint_id": step["checkpoint_id"],
                "tokens": list(tokens),
            })
    return {
        "token_count": token_count,
        "step_count": len(rows),
        "rows": rows[:_UNVERIFIED_TOKEN_DETAIL_CAP],
    }


def procedure_slug(task: str) -> str:
    """결정적 파일명 — 태그는 이미 공백 없는 형태라 위험 문자만 걷어낸다."""
    slug = re.sub(r'[\\/:*?"<>|#%\s]+', "-", task).strip("-")
    return slug or "task"


def _code_span(text: str) -> str:
    """백틱이 든 토큰도 깨지지 않는 Markdown 코드 스팬(CommonMark 규칙).

    펜스는 내부 최장 백틱 런보다 길어야 한다 — 고정 2중 펜스는 `` 포함
    토큰에서 스팬이 닫혀버린다(PR #98 리뷰).
    """
    longest_run = max(
        (len(run.group()) for run in re.finditer(r"`+", text)), default=0
    )
    fence = "`" * (longest_run + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def render_procedure_page(
    task: str,
    steps: list[ProcedureStep],
    ws_link: Callable[..., str],
    ws_href: Callable[..., str],
) -> str:
    workspaces: dict[str, list[ProcedureStep]] = {}
    for step in steps:
        workspaces.setdefault(step["workspace_id"], []).append(step)
    visual = sum(1 for s in steps if s["grounded"] in _VISUAL_GROUNDED)
    counts, _ratios = _evidence_counts(steps)
    consensus, offdomain = tool_coherence(steps)
    lines = [
        "---",
        "type: tca-wiki-procedure",
        # 프론트매터에 열거를 그대로 싣는다 — 사람 문장("시각 확인")만으로는
        # 어느 표면도 이 분포를 세지 못한다.
        "evidence: " + " ".join(
            f"{code}={counts.get(code, 0)}" for code in EVIDENCE_CODES
        ),
        "---",
        "",
        f"# {task}",
        "",
        f"재등장 {len(workspaces)}개 영상 · 단계 {len(steps)} · "
        f"시각 실지지 {visual}/{len(steps)}"
        + (
            " · 도구 합의 " + " ".join(_code_span(t) for t in consensus)
            if consensus else ""
        ),
        "",
    ]
    if _precision_warning(visual, len(steps)):
        lines.append(
            "⚠ 실행 정밀도 미보장 — 시각 확인 단계가 절반 미만이다. "
            "발화 근거만으로는 메뉴 경로·수치·명령 같은 실행 세부를 "
            "보장하지 않는다."
        )
        lines.append("")
    if offdomain:
        lines.append(
            "⚠ 취지 이탈 소스 — 도구 합의와 겹치지 않는 워크스페이스: "
            + ", ".join(offdomain)
            + ". 이 절차의 값·수치는 단일 취지 코퍼스에서만 취한다"
            "(혼입 방어) — 이탈 소스의 태그를 정정하거나 별도 태스크로 "
            "분리하기 전에는 스킬 후보로 승격되지 않는다."
        )
        lines.append("")
    for workspace_id in sorted(workspaces):
        lines.append(
            "## " + ws_link(workspace_id, up=2)
        )
        lines.append("")
        for order, step in enumerate(workspaces[workspace_id], start=1):
            marker = _GROUNDED_KO.get(step["grounded"], "근거 확인 필요")
            href = ws_href(
                workspace_id, up=2, checkpoint_id=step["checkpoint_id"]
            )
            span_label = (
                f"{fmt_ts_label(step['start'])}–{fmt_ts_label(step['end'])}"
            )
            tokens = step.get("exact_tokens") or ()
            token_suffix = (
                " · " + " ".join(_code_span(token) for token in tokens)
                if tokens else ""
            )
            lines.append(
                f"{order}. [{span_label}]({href}) "
                f"{step['situation']} — {marker}"
                f"({step['evidence']}){token_suffix}"
            )
        lines.append("")
    return "\n".join(lines)


_OWNERSHIP_MARKER: Final = "type: tca-wiki-procedure"


def _require_procedure_directory(path: Path) -> None:
    """심볼릭 링크·비디렉토리 절차 루트는 순회 전에 거부한다(fail-closed).

    링크를 따라가면 쓰기·스테일 정리가 코퍼스 밖 파일을 향한다 — 엔티티
    페이지 수명주기와 같은 규약(PR #97 리뷰).
    """
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise OSError(
            f"unsafe wiki procedure directory (not a directory): {path}"
        )


def _require_procedure_page(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError(
            f"unsafe wiki procedure page (not a regular file): {path}"
        )


def _owned_page(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return _OWNERSHIP_MARKER in text.splitlines()[:4]


def write_procedure_pages(
    wiki: Path,
    steps_by_task: Mapping[str, list[ProcedureStep]],
    ws_link: Callable[..., str],
    ws_href: Callable[..., str],
) -> dict[str, str]:
    """절차 페이지를 재생성하고 태스크→파일명 매핑을 돌려준다.

    수기(비소유) 페이지의 이름은 결코 차지하지 않고, 슬러그가 겹치는
    서로 다른 태스크는 결정적 접미사(-2, -3…)로 분리하며, 이번 빌드에
    없는 소유 페이지만 걷어낸다.
    """
    procedures = wiki / "procedures"
    procedures.mkdir(parents=True, exist_ok=True)
    _require_procedure_directory(procedures)
    owned: set[str] = set()
    unowned: set[str] = set()
    for page in procedures.glob("*.md"):
        _require_procedure_page(page)
        (owned if _owned_page(page) else unowned).add(page.name)
    used = {name.casefold() for name in unowned}
    names: dict[str, str] = {}
    for task in sorted(steps_by_task):
        base = procedure_slug(task)
        slug, suffix = base, 2
        while f"{slug}.md".casefold() in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(f"{slug}.md".casefold())
        names[task] = f"{slug}.md"
    for task, name in names.items():
        write_text_atomic(
            procedures / name,
            render_procedure_page(
                task, steps_by_task[task], ws_link, ws_href
            ),
        )
    current = set(names.values())
    for name in owned - current:
        (procedures / name).unlink()
    return names
