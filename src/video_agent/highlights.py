"""Audio-energy highlight candidates (placement only).

MIR signal -> JSON -> LLM plan hybrid: this module produces deterministic
energy-burst spans; deciding what they *mean* (selection) is the agent's job
via transcript + frame verification. Pure ffmpeg/stdlib — no extra deps.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from .fsio import write_text_atomic
from .proc import run
from .workspace import Workspace

SAMPLE_RATE = 16000


@dataclass
class Window:
    t: float
    rms_db: float


def rms_windows(video: Path, window: float = 0.5) -> list[Window]:
    """Per-window overall RMS (dB) via ffmpeg astats."""
    nsamples = max(1, round(window * SAMPLE_RATE))
    af = (
        f"aresample={SAMPLE_RATE},asetnsamples=n={nsamples},"
        "astats=metadata=1:reset=1,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    cmd = [
        "ffmpeg", "-loglevel", "error", "-i", str(video),
        "-vn", "-af", af, "-f", "null", "-",
    ]
    res = run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"ffmpeg astats failed ({res.returncode}): {res.stderr.strip()}"
        )
    windows: list[Window] = []
    t = 0.0
    for line in res.stdout.splitlines():
        if line.startswith("frame:"):
            for field in line.split():
                if field.startswith("pts_time:"):
                    t = float(field.removeprefix("pts_time:"))
        elif line.startswith("lavfi.astats.Overall.RMS_level="):
            raw = line.partition("=")[2]
            db = float("-inf") if raw in ("-inf", "nan") else float(raw)
            windows.append(Window(t=t, rms_db=db))
    return windows


def detect_highlights(
    windows: list[Window],
    top: int = 5,
    min_excess_db: float = 3.0,
    gap: int = 1,
    pad: int = 1,
) -> list[dict]:
    """Merge sustained energy peaks into ranked candidate spans."""
    finite = sorted(w.rms_db for w in windows if math.isfinite(w.rms_db))
    n = len(finite)
    if n < 4:
        return []
    median = (finite[(n - 1) // 2] + finite[n // 2]) / 2
    p75 = finite[int(0.75 * (n - 1))]
    threshold = max(p75, median + min_excess_db)

    hot_idx = [
        i for i, w in enumerate(windows)
        if math.isfinite(w.rms_db) and w.rms_db >= threshold
    ]
    if not hot_idx:
        return []
    dt = windows[1].t - windows[0].t if len(windows) > 1 else 0.5

    groups: list[list[int]] = [[hot_idx[0]]]
    for i in hot_idx[1:]:
        if i - groups[-1][-1] <= gap + 1:
            groups[-1].append(i)
        else:
            groups.append([i])

    spans = []
    for g in groups:
        i0 = max(0, g[0] - pad)
        i1 = min(len(windows) - 1, g[-1] + pad)
        hot_vals = [windows[i].rms_db for i in g]
        spans.append({
            "start": round(windows[i0].t, 3),
            "end": round(windows[i1].t + dt, 3),
            "score": round(sum(v - median for v in hot_vals), 1),
            "peak_db": round(max(hot_vals), 1),
        })
    spans.sort(key=lambda s: s["score"], reverse=True)
    return spans[:top]


def compute_highlights(
    ws: Workspace, window: float = 0.5, top: int = 5
) -> list[dict]:
    if not ws.manifest["has_audio"]:
        print("warning: video has no audio stream — no highlight candidates",
              file=sys.stderr)
        spans = []
    else:
        # 의도적으로 오디오 캐시를 쓰지 않는다: 캐시 wav는 -ac 1 모노
        # 다운믹스라 역위상·채널 국지 버스트가 감쇠돼, 캐시 유무에 따라
        # 하이라이트가 달라진다. 원본 채널 레이아웃의
        # RMS가 결정적 정본이다.
        spans = detect_highlights(rms_windows(ws.video, window), top=top)
    write_text_atomic(ws.root / "highlights.json",
                      json.dumps(spans, ensure_ascii=False, indent=1))
    return spans
