"""BGM 비트 그리드 — 스펙트럴 플럭스 온셋 + 자기상관 템포 (고정 템포 모델).

리듬 편집의 전제: 컷·씬 경계를 초 단위 감이 아니라 비트 타임스탬프에
양자화한다(정본=docs/research/2026-08-06-bgm-beat-sync-editing-methodology.md,
재현 스크립트와 실측은 docs/eval/2026-08-06-bgm-beat-grid/). 이 모듈은 그
방법을 1급 아티팩트(beats.json)로 승격한 것이다 — 편집 레인 전용이며 사실
원장(checkpoints)에 쓰는 것은 없다(ADR-0004 쓰기 경계 유지).

librosa 무의존: numpy STFT → 양의 스펙트럴 플럭스 novelty → 60~180BPM
자기상관 템포 → 한 주기 내 위상 탐색 → 비트별 로컬 피크(±12% 주기) 스냅.
고정 템포 가정 — 가변 템포 트랙은 DP 비트트래킹이 필요하며 지원하지
않는다. `tempo_model` 필드가 그 한계를 산출물 자체에 남긴다.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from pathlib import Path
from typing import Final, TypedDict

import numpy as np

from .fsio import write_text_atomic
from .proc import run

SAMPLE_RATE: Final = 22050
N_FFT: Final = 1024
HOP: Final = 512
TEMPO_MODEL: Final = "fixed-autocorrelation"
BPM_LO: Final = 60.0
BPM_HI: Final = 180.0
# 비트 스냅 반경 — 주기의 12%. 더 넓으면 이웃 온셋(하이햇·싱코페이션)을
# 비트로 오인하고, 더 좁으면 실연주의 미세 러버토를 놓친다(실측 채택값).
SNAP_RADIUS_RATIO: Final = 0.12


class BeatGrid(TypedDict):
    """beats.json 스키마 — 편집 레인의 배치 제약 레이어."""

    tempo_model: str
    bpm: float
    beat_times: list[float]
    energy_1s: list[float]
    analyzed_s: float


class BeatExtractionError(ValueError):
    """무음·과단신호 등 비트 추정이 정의되지 않는 입력."""


def decode_pcm_mono(
    media: Path, *, seconds: float | None = None, sr: int = SAMPLE_RATE
) -> np.ndarray:
    """ffmpeg로 모노 f32 PCM을 얻는다 — 영상 파일이면 오디오 트랙."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(media)]
    if seconds is not None:
        cmd += ["-t", str(seconds)]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    proc = run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"오디오 디코드 실패: {media} — {detail}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _spectral_flux(y: np.ndarray) -> np.ndarray:
    """프레임별 양의 스펙트럼 증분 합 — 온셋 novelty 곡선(0~1 정규화)."""
    n_frames = 1 + (len(y) - N_FFT) // HOP
    win = np.hanning(N_FFT)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    mag = np.abs(np.fft.rfft(y[idx] * win, axis=1))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    peak = float(flux.max())
    if peak <= 0.0:
        raise BeatExtractionError("무음 신호 — 비트를 추정할 수 없다")
    return flux / peak


