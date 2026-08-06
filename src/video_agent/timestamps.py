"""Timestamp parsing/formatting shared by all subcommands."""

from __future__ import annotations


def parse_ts(s: str) -> float:
    """Parse "12.5", "1:23.5", "01:02:03[.25]" into seconds."""
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid timestamp: {s!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid timestamp: {s!r}") from None
    if any(n < 0 for n in nums):
        raise ValueError(f"negative timestamp: {s!r}")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_ts(t: float) -> str:
    """Seconds -> "HH:MM:SS.mmm"."""
    ms = round(t * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_ts_compact(t: float) -> str:
    """사람 표시용: 1시간 미만 "MM:SS.mmm", 이상 "H:MM:SS.mmm".

    fmt_ts(t)[3:] 관용구의 대체 — 그 슬라이스는 1시간 미만을 전제해
    초장편(라이브 VOD)에서 시간 자리를 조용히 절단했다(6:22:21→22:21).
    """
    full = fmt_ts(t)
    hours, rest = full.split(":", 1)
    return rest if hours == "00" else f"{int(hours)}:{rest}"


def fmt_ts_label(t: float) -> str:
    """표시용 라벨: 밀리초가 000이면 잘라 읽기 소음을 줄인다.

    한 페이지에 렌더러가 여럿 실리므로 트리밍 규칙은 여기 한 곳이다 —
    렌더러별 재구현은 같은 컷의 시간 표기를 섹션마다 갈라놓는다.
    """
    return fmt_ts_compact(t).removesuffix(".000")


def frame_name(t: float) -> str:
    """Cache filename for a frame at t seconds: t{ms:09d}.jpg."""
    return f"t{round(t * 1000):09d}.jpg"
