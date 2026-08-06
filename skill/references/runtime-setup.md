# Runtime setup

Read this only when installation, backend recovery, runtime policy, URL access,
or cache placement is relevant. `SKILL.md` owns the preflight checks.

## Install

- If `va` is absent but a checkout exists, run `uv tool install .` at its root.
  For pyannote diarization use `uv tool install --editable '.[diarize]'`;
  bare `.` leaves sherpa as the only diarization backend.
- With only the installed skill, never guess a local source path. Follow the
  distribution README.

## Prepare backends

The default package bundles faster-whisper, MLX Whisper (Apple Silicon), OCR,
Sound Analysis, sherpa, and OTIO; the `diarize` extra adds pyannote.
`va runtime prepare` downloads ungated default models — only remote pyannote
needs an account token and accepted terms.

**An import failure on a supported platform is an incomplete install, not an
optional-feature signal.** Repair from the same distribution; do not degrade.

## Runtime policy

All features start enabled. Use these only when the user changes policy:

```bash
va runtime status
va runtime set feature.<name> on|off
va runtime set profile balanced|low-power|quality
```

`balanced` keeps the measured stable paths: faster-whisper and precise software
clip encoding. Only `low-power` auto-selects MLX and VideoToolbox on Apple
Silicon.

## URLs

Check yt-dlp only for URLs. Use Instagram cookies only when the user explicitly
sets `--cookies-from-browser chrome`. For Threads, ingest a browser-saved file.

## Cache

Set `VIDEO_AGENT_CACHE_DIR` before execution when a shared or sandbox-specific
cache is required. Linux also honors `XDG_CACHE_HOME`.

## Windows

Windows support is experimental. Locks use an msvcrt fallback that degrades
shared locks to exclusive locks. CI covers install, locking, and CLI smoke
tests; ffmpeg-based E2E remains unverified.
