"""va — transcript-first video understanding CLI entrypoint.

명령 핸들러(cmd_*)는 cli_commands_global·cli_commands_workspace가 소유한다
— 이 모듈은 파서 결선과 에러 표면만 가진다. 재수출 shim은 2026-07-25
감사에서 제거했다(87줄 중 76줄이 보일러플레이트, 소비처는 테스트 1곳).
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path

from .cli_parser import build_parser
from .runtime_config import Feature, feature_enabled
from .workspace import Workspace
from .workspace_discovery import corpus_root, find_workspaces
from .workspace_lock import stable_workspace_lock

__all__ = ("build_parser", "main")


def _freeze_command_paths(args) -> None:
    """Resolve user aliases once before deriving leases and handler inputs."""
    if workspace := getattr(args, "workspace", None):
        args.workspace = str(Path(workspace).resolve())
    if args.command in {"audit", "index", "search", "view", "wiki", "skillgen"}:
        if args.roots:
            args.roots = [str(Path(root).resolve()) for root in args.roots]
    elif args.command == "bridge":
        args.corpus = str(Path(args.corpus).resolve())
    elif args.command == "glossary":
        args.workspaces = [
            str(Path(workspace).resolve())
            for workspace in args.workspaces
        ]


def _command_workspace_paths(args) -> list[Path]:
    """Resolve workspaces that must remain live for one CLI command."""
    if workspace := getattr(args, "workspace", None):
        return [Path(workspace).resolve()]

    roots: list[str] | None = None
    if args.command in {"audit", "index", "search", "view", "wiki", "skillgen"}:
        roots = args.roots
    elif args.command == "bridge":
        roots = [args.corpus]
    elif args.command == "glossary":
        roots = list(args.workspaces)
        if args.all:
            roots.append(str(Path.cwd() / "va-out"))
        if not roots:
            return []
    if roots is None:
        return []
    return find_workspaces(roots)


def _dedupe_workspace_paths(paths: list[Path]) -> list[Path]:
    """Canonicalize paths and collapse case/Unicode aliases by inode."""
    resolved_paths = sorted((path.resolve() for path in paths), key=str)
    seen: set[tuple[str, int, int] | tuple[str, str]] = set()
    unique: list[Path] = []
    for path in resolved_paths:
        try:
            file_stat = path.stat()
            identity: tuple[str, int, int] | tuple[str, str] = (
                "inode",
                file_stat.st_dev,
                file_stat.st_ino,
            )
        except OSError:
            identity = ("path", str(path))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(path)
    return unique


def _projection_root(args) -> Path | None:
    """Freeze the projection owner before mutable aliases can be retargeted.

    search가 포함되는 이유: 검색 인덱스 DB(.tca-search-cache.db)의 소유
    루트를 리스 스냅샷과 같은 시점에 확정해야 핸들러가 인덱스를 쓸 수 있다
    — 루트 없이 경로 튜플만 받으면 부분집합과 구분이 안 돼 인덱스를
    우회한다.
    """
    if args.command in {"audit", "index", "view", "wiki", "search", "skillgen"}:
        return corpus_root(args.roots or None).resolve()
    if args.command == "bridge":
        return Path(args.corpus).resolve()
    return None


def _glossary_paths(args) -> tuple[Path, ...] | None:
    """Freeze corrections-bearing inputs, including pre-manifest legacy dirs."""
    if args.command != "glossary":
        return None
    paths = [Path(raw) for raw in args.workspaces]
    if args.all:
        root = Path.cwd() / "va-out"
        paths.extend(path.parent for path in root.rglob("corrections.jsonl"))
    return tuple(_dedupe_workspace_paths(paths))


def _run_with_workspace_leases(args) -> int:
    """Keep GC from claiming any workspace while this command uses it."""
    command_features = {
        "audioevents": Feature.AUDIO_EVENTS,
        "diarize": Feature.DIARIZE,
        "faces": Feature.FACES,
        "ocr": Feature.OCR,
    }
    feature = command_features.get(args.command)
    if args.command == "export" and args.format == "otio":
        feature = Feature.OTIO
    if feature is not None and not feature_enabled(feature):
        raise ValueError(
            f"{feature.value} 기능이 꺼져 있습니다 — "
            f"`va runtime set feature.{feature.value} on`으로 켜십시오"
        )
    _freeze_command_paths(args)
    roots = _dedupe_workspace_paths(_command_workspace_paths(args))
    projection_root = _projection_root(args)
    glossary_paths = _glossary_paths(args)
    with ExitStack() as stack:
        leased_roots: list[Path] = []
        for root in roots:
            if not (root / "manifest.json").is_file():
                continue
            workspace = Workspace.load(root)
            stack.enter_context(
                stable_workspace_lock(
                    workspace.root,
                    workspace.workspace_lock_path,
                    # Legacy workspaces multiplex every logical lock on the
                    # directory inode. Hold that fallback exclusively so an
                    # inner ledger writer remains serialized across processes;
                    # new sidecar workspaces keep normal shared operation
                    # concurrency.
                    exclusive=(
                        args.command == "diarize"
                        or not workspace.workspace_lock_path.is_file()
                    ),
                )
            )
            leased_roots.append(workspace.root.resolve())
        # Handlers must consume the exact snapshot protected above. Re-running
        # discovery inside a handler would admit a workspace published after
        # lease acquisition; retaining a user-supplied symlink would likewise
        # permit retargeting to an unleased workspace.
        args._workspace_paths = tuple(leased_roots)
        if getattr(args, "workspace", None) and roots:
            args.workspace = str(roots[0])
        if projection_root is not None:
            args._projection_root = projection_root
        if glossary_paths is not None:
            args._glossary_paths = glossary_paths
        return args.func(args)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        # cp1252/cp949 콘솔이 한국어 도움말·진단 출력에서 죽는다
        # (UnicodeEncodeError) — 표준 스트림을 UTF-8로 재구성한다.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return _run_with_workspace_leases(args)
    except KeyboardInterrupt:
        # 긴 transcode·다운로드 중 Ctrl-C — traceback 없이 관례 코드로.
        print("interrupted", file=sys.stderr)
        return 130
    except KeyError as e:
        # 워크스페이스 산출물의 필드 결손 — JSONDecodeError는 잡히는데
        # KeyError만 traceback으로 새고 있었다. 결손원은 manifest만이
        # 아니라 words.json 등일 수도 있어 단정하지 않는다.
        print(f"error: missing field {e} — 워크스페이스 산출물(JSON)이 "
              "잘렸거나 구버전일 수 있습니다. manifest.json이 원인이면 "
              "va ingest로 재생성하십시오", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError,
            json.JSONDecodeError) as e:
        # OSError가 FileNotFoundError·PermissionError·디스크풀을 포괄한다.
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
