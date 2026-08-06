# Batch ingest dispatch contract

Use for three or more videos when the harness can run independent workers. Parallelize only ingest and deterministic signal extraction; hypothesis formation, visual verification, checkpoint writes, and final selection stay in the main agent so judgments remain consistent.

- Codex: one video per multi-agent worker.
- Claude Code: the Agent tool or `.claude/workflows/tca-batch-ingest.js`.
- No worker tool: run it sequentially in the main agent.

## Worker prompt

```text
[objective]
Ingest one video, extract signals, and return its brief. Do not interpret it.

[scope]
source: {video_path_or_url}
workspace: {absolute_workspace}
  # URLs require -o (stable resume path); local files default CWD/va-out/<stem>.
commands:
  - If {ws}/manifest.json exists, never re-ingest; run only `va brief {ws}`.
  - Otherwise run `va ingest "{video}" --model small --signals [-o {ws}]`.

[acceptance]
- {ws}/manifest.json and transcript.json exist after an exit-0 command.
- Return the complete `va brief {ws}` output.

[boundaries]
- No capture, filmstrip, checkpoint writes, or clip extraction.
- Do not access paths outside the assigned workspace.
- Do not update the glossary; the main agent batches that at session end.

[return: one JSON object]
{"workspace": "<absolute path>", "duration_s": <float>,
 "mode": "<mode recommended by brief>", "chapters": <int>,
 "speech_ratio": <float>, "brief_text": "<complete va brief output>",
 "error": null | "<observed failure from stderr; no guesses>"}
```

The main agent prioritizes videos from the returned `brief_text`, then resumes each with `va brief <ws>`.
