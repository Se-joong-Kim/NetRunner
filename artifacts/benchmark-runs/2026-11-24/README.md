# NetRunner benchmark artifacts — 2026-11-24

These development artifacts contain answer-bearing scenario records, diagnostic outputs, probe results, and packet captures. They are included at the project owner's request for debugging and development.

## Original 450-round run

- `original/rounds-fixed-set-docker.jsonl`: all 450 records from the fixed-set comparison.
- `original/report-fixed-set-docker.html`: complete generated HTML report.

## Failure reruns

- `rerun/rounds.jsonl`: 51 rerun round records.
- `rerun/details.jsonl`: per-attempt comparison with the original failure and detailed lab/probe/score fields.
- `rerun/model-io.jsonl`: all 137 OpenCode calls, including prompts and raw stdout/stderr.
- `rerun/persistent-failure-calls.jsonl`: the 36 model calls belonging to the 16 failures that persisted.
- `rerun/analysis.md`: readable per-attempt analysis.
- `rerun/summary.json`, `rerun/manifest.json`: run metadata and totals.

All 44 packet captures referenced by the JSONL files are in `captures/`. `capture-path-map.json` maps the original private absolute paths to the copied relative paths.

The call logger did not record the child process environment. It recorded the prompt, command arguments, return code, stdout/stderr, or timeout details.
