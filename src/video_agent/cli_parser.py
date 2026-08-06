"""Public argparse schema for the video-agent CLI."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version

from . import cli_commands_global as global_commands
from . import cli_commands_workspace as workspace_commands
from .cli_ask_parser import add_ask_parser
from .cli_runtime_parser import add_runtime_parser

# 19개 명령이 공유하는 필수 위치 인자 — help 없이는 usage 한 줄만 보고
# 무엇을 넣는지 알 수 없다.
_WS_HELP = "워크스페이스 경로 (예: va-out/<영상이름>)"


def _package_version() -> str:
    try:
        return version("video-agent")
    except PackageNotFoundError:
        return "0+unknown"


def build_parser() -> argparse.ArgumentParser:
    # 상태 값은 원장 스키마가 정본이다 — 파서가 목록을 재정의하면
    # 검증과 표면이 갈라진다. import는 여기서(모듈 상단은 workspace를
    # 조기 로드해 CLI 시작을 무겁게 한다).
    from .checkpoint_schema import (
        STATUSES as CHECKPOINT_STATUSES,
    )
    from .checkpoint_schema import (
        PersonPresenceState,
    )

    parser = argparse.ArgumentParser(
        prog="va",
        description="Transcript-first, lazily-verified video understanding toolkit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add_runtime_parser(sub)

    p = sub.add_parser("ingest",
                       help="probe + transcribe -> workspace (local path or "
                            "URL via yt-dlp, captions-first)")
    p.add_argument("video", help="video file path or http(s) URL")
    p.add_argument("--max-height", type=int, default=1080,
                   help="URL download resolution cap (default 1080)")
    p.add_argument("--cookies-from-browser",
                   help="login-walled URL (e.g. Instagram): pass browser "
                        "cookies to yt-dlp — chrome|safari|firefox. "
                        "본인 세션이 필요할 때만 직접 지정")
    p.add_argument("-o", "--out", help="workspace dir (default ./va-out/<stem>)")
    p.add_argument("--model", default="small",
                   help="whisper size (tiny|base|small|medium|large-v3), "
                        "or any CTranslate2 model — local path or HF repo id "
                        "(e.g. Korean fine-tune ct2 conversion)")
    p.add_argument(
        "--asr-backend",
        choices=["auto", "faster-whisper", "mlx"],
        default="auto",
        help=(
            "ASR 실행 경로 (default auto: balanced/quality=faster-whisper, "
            "Apple Silicon low-power=MLX+VAD+품질 폴백)"
        ),
    )
    p.add_argument("--lang", help="e.g. ko, en (default: auto-detect)")
    p.add_argument("--hotwords",
                   help="domain terms fed to whisper (default: auto-load "
                        "from `va glossary` cache)")
    p.add_argument("--force-whisper", action="store_true",
                   help="transcribe even when the video ships subtitles")
    p.add_argument("--signals", action="store_true",
                   help="also compute highlights+scenes and print the brief "
                        "(one-call session start)")
    p.set_defaults(func=global_commands.cmd_ingest)

    p = sub.add_parser("brief",
                       help="one-call agent briefing (manifest/transcript/"
                            "signals/checkpoints/mode)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--why", default=None,
                   help="분석 의도/질문 — 관련 전사 구간·체크포인트를 "
                        "브리핑에 표면화")
    p.set_defaults(func=workspace_commands.cmd_brief)

    add_ask_parser(sub, _WS_HELP, workspace_commands.cmd_ask)

    p = sub.add_parser("capture", help="capture frames at timestamps")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("-t", action="append", required=True,
                   help="timestamp (12.5 | 1:23.5 | 01:02:03), repeatable")
    p.add_argument("--crop",
                   help="ffmpeg crop expr w:h:x:y (iw/ih ok) — capture just "
                        "a UI region, e.g. 'iw*0.35:ih*0.3:0:ih*0.6'")
    p.add_argument("--reason", action="append",
                   help="why this capture: the triggering signal "
                        "(burst-97s | scene-change | endcard | gap-verify …) "
                        "— appended as a causal edge to image-provenance.jsonl "
                        "for session-crossing auditability. repeatable: "
                        "1개면 전체 프레임, N개면 -t별 순서 페어링")
    p.add_argument("--sharp", action="store_true",
                   help="sharpness gate: pick the crispest of t±window "
                        "(sobel edge energy, 3 candidates) — motion-blur "
                        "safe single capture; full-frame only")
    p.add_argument("--window", type=float, default=0.3,
                   help="sharp-gate candidate offset seconds (default 0.3)")
    p.set_defaults(func=workspace_commands.cmd_capture)

    p = sub.add_parser("keyframes",
                       help="budget-aware capture plan — signal weight + "
                            "time-coverage greedy pick when candidates "
                            "exceed the capture budget")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--budget", type=int, default=12,
                   help="캡처 예산 장수 (default 12)")
    p.add_argument("--start", default=None,
                   help="window start (12.5 | 1:23.5 | 01:02:03)")
    p.add_argument("--end", default=None, help="window end")
    p.add_argument("--min-gap", type=float, default=1.0, dest="min_gap",
                   help="minimum seconds between picks (default 1.0)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="JSON 출력")
    p.add_argument("--explain", action="store_true",
                   help="탈락 후보와 사유(min_gap_displaced/budget_exhausted) 표시")
    p.add_argument("--legible-endcard", action="store_true",
                   dest="legible_endcard",
                   help="엔드카드를 고정 duration-1 대신 꼬리 후보 중 "
                        "가장 정적인(판독 가능한) 시점으로 — 프레임 디코드 필요")
    p.set_defaults(func=workspace_commands.cmd_keyframes)

    p = sub.add_parser("boundary-eval",
                       help="컷 경계 결정적 자문 지표(쿨레쇼프 루프 기계 "
                            "게이트) — 발화 절단·호흡·음량 급차·조인 병치")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("-t", dest="ts", action="append",
                   help="cut timestamp (12.5 | 1:23.5 | 01:02:03), repeatable")
    p.add_argument("--sequence", default=None,
                   help="편집 원장 시퀀스 id — 전 컷 경계+렌더 조인을 "
                        "한 번에 게이트 (-t와 배타)")
    p.add_argument("--window", type=float, default=0.4,
                   help="경계 전후 RMS 측정 구간 초 (default 0.4)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_boundary_eval)

    p = sub.add_parser("beat-eval",
                       help="컷 조인 비트 정합 게이트 — 출력 타임라인 조인 "
                            "vs 최근접 비트 오프셋. p90>gate-ms면 종료코드 1. "
                            "--snap은 span 제안만(원장 불변)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--beats", required=True,
                   help="va beats가 만든 beats.json 경로")
    p.add_argument("--sequence", required=True, help="편집 원장 시퀀스 id")
    p.add_argument("--gate-ms", type=float, default=40.0, dest="gate_ms",
                   help="p90 허용 오프셋 ms (default 40 — 방법론 수락 기준)")
    p.add_argument("--snap", action="store_true",
                   help="조인을 비트에 앉히는 컷 span 제안 출력")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_beat_eval)

    p = sub.add_parser("audioevents",
                       help="semantic audio events — laughter/applause/"
                            "cheering via bundled macOS Sound Analysis")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--min-conf", type=float, default=0.6,
                   help="이벤트 최소 확신도 (default 0.6)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_audioevents)

    p = sub.add_parser("diarize",
                       help="pyannote 3.1 화자분리 — transcript에 speaker 병합 "
                            "(HF 토큰/약관 동의 시 pyannote, 아니면 sherpa)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--num-speakers", type=int,
                   help="화자 수 힌트 (생략 시 자동 추정)")
    p.add_argument("--backend", choices=["auto", "pyannote", "sherpa"],
                   default="auto",
                   help="auto=pyannote(HF토큰) 우선, 실패 시 무게이트 sherpa 폴백")
    p.set_defaults(func=workspace_commands.cmd_diarize)

    p = sub.add_parser("faces",
                       help="on-screen face count per timestamp (Vision; "
                            "cast-composition signal)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("-t", action="append", required=True,
                   help="timestamp (12.5 | 1:23.5 | 01:02:03), repeatable")
    p.add_argument("--crop", help="ffmpeg crop expr w:h:x:y")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_faces)

    p = sub.add_parser("ocr",
                       help="on-device Vision OCR of frames (nameplates/"
                            "killfeed/overlays; bundled on macOS)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("-t", action="append",
                   help="timestamp, repeatable")
    p.add_argument("--every", help="scan mode: OCR every N seconds, merge "
                                   "repeats -> ocr_transcript.json "
                                   "(caption-driven/speechless videos)")
    p.add_argument("--start", help="scan span start (default 0)")
    p.add_argument("--end", help="scan span end (default duration)")
    p.add_argument("--crop", help="ffmpeg crop expr w:h:x:y")
    p.add_argument("--lang", help="comma list, default ko-KR,en-US")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_ocr)

    p = sub.add_parser("filmstrip",
                       help="tile n low-res frames of a span into ONE image "
                            "(overview scan, not verification)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--start", help="default: 0 (full-video overview)")
    p.add_argument("--end", help="default: video duration")
    p.add_argument("-n", type=int, default=9,
                   help="타일에 넣을 프레임 수 (default 9)")
    p.add_argument("--cols", type=int, default=3,
                   help="타일 열 수 (default 3)")
    p.add_argument("--auto", action="store_true",
                   help="density-rule tiling: split span into <=112s windows "
                        "with <=7s cell gap (multiple tiles as needed)")
    p.set_defaults(func=workspace_commands.cmd_filmstrip)

    p = sub.add_parser("checkpoint",
                       help="사실 원장 — 시간 주소 체크포인트 "
                            "(append-only, same id = update)")
    cp_sub = p.add_subparsers(dest="cp_command", required=True)
    pa = cp_sub.add_parser("add", help="append (same id = update)")
    pa.add_argument("workspace", help=_WS_HELP)
    pa.add_argument("--json", help="checkpoint object as JSON string")
    pa.add_argument("--json-file", help="path to JSON file, '-' for stdin")
    # 필드 플래그 — 스키마를 인터페이스에 드러낸다. 한글·따옴표가 섞인
    # 본문은 여전히 --json-file - + heredoc이 안전하다(셸 이스케이프).
    pa.add_argument("--id", dest="cp_id",
                    help="체크포인트 id (예: cp-001). 같은 id = 새 리비전")
    pa.add_argument("--span", nargs=2, metavar=("START", "END"),
                    help="구간 — 초 또는 mm:ss (예: --span 0 42.5 / 1:23 2:05)")
    pa.add_argument("--status", choices=CHECKPOINT_STATUSES,
                    help="가설 상태 — 검증 전 hypothesized, 확인 verified, "
                         "정정 corrected (terminal→hypothesized 되돌리기 불가)")
    pa.add_argument("--hypothesis",
                    help="그 구간에서 무엇이 벌어지는가 — 사람이 읽는 문장")
    pa.add_argument("--confidence", type=float,
                    help="0..1 확신도 (0.7 미만이면 시각 검증 대상)")
    pa.add_argument("--segments",
                    help="근거 전사 세그먼트 인덱스 (쉼표 구분: 0,1,2)")
    pa.add_argument("--visual-evidence", action="append", metavar="PATH",
                    help="<ws> 상대 프레임 경로 (반복 지정 가능)")
    pa.add_argument("--note", help="정정 사유·특이사항 — 사람이 읽는 문장")
    pa.set_defaults(func=workspace_commands.cmd_checkpoint_add)
    po = cp_sub.add_parser(
        "observe",
        help="선택한 전체 프레임을 사람 존재 관측과 함께 기록",
    )
    po.add_argument("workspace", help=_WS_HELP)
    po.add_argument("--id", dest="cp_id", required=True, help="체크포인트 id")
    po.add_argument(
        "--frame",
        required=True,
        help="<ws> 상대 경로인 provenance 추적 전체 프레임",
    )
    po.add_argument("--subject", required=True, help="관측 대상")
    po.add_argument(
        "--state",
        required=True,
        choices=tuple(state.value for state in PersonPresenceState),
        help="화면 존재 상태; 판독 불가는 uncertain",
    )
    po.add_argument("--hypothesis", required=True, help="프레임에서 본 내용")
    po.set_defaults(func=workspace_commands.cmd_checkpoint_observe)
    pl = cp_sub.add_parser("list", help="기록된 체크포인트 목록")
    pl.add_argument("workspace", help=_WS_HELP)
    pl.add_argument("--json", action="store_true", help="JSON 출력")
    pl.set_defaults(func=workspace_commands.cmd_checkpoint_list)

    p = sub.add_parser(
        "sequence",
        help="편집 결정 원장 — 근거 강제 컷 시퀀스 (append, same id = update)")
    seq_sub = p.add_subparsers(dest="seq_command", required=True)
    sa = seq_sub.add_parser("add", help="append (same id = update)")
    sa.add_argument("workspace", help=_WS_HELP)
    sa.add_argument("--json", help="sequence object as JSON string")
    sa.add_argument("--json-file", help="path to JSON file, '-' for stdin")
    sa.set_defaults(func=workspace_commands.cmd_sequence_add)
    sl = seq_sub.add_parser("list", help="기록된 편집안 목록")
    sl.add_argument("workspace", help=_WS_HELP)
    sl.add_argument("--json", action="store_true", help="JSON 출력")
    sl.set_defaults(func=workspace_commands.cmd_sequence_list)

    p = sub.add_parser("glossary",
                       help="corrections.jsonl -> whisper hotwords 용어사전 "
                            "(무학습 정확도 레버)")
    p.add_argument("workspaces", nargs="*",
                   help="갱신할 워크스페이스들 (생략 시 현황 출력)")
    p.add_argument("--all", action="store_true",
                   help="./va-out 하위 전 워크스페이스의 corrections 수집")
    p.set_defaults(func=global_commands.cmd_glossary)

    p = sub.add_parser("bridge",
                       help="외부 지식 vault에 라우팅 스텁 발행 "
                            "(정본=va-out, 병합 금지·재발행 멱등)")
    p.add_argument("vault", help="장착 대상 브레인 vault 경로 (기존 디렉터리)")
    p.add_argument("--corpus", default="./va-out",
                   help="스텁 소스 코퍼스 루트 (기본 ./va-out)")
    p.set_defaults(func=global_commands.cmd_bridge)

    p = sub.add_parser("status", help="coverage / gaps / confidence")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_status)

    p = sub.add_parser("audit",
                       help="deterministic corpus readiness and evidence audit")
    p.add_argument("roots", nargs="*",
                   help="va-out roots or workspace dirs (default ./va-out)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=global_commands.cmd_audit)

    p = sub.add_parser(
        "rebind",
        help="legacy 워크스페이스를 리비전 결박으로 백필 — 기본 dry-run, "
             "--apply로만 기록. 사후 검증 실패 시 전량 복원")
    p.add_argument("targets", nargs="*",
                   help="va-out roots or workspace dirs (default ./va-out)")
    p.add_argument("--apply", action="store_true",
                   help="실제로 쓴다 (없으면 무엇이 바뀔지만 보고)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=global_commands.cmd_rebind)

    p = sub.add_parser("highlights",
                       help="audio-energy highlight candidates (placement)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--window", type=float, default=0.5,
                   help="RMS window seconds (default 0.5)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_highlights)

    p = sub.add_parser("scenes",
                       help="scene-change timestamps (no frame extraction)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--threshold", type=float, default=0.3,
                   help="장면전환 감도 0~1, 낮을수록 민감 (default 0.3)")
    p.add_argument("--adaptive", action="store_true",
                   help="also catch gradual changes vs rolling average")
    p.add_argument("--color-check", action="store_true",
                   help="cross-check detections with brightness-free H-S "
                        "histogram to flag lighting false positives")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=workspace_commands.cmd_scenes)

    p = sub.add_parser("clip", help="extract a clip")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("-o", "--output", help="filename inside clips/")
    p.add_argument("--accurate", action="store_true",
                   help="re-encode for exact boundaries (default: stream copy)")
    p.add_argument("--hw", choices=["h264", "hevc"],
                   help="VideoToolbox hardware encode (macOS; reduces CPU "
                        "load on supported hardware; use for previews)")
    p.set_defaults(func=workspace_commands.cmd_clip)

    p = sub.add_parser(
        "export", help="EDL / xmeml / FCPXML / OTIO / SRT / markdown handoff")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("--format", required=True,
                   choices=["edl", "md", "xml", "otio", "fcpxml", "srt"])
    p.add_argument("--ids", help="comma-separated checkpoint ids")
    p.add_argument("--sequence",
                   help="편집안 id(seq-001) — 편집 원장의 컷으로 내보내기 "
                        "(edl/otio/fcpxml 전용, --ids와 배타)")
    p.add_argument("-o", "--output", help="출력 파일 경로 (생략 시 stdout)")
    p.add_argument("--receipt", action="store_true",
                   help="<output>.receipt.json 사이드카 — 산출물 sha256+"
                        "근거 체크포인트 리비전/상태 (원장 없는 수신측 검증용, "
                        "-o 필수)")
    p.set_defaults(func=workspace_commands.cmd_export)

    p = sub.add_parser("search",
                       help="cross-workspace lexical search over transcripts/"
                            "checkpoints/OCR (in-memory FTS5, never stale)")
    p.add_argument("query", help="space-separated terms (prefix-matched)")
    p.add_argument("roots", nargs="*",
                   help="va-out roots or workspace dirs (default ./va-out)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.set_defaults(func=global_commands.cmd_search)

    p = sub.add_parser("reframe",
                       help="16:9 clip -> 9:16 shorts: pan (hard-cut between "
                            "face ROIs by motion energy) or split (stacked)")
    p.add_argument("workspace", help=_WS_HELP)
    p.add_argument("clip", help="path or filename inside clips/")
    p.add_argument("--roi", action="append", required=True,
                   metavar="x,y,w,h",
                   help="face ROI in clip pixel coords (repeat per person; "
                        "agent eyeballs these from one captured frame)")
    p.add_argument("--mode", choices=["pan", "split"], default="pan")
    p.add_argument("--min-dwell", type=float, default=1.0,
                   help="min seconds per pan segment (shorter blips merge "
                        "into the previous speaker)")
    p.add_argument("-o", "--output", help="filename inside clips/")
    p.set_defaults(func=workspace_commands.cmd_reframe)

    p = sub.add_parser("index",
                       help="rebuild the corpus vault: va-out/INDEX.md "
                            "(routing map + entity reverse index) + every "
                            "workspace's <name>.md scene log — plain-markdown, "
                            "Obsidian-compatible, self-contained")
    p.add_argument("roots", nargs="*",
                   help="one corpus root or workspaces under one common root "
                        "(default ./va-out)")
    p.add_argument("--graph-reset", action="store_true",
                   help="Obsidian 그래프 표시 프리셋(.obsidian/graph.json)을 "
                        "표준값으로 덮어쓴다 — 기본은 없을 때만 생성")
    p.set_defaults(func=global_commands.cmd_index)

    p = sub.add_parser("view",
                       help="derived HTML views: per-workspace player with "
                            "checkpoint click-to-seek + corpus browser — "
                            "static, self-contained, zero server; markdown "
                            "stays the source of truth")
    p.add_argument("roots", nargs="*",
                   help="one corpus root or workspaces under one common root "
                        "(default ./va-out)")
    p.add_argument("--standalone", action="store_true",
                   help="also write view-standalone.html per workspace — "
                        "captures inlined as data URIs, no video/relative "
                        "refs, for single-file handoff")
    p.set_defaults(func=global_commands.cmd_view)

    p = sub.add_parser("wiki",
                       help="corpus wiki: entity/character pages with "
                            "preserved agent-notes blocks, genre-grouped "
                            "tag index, relation ledger + mermaid graph, "
                            "quote log — deterministic aggregation of "
                            "checkpoint labels (Karpathy LLM-wiki layout)")
    p.add_argument("roots", nargs="*",
                   help="one corpus root or workspaces under one common root "
                        "(default ./va-out)")
    p.add_argument(
        "--include-hypotheses",
        action="store_true",
        help="compatibility projection including hypothesized labels",
    )
    p.set_defaults(func=global_commands.cmd_wiki)

    p = sub.add_parser("skillgen",
                       help="compile promoted procedures (3+ recurring "
                            "videos) into human-approval-pending SKILL "
                            "drafts under wiki/skills — execution steps "
                            "from visually grounded checkpoints only, "
                            "known-weaknesses section mandatory")
    p.add_argument("roots", nargs="*",
                   help="one corpus root or workspaces under one common root "
                        "(default ./va-out)")
    p.add_argument("--route", action="store_true",
                   help="컴파일 없이 라우팅 표만 — 태스크별 게이트 상태"
                        "(draft/blocked-intent/needs-workspaces)와 사유")
    p.set_defaults(func=global_commands.cmd_skillgen)

    p = sub.add_parser("beats",
                       help="BGM 비트 그리드 추출(고정 템포 모델) — 편집 "
                            "레인 배치 제약 beats.json 생성. 컷 양자화 "
                            "게이트는 beat-eval")
    p.add_argument("media", help="오디오 또는 영상 파일(오디오 트랙 사용)")
    p.add_argument("--seconds", type=float, default=None,
                   help="앞에서부터 분석할 길이 초 (default 전체)")
    p.add_argument("--out", default=None,
                   help="beats.json 경로 (default <media옆>.beats.json)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="요약 JSON 출력")
    p.set_defaults(func=global_commands.cmd_beats)

    p = sub.add_parser("gc",
                       help="storage hygiene: per-workspace size report + "
                            "selective purge of media/captures/clips or stale "
                            "workspaces (dry-run unless --yes)")
    p.add_argument("paths", nargs="*",
                   help="workspace or root paths (default ./va-out)")
    p.add_argument("--purge", action="append",
                   metavar="captures|media|clips|workspace",
                   help="delete a category (repeatable or comma-separated). "
                        "media warns 'no re-capture'; text is never touched")
    p.add_argument("--keep-days", type=float,
                   help="with --purge workspace: drop workspaces whose newest "
                        "file is older than N days (required for that mode)")
    p.add_argument("--yes", action="store_true",
                   help="actually delete (otherwise dry-run listing only)")
    p.set_defaults(func=global_commands.cmd_gc)

    return parser
