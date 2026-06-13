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
REVIEW_DIR="$(python3 /path/to/this-skill/scripts/init_state.py --task-file - <<'EOF'
<original user request>
EOF
)"
```

3. Add related/future tasks when part of the requested work is intentionally deferred. These tasks give reviewers context so they do not flag planned follow-up work as a false positive:

```bash
python3 /path/to/this-skill/scripts/add_related_task.py \
  --review-dir "$REVIEW_DIR" \
  --name follow-up-name \
  --text "Describe the related task that will be addressed later."
```

For larger related tasks, use `--file <path>` or `--dir <path>` instead of `--text`.

4. Register review slices. Use broad native slices for small changes and focused prompted slices for larger or riskier changes. Reviewers automatically receive the session task context from `$REVIEW_DIR/task.md`; slice prompts should only describe slice-specific scope.

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

5. Run the state-managed review barrier in the foreground:

```bash
SKILL_DIR=/path/to/this-skill
python3 "$SKILL_DIR/scripts/run_reviews.py" --review-dir "$REVIEW_DIR"
```

6. After the command exits, use the single JSON object it printed. Then read only the review files listed in JSON `out`. If JSON `err` is non-null, inspect only the listed error logs needed to diagnose the failure. A copy of the last summary is also written to `$REVIEW_DIR/_last-run.json` for recovery if terminal output is lost.
7. Validate every finding against the actual code and task intent.
8. Fix only real, relevant findings. Add focused regression tests when they materially reduce risk.
9. If a slice's latest run has findings and you ignore one or more findings from that run, report the ignored count. The script decides whether that count completes the slice or leaves a follow-up run required:

```bash
python3 /path/to/this-skill/scripts/report_ignored_findings.py --review-dir "$REVIEW_DIR" --slice api-contracts --count 2
```

10. Run the relevant tests or checks.
11. Call the review barrier again after fixes or ignored-finding reports. Keep calling it until the JSON object printed by the command has `"ok":true` and `"rem":0`.

## Review Barrier Protocol

`run_reviews.py` is a foreground synchronization barrier.

Required behavior:

- Run it in the foreground.
- Do not run it with `&`, `nohup`, `disown`, `tmux`, `screen`, or an equivalent background mechanism.
- Do not tail logs while it runs.
- Do not inspect partial review outputs while it runs.
- Do not narrate progress while it runs.
- Do not start implementation, summarization, or follow-up reasoning until the command exits.
- Prefer a very long/no command timeout when the harness supports timeout configuration.
- Recommended minimum timeout: 2 hours / 7,200,000 ms.

Recommended invocation:

```bash
SKILL_DIR=/path/to/this-skill
python3 "$SKILL_DIR/scripts/run_reviews.py" --review-dir "$REVIEW_DIR"
```

After the command exits, read only:

1. The single JSON object printed by the command
2. Review files listed in the JSON `out` array
3. Error logs listed in the JSON `err` array, only when needed

The runner also writes the same final summary to `$REVIEW_DIR/_last-run.json` as the standard last-run record, but the normal agent workflow should use the single stdout JSON object and avoid extra pre-review file-reading steps.

For local human debugging only, `--stream-progress` can be used to emit per-slice progress to stderr. Do not use `--stream-progress` in agent workflows or in this skill's recommended command.

## Slice Selection

- Small changes: add 2 broad slices for up to 5 meaningful files, 3 for 6-10, and 4 for 11-20. Mechanical refactor churn does not need to increase the count.
- Bigger changes: prefer focused slices by feature, contract, subsystem, or risk area. Do not treat 4 as a limit.
- Add cross-cutting slices when useful: project structure, API/data contracts, migrations, tests/edge cases, performance, security, or UI flows.
- For native slices, use `--base <branch>` or `--commit <sha>` instead of `--uncommitted` when that is the correct review target.
- For prompted slices, put the target in the prompt text, such as `Review changes against main.` or `Review changes introduced by <sha>`.

## Guardrails

- The scripts own state, locking, output names, retry behavior, and deciding whether another pass is needed.
- Do not manually skip follow-up passes when the summary says work remains.
- Do not manually edit task-context paths in prompts. Initialize with the original user request and use `add_related_task.py`; the runner attaches `$REVIEW_DIR/task.md` consistently.
- Call `report_ignored_findings.py` with the number of findings ignored from the latest slice run. Do not infer completion from that number; let the script and the next `run_reviews.py` call decide.
- Do not treat review output as authoritative. Verify every finding before editing.
- Do not keep iterating when the latest full pass caused no code or test changes.
- Keep fixes scoped to validated review findings and the user’s requested change.
- Do not commit `.review/`.
