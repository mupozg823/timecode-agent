"""One-call agent briefing: everything the loop needs to (re)start.

Replaces four separate reads (manifest, transcript stats, signal files,
status) with one compact summary — the structural call-timing fix from
dogfooding rounds 1-4.
"""

from __future__ import annotations

import re
import unicodedata

from .status import workspace_status
from .transcript_segments import load_transcript_segments
from .workspace import Workspace, load_json


def build_brief(ws: Workspace, why: str | None = None) -> str:
    m = ws.manifest
    lines = [
        f"video: {ws.video.name}  {m['width']}x{m['height']}  "
        f"{m['duration']:.1f}s  fps={m['fps']:.0f}",
    ]
    if why:
        lines.extend(_intent_lines(ws, why))

    chapters = m.get("chapters")
    if chapters:
        head = " · ".join(
            f"{c['start']:.0f}s {c['title'] or '(무제)'}" for c in chapters[:6])
        lines.append(
            f"chapters: {len(chapters)}개(컨테이너 메타) — {head}"
            + (" …" if len(chapters) > 6 else "")
            + " — 의미 단위 사전 지도: 체크포인트 span 초안으로 사용")

    segs = load_transcript_segments(ws.transcript_path)
    speech = 0.0
    if segs:
        speech = sum(s["end"] - s["start"] for s in segs)
        low = 0
        for segment in segs:
            confidence = segment.get("conf")
            if (
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and confidence < 0.6
            ):
                low += 1
        lines.append(
            f"transcript: segments: {len(segs)}  speech: {speech:.0f}s "
            f"({speech / m['duration']:.0%})  low-conf(<0.6): {low}  "
            f"source: {m.get('transcript_source', '?')}"
        )
    else:
        lines.append("transcript: 없음 (또는 빈 전사)")
    lines.extend(_hotwords_lines(m))

    hl = load_json(ws.root / "highlights.json")
    if hl is not None:
        top = ", ".join(
            f"{h['start']:.0f}-{h['end']:.0f}s(score {h['score']})"
            for h in hl[:3]
        ) or "없음"
        lines.append(f"highlights: {len(hl)}개 — top: {top}")
    sc = load_json(ws.root / "scenes.json")
    if sc is not None:
        head = ", ".join(f"{s['t']:.0f}s" for s in sc[:5])
        lines.append(f"scenes: {len(sc)}개 {('— ' + head) if head else ''}")
    ae = load_json(ws.root / "audio_events.json")
    if ae:
        top = sorted(ae, key=lambda e: -(e["end"] - e["start"]))[:3]
        lines.append("audio_events: " + ", ".join(
            f"{e['label']} {e['start']:.0f}-{e['end']:.0f}s" for e in top))

    ocr_t = load_json(ws.root / "ocr_transcript.json")
    if ocr_t:
        lines.append(f"ocr_transcript: {len(ocr_t)}개 캡션 구간 "
                     f"({ocr_t[0]['start']:.0f}-{ocr_t[-1]['end']:.0f}s)")
    corr_path = ws.root / "corrections.jsonl"
    if corr_path.is_file():
        n = sum(1 for line in corr_path.read_text().splitlines()
                if line.strip())
        if n:
            lines.append(f"corrections: {n}건 — 세션 종료 시 `va glossary` 갱신")

    # 근거 기반(grounded) readiness가 표시 정본 — coverage-only 게이트는
    # readiness_legacy로만 병존한다 (CONTEXT.md 용어 정본).
    st = workspace_status(ws)
    c = st["counts"]
    lines.append(
        f"checkpoints: hypothesized={c['hypothesized']} "
        f"verified={c['verified']} corrected={c['corrected']}  "
        f"covered {st['covered_ratio']:.0%} / verified {st['verified_ratio']:.0%}"
    )
    if st["gaps"]:
        lines.append("gaps: " + ", ".join(f"{s:.0f}-{e:.0f}s"
                                          for s, e in st["gaps"][:5]))

    mode, rationale = recommend_mode(m, segs, speech, sc, hl, ocr_t)
    lines.append(f"mode: {mode}")
    lines.append(f"mode-signals: {rationale} — 추천은 placement 신호, "
                 f"세부 유형 확정은 에이전트 몫")

    r = st.get("readiness")
    if r:
        lines.append(f"readiness: {r['status']} (score {r['score']}) — "
                     f"{r['recommendation']}")
    return "\n".join(lines)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _intent_lines(ws: Workspace, why: str) -> list[str]:
    """의도 용어와 전사/체크포인트의 결정적 어휘 매치 — placement 신호.

    분석 목적을 먼저 받아 관련 구간을 표면화하면 재개/질의 세션의 첫
    캡처가 목적 지점으로 간다. 매치는 어휘 수준일 뿐 의미 확정이
    아니므로 선별·검증은 에이전트 몫(placement/selection 분리).
    """
    from .glossary import _norm

    # 질문형 입력("테스트?")의 구두점·대소문자·유니코드 조합 차이가 매치를
    # 막지 않도록: 단어 문자만 토큰화 → 조사 스트립 → NFC+casefold 대조.
    terms = [
        _fold(t)
        for token in re.findall(
            r"[0-9A-Za-z가-힣]+", unicodedata.normalize("NFC", why))
        if len(t := _norm(token)) >= 2
    ]
    lines = [f"intent: {why}"]

    segs = load_transcript_segments(ws.transcript_path)
    scored = []
    for s in segs:
        text = s.get("text") or ""
        folded = _fold(text)
        count = sum(1 for t in terms if t in folded)
        if count:
            scored.append((-count, s["start"], s["end"], text))
    scored.sort()
    if scored:
        head = ", ".join(
            f"{start:.0f}-{end:.0f}s \"{text.strip()[:24]}\""
            for _, start, end, text in scored[:3])
        lines.append(f"intent-hits: {head} — 어휘 매치(placement), 확정은 검증 후")

    from .checkpoints import load_checkpoints

    cps = []
    for cp in load_checkpoints(ws):
        hay = _fold(f"{cp.get('id', '')} {cp.get('hypothesis') or ''}")
        if any(t in hay for t in terms):
            cps.append(cp)
    if cps:
        lines.append("intent-checkpoints: " + ", ".join(
            f"{cp.get('id')} \"{(cp.get('hypothesis') or '')[:24]}\""
            f" ({cp.get('status')})" for cp in cps[:3]))
    if len(lines) == 1:
        lines.append("intent-hits: 전사·체크포인트 매치 없음 — "
                     "시각 검증 경로(필름스트립 조망→구간 캡처) 권장")
    return lines


