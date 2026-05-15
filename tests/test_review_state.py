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


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from review_state import (  # noqa: E402
    ReviewState,
    ReviewStateError,
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
        self.review_dir = init_review_state(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_creates_loadable_state(self) -> None:
        state_path = self.review_dir / "_state.json"
        self.assertTrue(state_path.exists())
        state = ReviewState.load(self.review_dir)
        self.assertEqual(state.data["schema_version"], 1)
        self.assertEqual(state.data["slices"], {})
        self.assertFalse(state.data["completed"])

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
        rc = run_reviews(self.review_dir, command_runner=_should_not_run, stdout=output)
        self.assertEqual(rc, 0)
        self.assertIn("done:", output.getvalue())


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
        self.review_dir = init_review_state(self.root)

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

    def test_quiet_slice_completes(self) -> None:
        self.add_slice("api")
        out = io.StringIO()
        run_reviews(self.review_dir, command_runner=_writes("No actionable issues found."), stdout=out)

        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertTrue((self.review_dir / "1-api.md").exists())
        self.assertIn("done:", out.getvalue())

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
        self.assertTrue((self.review_dir / "1-api.md").exists())

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())
        state = ReviewState.load(self.review_dir)
        self.assertTrue(state.data["slices"]["api"]["complete"])
        self.assertTrue((self.review_dir / "2-api.md").exists())

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

        self.assertEqual((self.review_dir / "1-api.md").read_text(encoding="utf-8"), "partial stderr context")
        self.assertTrue((self.review_dir / "1-api-retry2.md").exists())
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
        run_reviews(self.review_dir, command_runner=_should_not_run, stdout=out)
        self.assertIn("done:", out.getvalue())

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
        self.assertTrue((self.review_dir / "1-api-retry2.md").exists())
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
        self.assertTrue((self.review_dir / "1-empty-retry2.md").exists())
        self.assertTrue((self.review_dir / "1-missing-retry2.md").exists())

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
                self.assertEqual(future.result(timeout=5), 0)

        self.assertEqual(calls, ["api"])
        state = ReviewState.load(self.review_dir)
        self.assertEqual(len(state.data["slices"]["api"]["runs"]), 1)

    def test_runner_builds_expected_native_command(self) -> None:
        self.add_slice("api")

        def runner(cmd, cwd, input_text, output_file, slice_data):
            self.assertEqual(cwd, self.root)
            self.assertIsNone(input_text)
            self.assertEqual(output_file, self.review_dir / "1-api.md")
            self.assertEqual(slice_data["target"], {"uncommitted": True})
            self.assertEqual(cmd[:4], ["codex", "exec", "review", "--ephemeral"])
            self.assertIn("-m", cmd)
            self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.5")
            self.assertIn('-c', cmd)
            self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="high"')
            self.assertIn("--uncommitted", cmd)
            self.assertEqual(cmd[-2:], ["-o", str(output_file)])
            output_file.write_text("No findings.", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        run_reviews(self.review_dir, command_runner=runner, stdout=io.StringIO())

    def test_build_review_command_uses_base_and_commit_targets(self) -> None:
        base_cmd, _base_input = build_review_command(
            {
                "name": "base",
                "mode": "native",
                "target": {"base": "main"},
                "model": "gpt-5.5",
                "reasoning": "high",
            },
            self.review_dir / "1-base.md",
        )
        commit_cmd, _commit_input = build_review_command(
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
        self.assertIn("--commit", commit_cmd)
        self.assertEqual(commit_cmd[commit_cmd.index("--commit") + 1], "abc123")
        self.assertNotIn("--uncommitted", commit_cmd)

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

        self.assertEqual(input_text, "Review only API code.")
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
        for script in ("init_state.py", "add_slice.py", "run_reviews.py", "report_ignored_findings.py"):
            proc = self.run_cli(str(SCRIPTS / script), "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage:", proc.stdout)

    def test_cli_paths_with_spaces_and_outside_skill_dir(self) -> None:
        init = self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root), cwd=Path(self.tmp.name))
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
        proc = self.run_cli(str(SCRIPTS / "new_review_dir.py"), "--root", str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((Path(proc.stdout.strip()) / "_state.json").exists())

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
            self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root)).stdout.strip()
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
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_init_cli_reports_clear_error_for_invalid_root(self) -> None:
        root_file = Path(self.tmp.name) / "not-a-directory"
        root_file.write_text("", encoding="utf-8")

        for script in ("init_state.py", "new_review_dir.py"):
            proc = self.run_cli(str(SCRIPTS / script), "--root", str(root_file), cwd=Path(self.tmp.name))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_report_ignored_findings_cli_completes_slice(self) -> None:
        review_dir = Path(
            self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root)).stdout.strip()
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
            self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root)).stdout.strip()
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
            self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root)).stdout.strip()
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
            self.run_cli(str(SCRIPTS / "init_state.py"), "--root", str(self.root)).stdout.strip()
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
        self.assertEqual(captured_prompt.read_text(encoding="utf-8"), prompt)
        state = ReviewState.load(review_dir)
        self.assertEqual(state.data["slices"]["api-prompt"]["mode"], "prompt")
        self.assertTrue(state.data["slices"]["api-prompt"]["complete"])


def _writes(text: str):
    def runner(cmd, cwd, input_text, output_file, slice_data):
        output_file.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return runner


def _should_not_run(cmd, cwd, input_text, output_file, slice_data):
    raise AssertionError("runner should not be invoked")


if __name__ == "__main__":
    unittest.main()
