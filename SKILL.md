---
name: multi-shot-review
description: Run repeated non-interactive Codex CLI review passes after broad or risky project changes. Use strong models with high reasoning, choose broad or sliced native review instances, validate findings, fix real issues, add useful regression tests, and repeat until reviews are quiet or no findings are actionable.
---

# Multi-Shot Review

Use this after substantial code changes, especially broad refactors, contract changes, migrations, or edits spanning multiple files.

## Workflow

1. Inspect only the changes you are responsible for reviewing. Ignore unrelated user changes.
2. Initialize review state once from the repo being reviewed:

```bash
REVIEW_DIR="$(python3 /path/to/this-skill/scripts/init_state.py)"
```

3. Register review slices. Use broad native slices for small changes and focused prompted slices for larger or riskier changes.

Broad uncommitted slice:

```bash
python3 /path/to/this-skill/scripts/add_slice.py --review-dir "$REVIEW_DIR" --name broad-1 --uncommitted
```

Prompted focused slice:

```bash
python3 /path/to/this-skill/scripts/add_slice.py --review-dir "$REVIEW_DIR" --name api-contracts --prompt-file - <<'EOF'
Review the current uncommitted changes.
Slice: API and data-contract changes only.
Scope: <exact features, directories, files, or contracts in this slice>.
Focus on request/response compatibility, validation, migration risks, and call sites.
Ignore unrelated UI, styling, and mechanical refactor churn unless it breaks this slice.
EOF
```

Structure slice:

```bash
python3 /path/to/this-skill/scripts/add_slice.py --review-dir "$REVIEW_DIR" --name structure --prompt-file - <<'EOF'
Review the current uncommitted changes.
Slice: project structure and maintainability.
Read /path/to/this-skill/references/software-structure.md and apply those guidelines.
Focus on colocation, file sizing, naming, reuse boundaries, state modeling, and over/under-abstraction.
EOF
```

4. Run the state-managed review pass:

```bash
python3 /path/to/this-skill/scripts/run_reviews.py --review-dir "$REVIEW_DIR"
```

5. Read each produced review file. Validate every finding against the actual code and task intent.
6. Fix only real, relevant findings. Add focused regression tests when they materially reduce risk.
7. If a slice's latest run has findings and you ignore one or more findings from that run, report the ignored count. The script decides whether that count completes the slice or leaves a follow-up run required:

```bash
python3 /path/to/this-skill/scripts/report_ignored_findings.py --review-dir "$REVIEW_DIR" --slice api-contracts --count 2
```

8. Run the relevant tests or checks.
9. Call `run_reviews.py` again after fixes or ignored-finding reports. Keep calling it until it prints `done`.

## Slice Selection

- Small changes: add 2 broad slices for up to 5 meaningful files, 3 for 6-10, and 4 for 11-20. Mechanical refactor churn does not need to increase the count.
- Bigger changes: prefer focused slices by feature, contract, subsystem, or risk area. Do not treat 4 as a limit.
- Add cross-cutting slices when useful: project structure, API/data contracts, migrations, tests/edge cases, performance, security, or UI flows.
- For native slices, use `--base <branch>` or `--commit <sha>` instead of `--uncommitted` when that is the correct review target.
- For prompted slices, put the target in the prompt text, such as `Review changes against main.` or `Review changes introduced by <sha>.`

## Guardrails

- The scripts own state, locking, output names, retry behavior, and deciding whether another pass is needed.
- Do not manually skip follow-up passes when `run_reviews.py` says `call again`.
- Call `report_ignored_findings.py` with the number of findings ignored from the latest slice run. Do not infer completion from that number; let the script and the next `run_reviews.py` call decide.
- Do not treat review output as authoritative. Verify every finding before editing.
- Do not keep iterating when the latest full pass caused no code or test changes.
- Keep fixes scoped to validated review findings and the user’s requested change.
- Do not commit `.review/`.
