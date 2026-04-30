---
name: multi-shot-review
description: Run repeated non-interactive Codex CLI review passes after large or risky project file changes. Use after broad edits, touched multiple files or contracts, needs independent review without subagents, should run four native review instances, validate findings, fix real issues, add useful regression tests, and repeat until reviews are quiet or no findings are actionable.
---

# Multi-Shot Review

Use this after substantial code changes, especially broad refactors, contract changes, migrations, or edits spanning multiple files.

## Workflow

1. Inspect the changed scope and choose `--uncommitted`, `--base <branch>`, or `--commit <sha>`.
2. Run four `codex exec review` processes directly. Do not use subagents.
3. Use `--ephemeral` so review runs do not persist live history. Use `-o <file>` for each final review result.
4. Validate each finding against the actual code and task intent. Fix valid issues. Ignore unsuitable, irrelevant, duplicate, or low-value findings.
5. Add or update focused regression tests when they materially reduce risk.
6. Run the relevant tests or checks.
7. Run the four-review pass again after fixes. Repeat until every review is quiet, or a full pass produces only findings that are ignored and no action is taken.

## CLI Shape

Prefer this pattern, adjusting target and filenames:

```bash
codex exec review --ephemeral --uncommitted -o .review/review-1.json
```

Run four independent instances with different output files.

## Guardrails

- Do not treat review output as authoritative. Verify every input before editing.
- Do not keep iterating when the latest full pass caused no code or test changes.
- Keep fixes scoped to validated review findings and the user’s requested change.
