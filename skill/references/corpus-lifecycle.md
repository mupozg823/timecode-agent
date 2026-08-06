# Corpus lifecycle

Read this only to resume, search, update, or clean a multi-video corpus.
`SKILL.md` owns first-pass analysis, `verification-toolbox.md` owns evidence
tools, and `wiki-schema.md` owns labels.

## Resume and search

- If `<ws>/manifest.json` exists, resume with `va brief <ws>`; never re-ingest.
- For scene recall, run `va search "<query>"` before walking workspaces.
  `hypothesized` search hits are navigation hints, not answer evidence.
- End sessions with `va index` (rebuilds INDEX, scene logs, group hubs);
  start the next session from `INDEX.md`.
- `va view` HTML derives from Markdown ledgers — safe to delete; it falls
  back when media has been cleaned.

## Transcript corrections and glossary

Write `<ws>/corrections.jsonl` only after visual evidence proves an ASR error.

```json
{"span":[12.0,14.2],"asr":"기태 먹었어요","corrected":"큐 키트 먹었어요","basis":"프레임 근거"}
```

- Prefix non-correction annotations in `corrected` with `(` so their words
  cannot enter hotwords.
- Glossary candidates: proper nouns, domain terms, channel spellings.
  Common-word corrections stay local to the video.
- At session end run `va glossary --all` or `va glossary <ws>...`.
  Inject video-local terms with `va ingest --hotwords "용어1 용어2"`.
- Fine-tune only after the same error recurs 3+ times despite hotwords.
  Never inject the entire glossary into every video.

## Wiki promotion and skill routing

- End sessions with `va index && va wiki`.
- Promote only `verified`/`corrected` checkpoints with currently resolvable
  visual or transcript support into the active semantic layer.
- Preserved prose is regeneration input — never discard wiki `tca:notes` or
  scene-log narrative blocks.
- Follow [wiki schema](wiki-schema.md) for labels, relations, narratives, and
  index-first queries.
- `va skillgen` compiles recurring (3+ videos), visually grounded,
  intent-coherent procedures into approval-pending drafts under
  `wiki/skills/` (inactive until a human approves). `--route` reports per
  task which gate still blocks (`blocked-intent` names off-domain sources
  to retag or split).

## Batch

For three or more videos, read the [batch contract](batch-ingest.md).
Parallelize ingest and deterministic signals only. The main agent performs
hypotheses, frame interpretation, checkpoint writes, and final selection.

## Storage hygiene

Inspect size with `va gc` report mode first. Delete only after the user names
the scope.

```bash
va gc --purge captures --yes
va gc --purge media --yes
va gc --purge workspace --keep-days 30 --yes
```

- Without `--yes`, every command is a dry run.
- `captures` removes regenerable captures and filmstrips.
- `media` removes downloaded sources; recapture then requires URL redownload.
- `clips` are deliverables; remove them only when explicitly requested.
- `workspace` requires `--keep-days N`.
- Category purges preserve text ledgers such as manifest, transcript,
  checkpoints, corrections, glossary, and markers.