def _hotwords_lines(m) -> list[str]:
    """전사 설정 계보 표면 — 스탬프와 현행 글로서리의 세대 대조.

    whisper 전사가 아니면(자막·무음성) 계보 표시가 무의미하므로 침묵한다.
    """
    from .glossary import load_hotwords  # runtime: 테스트가 경로 재지정 가능
    from .priors import prior_hotwords

    if m.get("transcript_source") != "whisper":
        return []
    # 드리프트가 묻는 것은 "코퍼스 글로서리가 전사 이후 갱신됐는가"다.
    # 적용값에는 이 영상의 사전지식도 섞여 있으므로 그 몫을 뺀 뒤 비교한다
    # (프라이어는 제목에서 오므로 세대가 바뀌지 않는다).
    shared = load_hotwords() or ""
    prior = prior_hotwords(m)
    current = shared or None
    if "hotwords" not in m:
        # 스탬프 도입 이전 전사 — 현행 글로서리가 있을 때만 비교 불가를 알린다.
        return (
            ["hotwords: 전사 설정 미기록(레거시) — 현행 글로서리와 세대 "
             "비교 불가, 새 -o 워크스페이스 재전사 시 스탬핑됨"]
            if current else []
        )
    applied = m.get("hotwords")
    rejected = m.get("hotwords_rejected")
    lines: list[str] = []
    if applied:
        lines.append(f"hotwords: {len(applied.split())}개 적용")
    elif rejected:
        lines.append("hotwords: 오염 감지로 미적용(반복 환각) — "
                     "같은 글로서리 재적용 금지 이력")
    baseline = applied or rejected
    # 접미를 구조적으로 떼어낸다 — 값으로 걸러내면 글로서리와 제목에 같은
    # 용어가 있을 때 글로서리 쪽 출현까지 지워져 갱신 없는 글로서리가
    # 드리프트로 오탐된다(적용값은 "<글로서리> <프라이어>"로 만들어진다).
    baseline_shared = baseline
    if baseline and prior and baseline.endswith(prior):
        baseline_shared = baseline[: -len(prior)].strip() or None
    if current != baseline_shared:
        lines.append("hotwords-드리프트: 글로서리가 전사 이후 갱신됨 — "
                     "새 -o 워크스페이스 재전사 시 최신 용어 교정 반영 가능")
    return lines


