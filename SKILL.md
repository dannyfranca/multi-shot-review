---
name: multi-shot-review
description: Run repeated non-interactive Codex CLI review passes after broad or risky project changes. Use after edits spanning multiple files or contracts, choose broad or sliced native review instances, validate findings, fix real issues, add useful regression tests, and repeat until reviews are quiet or no findings are actionable.
---

# Multi-Shot Review

Use this after substantial code changes, especially broad refactors, contract changes, migrations, or edits spanning multiple files.

## Workflow

1. Inspect the changed scope and choose `--uncommitted`, `--base <branch>`, or `--commit <sha>`.
2. From the repo being reviewed, run this skill's `scripts/new-review-dir.py` once to create and print the review artifact base path.
3. Decide the review round shape:
    - Small changes: run 2 broad instances for up to 5 meaningful files, 3 for 6-10, and 4 for 11-20. Mechanical refactor churn does not need to increase the count.
    - Bigger changes: skip broad generic reviewers. Slice the diff by feature, contract, subsystem, or risk area, and run one focused reviewer per slice. Do not treat 4 as a limit.
    - Add cross-cutting slices when useful: project structure, API/data contracts, migrations, tests/edge cases, performance, security, or UI flows.
4. Use `--ephemeral` so review runs do not persist live history. Use `-o <file>` for each final review result.
5. Validate each finding against the actual code and task intent. Fix valid issues. Ignore unsuitable, irrelevant, duplicate, or low-value findings.
6. Add or update focused regression tests when they materially reduce risk.
7. Run the relevant tests or checks.
8. Run another pass after fixes. Keep valuable slices, drop quiet or low-signal slices, and repeat until every review is quiet or the latest full pass produces only ignored findings.

## Review Artifacts

Keep review artifacts under the base path printed by `scripts/new-review-dir.py`. The script creates `.review/<timestamp-random>/` in the repo being reviewed, using a directory-friendly UTC ISO timestamp with millisecond precision plus a random suffix.

Name outputs by pass and run number or slice:

```text
$REVIEW_DIR/1-1.md
$REVIEW_DIR/1-2.md
$REVIEW_DIR/1-3.md
$REVIEW_DIR/1-4.md
$REVIEW_DIR/1-api-contracts.md
$REVIEW_DIR/1-structure.md
$REVIEW_DIR/2-1.md
```

The first number is the review pass. Use a run number for broad reviewers and a short slice name for focused reviewers. Do not commit `.review/`.

## CLI Shape

Broad small-change review:

```bash
REVIEW_DIR="$(python3 /path/to/this-skill/scripts/new-review-dir.py)"
codex exec review --ephemeral --uncommitted -o "$REVIEW_DIR/1-1.md"
```

Run independent instances with different output files: `1-1.md`, `1-2.md`, then `2-1.md`, `2-2.md`, and so on.

Sliced big-change review. Custom prompts cannot be combined with `--uncommitted`, `--base`, or `--commit`, so put the diff target and slice scope in the prompt text. For base or commit targets, replace the first prompt line with `Review changes against <branch>.` or `Review changes introduced by <sha>.`

```bash
codex exec review --ephemeral -o "$REVIEW_DIR/1-api-contracts.md" - <<'EOF'
Review the current uncommitted changes.
Slice: API and data-contract changes only.
Scope: <exact features, directories, files, or contracts in this slice>.
Focus on request/response compatibility, validation, migration risks, and call sites.
Ignore unrelated UI, styling, and mechanical refactor churn unless it breaks this slice.
EOF
```

Structure slice:

```bash
codex exec review --ephemeral -o "$REVIEW_DIR/1-structure.md" - <<'EOF'
Review the current uncommitted changes.
Slice: project structure and maintainability.
Read /path/to/this-skill/references/software-structure.md and apply those guidelines.
Focus on colocation, file sizing, naming, reuse boundaries, state modeling, and over/under-abstraction.
EOF
```

## Guardrails

- Do not treat review output as authoritative. Verify every input before editing.
- Do not keep iterating when the latest full pass caused no code or test changes.
- Keep fixes scoped to validated review findings and the user’s requested change.