def compute_beat_grid(y: np.ndarray, *, sr: int = SAMPLE_RATE) -> BeatGrid:
    """PCM 배열에서 비트 그리드를 계산한다 (순수 — I/O 없음)."""
    if len(y) < N_FFT * 8:
        raise BeatExtractionError(
            f"오디오가 너무 짧다({len(y)} samples) — 템포 자기상관에 "
            "최소 수 초가 필요하다"
        )
    flux = _spectral_flux(y)
    fps = sr / HOP

    centered = flux - flux.mean()
    ac = np.correlate(centered, centered, mode="full")[len(flux) - 1:]
    lag_lo = int(fps * 60.0 / BPM_HI)
    lag_hi = int(fps * 60.0 / BPM_LO)
    if lag_hi >= len(ac):
        raise BeatExtractionError(
            "오디오가 짧아 60BPM 주기 자기상관을 세울 수 없다"
        )
    # 정수 랙 argmax가 정본이다 — 포물선 보간·스냅 재정박은 실음원에서
    # 정본 그리드(117.45BPM/176박)를 회귀시키는 것이 실측됐다(보간이
    # 템포를 116.53으로 끌고, 재정박은 위상 랜덤워크로 최대 1s 드리프트).
    # 그럴듯한 정밀화가 측정을 이기지 못한다.
    lag = float(lag_lo + int(np.argmax(ac[lag_lo:lag_hi + 1])))
    # 옥타브 교정 — argmax는 2배 랙(절반 템포)을 곧잘 집는다. 특히 참
    # 주기가 비정수 프레임이면 그 봉우리는 인접 2빈으로 갈라지는데 2배
    # 주기는 근사 정수가 되어 한 빈에 온전히 쌓인다(실측: 120BPM,
    # fps 43.07 → 21.5프레임 분할 vs 43프레임 결집). 그래서 절반 랙은
    # 단일 빈이 아니라 분할 빈 합으로도 비교한다. 실음원 정본 케이스는
    # 절반 랙(11)이 하한(14) 아래라 이 루프가 닿지 않는다.
    while lag / 2.0 >= lag_lo:
        base = float(ac[int(round(lag))])
        lo_bin = int(lag / 2.0)
        pair = float(ac[lo_bin]), float(ac[lo_bin + 1])
        clean = max(pair) >= 0.7 * base
        split = min(pair) >= 0.2 * base and sum(pair) >= 0.7 * base
        if not (clean or split):
            break
        lag /= 2.0
    bpm = 60.0 * fps / lag

    period = 60.0 * fps / bpm
    best_phase, best_score = 0, -1.0
    for phase in range(int(period)):
        grid = np.arange(phase, len(flux), period).astype(int)
        score = float(flux[grid[grid < len(flux)]].sum())
        if score > best_score:
            best_phase, best_score = phase, score

    beats: list[float] = []
    pos = float(best_phase)
    radius = max(1, int(period * SNAP_RADIUS_RATIO))
    while pos < len(flux):
        center = int(round(pos))
        lo = max(0, center - radius)
        hi = min(len(flux), center + radius + 1)
        snap = lo + int(np.argmax(flux[lo:hi]))
        beats.append(round(snap / fps, 4))
        pos += period

    hop_s = sr
    rms = [
        round(float(np.sqrt(np.mean(y[i:i + hop_s] ** 2))), 5)
        for i in range(0, len(y) - hop_s, hop_s)
    ]
    return {
        "tempo_model": TEMPO_MODEL,
        "bpm": round(float(bpm), 2),
        "beat_times": beats,
        "energy_1s": rms,
        "analyzed_s": round(len(y) / sr, 2),
    }


def extract_beats(
    media: Path, *, seconds: float | None = None
) -> BeatGrid:
    return compute_beat_grid(decode_pcm_mono(media, seconds=seconds))


def write_beats_json(path: Path, grid: BeatGrid) -> None:
    write_text_atomic(path, json.dumps(grid, ensure_ascii=False))


def load_beats_json(path: Path) -> BeatGrid:
    """beats.json 검증 로드 — 외부 생성 파일(추가 키 허용)도 받는다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"beats.json은 JSON 객체여야 한다: {path}")
    bpm = data.get("bpm")
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or bpm <= 0:
        raise ValueError(f"beats.json의 bpm이 양수가 아니다: {path}")
    raw = data.get("beat_times")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"beats.json의 beat_times가 비어 있다: {path}")
    times: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"beat_times에 숫자가 아닌 값: {path}")
        times.append(float(value))
    if any(b - a <= 0 for a, b in zip(times, times[1:])):
        raise ValueError(f"beat_times는 순증가여야 한다: {path}")
    energy = data.get("energy_1s")
    return {
        "tempo_model": str(data.get("tempo_model", "unknown")),
        "bpm": float(bpm),
        "beat_times": times,
        "energy_1s": [
            float(v) for v in energy
            if not isinstance(v, bool) and isinstance(v, (int, float))
        ] if isinstance(energy, list) else [],
        "analyzed_s": (
            float(data["analyzed_s"])
            if isinstance(data.get("analyzed_s"), (int, float))
            and not isinstance(data.get("analyzed_s"), bool)
            else times[-1]
        ),
    }


def nearest_beat(t: float, beat_times: list[float]) -> float:
    """정렬된 비트열에서 t에 가장 가까운 비트 시각."""
    i = bisect_left(beat_times, t)
    if i == 0:
        return beat_times[0]
    if i == len(beat_times):
        return beat_times[-1]
    before, after = beat_times[i - 1], beat_times[i]
    return before if t - before <= after - t else after
