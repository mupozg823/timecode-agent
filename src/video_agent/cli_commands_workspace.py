"""Command adapters that operate on an existing video workspace.

No-excuse size marker: ``# noqa: SIZE_OK``. This pre-existing command-family
adapter remains one public CLI surface; splitting it is a separate refactor.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Final, assert_never

from .fsio import write_text_atomic
from .timestamps import parse_ts

OCR_INPUT_REQUIRED_MESSAGE: Final = "provide -t timestamps or --every for a scan"
CHECKPOINT_INPUT_REQUIRED_MESSAGE: Final = (
    "provide field flags (--id/--span/--status/--hypothesis) "
    "or --json/--json-file (use '-' for stdin)"
)
CHECKPOINT_SCHEMA_EXAMPLE: Final = (
    '예시: {"id": "kill-1", "span": [93.5, 96.0], '
    '"status": "hypothesized", "hypothesis": "첫 킬 장면", '
    '"evidence": "seg12 + burst"}'
)
CHECKPOINT_FLAG_EXAMPLE: Final = (
    "예시: va checkpoint add <ws> --id cp-001 --span 0 42.5 "
    '--status hypothesized --hypothesis "MC가 퀴즈 규칙을 설명한다"'
)
CHECKPOINT_INPUT_CONFLICT_MESSAGE: Final = (
    "--json/--json-file과 필드 플래그는 함께 쓸 수 없습니다 — "
    "한쪽만 고르십시오"
)
# 플래그 경로가 조립하는 필드 — argparse dest -> 원장 키.
# (--id는 argparse의 기본 dest가 'id'라 예약어와 겹쳐 cp_id로 받는다)
_CHECKPOINT_FLAG_FIELDS: Final = (
    ("cp_id", "id"),
    ("status", "status"),
    ("hypothesis", "hypothesis"),
    ("confidence", "confidence"),
    ("note", "note"),
    ("visual_evidence", "visual_evidence"),
)


class WorkspaceCommandUsageError(ValueError):
    """Raised when workspace command options form an invalid combination."""


def _ws(path: str):
    from .workspace import Workspace

    return Workspace.load(Path(path))


def cmd_brief(args) -> int:
    from .brief import build_brief

    print(build_brief(_ws(args.workspace), why=args.why))
    return 0


def cmd_ask(args) -> int:
    from .ask import answer_question
    from .ask_render import render_report
    from .ask_serialization import envelope_to_payload
    from .cli_ask_parser import AskOutputFormat

    report = answer_question(
        _ws(args.workspace),
        args.question,
        reply_locale=args.lang,
    )
    output_format: AskOutputFormat = args.format
    match output_format:
        case AskOutputFormat.HUMAN:
            print(render_report(report))
        case AskOutputFormat.AGENT_JSON:
            print(
                json.dumps(
                    envelope_to_payload(report),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        case unreachable:
            assert_never(unreachable)
    return 0


def cmd_capture(args) -> int:
    from .capture import capture_frames, capture_sharpest
    from .image_index import image_index_consistency, image_index_is_stale

    ws = _ws(args.workspace)
    ts = [parse_ts(t) for t in args.t]
    if getattr(args, "sharp", False):
        if args.crop:
            raise WorkspaceCommandUsageError("--sharp is full-frame only — drop --crop")
        paths = capture_sharpest(ws, ts, window=args.window,
                                 reason=args.reason)
    else:
        paths = capture_frames(ws, ts, crop=args.crop, reason=args.reason)
    for p in paths:
        print(p)
    # 촬영은 원장에만 append하고 투영은 `va index`/`va view`가 따로 렌더한다.
    # 그 사이 스틸은 원장에 정상 기록된 채 사람이 읽는 인덱스에서만 빠지고,
    # 아무 표면도 그 차이를 말하지 않았다 — 실측 코퍼스에서 13장 중 2장이
    # 이렇게 미발행으로 남았다(2026-08-05). 여기서 한 줄로 닫는다.
    row = image_index_consistency(ws)
    if row["doc_present"] and image_index_is_stale(row):
        print(
            f"주의: 이미지 인덱스가 원장보다 {row['catalog'] - row['indexed']}장 "
            f"뒤처졌습니다(원장 {row['catalog']} · 인덱스 {row['indexed']}) — "
            "`va index <코퍼스 루트>`로 재발행하십시오",
            file=sys.stderr,
        )
    return 0


def cmd_keyframes(args) -> int:
    from .keyframes import plan_report

    ws = _ws(args.workspace)
    report = plan_report(
        ws, args.budget,
        start=parse_ts(args.start) if args.start else None,
        end=parse_ts(args.end) if args.end else None,
        min_gap=args.min_gap,
        legible_endcard=getattr(args, "legible_endcard", False),
    )
    picks = report["picks"]
    explain = bool(getattr(args, "explain", False))
    if args.as_json:
        payload: object = report if explain else picks
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not picks:
        print("후보 없음 — 신호 파일(scenes/highlights)을 먼저 생성하세요")
        return 0
    for c in picks:
        print(f"t={c['t']:>9.2f}  w={c['weight']:.2f}  {c['source']}")
    if explain and report["rejected"]:
        print(f"\n탈락 {len(report['rejected'])}건:")
        for r in report["rejected"]:
            displaced = (
                f" ← t={r['displaced_by']:.2f}에 밀림"
                if r["displaced_by"] is not None else ""
            )
            print(f"t={r['t']:>9.2f}  w={r['weight']:.2f}  {r['source']}  "
                  f"{r['reason']}{displaced}")
    ts_args = " ".join(f"-t {c['t']}" for c in picks)
    print(f"\nva capture {ws.root} {ts_args} "
          f"--reason keyframes-budget{args.budget}")
    return 0


def cmd_boundary_eval(args) -> int:
    from .boundary_eval import (
        JoinReport,
        cut_boundary_report,
        sequence_gate_report,
    )

    if bool(args.sequence) == bool(args.ts):
        raise WorkspaceCommandUsageError(
            "-t 또는 --sequence 중 정확히 하나만 지정하십시오 "
            "(동시 지정은 한쪽이 조용히 버려질 위험)")
    ws = _ws(args.workspace)
    joins: list[JoinReport] = []
    if args.sequence:
        gate = sequence_gate_report(ws, args.sequence, window=args.window)
        reports, joins = gate["boundaries"], gate["joins"]
    else:
        reports = [
            cut_boundary_report(ws, parse_ts(t), window=args.window)
            for t in args.ts
        ]
    if args.as_json:
        payload: object = (
            {"boundaries": reports, "joins": joins}
            if args.sequence else reports
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    for report in reports:
        flags = ",".join(report["flags"]) or "clean"
        step = report["rms_step_db"]
        gap = (report["alignment"] or {}).get("gap")
        print(
            f"t={report['t']:>9.2f}  {flags}  "
            f"rms_step={step if step is not None else '-'}dB  "
            f"gap={gap if gap is not None else '-'}s"
        )
    for join in joins:
        flags = ",".join(join["flags"]) or "clean"
        step = join["rms_step_db"]
        print(
            f"join {join['from_t']:>8.2f}→{join['to_t']:<8.2f}  {flags}  "
            f"rms_step={step if step is not None else '-'}dB  "
            f"ssim={join['ssim'] if join['ssim'] is not None else '-'}"
        )
    return 0


def cmd_beat_eval(args) -> int:
    """조인-비트 오프셋 게이트 — 통과 0, p90 초과 1을 돌려준다."""
    from .beat_audit import (
        beat_alignment_report,
        sequence_cuts,
        snap_proposal,
    )
    from .beats import load_beats_json

    ws = _ws(args.workspace)
    grid = load_beats_json(Path(args.beats))
    cuts = sequence_cuts(ws, args.sequence)
    report = beat_alignment_report(
        args.sequence, cuts, grid, gate_ms=args.gate_ms
    )
    snap = (
        snap_proposal(
            cuts, grid, duration=float(ws.manifest["duration"])
        )
        if args.snap else None
    )
    if args.as_json:
        payload: dict[str, object] = {"report": report}
        if snap is not None:
            payload["snap"] = snap
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    for join in report["joins"]:
        print(
            f"join@{join['instant']:>9.3f}s  "
            f"beat {join['nearest_beat']:>9.3f}  "
            f"offset {join['offset_ms']:+7.1f}ms"
        )
    p90 = report["p90_ms"]
    print(
        f"p90 {p90 if p90 is not None else '-'}ms  "
        f"gate {report['gate_ms']}ms  "
        + ("PASS" if report["passed"] else "FAIL")
        + ("  (조인 없음 — 단일 컷)" if not report["join_count"] else "")
    )
    if snap is not None:
        for cut in snap:
            mark = "→" if cut["snapped"] else "·"
            reason = f"  ({cut['reason']})" if cut["reason"] else ""
            print(
                f"{mark} order {cut['order']}: span "
                f"[{cut['span'][0]:.4f}, {cut['span'][1]:.4f}] "
                f"end{cut['end_delta_s']:+.4f}s{reason}"
            )
        print("제안은 원장 불변 — 적용은 시퀀스 새 리비전으로, 적용 후 "
              "boundary-eval 재판독이 전제다")
    return 0 if report["passed"] else 1


def cmd_filmstrip(args) -> int:
    from .filmstrip import make_filmstrip, plan_windows

    ws = _ws(args.workspace)
    duration = float(ws.manifest["duration"])
    if args.auto:
        windows = plan_windows(
            duration,
            start=parse_ts(args.start) if args.start else 0.0,
            end=parse_ts(args.end) if args.end else None,
        )
    else:
        windows = [(
            parse_ts(args.start) if args.start else 0.0,
            parse_ts(args.end) if args.end else duration,
            args.n,
        )]
    # 창마다 독립 ffmpeg 1회 — bounded pool로 스폰만 겹치고 출력은
    # 계획 순서 그대로 (전수점검 실측: 2~3 워커에서 벽시계 20~38%↓,
    # full fan-out은 방송 공존 제약으로 금지).
    from concurrent.futures import ThreadPoolExecutor

    workers = min(3, len(windows), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda w: make_filmstrip(ws, w[0], w[1], n=w[2], cols=args.cols),
            windows,
        ))
    for path, mids in results:
        print(path)
        for i, t in enumerate(mids, 1):
            print(f"cell {i}: {t:.1f}s")
    return 0


def cmd_audioevents(args) -> int:
    from .audio_events import compute_audio_events

    events = compute_audio_events(_ws(args.workspace), min_conf=args.min_conf)
    if args.json:
        print(json.dumps(events, ensure_ascii=False))
    else:
        for e in events:
            print(f"{e['start']:.1f}s–{e['end']:.1f}s  {e['label']}  "
                  f"peak={e['peak_conf']}")
        if not events:
            print("no audio events")
    return 0


def cmd_diarize(args) -> int:
    from .diarize import compute_diarization

    turns = compute_diarization(_ws(args.workspace),
                                num_speakers=args.num_speakers,
                                backend=args.backend)
    for t in turns[:30]:
        print(f"{t['start']:.1f}s–{t['end']:.1f}s  {t['speaker']}")
    if len(turns) > 30:
        print(f"... 외 {len(turns) - 30}개 (diarization.json)")
    return 0


def cmd_faces(args) -> int:
    from .faces import count_faces

    results = count_faces(_ws(args.workspace),
                          [parse_ts(t) for t in args.t], crop=args.crop)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for r in results:
            scale = f"  scale={r['scale']}" if r["scale"] else ""
            print(f"{r['t']:.1f}s  faces={r['count']}{scale}")
    return 0


def cmd_ocr(args) -> int:
    from .ocr import ocr_frame, ocr_scan

    ws = _ws(args.workspace)
    if args.every:
        entries = ocr_scan(
            ws, every=float(args.every), crop=args.crop,
            langs=args.lang.split(",") if args.lang else None,
            start=parse_ts(args.start) if args.start else 0.0,
            end=parse_ts(args.end) if args.end else None,
        )
        if args.json:
            print(json.dumps(entries, ensure_ascii=False))
        else:
            for e in entries:
                print(f"{e['start']:.1f}s–{e['end']:.1f}s  {e['text']}")
            print(f"({len(entries)}개 캡션 구간 — ocr_transcript.json 저장)",
                  file=sys.stderr)
        return 0
    if not args.t:
        raise ValueError(OCR_INPUT_REQUIRED_MESSAGE)
    for t in args.t:
        results = ocr_frame(ws, parse_ts(t), crop=args.crop,
                            langs=args.lang.split(",") if args.lang else None)
        if args.json:
            print(json.dumps({"t": parse_ts(t), "results": results},
                             ensure_ascii=False))
        else:
            print(f"# t={t}")
            for r in results:
                print(f"  [{r['confidence']:.2f}] {r['text']}")
    return 0


def _checkpoint_from_flags(args) -> dict | None:
    """필드 플래그로 체크포인트를 조립한다 — 하나도 없으면 None.

    스키마를 CLI 표면에 드러내는 경로다. 한글·따옴표가 긴 본문은 셸
    이스케이프 사고가 잦아 --json-file - + heredoc이 여전히 안전하다.
    """
    obj: dict = {}
    for dest, key in _CHECKPOINT_FLAG_FIELDS:
        value = getattr(args, dest, None)
        if value is not None:
            obj[key] = value
    span = getattr(args, "span", None)
    if span:
        try:
            obj["span"] = [parse_ts(span[0]), parse_ts(span[1])]
        except ValueError as e:
            raise WorkspaceCommandUsageError(f"--span: {e}") from None
    segments = getattr(args, "segments", None)
    if segments is not None:
        try:
            obj["segments"] = [
                int(s) for s in segments.split(",") if s.strip()
            ]
        except ValueError:
            raise WorkspaceCommandUsageError(
                f"--segments는 쉼표로 구분한 정수 인덱스입니다: {segments!r}"
            ) from None
    return obj or None


def cmd_checkpoint_add(args) -> int:
    from .checkpoints import append_checkpoint

    ws = _ws(args.workspace)
    from_flags = _checkpoint_from_flags(args)
    if from_flags is not None:
        # 전달 여부는 None으로 판정한다 — 래퍼가 `--json "$EMPTY"`를 넘기면
        # truthiness 검사는 옵션이 없다고 보고 상호배타 계약이 조용히 깨진다.
        if args.json is not None or args.json_file is not None:
            raise WorkspaceCommandUsageError(CHECKPOINT_INPUT_CONFLICT_MESSAGE)
        try:
            added = append_checkpoint(ws, from_flags)
        except ValueError as e:
            raise ValueError(
                str(e) + "\n" + CHECKPOINT_FLAG_EXAMPLE) from None
        print(f"checkpoint {added['id']} ({added['status']}) 기록됨")
        return 0
    if args.json_file:
        raw = (
            sys.stdin.read()
            if args.json_file == "-"
            else Path(args.json_file).read_text()
        )
    else:
        raw = args.json
    if not raw:
        raise ValueError(CHECKPOINT_INPUT_REQUIRED_MESSAGE)
    # 파싱만 따로 감싼다 — append 중 손상된 manifest가 던지는
    # JSONDecodeError를 페이로드 탓으로 오귀책하면 안 된다.
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        # sequence add와 대칭 — 파싱 실패도 스키마 예시까지 안내한다
        # (한글·따옴표 셸 이스케이프 사고가 가장 흔한 원인).
        raise ValueError(
            f"JSON 파싱 실패({e}) — 한글·따옴표가 섞이면 "
            "`--json-file -` + heredoc으로 넣으십시오\n"
            + CHECKPOINT_SCHEMA_EXAMPLE) from None
    try:
        added = append_checkpoint(ws, obj)
    except ValueError as e:
        raise ValueError(str(e) + "\n" + CHECKPOINT_SCHEMA_EXAMPLE) from None
    print(f"checkpoint {added['id']} ({added['status']}) 기록됨")
    return 0


def cmd_checkpoint_observe(args) -> int:
    from .checkpoint_schema import PersonPresenceState
    from .person_presence import (
        PersonPresenceJudgment,
        record_person_presence,
    )

    judgment = PersonPresenceJudgment(
        checkpoint_id=args.cp_id,
        frame_ref=args.frame,
        subject=args.subject,
        state=PersonPresenceState(args.state),
        hypothesis=args.hypothesis,
    )
    added = record_person_presence(_ws(args.workspace), judgment)
    print(f"checkpoint {added['id']} ({added['status']}) 기록됨")
    return 0


def cmd_checkpoint_list(args) -> int:
    from .checkpoints import load_checkpoints

    cps = load_checkpoints(_ws(args.workspace))
    if args.json:
        print(json.dumps(cps, ensure_ascii=False))
    else:
        for c in cps:
            span = c.get("span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    for value in span
                )
            ):
                continue
            s, e = span
            raw_situation = c.get("situation")
            raw_hypothesis = c.get("hypothesis")
            if isinstance(raw_situation, str) and raw_situation:
                summary = raw_situation
            elif isinstance(raw_hypothesis, str):
                summary = raw_hypothesis
            else:
                summary = ""
            print(f"{c['id']}  [{s:.1f}s–{e:.1f}s]  {c['status']}  "
                  f"{summary[:60]}")
    return 0


# 발동한 신호만 이름으로 보여준다 — 점수 하나로는 "왜 이게 먼저인지"가
# 감사되지 않는다.
_VERIFY_SIGNAL_KO = (("self_doubt", "저확신"),
                     ("transcript_silence", "무전사"),
                     ("asr_weakness", "ASR저신뢰"),
                     ("speaker_shift", "화자전환"))


def _verify_queue_entry(item) -> str:
    flags = "·".join(label for name, label in _VERIFY_SIGNAL_KO
                     if item["signals"][name] >= 0.5)
    return (f"{item['id']} {item['score']:g}"
            + (f"({flags})" if flags else ""))


def cmd_status(args) -> int:
    from .status import workspace_status

    workspace = _ws(args.workspace)
    st = workspace_status(workspace)
    audit = st["verification_audit"]
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
    else:
        c = st["counts"]
        print(f"duration: {st['total_duration']:.1f}s")
        print(f"checkpoints: hypothesized={c['hypothesized']} "
              f"verified={c['verified']} corrected={c['corrected']}")
        print(f"covered: {st['covered_ratio']:.0%}  "
              f"verified: {st['verified_ratio']:.0%}  "
              f"mean_confidence: {st['mean_confidence']}")
        for s, e in st["gaps"]:
            print(f"gap: {s:.1f}s–{e:.1f}s")
        r = st["readiness"]
        print(f"readiness: {r['status']} (score {r['score']}) — "
              f"{r['recommendation']}")
        queue = st["verify_queue"]
        if queue:
            print("verify next: " + " · ".join(
                _verify_queue_entry(item) for item in queue))
        print(
            "evidence audit: "
            f"supported={audit['supported_count']}/{audit['terminal_count']} "
            f"warnings={len(audit['issues'])} "
            f"legacy_readiness={st['readiness_legacy']['status']}"
        )
    return 0


def cmd_highlights(args) -> int:
    from .highlights import compute_highlights

    spans = compute_highlights(_ws(args.workspace), window=args.window,
                               top=args.top)
    if args.json:
        print(json.dumps(spans, ensure_ascii=False))
    else:
        for s in spans:
            print(f"{s['start']:.1f}s–{s['end']:.1f}s  "
                  f"score={s['score']}  peak={s['peak_db']}dB")
        if not spans:
            print("no highlight candidates")
    return 0


def cmd_scenes(args) -> int:
    from .scenes import compute_scenes

    ws = _ws(args.workspace)
    scenes = compute_scenes(ws, threshold=args.threshold,
                            adaptive=args.adaptive)
    if scenes and args.color_check:
        from .color_check import color_check_scenes, color_distance_series

        scenes = color_check_scenes(scenes, color_distance_series(ws.video))
        write_text_atomic(ws.root / "scenes.json",
                          json.dumps(scenes, ensure_ascii=False, indent=1))
    if args.json:
        print(json.dumps(scenes, ensure_ascii=False))
    else:
        for sc in scenes:
            color = ""
            if "color_confirmed" in sc:
                color = ("  색 확인" if sc["color_confirmed"]
                         else "  조명 오탐 의심")
            print(f"{sc['t']:.1f}s  score={sc['score']}{color}")
        if not scenes:
            print("no scene changes")
        elif args.color_check:
            confirmed = sum(1 for sc in scenes if sc["color_confirmed"])
            print(f"색 교차확인: {confirmed}/{len(scenes)} 확인 — "
                  f"조명 오탐 의심 {len(scenes) - confirmed}건")
    return 0


def cmd_clip(args) -> int:
    from .clip import extract_clip
    from .runtime_config import (
        ClipEncoder,
        load_runtime_config,
        resolve_clip_encoder,
    )

    hw = args.hw
    if args.accurate and hw is None:
        selected = resolve_clip_encoder(load_runtime_config())
        if selected is ClipEncoder.H264_VIDEOTOOLBOX:
            hw = "h264"
        elif selected is ClipEncoder.HEVC_VIDEOTOOLBOX:
            hw = "hevc"
    out = extract_clip(
        _ws(args.workspace),
        parse_ts(args.start),
        parse_ts(args.end),
        name=args.output,
        accurate=args.accurate,
        hw=hw,
    )
    print(out)
    return 0


def cmd_export(args) -> int:
    from .export import (
        export_edl,
        export_fcpxml,
        export_md,
        export_otio,
        export_srt,
        export_xml,
        validate_export_request,
    )

    ws = _ws(args.workspace)
    ids = args.ids.split(",") if args.ids else None
    exporters = {"edl": export_edl, "md": export_md,
                 "xml": export_xml, "otio": export_otio,
                 "fcpxml": export_fcpxml, "srt": export_srt}
    want_receipt = bool(getattr(args, "receipt", False))
    sequence_id = getattr(args, "sequence", None)
    # 옵션 조합 정책은 export.py의 포맷 능력표가 소유한다 — 어댑터는 판정
    # 결과만 소비한다(main()이 ValueError를 error: 한 줄로 접는다).
    validate_export_request(
        args.format, ids=ids, sequence_id=sequence_id,
        receipt=want_receipt, output=args.output)
    selection = None
    direct_checkpoints = None
    if sequence_id:
        from .sequence import sequence_cut_selection

        selection = sequence_cut_selection(ws, sequence_id)
        text = exporters[args.format](ws, None, selection=selection)
    elif want_receipt:
        # 렌더와 Receipt가 같은 스냅샷을 봐야 한다 — 각자 원장을 읽으면
        # 그 사이 갱신이 끼어들어 아티팩트와 다른 리비전을 인증한다.
        from .checkpoint_selection import selected_checkpoints

        direct_checkpoints = selected_checkpoints(ws, ids)
        text = exporters[args.format](ws, None, selection=direct_checkpoints)
    else:
        text = exporters[args.format](ws, ids)
    if args.output:
        # 본 산출물도 원자적으로 — 사이드카 Receipt만 atomic이면 중단 시
        # 절단된 EDL 옆에 완전한 Receipt가 남아 sha256이 불일치한다.
        write_text_atomic(Path(args.output), text)
        print(args.output)
        if want_receipt:
            from .export import build_receipt

            receipt = build_receipt(
                text, ws=ws, fmt=args.format, selection=selection,
                checkpoints=direct_checkpoints)
            sidecar = Path(str(args.output) + ".receipt.json")
            write_text_atomic(
                sidecar,
                json.dumps(receipt, ensure_ascii=False, indent=1) + "\n")
            print(sidecar)
    else:
        print(text)
    return 0


def cmd_sequence_add(args) -> int:
    from .sequence import SEQUENCE_SCHEMA_EXAMPLE, append_sequence

    ws = _ws(args.workspace)
    if args.json_file:
        raw = (
            sys.stdin.read()
            if args.json_file == "-"
            else Path(args.json_file).read_text()
        )
    else:
        raw = args.json
    if not raw:
        raise ValueError(
            "--json 또는 --json-file이 필요합니다\n" + SEQUENCE_SCHEMA_EXAMPLE)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON 파싱 실패({e}) — 한글·따옴표가 섞이면 "
            "`--json-file -` + heredoc으로 넣으십시오\n"
            + SEQUENCE_SCHEMA_EXAMPLE) from None
    try:
        added = append_sequence(ws, obj)
    except ValueError as e:
        raise ValueError(str(e) + "\n" + SEQUENCE_SCHEMA_EXAMPLE) from None
    print(f"sequence {added['id']} ({added['status']}, "
          f"컷 {len(added['cuts'])}개) 기록됨")
    return 0


def cmd_sequence_list(args) -> int:
    from .sequence import SEQ_STATUS_KO, load_sequences

    ws = _ws(args.workspace)
    sequences = sorted(load_sequences(ws), key=lambda s: s["id"])
    if args.json:
        print(json.dumps(sequences, ensure_ascii=False, indent=2))
        return 0
    for seq in sequences:
        status = SEQ_STATUS_KO.get(seq["status"], seq["status"])
        print(f"{seq['id']}  [{status}]  컷 {len(seq['cuts'])}개  "
              f"{seq.get('intent', '')}")
    if not sequences:
        print("(편집안 없음 — va sequence add로 기록)", file=sys.stderr)
    return 0


def cmd_reframe(args) -> int:
    from .reframe import parse_roi, reframe

    dest, timeline = reframe(
        _ws(args.workspace), args.clip,
        [parse_roi(r) for r in args.roi],
        mode=args.mode, min_dwell=args.min_dwell, name=args.output,
    )
    print(dest)
    for seg in timeline[:20]:
        print(f"  {seg['start']:.1f}s–{seg['end']:.1f}s  roi={seg['roi']}",
              file=sys.stderr)
    return 0
