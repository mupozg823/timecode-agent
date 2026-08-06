# Verification toolbox

Deep reference for loop §2. `SKILL.md` owns triggers and hard limits; this file
owns backend details and interpretation.

## Transcript trust before mode selection

Sparse text cannot distinguish a quiet video from a stalled decode — use
manifest evidence:

- `transcript_coverage`: transcribed speech ÷ VAD speech. Below 0.5 with 60s+
  of speech signals collapse (short audio exempt). Never infer “visual-led”
  from a collapsed transcript; disclose unresolved low coverage instead of
  forcing a mode.
- `transcript_repair`: automatic recovery ran — after `tail-retranscribe`,
  sample content past the repair boundary.
- `hotwords_rejected`: glossary terms hallucinated repetitively and were
  removed; domain-term errors may remain.
- `asr_backend`: backend provenance (including MLX fallback) for reproduction.

`발화 주도(혼합형)` in the brief means the speech ratio hides a 2+ minute
silent stretch. Never cover it with one checkpoint — run it as its own
visual-driven segment (overview, captures, OCR sweep): timelapses put key
instructions in graphics the transcript never sees.

## Ingest and overview exceptions

- Empty transcript: start with 4–6 evenly spaced overview frames.
- Censored/BGM variants confuse VAD — transcribe the original when one exists
  and visually inspect the variant.
- 10+ scene changes per minute is a montage signal; prioritize scene
  boundaries.
- For `-00N` split filenames, record that the part may omit start or ending.
- Keep overview gaps at most 7 seconds and each tile at most 112 seconds
  (`va filmstrip --auto` applies this density).
- Even sampling can miss the final 1–2 seconds. Do not hardcode `duration-1`;
  inspect the readable tail chosen by `va keyframes <ws> --legible-endcard`.

## Time, direction, and fine detail

- Time-specific questions need the complete span transcript, a 1–2s dense
  overview, and one frame on each boundary. For actions, answer the
  before→after change.
- For left/right, evaluate camera and subject frames. With no discriminator,
  use camera/viewer coordinates and lower confidence.
- Inspect possessions, clothing, and body state across frames 0.3–0.5s apart.
  For fast motion (sports and dancing count, not just impacts) use
  `va capture <ws> -t <t> --sharp --reason <signal>`.

## Image provenance and support

- `--reason` appends image-ID↔cause-ID records to `image-provenance.jsonl`;
  `captures.json` is legacy input only.
- `va index` rebuilds `<workspace>-images.md` plus backlinks from INDEX and
  scene checkpoints to image IDs.
- Image support needs all three: decode inside `frames/`, tracked provenance,
  checkpoint-span overlap. Absolute paths, `..`, external symlinks, partial
  or untracked legacy files stay detail-only.
- Rejection codes:
  - `evidence_provenance_missing`: missing or untracked cause record.
  - `evidence_role_not_verification`: overview filmstrip selects candidates
    but cannot prove a claim; confirm with a full-resolution capture.
  - `evidence_time_unavailable`: no timestamp.
  - `evidence_outside_checkpoint`: frame outside the checkpoint span.

## `verification_audit` codes

- `missing_support`: a terminal checkpoint declares no support.
- `legacy_unstructured`: support uses the old free-form shape.
- `artifact_unavailable`: referenced media can no longer be opened.
- `correction_note_missing`: a `corrected` checkpoint omits what changed.

Non-blocking audit findings — they never rewrite ledgers or change readiness.

## Diarization

```bash
va diarize <ws> [--num-speakers N]
```

- Auto backend: with an HF token try pyannote, else ungated sherpa
  immediately.
- Run before checkpoints, image provenance, sequences, corrections, or
  authored wiki evidence — it advances the transcript revision and refuses
  after transcript-dependent evidence exists.
- A pre-revision markerless workspace is read-only — ingest into a fresh
  output path first. Rewrites use an fsynced rollback journal; interrupted
  rollbacks complete on the next workspace open.
- BGM-heavy content over-segments; prioritize the most-spoken speakers.
- Map anonymous labels (S0/S1) to visual nameplates or microphone flags; set
  known cast size with `--num-speakers`.

## OCR

```bash
va ocr <ws> -t 183 -t 520
va ocr <ws> --every 5 --crop 'iw:ih*0.35:0:ih*0.6'
```

- `--every` can replace ASR for BGM stories or on-screen posts; repeated text
  merges into `ocr_transcript.json`. Crop known subtitle regions against UI
  noise without overfitting away useful repeats.
- OCR suffices for clean overlays and nameplates; confirm stylized or suspect
  text in a full-resolution frame. When captions mirror narration, correct
  suspect ASR segments from OCR.
- In gameplay, pair periodic killfeed OCR with spoken callouts (hard-cut
  scene signals are weak).
- Import failure on macOS means incomplete install; on Linux read frames
  directly.

## Faces

```bash
va faces <ws> -t <ts...>
```

- Count changes signal entrances/exits/composition shifts; scale = max
  face-area ratio and can seed `camera-*` tags.
- Bowed/occluded/rear heads can read zero — counts and scale are lower bounds.

## Scene false positives

ffmpeg scene scores are luma-based — they miss equal-luminance color changes
and mistake lighting flashes for cuts. If detections exceed 10/min while the
transcript describes one situation, run `va scenes --adaptive --color-check`.
Suspicion lowers capture priority; it never auto-deletes a cut.

## Audio events

Sound Analysis proposes laughter, applause, cheering, and screams as learned
placement signals. The score is classifier confidence, not editorial
importance — place candidates with it, then verify meaning against transcript
and surrounding audio/video.
