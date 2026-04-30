---
name: multi-shot-review
description: Run repeated non-interactive Codex CLI review passes after large or risky project file changes. Use after broad edits, touched multiple files or contracts, needs independent review without subagents, should run four native review instances, validate findings, fix real issues, add useful regression tests, and repeat until reviews are quiet or no findings are actionable.
---

# Multi-Shot Review

Use this after substantial code changes, especially broad refactors, contract changes, migrations, or edits spanning multiple files.

## Workflow

1. Inspect the changed scope and choose `--uncommitted`, `--base <branch>`, or `--commit <sha>`.
2. From the repo being reviewed, run this skill's `scripts/new-review-dir.py` once to create and print the review artifact base path.
3. Run four `codex exec review` processes directly. Do not use subagents.
4. Use `--ephemeral` so review runs do not persist live history. Use `-o <file>` for each final review result.
5. Validate each finding against the actual code and task intent. Fix valid issues. Ignore unsuitable, irrelevant, duplicate, or low-value findings.
6. Add or update focused regression tests when they materially reduce risk.
7. Run the relevant tests or checks.
8. Run the four-review pass again after fixes. Repeat until every review is quiet, or a full pass produces only findings that are ignored and no action is taken.

## Review Artifacts

Keep review artifacts under the base path printed by `scripts/new-review-dir.py`. The script creates `.review/<timestamp-random>/` in the repo being reviewed, using a directory-friendly UTC ISO timestamp with millisecond precision plus a random suffix.

Name outputs by pass and run number:

```text
$REVIEW_DIR/1-1.json
$REVIEW_DIR/1-2.json
$REVIEW_DIR/1-3.json
$REVIEW_DIR/1-4.json
$REVIEW_DIR/2-1.json
```

The first number is the review pass; the second is the independent run in that pass. Do not commit `.review/`.

## CLI Shape

```bash
REVIEW_DIR="$(python3 /path/to/this-skill/scripts/new-review-dir.py)"
codex exec review --ephemeral --uncommitted -o "$REVIEW_DIR/1-1.json"
```

Run four independent instances for each pass with different output files: `1-1.json` through `1-4.json`, then `2-1.json` through `2-4.json`, and so on.

## Guardrails

- Do not treat review output as authoritative. Verify every input before editing.
- Do not keep iterating when the latest full pass caused no code or test changes.
- Keep fixes scoped to validated review findings and the user’s requested change.
