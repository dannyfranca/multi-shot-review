> **Deprecated**. Use instead: https://github.com/dannyfranca/skills/tree/main/skills/multi-shot-review

# multi-shot-review

`multi-shot-review` runs repeated Codex review slices against a change and keeps state under a `.review/...` directory.

## Foreground review barrier

The runner is a foreground synchronization barrier by default:

```bash
python3 scripts/run_reviews.py --review-dir "$REVIEW_DIR"
```

During a batch, child review stdout and stderr are captured to files under `$REVIEW_DIR/_logs/` instead of streaming to the parent process. When the batch finishes, stdout receives one compact JSON summary.

The same final summary is also written automatically to `$REVIEW_DIR/_last-run.json`. This is a recovery path for humans or tooling if terminal output is lost; normal agent execution should use the single stdout JSON from the foreground command.

For local human debugging, pass `--stream-progress` to emit per-slice progress to stderr while preserving machine-readable stdout. Do not use this for normal agent workflows.

## Summary records

The compact summary includes:

- `ok`: whether the batch completed without runner or child failures
- `st`: `done`, `no_work`, `partial`, `failed`, or `aborted`
- `ran`: number of review attempts launched
- `rem`: remaining incomplete slices
- `out`: review files produced by successful child runs
- `err`: compact error records with log paths when failures occur

Child stdout/stderr and review bodies are intentionally not embedded in the summary JSON.
