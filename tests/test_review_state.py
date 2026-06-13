from __future__ import annotations

import io
import json
import os
import fcntl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TIMESTAMPED_REVIEW_FILE_RE = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-"
    r"\d+-[a-z0-9._-]+(?:-retry\d+)?\.md$"
)
TIMESTAMPED_REVIEW_DIR_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{8}$"
sys.path.insert(0, str(SCRIPTS))

import review_state as review_state_module  # noqa: E402
from review_state import (  # noqa: E402
    ReviewState,
    ReviewStateError,
    add_related_task,
    build_review_command,
    classify_output,
    count_findings,
    init_review_state,
    run_reviews,
)


class ReviewStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.review_dir = init_review_state(self.root, "Implement the requested API change.")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_creates_loadable_state(self) -> None:
        state_path = self.review_dir / "_state.json"
        self.assertTrue(state_path.exists())
        self.assertRegex(self.review_dir.name, TIMESTAMPED_REVIEW_DIR_RE)
        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["schema_version"], 1)
        self.assertEqual(state.data["slices"], {})
        self.assertFalse(state.data["completed"])
        task_text = (self.review_dir / "task.md").read_text(encoding="utf-8")
        self.assertIn("Implement the requested API change.", task_text)
        self.assertIn("No related/future tasks registered.", task_text)
        self.assertTrue((self.review_dir / "related-tasks").is_dir())

    def test_init_rejects_empty_task(self) -> None:
        with self.assertRaises(ReviewStateError):
            init_review_state(self.root, "  ")

    def test_related_tasks_update_task_entrypoint_from_text_file_and_directory(self) -> None:
        add_related_task(
            self.review_dir,
            "follow-up",
            text="Address the broader reporting workflow later.",
            file=None,
            directory=None,
        )
        self.assertEqual(
            (self.review_dir / "related-tasks" / "follow-up.md").read_text(encoding="utf-8"),
            "Address the broader reporting workflow later.",
        )
        task_text = (self.review_dir / "task.md").read_text(encoding="utf-8")
        self.assertIn("[follow-up](related-tasks/follow-up.md)", task_text)
        self.assertIn("Implement the requested API change.", task_text)

        source_file = self.root / "later.md"
        source_file.write_text("Tighten the generated docs in a follow-up.", encoding="utf-8")
        add_related_task(self.review_dir, "docs", text=None, file=source_file, directory=None)
        self.assertEqual(
            (self.review_dir / "related-tasks" / "docs.md").read_text(encoding="utf-8"),
            "Tighten the generated docs in a follow-up.",
        )

        source_dir = self.root / "future-work"
        source_dir.mkdir()
        (source_dir / "README.md").write_text("Future workflow notes.", encoding="utf-8")
        add_related_task(self.review_dir, "workflow", text=None, file=None, directory=source_dir)
        self.assertEqual(
            (self.review_dir / "related-tasks" / "workflow" / "README.md").read_text(encoding="utf-8"),
            "Future workflow notes.",
        )
        task_text = (self.review_dir / "task.md").read_text(encoding="utf-8")
        self.assertIn("[docs](related-tasks/docs.md)", task_text)
        self.assertIn("[workflow](related-tasks/workflow/)", task_text)

    def test_related_task_overwrite_switches_between_file_and_directory(self) -> None:
        add_related_task(self.review_dir, "future", text="Future text.", file=None, directory=None)
        self.assertTrue((self.review_dir / "related-tasks" / "future.md").exists())

        source_dir = self.root / "future"
        source_dir.mkdir()
        (source_dir / "README.md").write_text("Directory future task.", encoding="utf-8")
        add_related_task(self.review_dir, "future", text=None, file=None, directory=source_dir)

        self.assertFalse((self.review_dir / "related-tasks" / "future.md").exists())
        self.assertTrue((self.review_dir / "related-tasks" / "future" / "README.md").exists())
        task_text = (self.review_dir / "task.md").read_text(encoding="utf-8")
        self.assertNotIn("[future](related-tasks/future.md)", task_text)
        self.assertIn("[future](related-tasks/future/)", task_text)

    def test_related_task_failed_replace_preserves_existing_context(self) -> None:
        add_related_task(self.review_dir, "future", text="Existing future task.", file=None, directory=None)

        with self.assertRaises(ReviewStateError):
            add_related_task(self.review_dir, "future", text="  ", file=None, directory=None)

        self.assertEqual(
            (self.review_dir / "related-tasks" / "future.md").read_text(encoding="utf-8"),
            "Existing future task.",
        )
        self.assertIn("[future](related-tasks/future.md)", (self.review_dir / "task.md").read_text(encoding="utf-8"))

    def test_related_task_file_replace_failure_preserves_existing_file(self) -> None:
        add_related_task(self.review_dir, "future", text="Existing future task.", file=None, directory=None)
        real_replace = os.replace

        def fail_future_replace(src: Path, dst: Path) -> None:
            if Path(dst).name == "future.md" and Path(src).name.endswith(".tmp.md"):
                raise OSError("replace failed")
            real_replace(src, dst)

        with mock.patch.object(review_state_module.os, "replace", fail_future_replace):
            with self.assertRaises(OSError):
                add_related_task(self.review_dir, "future", text="Replacement task.", file=None, directory=None)

        self.assertEqual(
            (self.review_dir / "related-tasks" / "future.md").read_text(encoding="utf-8"),
            "Existing future task.",
        )

    def test_related_task_type_switch_replace_failure_restores_existing_file(self) -> None:
        add_related_task(self.review_dir, "future", text="Existing future task.", file=None, directory=None)
        source_dir = self.root / "future-source"
        source_dir.mkdir()
        (source_dir / "README.md").write_text("Replacement directory task.", encoding="utf-8")
        real_replace = os.replace

        def fail_final_dir_replace(src: Path, dst: Path) -> None:
            if Path(dst).name == "future":
                raise OSError("replace failed")
            real_replace(src, dst)

        with mock.patch.object(review_state_module.os, "replace", fail_final_dir_replace):
            with self.assertRaises(OSError):
                add_related_task(self.review_dir, "future", text=None, file=None, directory=source_dir)

        self.assertEqual(
            (self.review_dir / "related-tasks" / "future.md").read_text(encoding="utf-8"),
            "Existing future task.",
        )
        self.assertFalse((self.review_dir / "related-tasks" / "future").exists())

    def test_related_task_refresh_failure_restores_existing_file(self) -> None:
        add_related_task(self.review_dir, "future", text="Existing future task.", file=None, directory=None)
        real_replace = os.replace

        def fail_task_entrypoint_replace(src: Path, dst: Path) -> None:
            if Path(dst).name == "task.md":
                raise OSError("task refresh failed")
            real_replace(src, dst)

        with mock.patch.object(review_state_module.os, "replace", fail_task_entrypoint_replace):
            with self.assertRaises(OSError):
                add_related_task(self.review_dir, "future", text="Replacement task.", file=None, directory=None)

        self.assertEqual(
            (self.review_dir / "related-tasks" / "future.md").read_text(encoding="utf-8"),
            "Existing future task.",
        )

    def test_related_task_refresh_preserves_user_request_with_generated_heading_text(self) -> None:
        review_dir = init_review_state(
            self.root,
            "Implement the change.\n\n## Related/Future Tasks\n\nThis heading is part of the user request.",
        )

        add_related_task(review_dir, "later", text="Address this later.", file=None, directory=None)

        task_text = (review_dir / "task.md").read_text(encoding="utf-8")
        self.assertIn("This heading is part of the user request.", task_text)
        self.assertIn("[later](related-tasks/later.md)", task_text)

    def test_related_task_refresh_preserves_user_request_with_sentinel_text(self) -> None:
        request = (
            "Document the generated marker text.\n"
            "<!-- multi-shot-review:original-request:end -->\n"
            "This line is still part of the original request."
        )
        review_dir = init_review_state(self.root, request)

        add_related_task(review_dir, "later", text="Address this later.", file=None, directory=None)

        task_text = (review_dir / "task.md").read_text(encoding="utf-8")
        self.assertIn("This line is still part of the original request.", task_text)
        self.assertIn("[later](related-tasks/later.md)", task_text)

    def test_related_task_directory_cannot_contain_review_directory(self) -> None:
        with self.assertRaises(ReviewStateError):
            add_related_task(self.review_dir, "repo-root", text=None, file=None, directory=self.root)

        self.assertFalse((self.review_dir / "related-tasks" / "repo-root").exists())

    def test_locked_add_slice_and_reload(self) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            state.save()

        reloaded = ReviewState.load(self.review_dir)
        self.assertEqual(reloaded.data["slices"]["api"]["next_pass"], 1)
        self.assertFalse(reloaded.data["slices"]["api"]["complete"])

    def test_rejects_duplicate_and_unsafe_slice_names(self) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            with self.assertRaises(ReviewStateError):
                state.add_slice(
                    name="api",
                    mode="native",
                    target={"uncommitted": True},
                    prompt=None,
                    cwd=self.root,
                )
            with self.assertRaises(ReviewStateError):
                state.add_slice(
                    name="../bad",
                    mode="native",
                    target={"uncommitted": True},
                    prompt=None,
                    cwd=self.root,
                )

    def test_schema_validation_rejects_invalid_state(self) -> None:
        (self.review_dir / "_state.json").write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

    def test_schema_validation_rejects_missing_session_root(self) -> None:
        state_data = json.loads((self.review_dir / "_state.json").read_text(encoding="utf-8"))
        del state_data["session"]["root"]
        (self.review_dir / "_state.json").write_text(json.dumps(state_data), encoding="utf-8")

        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

    def test_schema_validation_rejects_malformed_runs(self) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            state.save()

        state_data = json.loads((self.review_dir / "_state.json").read_text(encoding="utf-8"))
        state_data["slices"]["api"]["runs"].append({"id": "missing-required-fields"})
        (self.review_dir / "_state.json").write_text(json.dumps(state_data), encoding="utf-8")

        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

    def test_schema_validation_rejects_malformed_slice_contracts(self) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            state.add_slice(
                name="prompted",
                mode="prompt",
                target=None,
                prompt="Review API contracts.",
                cwd=self.root,
            )
            state.save()

        state_data = json.loads((self.review_dir / "_state.json").read_text(encoding="utf-8"))
        state_data["slices"]["api"]["target"] = None
        (self.review_dir / "_state.json").write_text(json.dumps(state_data), encoding="utf-8")
        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

        state_data["slices"]["api"]["target"] = {"base": "", "commit": "abc"}
        (self.review_dir / "_state.json").write_text(json.dumps(state_data), encoding="utf-8")
        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

        state_data["slices"]["api"]["target"] = {"uncommitted": True}
        state_data["slices"]["prompted"]["prompt"] = ""
        (self.review_dir / "_state.json").write_text(json.dumps(state_data), encoding="utf-8")
        with self.assertRaises(ReviewStateError):
            ReviewState.load(self.review_dir)

    def test_terminal_state_is_noop(self) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            reservation = state.reserve_eligible()[0]
            state.complete_run(
                run_id=reservation.run_id,
                slice_name="api",
                status="quiet",
                exit_code=0,
                classification="quiet",
            )
            state.save()

        output = io.StringIO()
        rc, summary = run_reviews(self.review_dir, command_runner=_should_not_run, stdout=output)
        self.assertEqual(rc, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(summary["st"], "no_work")


class ClassifierTests(unittest.TestCase):
    def test_findings_patterns(self) -> None:
        self.assertEqual(classify_output("## Review\n[P1] Breaks callers"), "findings")
        self.assertEqual(classify_output('[P1] The classifier treats "No findings" output as quiet'), "findings")
        self.assertEqual(classify_output("Review comment: this leaks state"), "findings")
        self.assertEqual(
            classify_output("Intro text\n\nFull review comments:\n- Validate migration rollback"),
            "findings",
        )

    def test_quiet_patterns(self) -> None:
        self.assertEqual(classify_output("No actionable issues found."), "quiet")
        self.assertEqual(classify_output("LGTM"), "quiet")
        self.assertEqual(classify_output("Full review comments: no findings"), "quiet")
        self.assertEqual(classify_output("Full review comments:\nNo [P1] or [P2] findings."), "quiet")
        self.assertEqual(classify_output("I did not find a discrete CLI or workflow bug."), "quiet")
        self.assertEqual(classify_output("No [P1] findings."), "quiet")
        self.assertEqual(classify_output("There are no [P1] findings."), "quiet")
        self.assertEqual(classify_output("No [P1] or [P2] findings."), "quiet")
        self.assertEqual(classify_output("No [P1], [P2], or [P3] findings."), "quiet")
        self.assertEqual(classify_output("No [P1] or [P2] findings remain."), "quiet")
        self.assertEqual(classify_output("I did not find any [P1] issues."), "quiet")
        self.assertEqual(classify_output("No findings above [P2]."), "quiet")
        self.assertEqual(
            classify_output(
                "The cashier generated client signatures, required company/auth headers, discriminated Pix response "
                "handling, status polling, and query invalidations are consistent with the Mobile BFF cashier contract "
                "and existing generated client style. Targeted typecheck and cashier tests passed."
            ),
            "quiet",
        )
        self.assertEqual(classify_output("No [P1] findings, but [P2] retry can loop forever"), "findings")
        self.assertEqual(
            classify_output("I did not find any API bugs, but the retry loop can run forever"),
            "uncertain",
        )

    def test_uncertain_successful_output(self) -> None:
        self.assertEqual(classify_output("The implementation was inspected carefully."), "uncertain")

    def test_finding_counts(self) -> None:
        self.assertEqual(count_findings("[P1] One\n[P2] Two\nReview comment: three"), 3)
        self.assertEqual(count_findings("Review comment:\n[P2] One issue"), 1)
        self.assertEqual(count_findings("Review comment: [P2] One issue"), 1)
        self.assertEqual(count_findings("Review comment: unprioritized issue"), 1)
        self.assertEqual(count_findings("Review comment: No findings are reported when retry fails"), 1)
        self.assertEqual(count_findings('[P1] The classifier treats "No findings" output as quiet'), 1)
        self.assertEqual(count_findings("No [P1] findings.\nNo [P1] or [P2] findings.\nNo findings above [P2]."), 0)
        self.assertEqual(count_findings("No [P1], [P2], or [P3] findings."), 0)
        self.assertEqual(count_findings("No [P1] or [P2] findings remain.\nI did not find any [P1] issues."), 0)
        self.assertEqual(count_findings("No [P1] findings, but [P2] retry can loop forever"), 1)
        self.assertEqual(count_findings("No [P1] findings. However, [P2] retry can loop forever"), 1)
        self.assertEqual(count_findings("Full review comments:\nNo [P1] or [P2] findings."), 0)
        self.assertEqual(count_findings("Full review comments:\nNone."), 0)
        self.assertEqual(count_findings("Full review comments:\n- No findings.\n* No issues found."), 0)
        self.assertEqual(count_findings("Full review comments:\n- None.\n- N/A"), 0)
        self.assertEqual(count_findings('Full review comments:\n- The classifier treats "No findings" output as quiet'), 1)
        self.assertEqual(count_findings("Full review comments:\n- [P1] One\n- Two"), 2)
        self.assertEqual(count_findings("Full review comments:\n- No [P1] findings, but [P2] retry can loop forever"), 1)
        self.assertEqual(count_findings("Full review comments:\n- No [P1] findings; [P2] retry can loop forever"), 1)
        self.assertEqual(count_findings("Full review comments:\nNo [P1] findings.\n[P2] retry can loop forever"), 1)
        self.assertEqual(count_findings("Full review comments:\nNo findings are reported when retry fails"), 1)
        self.assertEqual(count_findings("Full review comments:\nNo [P1] findings.\nNo [P2] findings."), 0)
        self.assertEqual(count_findings("Full review comments:\n[P1] One\n[P2] Two"), 2)
        self.assertEqual(count_findings('Full review comments:\n- [P2] One\n  - nested detail\n  - another detail'), 1)
        self.assertEqual(count_findings("Full review comments:\n- No findings.\n[P2] retry can loop forever"), 1)
        self.assertEqual(count_findings('[P1] Parser treats "Full review comments: no findings" as quiet'), 1)
        self.assertEqual(count_findings("Full review comments:\n- One\n- Two"), 2)
        self.assertEqual(count_findings("Full review comments: no findings"), 0)
        self.assertEqual(count_findings("Full review comments:\nNo issues found."), 0)


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.review_dir = init_review_state(self.root, "Review the current uncommitted changes.")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_slice(self, name: str) -> None:
        with ReviewState.locked(self.review_dir) as state:
            state.add_slice(
                name=name,
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            state.save()

    def run_reviews(self, *args, **kwargs) -> tuple[int, dict]:
        return run_reviews(self.review_dir, *args, **kwargs)

    def test_default_run_reviews_is_quiet_until_barrier_completes(self) -> None:
        self.add_slice("fast")
        self.add_slice("slow")

        fast_done = threading.Event()
        slow_can_finish = threading.Event()
        stdout = io.StringIO()
        results: list[tuple[int, dict]] = []
        errors: list[BaseException] = []

        def runner(cmd, cwd, input_text, output_file, slice_data):
            if slice_data["name"] == "fast":
                output_file.write_text("No findings.", encoding="utf-8")
                fast_done.set()
                return subprocess.CompletedProcess(cmd, 0, "fast stdout", "fast stderr")
            self.assertTrue(fast_done.wait(timeout=2))
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(slow_can_finish.wait(timeout=2))
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "slow stdout", "slow stderr")

        def invoke() -> None:
            try:
                results.append(self.run_reviews(command_runner=runner, stdout=stdout, stdout_json=True))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(fast_done.wait(timeout=2))
            time.sleep(0.05)
            self.assertEqual(stdout.getvalue(), "")
            slow_can_finish.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            if errors:
                raise errors[0]
        finally:
            slow_can_finish.set()
            thread.join(timeout=2)

        self.assertEqual(results[0][0], 0)
        emitted = stdout.getvalue()
        self.assertEqual(emitted.count("\n"), 1)
        summary = json.loads(emitted)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["st"], "done")
        self.assertEqual(summary["ran"], 2)
        self.assertEqual(summary["rem"], 0)
        self.assertEqual(results[0][1], summary)
        self.assertEqual(json.loads((self.review_dir / "_last-run.json").read_text(encoding="utf-8")), summary)
        self.assertFalse(any(path.name.startswith("._last-run.json") for path in self.review_dir.iterdir()))

    def test_summary_json_no_stdout_writes_atomic_summary(self) -> None:
        self.add_slice("api")
        summary_path = self.review_dir / "_last-run.json"
        stdout = io.StringIO()

        rc, summary = self.run_reviews(
            command_runner=_writes("No findings."),
            summary_json=summary_path,
            no_stdout=True,
            stdout=stdout,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(summary_path.read_text(encoding="utf-8")), summary)
        self.assertFalse(any(path.name.startswith("._last-run.json") for path in self.review_dir.iterdir()))

    def test_child_output_is_logged_not_streamed_and_failure_summary_has_log_paths(self) -> None:
        self.add_slice("api")
        stdout = io.StringIO()

        def runner(cmd, cwd, input_text, output_file, slice_data):
            return subprocess.CompletedProcess(cmd, 1, "CHILD STDOUT BODY", "CHILD STDERR BODY")

        rc, summary = self.run_reviews(command_runner=runner, stdout=stdout, stdout_json=True)

        self.assertEqual(rc, 2)
        self.assertNotIn("CHILD STDOUT BODY", stdout.getvalue())
        self.assertNotIn("CHILD STDERR BODY", stdout.getvalue())
        self.assertFalse(summary["ok"])
        self.assertIn(summary["st"], {"partial", "failed"})
        self.assertIsInstance(summary["err"], list)
        err = summary["err"][0]
        self.assertIn("stdout", err)
        self.assertIn("stderr", err)
        self.assertNotIn("CHILD STDOUT BODY", json.dumps(summary))
        self.assertNotIn("CHILD STDERR BODY", json.dumps(summary))
        stdout_log = self.root / err["stdout"]
        if not stdout_log.exists():
            stdout_log = self.review_dir / err["stdout"]
        stderr_log = self.root / err["stderr"]
        if not stderr_log.exists():
            stderr_log = self.review_dir / err["stderr"]
        self.assertTrue(stdout_log.exists())
        self.assertTrue(stderr_log.exists())
        self.assertEqual(stdout_log.read_text(encoding="utf-8"), "CHILD STDOUT BODY")
        self.assertEqual(stderr_log.read_text(encoding="utf-8"), "CHILD STDERR BODY")

    def test_no_work_summary_is_compact_success(self) -> None:
        rc, summary = self.run_reviews(command_runner=_should_not_run)

        self.assertEqual(rc, 0)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["st"], "no_work")
        self.assertEqual(summary["ran"], 0)
        self.assertEqual(summary["out"], [])
        self.assertIsNone(summary["err"])
        self.assertEqual(json.loads((self.review_dir / "_last-run.json").read_text(encoding="utf-8")), summary)

    def test_child_timeout_marks_attempt_failed_with_log_paths(self) -> None:
        self.add_slice("api")

        def runner(cmd, cwd, input_text, output_file, slice_data):
            raise subprocess.TimeoutExpired(cmd, timeout=1, output="partial stdout", stderr="partial stderr")

        rc, summary = self.run_reviews(command_runner=runner, child_timeout_seconds=1)

        self.assertEqual(rc, 2)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["st"], "failed")
        err = summary["err"][0]
        self.assertEqual(err["st"], "timeout")
        self.assertEqual(err["code"], 124)
        self.assertIn("stdout", err)
        self.assertIn("stderr", err)
        self.assertEqual(ReviewState.load(self.review_dir).data["slices"]["api"]["runs"][0]["status"], "timeout")

    def test_quiet_slice_completes(self) -> None:
        self.add_slice("api")
        out = io.StringIO()
        rc, summary = run_reviews(self.review_dir, command_runner=_writes("No actionable issues found."), stdout=out)

        state = ReviewState.load(self.review_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(summary["st"], "done")
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertRegex(
            _single_review_file(self.review_dir, "*-1-api.md").name,
            TIMESTAMPED_REVIEW_FILE_RE,
        )
        self.assertEqual(out.getvalue(), "")

    def test_finding_slice_advances_pass_number(self) -> None:
        self.add_slice("api")
        calls = {"count": 0}

        def runner(cmd, cwd, input_text, output_file, slice_data):
            calls["count"] += 1
            if calls["count"] == 1:
                output_file.write_text("[P2] Validate retry state", encoding="utf-8")
            else:
                output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertFalse(state.data["slices"]["api"]["complete"])
        self.assertEqual(state.data["slices"]["api"]["next_pass"], 2)
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["finding_count"], 1)
        self.assertTrue(_single_review_file(self.review_dir, "*-1-api.md").exists())

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertTrue(_single_review_file(self.review_dir, "*-2-api.md").exists())

    def test_mixed_quiet_finding_and_failed_slices_update_independently(self) -> None:
        for name in ("quiet", "finding", "failed"):
            self.add_slice(name)

        def runner(cmd, cwd, input_text, output_file, slice_data):
            name = slice_data["name"]
            if name == "quiet":
                output_file.write_text("No issues found.", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if name == "finding":
                output_file.write_text("[P3] Missing edge-case test", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "bad", "worse")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["quiet"]["complete"])
        self.assertFalse(state.data["slices"]["finding"]["complete"])
        self.assertEqual(state.data["slices"]["finding"]["next_pass"], 2)
        self.assertFalse(state.data["slices"]["failed"]["complete"])
        self.assertEqual(state.data["slices"]["failed"]["next_pass"], 1)
        self.assertTrue((self.review_dir / "_errors.md").exists())

    def test_eligible_slices_run_in_parallel_within_one_invocation(self) -> None:
        for name in ("api", "tests", "ui"):
            self.add_slice(name)

        barrier = threading.Barrier(3)
        started = []
        lock = threading.Lock()

        def runner(cmd, cwd, input_text, output_file, slice_data):
            with lock:
                started.append(slice_data["name"])
            barrier.wait(timeout=2)
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())

        self.assertEqual(set(started), {"api", "tests", "ui"})
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["completed"])
        self.assertTrue(all(item["complete"] for item in state.data["slices"].values()))

    def test_run_reviews_waits_for_slowest_parallel_slice_before_returning(self) -> None:
        self.add_slice("fast")
        self.add_slice("slow")

        fast_started = threading.Event()
        slow_started = threading.Event()
        fast_finished = threading.Event()
        slow_can_finish = threading.Event()
        returned = threading.Event()
        errors = []
        results = []

        def runner(cmd, cwd, input_text, output_file, slice_data):
            name = slice_data["name"]
            if name == "fast":
                fast_started.set()
                if not slow_started.wait(timeout=2):
                    raise AssertionError("slow slice did not start before fast slice finished")
                output_file.write_text("No findings.", encoding="utf-8")
                fast_finished.set()
                return subprocess.CompletedProcess(cmd, 0, "", "")

            slow_started.set()
            if not slow_can_finish.wait(timeout=2):
                raise AssertionError("slow slice was not released")
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def invoke() -> None:
            try:
                results.append(run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO()))
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(fast_started.wait(timeout=2))
            self.assertTrue(slow_started.wait(timeout=2))
            self.assertTrue(fast_finished.wait(timeout=2))

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if errors:
                    raise errors[0]
                state = ReviewState.load(self.review_dir)
                fast_status = state.data["slices"]["fast"]["runs"][0]["status"]
                slow_status = state.data["slices"]["slow"]["runs"][0]["status"]
                if fast_status == "quiet" and slow_status == "running":
                    break
                time.sleep(0.01)
            else:
                self.fail("fast slice was not completed while slow slice remained running")

            self.assertFalse(returned.is_set())
            slow_can_finish.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            if errors:
                raise errors[0]
            self.assertEqual([rc for rc, _summary in results], [0])
        finally:
            slow_can_finish.set()
            thread.join(timeout=2)

    def test_failed_output_is_retryable_without_overwriting_prior_file(self) -> None:
        self.add_slice("api")

        def fail_then_quiet(cmd, cwd, input_text, output_file, slice_data):
            if not hasattr(fail_then_quiet, "called"):
                fail_then_quiet.called = True
                output_file.write_text("partial stderr context", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 1, "", "failed")
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=fail_then_quiet, stdout=io.StringIO())
        run_reviews(self.review_dir, command_runner=fail_then_quiet, stdout=io.StringIO())

        self.assertEqual(
            _single_review_file(self.review_dir, "*-1-api.md").read_text(encoding="utf-8"),
            "partial stderr context",
        )
        self.assertTrue(_single_review_file(self.review_dir, "*-1-api-retry2.md").exists())
        self.assertTrue(ReviewState.load(self.review_dir).data["slices"]["api"]["complete"])

    def test_launch_failure_records_error_and_remains_retryable(self) -> None:
        self.add_slice("api")

        def runner(cmd, cwd, input_text, output_file, slice_data):
            raise FileNotFoundError("codex")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertFalse(state.data["slices"]["api"]["complete"])
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["status"], "failed")
        self.assertIn("failed to launch", (self.review_dir / "_errors.md").read_text(encoding="utf-8"))

    def test_ignored_count_less_than_findings_leaves_state_unchanged(self) -> None:
        self.add_slice("api")
        run_reviews(self.review_dir, command_runner=_writes("[P1] One\n[P2] Two"), stdout=io.StringIO())

        with ReviewState.locked(self.review_dir) as state:
            changed, message = state.report_ignored_findings(ignored_count=1, slice_name="api")
            state.save()

        self.assertFalse(changed)
        self.assertIn("unchanged", message)
        state = ReviewState.load(self.review_dir)
        self.assertFalse(state.data["slices"]["api"]["complete"])
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["status"], "findings")
        self.assertEqual(state.data["slices"]["api"]["next_pass"], 2)

    def test_ignored_count_matching_findings_completes_slice(self) -> None:
        self.add_slice("api")
        run_reviews(self.review_dir, command_runner=_writes("[P1] One\n[P2] Two"), stdout=io.StringIO())

        with ReviewState.locked(self.review_dir) as state:
            changed, message = state.report_ignored_findings(ignored_count=2, slice_name="api")
            state.save()

        self.assertTrue(changed)
        self.assertIn("complete", message)
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertTrue(state.data["completed"])
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["status"], "ignored")
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["ignored_count"], 2)

        out = io.StringIO()
        rc, summary = run_reviews(self.review_dir, command_runner=_should_not_run, stdout=out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(summary["st"], "no_work")

    def test_ignored_report_without_slice_requires_unambiguous_run(self) -> None:
        self.add_slice("api")
        self.add_slice("ui")
        run_reviews(self.review_dir, command_runner=_writes("[P2] Finding"), stdout=io.StringIO())

        with ReviewState.locked(self.review_dir) as state:
            with self.assertRaises(ReviewStateError):
                state.report_ignored_findings(ignored_count=1)

    def test_ignored_report_targets_latest_finding_run_for_slice(self) -> None:
        self.add_slice("api")
        run_reviews(self.review_dir, command_runner=_writes("[P2] First pass"), stdout=io.StringIO())
        run_reviews(self.review_dir, command_runner=_writes("[P2] Second pass"), stdout=io.StringIO())

        with ReviewState.locked(self.review_dir) as state:
            with self.assertRaises(ReviewStateError):
                state.report_ignored_findings(ignored_count=1, slice_name="api", pass_number=1)
            changed, message = state.report_ignored_findings(ignored_count=1, slice_name="api")
            state.save()

        self.assertTrue(changed)
        self.assertIn("pass 2", message)
        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["slices"]["api"]["runs"][0]["status"], "findings")
        self.assertEqual(state.data["slices"]["api"]["runs"][1]["status"], "ignored")
        self.assertTrue(state.data["slices"]["api"]["complete"])

    def test_stale_running_reservation_is_recovered_and_retried(self) -> None:
        self.add_slice("api")
        with ReviewState.locked(self.review_dir) as state:
            state.reserve_eligible()
            state.data["slices"]["api"]["runs"][0]["runner_pid"] = -1
            state.save()

        run_reviews(self.review_dir, command_runner=_writes("No findings."), stdout=io.StringIO())

        state = ReviewState.load(self.review_dir)
        runs = state.data["slices"]["api"]["runs"]
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("stale running", runs[0]["error"])
        self.assertEqual(runs[1]["status"], "quiet")
        self.assertTrue(_single_review_file(self.review_dir, "*-1-api-retry2.md").exists())
        self.assertTrue(state.data["slices"]["api"]["complete"])

    def test_reused_pid_running_reservation_is_recovered(self) -> None:
        self.add_slice("api")
        with ReviewState.locked(self.review_dir) as state:
            state.reserve_eligible()
            run = state.data["slices"]["api"]["runs"][0]
            run["runner_pid"] = os.getpid()
            run["runner_key"] = f"{os.getpid()}:not-this-process"
            state.save()

        run_reviews(self.review_dir, command_runner=_writes("No findings."), stdout=io.StringIO())

        runs = ReviewState.load(self.review_dir).data["slices"]["api"]["runs"]
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[1]["status"], "quiet")

    def test_late_completion_for_recovered_run_is_ignored(self) -> None:
        self.add_slice("api")
        with ReviewState.locked(self.review_dir) as state:
            stale = state.reserve_eligible()[0]
            run = state.data["slices"]["api"]["runs"][0]
            run["runner_pid"] = -1
            state.save()

        run_reviews(self.review_dir, command_runner=_writes("[P2] Retry finding"), stdout=io.StringIO())

        with ReviewState.locked(self.review_dir) as state:
            changed = state.complete_run(
                run_id=stale.run_id,
                slice_name="api",
                status="quiet",
                exit_code=0,
                classification="quiet",
                finding_count=0,
            )
            state.save()

        state = ReviewState.load(self.review_dir)
        runs = state.data["slices"]["api"]["runs"]
        self.assertFalse(changed)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[1]["status"], "findings")
        self.assertFalse(state.data["slices"]["api"]["complete"])

    def test_followup_reservations_wait_for_active_batch(self) -> None:
        self.add_slice("api")
        self.add_slice("ui")
        with ReviewState.locked(self.review_dir) as state:
            reservations = state.reserve_eligible()
            api = next(reservation for reservation in reservations if reservation.slice_name == "api")
            state.complete_run(
                run_id=api.run_id,
                slice_name="api",
                status="findings",
                exit_code=0,
                classification="findings",
                finding_count=1,
            )
            followups = state.reserve_eligible()
            state.save()

        self.assertEqual(followups, [])
        state = ReviewState.load(self.review_dir)
        self.assertEqual(len(state.data["slices"]["api"]["runs"]), 1)
        self.assertEqual(state.data["slices"]["ui"]["runs"][0]["status"], "running")

    def test_slice_added_mid_batch_starts_at_pass_one_while_existing_followups_continue(self) -> None:
        self.add_slice("api")
        self.add_slice("ui")
        for _ in range(4):
            with ReviewState.locked(self.review_dir) as state:
                reservations = state.reserve_eligible()
                for reservation in reservations:
                    state.complete_run(
                        run_id=reservation.run_id,
                        slice_name=reservation.slice_name,
                        status="findings",
                        exit_code=0,
                        classification="findings",
                        finding_count=1,
                    )
                state.save()

        with ReviewState.locked(self.review_dir) as state:
            reservations = state.reserve_eligible()
            self.assertEqual(
                {reservation.slice_name: reservation.pass_number for reservation in reservations},
                {"api": 5, "ui": 5},
            )
            api = next(reservation for reservation in reservations if reservation.slice_name == "api")
            ui = next(reservation for reservation in reservations if reservation.slice_name == "ui")
            state.complete_run(
                run_id=api.run_id,
                slice_name="api",
                status="findings",
                exit_code=0,
                classification="findings",
                finding_count=1,
            )
            state.add_slice(
                name="docs",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            followups = state.reserve_eligible()
            state.save()

        self.assertEqual(followups, [])
        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["slices"]["api"]["next_pass"], 6)
        self.assertEqual(state.data["slices"]["docs"]["next_pass"], 1)
        self.assertEqual(state.data["slices"]["docs"]["runs"], [])
        self.assertEqual(state.data["slices"]["ui"]["runs"][-1]["status"], "running")

        with ReviewState.locked(self.review_dir) as state:
            state.complete_run(
                run_id=ui.run_id,
                slice_name="ui",
                status="quiet",
                exit_code=0,
                classification="quiet",
                finding_count=0,
            )
            followups = state.reserve_eligible()
            state.save()

        self.assertEqual(
            {reservation.slice_name: reservation.pass_number for reservation in followups},
            {"api": 6, "docs": 1},
        )

    def test_uncertain_success_logs_diagnostic_and_completes(self) -> None:
        self.add_slice("api")
        run_reviews(self.review_dir, command_runner=_writes("Inspected the changes."), stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertIn("uncertain", (self.review_dir / "_errors.md").read_text(encoding="utf-8"))

    def test_empty_and_missing_outputs_are_failed_retryable(self) -> None:
        self.add_slice("empty")
        self.add_slice("missing")

        def runner(cmd, cwd, input_text, output_file, slice_data):
            if slice_data["name"] == "empty":
                output_file.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertFalse(state.data["slices"]["empty"]["complete"])
        self.assertFalse(state.data["slices"]["missing"]["complete"])
        self.assertEqual(state.data["slices"]["empty"]["next_pass"], 1)
        self.assertEqual(state.data["slices"]["missing"]["next_pass"], 1)
        self.assertEqual(state.data["slices"]["empty"]["runs"][0]["status"], "failed")
        self.assertEqual(state.data["slices"]["missing"]["runs"][0]["status"], "failed")

        run_reviews(self.review_dir, command_runner=_writes("No findings."), stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["slices"]["empty"]["runs"][1]["status"], "quiet")
        self.assertEqual(state.data["slices"]["missing"]["runs"][1]["status"], "quiet")
        self.assertTrue(_single_review_file(self.review_dir, "*-1-empty-retry2.md").exists())
        self.assertTrue(_single_review_file(self.review_dir, "*-1-missing-retry2.md").exists())

    def test_terminal_recovery_clears_session_last_error(self) -> None:
        self.add_slice("api")

        def fail_then_quiet(cmd, cwd, input_text, output_file, slice_data):
            if len(slice_data["runs"]) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "failed")
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=fail_then_quiet, stdout=io.StringIO())
        self.assertIsNotNone(ReviewState.load(self.review_dir).data["last_error"])

        run_reviews(self.review_dir, command_runner=fail_then_quiet, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["completed"])
        self.assertIsNone(state.data["last_error"])

    def test_concurrent_run_reviews_do_not_duplicate_reservations(self) -> None:
        self.add_slice("api")
        calls = []
        lock = threading.Lock()

        def runner(cmd, cwd, input_text, output_file, slice_data):
            with lock:
                calls.append(slice_data["name"])
            time.sleep(0.1)
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(run_reviews, self.review_dir, command_runner=runner, stdout=io.StringIO())
                for _ in range(2)
            ]
            for future in futures:
                rc, _summary = future.result(timeout=5)
                self.assertEqual(rc, 0)

        self.assertEqual(calls, ["api"])
        state = ReviewState.load(self.review_dir)
        self.assertEqual(len(state.data["slices"]["api"]["runs"]), 1)

    def test_waited_concurrent_failure_is_reported(self) -> None:
        self.add_slice("api")
        started = threading.Event()
        release = threading.Event()
        first_result: list[tuple[int, dict]] = []

        def failing_runner(cmd, cwd, input_text, output_file, slice_data):
            started.set()
            release.wait(timeout=2)
            return subprocess.CompletedProcess(cmd, 1, "failed stdout", "failed stderr")

        thread = threading.Thread(
            target=lambda: first_result.append(run_reviews(self.review_dir, command_runner=failing_runner, stdout=io.StringIO()))
        )
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        second_result: list[tuple[int, dict]] = []
        second = threading.Thread(
            target=lambda: second_result.append(run_reviews(self.review_dir, command_runner=_should_not_run, stdout=io.StringIO()))
        )
        second.start()
        time.sleep(0.05)
        release.set()
        thread.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(second.is_alive())
        rc, summary = second_result[0]

        self.assertEqual(rc, 2)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["st"], "failed")
        self.assertEqual(summary["ran"], 0)
        self.assertEqual(summary["err"][0]["s"], "api")
        self.assertEqual(first_result[0][0], 2)

    def test_runner_builds_expected_native_command(self) -> None:
        self.add_slice("api")

        def runner(cmd, cwd, input_text, output_file, slice_data):
            self.assertEqual(cwd, self.root)
            self.assertIsNotNone(input_text)
            self.assertIn(str(self.review_dir / "task.md"), input_text)
            self.assertEqual(output_file.parent, self.review_dir)
            self.assertRegex(output_file.name, TIMESTAMPED_REVIEW_FILE_RE)
            self.assertTrue(output_file.name.endswith("-1-api.md"))
            self.assertEqual(slice_data["target"], {"uncommitted": True})
            self.assertEqual(cmd[:4], ["codex", "exec", "review", "--ephemeral"])
            self.assertIn("-m", cmd)
            self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.5")
            self.assertIn('-c', cmd)
            self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="high"')
            self.assertIn("--uncommitted", cmd)
            self.assertEqual(cmd[-3:], ["-o", str(output_file), "-"])
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())

    def test_missing_task_entrypoint_fails_before_reserving_runs(self) -> None:
        self.add_slice("api")
        (self.review_dir / "task.md").unlink()

        with self.assertRaises(ReviewStateError):
            run_reviews(self.review_dir, command_runner=_should_not_run, stdout=io.StringIO())

        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["slices"]["api"]["runs"], [])

    def test_build_review_command_uses_base_and_commit_targets(self) -> None:
        base_cmd, base_input = build_review_command(
            {
                "name": "base",
                "mode": "native",
                "target": {"base": "main"},
                "model": "gpt-5.5",
                "reasoning": "high",
            },
            self.review_dir / "1-base.md",
        )
        commit_cmd, commit_input = build_review_command(
            {
                "name": "commit",
                "mode": "native",
                "target": {"commit": "abc123"},
                "model": "gpt-5.5",
                "reasoning": "high",
            },
            self.review_dir / "1-commit.md",
        )

        self.assertIn("--base", base_cmd)
        self.assertEqual(base_cmd[base_cmd.index("--base") + 1], "main")
        self.assertNotIn("--uncommitted", base_cmd)
        self.assertEqual(base_cmd[-3:], ["-o", str(self.review_dir / "1-base.md"), "-"])
        self.assertIn(str(self.review_dir / "task.md"), base_input)
        self.assertIn("--commit", commit_cmd)
        self.assertEqual(commit_cmd[commit_cmd.index("--commit") + 1], "abc123")
        self.assertNotIn("--uncommitted", commit_cmd)
        self.assertEqual(commit_cmd[-3:], ["-o", str(self.review_dir / "1-commit.md"), "-"])
        self.assertIn(str(self.review_dir / "task.md"), commit_input)

    def test_build_review_command_uses_prompt_stdin_and_output(self) -> None:
        cmd, input_text = build_review_command(
            {
                "name": "api",
                "mode": "prompt",
                "prompt": "Review only API code.",
                "model": "gpt-5.5",
                "reasoning": "high",
            },
            self.review_dir / "1-api.md",
        )

        self.assertIn(str(self.review_dir / "task.md"), input_text)
        self.assertIn("Slice instructions:\nReview only API code.", input_text)
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.5")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="high"')
        self.assertEqual(cmd[-3:], ["-o", str(self.review_dir / "1-api.md"), "-"])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo with spaces"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str, input_text: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            cwd=cwd or self.root,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_help_outputs(self) -> None:
        for script in (
            "init_state.py",
            "add_slice.py",
            "add_related_task.py",
            "run_reviews.py",
            "report_ignored_findings.py",
        ):
            proc = self.run_cli(str(SCRIPTS / script), "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage:", proc.stdout)

    def test_cli_paths_with_spaces_and_outside_skill_dir(self) -> None:
        init = self.run_cli(
            str(SCRIPTS / "init_state.py"),
            "--root",
            str(self.root),
            "--task",
            "Review paths with spaces.",
            cwd=Path(self.tmp.name),
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        review_dir = Path(init.stdout.strip())
        add = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "api",
            "--uncommitted",
            cwd=Path(self.tmp.name),
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        state = ReviewState.load(review_dir)
        self.assertIn("api", state.data["slices"])
        self.assertEqual(Path(state.data["slices"]["api"]["cwd"]), self.root.resolve())

    def test_compatibility_wrapper_creates_state(self) -> None:
        proc = self.run_cli(
            str(SCRIPTS / "new_review_dir.py"),
            "--root",
            str(self.root),
            "--task",
            "Review compatibility wrapper behavior.",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((Path(proc.stdout.strip()) / "_state.json").exists())
        self.assertTrue((Path(proc.stdout.strip()) / "task.md").exists())

    def test_init_cli_requires_task_and_accepts_stdin_task_file(self) -> None:
        missing = self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("choose exactly one task source", missing.stderr)

        init = self.run_cli(
            str(SCRIPTS / "init_state.py"),
            "--root",
            str(self.root),
            "--task-file",
            "-",
            input_text="Review task from stdin.",
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        task_text = (Path(init.stdout.strip()) / "task.md").read_text(encoding="utf-8")
        self.assertIn("Review task from stdin.", task_text)

        both = self.run_cli(
            str(SCRIPTS / "init_state.py"),
            "--root",
            str(self.root),
            "--task",
            "Inline task.",
            "--task-file",
            "-",
            input_text="Stdin task.",
        )
        self.assertEqual(both.returncode, 2)
        self.assertIn("choose exactly one task source", both.stderr)

        empty_inline_with_file = self.run_cli(
            str(SCRIPTS / "init_state.py"),
            "--root",
            str(self.root),
            "--task",
            "",
            "--task-file",
            "-",
            input_text="Stdin task.",
        )
        self.assertEqual(empty_inline_with_file.returncode, 2)
        self.assertIn("choose exactly one task source", empty_inline_with_file.stderr)

    def test_add_related_task_cli_updates_task_entrypoint(self) -> None:
        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review related task CLI.",
            ).stdout.strip()
        )

        text = self.run_cli(
            str(SCRIPTS / "add_related_task.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "next-step",
            "--text",
            "Implement the next workflow stage later.",
        )
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(
            (review_dir / "related-tasks" / "next-step.md").read_text(encoding="utf-8"),
            "Implement the next workflow stage later.",
        )
        self.assertIn(
            "[next-step](related-tasks/next-step.md)",
            (review_dir / "task.md").read_text(encoding="utf-8"),
        )

        invalid = self.run_cli(
            str(SCRIPTS / "add_related_task.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "next-step",
            "--text",
            "Inline.",
            "--file",
            str(review_dir / "related-tasks" / "next-step.md"),
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("choose exactly one", invalid.stderr)

    def test_cli_clear_errors_for_missing_state_and_invalid_args(self) -> None:
        missing = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(self.root / ".review" / "missing"),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("missing review state", missing.stderr)

        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review invalid add-slice arguments.",
            ).stdout.strip()
        )
        invalid = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "api",
            "--uncommitted",
            "--base",
            "main",
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("choose only one", invalid.stderr)

        bad_review_dir = self.root / "not-a-review-dir"
        bad_review_dir.write_text("", encoding="utf-8")
        for script in ("run_reviews.py", "report_ignored_findings.py"):
            proc = self.run_cli(
                str(SCRIPTS / script),
                "--review-dir",
                str(bad_review_dir),
                *([] if script == "run_reviews.py" else ["--count", "1"]),
            )
            expected_returncode = 1 if script == "run_reviews.py" else 2
            self.assertEqual(proc.returncode, expected_returncode)
            self.assertIn("error:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_init_cli_reports_clear_error_for_invalid_root(self) -> None:
        root_file = Path(self.tmp.name) / "not-a-directory"
        root_file.write_text("", encoding="utf-8")

        for script in ("init_state.py", "new_review_dir.py"):
            proc = self.run_cli(
                str(SCRIPTS / script),
                "--root",
                str(root_file),
                "--task",
                "Review invalid root handling.",
                cwd=Path(self.tmp.name),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_report_ignored_findings_cli_completes_slice(self) -> None:
        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review ignored finding reporting.",
            ).stdout.strip()
        )
        with ReviewState.locked(review_dir) as state:
            state.add_slice(
                name="api",
                mode="native",
                target={"uncommitted": True},
                prompt=None,
                cwd=self.root,
            )
            reservation = state.reserve_eligible()[0]
            state.complete_run(
                run_id=reservation.run_id,
                slice_name="api",
                status="findings",
                exit_code=0,
                classification="findings",
                finding_count=2,
            )
            state.save()

        proc = self.run_cli(
            str(SCRIPTS / "report_ignored_findings.py"),
            "--review-dir",
            str(review_dir),
            "--slice",
            "api",
            "--count",
            "2",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("complete", proc.stdout)
        self.assertTrue(ReviewState.load(review_dir).data["slices"]["api"]["complete"])

    def test_concurrent_add_slice_cli_has_no_lost_updates(self) -> None:
        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review concurrent add-slice behavior.",
            ).stdout.strip()
        )
        commands = [
            [
                sys.executable,
                str(SCRIPTS / "add_slice.py"),
                "--review-dir",
                str(review_dir),
                "--name",
                name,
                "--uncommitted",
            ]
            for name in ("api", "ui")
        ]
        with (review_dir / "_state.lock").open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            procs = [
                subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for cmd in commands
            ]
            time.sleep(0.1)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            results = [proc.communicate(timeout=10) + (proc.returncode,) for proc in procs]
        for stdout, stderr, returncode in results:
            self.assertEqual(returncode, 0, stderr + stdout)
        state = ReviewState.load(review_dir)
        self.assertEqual(set(state.data["slices"]), {"api", "ui"})

    def test_concurrent_run_reviews_cli_with_fake_codex_has_no_duplicate_reservations(self) -> None:
        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review concurrent run behavior.",
            ).stdout.strip()
        )
        add = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(add.returncode, 0, add.stderr)

        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        invocation_log = Path(self.tmp.name) / "codex-invocations.log"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "data = sys.stdin.read()\n"
            "assert 'task.md' in data\n"
            "assert sys.argv[-1] == '-'\n"
            "time.sleep(0.2)\n"
            "with open(os.environ['CODEX_INVOCATION_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write('called\\n')\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w', encoding='utf-8').write('No findings.')\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CODEX_INVOCATION_LOG": str(invocation_log),
        }
        cmd = [
            sys.executable,
            str(SCRIPTS / "run_reviews.py"),
            "--review-dir",
            str(review_dir),
        ]

        procs = [
            subprocess.Popen(cmd, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        results = [proc.communicate(timeout=10) + (proc.returncode,) for proc in procs]

        for stdout, stderr, returncode in results:
            self.assertEqual(returncode, 0, stderr + stdout)
        state = ReviewState.load(review_dir)
        runs = state.data["slices"]["api"]["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "quiet")
        self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["called"])

    def test_prompt_file_stdin_cli_passes_prompt_to_fake_codex(self) -> None:
        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review prompted slices.",
            ).stdout.strip()
        )
        prompt = "Review the current uncommitted changes.\nSlice: API only.\n"
        add = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(review_dir),
            "--name",
            "api-prompt",
            "--prompt-file",
            "-",
            input_text=prompt,
        )
        self.assertEqual(add.returncode, 0, add.stderr)

        fake_bin = Path(self.tmp.name) / "prompt-bin"
        fake_bin.mkdir()
        captured_prompt = Path(self.tmp.name) / "captured-prompt.txt"
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "data = sys.stdin.read()\n"
            "open(os.environ['CAPTURED_PROMPT'], 'w', encoding='utf-8').write(data)\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w', encoding='utf-8').write('No findings.')\n"
            "assert sys.argv[-1] == '-'\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CAPTURED_PROMPT": str(captured_prompt),
        }
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_reviews.py"), "--review-dir", str(review_dir)],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        captured = captured_prompt.read_text(encoding="utf-8")
        self.assertIn(str(review_dir / "task.md"), captured)
        self.assertIn("Slice instructions:\n" + prompt, captured)
        state = ReviewState.load(review_dir)
        self.assertEqual(state.data["slices"]["api-prompt"]["mode"], "prompt")
        self.assertTrue(state.data["slices"]["api-prompt"]["complete"])

    def test_run_reviews_cli_no_stdout_summary_file_and_stream_progress_flags(self) -> None:
        fake_bin = Path(self.tmp.name) / "barrier-bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "sys.stdout.write('CHILD STDOUT\\n')\n"
            "sys.stderr.write('CHILD STDERR\\n')\n"
            "time.sleep(0.05)\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w', encoding='utf-8').write('No findings.')\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

        review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review quiet CLI barrier.",
            ).stdout.strip()
        )
        add = self.run_cli(str(SCRIPTS / "add_slice.py"), "--review-dir", str(review_dir), "--name", "api", "--uncommitted")
        self.assertEqual(add.returncode, 0, add.stderr)
        summary_path = review_dir / "_last-run.json"

        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "run_reviews.py"),
                "--review-dir",
                str(review_dir),
                "--summary-json",
                str(summary_path),
                "--no-stdout",
            ],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["st"], "done")
        self.assertNotIn("CHILD STDOUT", json.dumps(summary))
        self.assertNotIn("CHILD STDERR", json.dumps(summary))
        success_stdout_logs = sorted((review_dir / "_logs").glob("*.stdout.log"))
        success_stderr_logs = sorted((review_dir / "_logs").glob("*.stderr.log"))
        self.assertEqual(len(success_stdout_logs), 1)
        self.assertEqual(len(success_stderr_logs), 1)
        self.assertIn("CHILD STDOUT", success_stdout_logs[0].read_text(encoding="utf-8"))
        self.assertIn("CHILD STDERR", success_stderr_logs[0].read_text(encoding="utf-8"))

        no_work = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_reviews.py"), "--review-dir", str(review_dir)],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(no_work.returncode, 0, no_work.stderr)
        self.assertEqual(no_work.stderr, "")
        no_work_summary = json.loads(no_work.stdout)
        self.assertEqual(no_work_summary["st"], "no_work")
        self.assertEqual(no_work_summary["ran"], 0)

        default_review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review default CLI JSON.",
            ).stdout.strip()
        )
        add_default = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(default_review_dir),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(add_default.returncode, 0, add_default.stderr)
        default_proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_reviews.py"), "--review-dir", str(default_review_dir)],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(default_proc.returncode, 0, default_proc.stderr)
        self.assertEqual(default_proc.stderr, "")
        self.assertEqual(default_proc.stdout.count("\n"), 1)
        default_summary = json.loads(default_proc.stdout)
        self.assertEqual(default_summary["st"], "done")
        self.assertEqual(default_summary["ran"], 1)
        self.assertNotIn("CHILD STDOUT", default_proc.stdout)
        self.assertEqual(
            json.loads((default_review_dir / "_last-run.json").read_text(encoding="utf-8")),
            default_summary,
        )

        fail_bin = Path(self.tmp.name) / "barrier-fail-bin"
        fail_bin.mkdir()
        fail_codex = fail_bin / "codex"
        fail_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.write('FAILED CHILD STDOUT\\n')\n"
            "sys.stderr.write('FAILED CHILD STDERR\\n')\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        fail_codex.chmod(0o755)
        fail_env = {**os.environ, "PATH": f"{fail_bin}{os.pathsep}{os.environ['PATH']}"}
        fail_review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review failed CLI logging.",
            ).stdout.strip()
        )
        add_fail = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(fail_review_dir),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(add_fail.returncode, 0, add_fail.stderr)
        fail_summary_path = fail_review_dir / "_last-run.json"
        fail_proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "run_reviews.py"),
                "--review-dir",
                str(fail_review_dir),
                "--summary-json",
                str(fail_summary_path),
                "--no-stdout",
            ],
            cwd=self.root,
            env=fail_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(fail_proc.returncode, 2)
        self.assertEqual(fail_proc.stdout, "")
        self.assertEqual(fail_proc.stderr, "")
        fail_summary = json.loads(fail_summary_path.read_text(encoding="utf-8"))
        fail_err = fail_summary["err"][0]
        stdout_log = self.root / fail_err["stdout"]
        stderr_log = self.root / fail_err["stderr"]
        self.assertIn("FAILED CHILD STDOUT", stdout_log.read_text(encoding="utf-8"))
        self.assertIn("FAILED CHILD STDERR", stderr_log.read_text(encoding="utf-8"))
        self.assertNotIn("FAILED CHILD STDOUT", json.dumps(fail_summary))
        self.assertNotIn("FAILED CHILD STDERR", json.dumps(fail_summary))

        default_fail_review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review failed default CLI logging.",
            ).stdout.strip()
        )
        add_default_fail = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(default_fail_review_dir),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(add_default_fail.returncode, 0, add_default_fail.stderr)
        default_fail_proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_reviews.py"), "--review-dir", str(default_fail_review_dir)],
            cwd=self.root,
            env=fail_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(default_fail_proc.returncode, 2)
        self.assertEqual(default_fail_proc.stderr, "")
        default_fail_summary = json.loads(default_fail_proc.stdout)
        self.assertFalse(default_fail_summary["ok"])
        self.assertEqual(
            json.loads((default_fail_review_dir / "_last-run.json").read_text(encoding="utf-8")),
            default_fail_summary,
        )
        self.assertNotIn("FAILED CHILD STDOUT", default_fail_proc.stdout)
        self.assertNotIn("FAILED CHILD STDERR", default_fail_proc.stdout)

        invalid = self.run_cli(
            str(SCRIPTS / "run_reviews.py"),
            "--review-dir",
            str(review_dir),
            "--summary-json",
            str(summary_path),
            "--no-stdout",
            "--stream-progress",
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("incompatible", invalid.stderr)

        stream_review_dir = Path(
            self.run_cli(
                str(SCRIPTS / "init_state.py"),
                "--root",
                str(self.root),
                "--task",
                "Review stream progress opt-in.",
            ).stdout.strip()
        )
        add_stream = self.run_cli(
            str(SCRIPTS / "add_slice.py"),
            "--review-dir",
            str(stream_review_dir),
            "--name",
            "api",
            "--uncommitted",
        )
        self.assertEqual(add_stream.returncode, 0, add_stream.stderr)
        stream_proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_reviews.py"), "--review-dir", str(stream_review_dir), "--stream-progress"],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(stream_proc.returncode, 0, stream_proc.stderr)
        self.assertIn("api: pass 1", stream_proc.stderr)
        self.assertEqual(json.loads(stream_proc.stdout)["st"], "done")
        self.assertNotIn("CHILD STDOUT", stream_proc.stdout)
        self.assertNotIn("CHILD STDERR", stream_proc.stderr)

    def test_skill_documents_review_barrier_protocol(self) -> None:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## Review Barrier Protocol", text)
        self.assertIn('python3 "$SKILL_DIR/scripts/run_reviews.py" --review-dir "$REVIEW_DIR"', text)
        self.assertNotIn("--summary-json", text)
        self.assertNotIn("--no-stdout", text)
        self.assertNotIn("--stream-progress", text)
        self.assertNotIn("_last-run.json", text)
        self.assertIn("Do not run it with `&`, `nohup`, `disown`, `tmux`, `screen`", text)
        self.assertIn("2 hours / 7,200,000 ms", text)
        self.assertNotIn("Keep calling it until it prints `done`", text)

    def test_readme_documents_human_recovery_and_debug_options(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("_last-run.json", text)
        self.assertIn("recovery path", text)
        self.assertIn("--stream-progress", text)
        self.assertIn("local human debugging", text)


def _writes(text: str):
    def runner(cmd, cwd, input_text, output_file, slice_data):
        output_file.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return runner


def _should_not_run(cmd, cwd, input_text, output_file, slice_data):
    raise AssertionError("runner should not be invoked")


def _single_review_file(review_dir: Path, pattern: str) -> Path:
    files = sorted(review_dir.glob(pattern))
    if len(files) != 1:
        raise AssertionError(
            f"expected exactly one file matching {pattern!r}, got {[path.name for path in files]}"
        )
    return files[0]


if __name__ == "__main__":
    unittest.main()
