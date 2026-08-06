"""Command adapters that create or operate across workspaces."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_runtime(args) -> int:
    from .runtime_config import (
        Feature,
        RuntimeConfig,
        load_runtime_config,
        resolve_asr_backend,
        resolve_clip_encoder,
        runtime_config_path,
        save_runtime_config,
        set_runtime_value,
    )

    if args.runtime_action == "prepare":
        from .runtime_prepare import prepare_runtime_assets

        result = prepare_runtime_assets()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for backend, state in result.items():
                print(f"{backend}: {state}")
        return 0

    if args.runtime_action == "reset":
        config = RuntimeConfig()
        path = save_runtime_config(config)
        print(f"{path} — all-on balanced 기본값으로 복원")
        return 0

    config = load_runtime_config()
    if args.runtime_action == "set":
        try:
            config = set_runtime_value(config, args.key, args.value)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        path = save_runtime_config(config)
        print(f"{path} — {args.key}={args.value}")
        return 0

    report = {
        "config": str(runtime_config_path()),
        "profile": config.profile.value,
        "asr_backend": config.asr_backend.value,
        "resolved_asr_backend": resolve_asr_backend(config).value,
        "clip_encoder": config.clip_encoder.value,
        "resolved_clip_encoder": resolve_clip_encoder(config).value,
        "features": {
            feature.value: config.features.get(feature, True)
            for feature in Feature
        },
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"profile: {report['profile']}")
        print(
            "asr-backend: "
            f"{report['asr_backend']} → {report['resolved_asr_backend']}"
        )
        print(
            "clip-encoder: "
            f"{report['clip_encoder']} → {report['resolved_clip_encoder']}"
        )
        print(
            "features: "
            + " ".join(
                f"{name}={'on' if enabled else 'off'}"
                for name, enabled in report["features"].items()
            )
        )
        print(f"config: {report['config']}")
    return 0


def cmd_audit(args) -> int:
    """Print an aggregate readiness report for manifest-backed workspaces."""
    from .corpus_audit import audit_corpus

    report = audit_corpus(
        args.roots or None,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print(
        f"workspaces={report['workspace_count']} "
        f"terminal={report['terminal_count']} "
        f"supported={report['supported_count']}"
    )
    print(
        f"issues={report['issue_counts']} "
        f"readiness={report['readiness_counts']} "
        f"legacy={report['readiness_legacy_counts']}"
    )
    alignment = report["trigger_alignment"]
    if alignment["verify_transitions"]:
        print(f"검증 트리거 정렬: {alignment['aligned_top3']}/"
              f"{alignment['verify_transitions']} top3")
    maturity = report["procedure_maturity"]
    if maturity:
        head = " · ".join(
            f"{row['task']} {row['workspace_count']}영상/"
            f"{row['step_count']}단계(시각 "
            f"{row['visually_grounded_ratio']:.0%})"
            + (" ⚠정밀도 미보장" if row["precision_warning"] else "")
            + (" ⚠취지 이탈" if not row["intent_coherent"] else "")
            + (" ★스킬 후보" if row["skill_candidate"] else "")
            for row in maturity[:5])
        print(f"절차 성숙도: {head}"
              + (f" 외 {len(maturity) - 5}" if len(maturity) > 5 else ""))
    evidence = report.get("procedure_evidence")
    if evidence and evidence["step_count"]:
        from .wiki_procedures import EVIDENCE_CODES, EVIDENCE_KO

        # 분포는 게이트가 아니라 보고다 — 추정 단계가 있다는 사실이 아니라
        # 그것이 어느 표면에도 보이지 않는 것이 실패다.
        dist = " · ".join(
            f"{EVIDENCE_KO[code]} {evidence['counts'].get(code, 0)}"
            f"({evidence['ratios'].get(code, 0.0):.0%})"
            for code in EVIDENCE_CODES)
        print(f"절차 근거 분포: {dist} — 총 {evidence['step_count']}단계")
        estimated = [
            row for row in evidence["by_task"]
            if row["counts"].get("agent_estimated")
        ]
        for row in estimated[:5]:
            print(f"  ? {row['task']}: 추정 "
                  f"{row['counts']['agent_estimated']}/{row['step_count']}단계")
    unverified = report.get("procedure_unverified_tokens")
    if unverified and unverified["token_count"]:
        # exact_tokens는 "화면에서 확정한 문자열"로 읽힌다 — 발화 단독 근거인
        # 토큰이 그 이름 아래 섞여 있으면 사람도 다음 파이프라인도 확정으로
        # 읽는다. 임계도 종료코드도 걸지 않고 구분만 세운다.
        print(f"시각 미확정 토큰: {unverified['token_count']}개 "
              f"({unverified['step_count']}단계) — evidence=speech_only 단계의 "
              "exact_tokens는 발화 근거일 뿐 화면 확정이 아니다")
        for row in unverified["rows"][:5]:
            print(f"  ~ {row['task']}/{row['workspace_id']} "
                  f"{row['checkpoint_id']}: {' '.join(row['tokens'])}")
    images = report.get("image_consistency")
    if images and images["mismatched_count"]:
        # 촬영은 원장에만 append되고 사람이 읽는 인덱스는 따로 렌더된다 —
        # 그 사이 스틸은 정상 기록된 채로 발행에서만 빠진다.
        print(f"이미지 정합: {images['mismatched_count']}"
              f"/{images['workspace_count']}개 워크스페이스 불일치 "
              f"(원장 {images['catalog_total']} vs 인덱스 "
              f"{images['indexed_total']}) — `va index <코퍼스>`로 재발행")
        for row in images["rows"][:8]:
            declared = "없음" if row["declared"] is None else row["declared"]
            print(f"  ≠ {row['workspace']}: 원장 {row['catalog']} · 인덱스 "
                  f"{row['indexed']} · 선언 {declared}")
            for item in row["missing"][:4]:
                print(f"      미발행 {item}")
            for item in row["orphaned"][:4]:
                print(f"      원장에 없음 {item}")
    declared = report.get("verification_levels_declared") or {}
    grounded = report.get("verification_levels_grounded") or {}
    if declared or grounded:
        # 두 축을 함께 인쇄한다. 선언 축만 보이던 동안 실측 코퍼스의 무근거
        # 46/165가 요약에서 사라졌다 — 계산해 두고 말하지 않으면 사람은
        # "전부 근거 있음"으로 읽는다.
        ko = {"cross_modal": "교차모달", "visual_only": "시각단독",
              "transcript_only": "전사단독", "unsupported": "무근거"}

        def _fmt(levels: dict[str, int], *, show_zero_unsupported: bool) -> str:
            items = dict(levels)
            if show_zero_unsupported:
                items.setdefault("unsupported", 0)
            return " · ".join(
                f"{ko.get(code, code)} {count}"
                for code, count in items.items())

        print("검증 수준(선언): " + _fmt(declared, show_zero_unsupported=False))
        # 실지지 축은 무근거 0도 인쇄한다 — 경계가 비었다는 사실 자체가 신호다.
        print("검증 수준(실지지): " + _fmt(grounded, show_zero_unsupported=True))
    wiki = report.get("wiki")
    if wiki:
        over = " 초과!" if wiki["corpus_index_over_limit"] else ""
        print(
            f"wiki: 엔티티 {wiki['entities']}"
            f"(정착 {wiki['durable_entities']}"
            f"/후보 {wiki['candidate_entities']}) "
            f"관계 {wiki['relations']} 대사 {wiki['quotes']} "
            f"서술 {wiki['notes_filled']} "
            f"통합후보 {wiki['alias_groups']} "
            f"인덱스 {wiki['corpus_index_bytes'] / 1024:.1f}KB{over}"
        )
    candidate_total = report.get("relation_candidate_total")
    if candidate_total:
        # 엣지를 자동으로 만들지 않는다 — 술어는 사람이 판정해 체크포인트에
        # 적고, 그 원장을 거쳐 위키 관계로 나온다.
        print(
            f"관계 후보: {candidate_total}건 "
            "(엔티티 2+ 인데 relations 미기재 — --json의 "
            "relation_candidates에서 대상 확인)"
        )
    binding = report.get("binding_counts") or {}
    if binding:
        # 결박 상태는 "새 증거를 쓸 수 있는가"의 다른 이름이다. 이것이 보이지
        # 않으면 코퍼스가 통째로 동결돼도 어느 표면도 말하지 않는다
        # (2026-07-28 실측: 40/40 미결박 — 이해 루프 정지가 무표시였다).
        # draft는 결박이 없지만 첫 기입 때 스스로 발행한다 — 동결이 아니다.
        ko = {"bound": "결박됨", "draft": "초안(쓰기 가능)", "legacy": "미결박",
              "incomplete": "결박 불완전", "unreadable": "판독 불가"}
        line = " · ".join(
            f"{ko.get(state, state)} {count}"
            for state, count in binding.items())
        frozen = binding.get("legacy", 0) + binding.get("incomplete", 0)
        tail = (f" — {frozen}개는 새 증거를 쓸 수 없다(읽기 전용). 이해를 "
                "이어가려면 `va ingest`로 새 워크스페이스를 만든다"
                if frozen else "")
        print(f"결박: {line}{tail}")
    access = report.get("access") or {}
    if access.get("total"):
        by_command = " · ".join(
            f"{name} {count}" for name, count in access["by_command"].items())
        empty = access.get("empty_result_reads") or 0
        # 결과 0건 조회는 코퍼스가 답하지 못한 질문 — 커버리지 공백의 신호다.
        tail = f" · 무응답 {empty}" if empty else ""
        print(f"조회: 총 {access['total']}회 ({by_command})"
              f"{tail} · 최근 {access['last_ts']}")
    else:
        print("조회: 기록 없음 — 이 코퍼스가 읽힌 적이 있는지 알 수 없다")
    integrity = report.get("wiki_integrity")
    if integrity:
        broken = integrity["broken_links"]
        orphans = integrity["orphan_entities"]
        # 재배정은 링크가 살아 있는 채로 대상만 바뀌는 회귀라, 깨진 링크
        # 검사로는 절대 잡히지 않는다.
        reassigned = integrity.get("reassigned_entities") or []
        print(f"무결성: 링크 깨짐 {len(broken)} · 고아 페이지 {len(orphans)}"
              f" · 슬러그 재배정 {len(reassigned)}")
        for item in (broken + orphans)[:8]:
            print(f"  ✗ {item}")
        for item in reassigned[:8]:
            print(f"  ↻ {item}")
    improvements = report.get("improvements")
    if improvements and any(improvements.values()):
        print("개선 후보:")
        label_ko = {
            "notes_missing_durable": "서술 없는 정착 엔티티",
            "relations_missing_recurring": "관계 없는 다회 등장",
            "narrative_missing_converged": "서사 미작성 완결 영상",
        }
        for key, items in improvements.items():
            if items:
                head = " · ".join(items[:6])
                more = f" 외 {len(items)-6}" if len(items) > 6 else ""
                print(f"  {label_ko[key]} {len(items)}: {head}{more}")
    for workspace in report["workspaces"]:
        if (
            workspace["issue_counts"]
            or workspace["readiness"] != workspace["readiness_legacy"]
        ):
            print(
                f"{workspace['path']}: readiness={workspace['readiness']} "
                f"legacy={workspace['readiness_legacy']} "
                f"terminal={workspace['terminal_count']} "
                f"supported={workspace['supported_count']} "
                f"issues={workspace['issue_counts']}"
            )
    return 0


def cmd_ingest(args) -> int:
    from .ingest import ingest_session

    if not args.video.strip():
        # 빈 문자열은 Path("")가 cwd로 해석돼 "video not found: <cwd>" 라는
        # 사용자가 준 값과 무관해 보이는 메시지가 나온다.
        raise ValueError("video 인자가 비어 있습니다 — 파일 경로 또는 "
                         "http(s) URL을 지정하십시오")
    with ingest_session(
        args.video,
        out=Path(args.out) if args.out else None,
        model=args.model,
        asr_backend=getattr(args, "asr_backend", "auto"),
        lang=args.lang,
        force_whisper=args.force_whisper,
        max_height=args.max_height,
        hotwords=args.hotwords,
        cookies_from_browser=args.cookies_from_browser,
        signals=args.signals,
    ) as ws:
        if args.signals:
            from .brief import build_brief

            print(build_brief(ws))
            print(f"workspace: {ws.root}")
            return 0
        m = ws.manifest
        print(f"workspace: {ws.root}")
        print(f"transcript: {ws.transcript_path} "
              f"({m['segments']} segments, source={m['transcript_source']})")
    return 0


def cmd_bridge(args) -> int:
    # 에러는 main()의 공통 래핑으로 — 여기서 stdout에 찍으면 파이프라인에서
    # 실패 메시지가 성공 출력에 섞인다(다른 모든 명령은 stderr).
    from .bridge import publish_bridge

    corpus = Path(getattr(args, "_projection_root", args.corpus))
    report = publish_bridge(
        corpus,
        Path(args.vault),
        workspace_paths=getattr(args, "_workspace_paths", None),
    )
    print(
        f"{report['dest']} — 스텁 {report['published']}개 발행"
        f" (stale 제거 {report['removed']}, 외부 파일 보존 {report['foreign']})"
    )
    return 0


def cmd_glossary(args) -> int:
    from .glossary import GLOSSARY_PATH, load_hotwords, update_glossary

    frozen = getattr(args, "_glossary_paths", None)
    if frozen is not None:
        targets = list(frozen)
    else:
        targets = [Path(w) for w in args.workspaces]
        if args.all:
            root = Path.cwd() / "va-out"
            # rglob: 워크스페이스는 va-out/쇼츠/<ws>처럼 분류 폴더 밑에도 산다.
            # 1단계 glob은 그런 워크스페이스를 통째로 놓쳤다
            # (실측 2026-07-25: 7개 중 3개만 수집).
            targets += sorted(p.parent for p in root.rglob("corrections.jsonl"))
    if targets:
        path, added, total = update_glossary(targets)
        print(f"{path} — +{added} 용어 (총 {total})")
    else:
        hot = load_hotwords()
        if hot:
            print(hot)
            print(f"({len(hot.split())}개 — {GLOSSARY_PATH})",
                  file=sys.stderr)
        else:
            print("glossary 비어 있음 — corrections.jsonl이 있는 워크스페이스로 "
                  "`va glossary <ws>...` 실행", file=sys.stderr)
    return 0


def _resolve_corpus(args) -> Path | None:
    from .workspace_discovery import MultipleProjectionRootsError, corpus_root

    explicit = getattr(args, "_projection_root", None)
    if explicit:
        return Path(explicit)
    try:
        return corpus_root(args.roots or None)
    except (MultipleProjectionRootsError, OSError, ValueError):
        return None


def _record_corpus_read(args, command: str, **detail) -> None:
    """조회를 관측한다 — 실패해도 조회 자체는 막지 않는다."""
    from .access_log import record_access

    corpus = _resolve_corpus(args)
    if corpus is None:
        return
    record_access(corpus, command, **detail)


def cmd_search(args) -> int:
    from .search import search_workspaces

    hits = search_workspaces(
        args.query,
        roots=args.roots or None,
        top=args.top,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    _record_corpus_read(args, "search", query=args.query, hits=len(hits))
    if args.json:
        print(json.dumps(hits, ensure_ascii=False))
    else:
        for h in hits:
            source = h["source"]
            if status := h.get("status"):
                source = f"{source}|{status}"
            print(f"{h['ws']}  {h['start']:.1f}s–{h['end']:.1f}s  "
                  f"[{source}]  {h['text'][:70]}")
        if not hits:
            print("no matches", file=sys.stderr)
    return 0


def cmd_index(args) -> int:
    from .index import build_index

    dest, n = build_index(
        args.roots or None,
        graph_reset=getattr(args, "graph_reset", False),
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    _record_corpus_read(args, "index", hits=n)
    print(f"{dest} — {n}개 워크스페이스")
    return 0


def cmd_view(args) -> int:
    from .view import build_view

    dest, n = build_view(
        args.roots or None,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
        standalone=getattr(args, "standalone", False),
    )
    _record_corpus_read(args, "view", hits=n)
    print(f"{dest} — {n}개 워크스페이스")
    return 0


def cmd_wiki(args) -> int:
    from .wiki import build_wiki

    dest, c = build_wiki(
        args.roots or None,
        include_hypotheses=args.include_hypotheses,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    _record_corpus_read(args, "wiki", hits=c["entities"])
    print(f"{dest} — 엔티티 {c['entities']} · 태그 {c['tags']} · "
          f"관계 {c['relations']} · 대사 {c['quotes']}"
          + (f" · 절차 {c['procedures']}" if c.get("procedures") else "")
          + (f" · 스킬 초안 {c['skill_drafts']}"
             if c.get("skill_drafts") else ""))
    return 0


def cmd_beats(args) -> int:
    """BGM 비트 그리드를 추출해 beats.json 아티팩트로 남긴다."""
    from .beats import extract_beats, write_beats_json

    media = Path(args.media)
    if not media.is_file():
        print(f"미디어 파일이 없다: {media}", file=sys.stderr)
        return 2
    grid = extract_beats(media, seconds=args.seconds)
    out = (
        Path(args.out) if args.out
        else media.with_name(media.stem + ".beats.json")
    )
    write_beats_json(out, grid)
    summary = {
        "bpm": grid["bpm"],
        "beats": len(grid["beat_times"]),
        "analyzed_s": grid["analyzed_s"],
        "tempo_model": grid["tempo_model"],
        "out": str(out),
    }
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        first = [round(b, 2) for b in grid["beat_times"][:8]]
        print(
            f"BPM {grid['bpm']}  beats {len(grid['beat_times'])}  "
            f"analyzed {grid['analyzed_s']}s  first8 {first}"
        )
        print(f"{out} — 컷 양자화 게이트는 va beat-eval")
    return 0


def _skillgen_route(args) -> int:
    """컴파일 없이 태스크별 스킬 라우팅 표만 — 게이트 미달 사유 포함."""
    from .corpus_audit import audit_corpus
    from .wiki_procedures import route_verdict

    report = audit_corpus(
        args.roots or None,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    rows = report["procedure_maturity"]
    _record_corpus_read(args, "skillgen-route", hits=len(rows))
    if not rows:
        print("절차가 아직 없다 — task-* 태그 체크포인트가 라우팅의 "
              "입력이다(절차 규약은 wiki-schema 참조)")
        return 0
    for row in rows:
        line = (
            f"{row['task']}  {row['workspace_count']}영상/"
            f"{row['step_count']}단계 시각 "
            f"{row['visually_grounded_ratio']:.0%}  "
            f"[{route_verdict(row)}]"
        )
        if row["tool_consensus"]:
            line += "  도구 " + ",".join(row["tool_consensus"])
        if row["offdomain_workspaces"]:
            line += "  이탈 " + ",".join(row["offdomain_workspaces"])
        if row["precision_warning"]:
            line += "  ⚠정밀도"
        print(line)
    print("draft=컴파일 대상 · blocked-intent=취지 이탈 소스 정정 전 "
          "승격 금지 · needs-workspaces=재등장 부족")
    return 0


def cmd_skillgen(args) -> int:
    """승격된 절차의 SKILL 초안을 컴파일하고 승인 절차를 안내한다."""
    from .skillgen import is_skill_draft
    from .wiki import build_wiki

    if getattr(args, "route", False):
        return _skillgen_route(args)
    dest, c = build_wiki(
        args.roots or None,
        workspace_paths=getattr(args, "_workspace_paths", None),
        projection_root=getattr(args, "_projection_root", None),
    )
    _record_corpus_read(args, "skillgen", hits=c.get("skill_drafts", 0))
    skills = dest.parent / "skills"
    # 수기·승인 사본(비소유)은 초안이 아니다 — 생성분만 나열한다.
    drafts = sorted(
        page.name for page in skills.glob("*.md") if is_skill_draft(page)
    ) if skills.is_dir() else []
    if not c.get("skill_drafts"):
        print("스킬 후보 절차가 아직 없다 — 재등장 3영상 이상이 승격 기준"
              "(va audit의 절차 성숙도 축 참고)")
        return 0
    print(f"스킬 초안 {c['skill_drafts']}건 — {skills}")
    for name in drafts:
        print(f"  {name}")
    print("초안은 사람 승인 전 활성 금지 — 검토 후 승격 사본을 만들어 "
          "쓰고, 원본 초안은 위키 재생성이 관리한다")
    return 0


def cmd_gc(args) -> int:
    from .gc import run_gc

    text, code = run_gc(
        paths=args.paths,
        purge=args.purge,
        keep_days=args.keep_days,
        yes=args.yes,
    )
    print(text)
    return code


def cmd_rebind(args) -> int:
    """Bind legacy workspaces after the fact — dry run unless `--apply`.

    되돌릴 수 없는 데이터 변경이라 기본값이 실행이 아니다. 무엇이 바뀌는지
    먼저 보여주고, 사용자가 `--apply`를 적을 때만 쓴다.
    """
    from .revision_backfill import BackfillRefused, backfill_workspace
    from .workspace import Workspace
    from .workspace_discovery import find_workspaces

    paths = find_workspaces(args.targets or None)
    if not paths:
        print("워크스페이스를 찾지 못했다", file=sys.stderr)
        return 1

    done: list[dict] = []
    refused: list[tuple[str, str]] = []
    for path in paths:
        try:
            done.append(backfill_workspace(Workspace(path), apply=args.apply))
        except BackfillRefused as error:
            refused.append((path.name, str(error)))
        except (OSError, ValueError) as error:
            refused.append((path.name, f"{type(error).__name__}: {error}"))

    if args.json:
        print(json.dumps({
            "applied": args.apply,
            "workspaces": done,
            "refused": [{"workspace": name, "reason": why}
                        for name, why in refused],
        }, ensure_ascii=False, indent=1))
        return 0

    clamped = sum(item["clamped_spans"] for item in done)
    records = sum(sum(item["records"].values()) for item in done)
    verb = "결박함" if args.apply else "결박 가능"
    print(f"{verb}: {len(done)}개 · 기록 {records}줄" + (
        f" · span 클램프 {clamped}건" if clamped else ""))
    for item in done:
        note = (f"  (span {item['clamped_spans']}건을 미디어 끝에 맞춤)"
                if item["clamped_spans"] else "")
        print(f"  · {item['workspace']} — 기록 "
              f"{sum(item['records'].values())}줄{note}")
    if refused:
        print(f"\n거부: {len(refused)}개")
        for name, why in refused:
            print(f"  ✗ {name}: {why}")
    if not args.apply and done:
        print("\n실제로 쓰려면 --apply. 원장은 사후 검증에 실패하면 전량 복원된다.")
    return 0