def recommend_mode(m, segs, speech, sc, hl, ocr_t):
    """SKILL §0-1 5-type table as a deterministic recommendation.

    ModeRecommendation pattern (borrowed from depixelate_v3 studio): return
    the pick plus the signal breakdown that justified it, so the agent can
    audit — and override — the call instead of re-deriving the signals.
    """
    duration = m["duration"] or 1.0
    ratio = speech / duration
    spm = len(sc) / (duration / 60) if sc is not None and duration else 0.0
    rationale = (f"speech {ratio:.0%} · scenes/min {spm:.1f}"
                 + (f" · bursts {len(hl)}" if hl is not None else "")
                 + (f" · ocr구간 {len(ocr_t)}" if ocr_t else ""))
    # 자막 주도: 발화가 희소한데 화면 자막이 분당 5구간+ 연속 = 자막이
    # 콘텐츠 정본 (실측: BGM 사연 영상이 시각 주도로 오판되던 클래스)
    opm = len(ocr_t) / (duration / 60) if ocr_t else 0.0
    if ratio < 0.35 and opm >= 5 and len(ocr_t) >= 5:
        return ("텍스트-온-스크린 후보(자막 주도) — ocr_transcript가 전사 "
                "정본, 프레임 대조로 확정", rationale)
    if not segs:
        if sc is not None and hl is not None and len(sc) <= 1 and not hl:
            return ("루프 모션그래픽 추정 — 필름스트립 1회로 완결, "
                    "추가 캡처 금지 (무발화·전환0·버스트0)", rationale)
        if ocr_t:
            return ("무발화 — ocr_transcript 재사용(자막 스캔 완료), "
                    "시각 검증만 보강", rationale)
        return ("무발화 — scenes→필름스트립→균등 캡처, "
                "자막 보이면 ocr --every 스캔", rationale)
    # 혼합형: 전체 발화율이 높아도 긴 무발화 스트레치가 숨어 있으면 그
    # 구간의 온스크린 캡션·실작업 지식이 통째로 소실된다(2026-08-04 실측:
    # 발화 51% 튜토리얼의 18.7분 타임랩스를 체크포인트 1개로 덮음).
    # OCR 조기 반환보다 먼저 판정한다 — 표적 OCR 1건이 게이트를 우회하면
    # 정확히 그 한-체크포인트 실패가 재발한다(PR #100 리뷰).
    longest_gap, cursor = 0.0, 0.0
    for seg in sorted(segs, key=lambda s: s.get("start", 0.0)):
        start = float(seg.get("start", 0.0))
        longest_gap = max(longest_gap, start - cursor)
        cursor = max(cursor, float(seg.get("end", start)))
    longest_gap = max(longest_gap, duration - cursor)
    if ratio >= 0.35 and longest_gap >= 120.0:
        rationale += f" · 무발화 최장 {longest_gap / 60:.0f}m"
        coverage = m.get("transcript_coverage")
        if isinstance(coverage, (int, float)) and coverage < 0.5:
            # 붕괴한 전사의 구멍은 침묵이 아니다 — 시각 주도로 단정하지
            # 말고 미해소 상태를 공개한다(toolbox 전사 신뢰 규약).
            rationale += f" · transcript_coverage {coverage:.2f}"
            return ("발화 주도(혼합형?) — 무발화 구간이 전사 붕괴 구멍일 수 "
                    "있음: 전사 신뢰(coverage·repair)부터 점검하고 판정",
                    rationale)
        return ("발화 주도(혼합형) — 전사 패턴 우선하되, 긴 무발화 구간은 "
                "시각 주도로 분리(필름스트립 조망+온스크린 캡처·OCR 스윕)",
                rationale)
    if ratio >= 0.35 and ocr_t:
        return ("텍스트-온-스크린 후보 — OCR로 ASR 오인식 교정, "
                "자막↔내레이션 1:1 여부는 직접 확정", rationale)
    if ratio < 0.35:
        return "시각 주도 — 필름스트립 조망+버스트 우선, 전사는 뼈대", rationale
    return "발화 주도 — 전사 패턴 우선, 버스트로 보강", rationale
