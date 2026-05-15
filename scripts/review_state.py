#!/usr/bin/env python3
"""State management and runner helpers for multi-shot Codex reviews."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING = "high"
SLICE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PRIORITY_RE = re.compile(r"\[(?:P[0-3])\]", re.IGNORECASE)
REVIEW_COMMENT_RE = re.compile(r"Review comment:", re.IGNORECASE)
FULL_REVIEW_COMMENTS_RE = re.compile(r"^Full review comments:\s*(.*)", re.IGNORECASE | re.DOTALL | re.MULTILINE)
QUIET_RE = re.compile(
    r"\b(no actionable issues|no blocking issues|no findings|no issues found|did not find\b.*\b(?:bug|issue|problem|defect)s?|lgtm|looks good to me)\b",
    re.IGNORECASE,
)
CONTRAST_RE = re.compile(r"\b(but|however|except|although)\b", re.IGNORECASE)
QUIET_SUMMARY_RE = re.compile(
    r"^\s*(?:no actionable issues(?: found)?|no blocking issues(?: found)?|no findings(?: found)?|no issues found|lgtm|looks good to me)\.?\s*$",
    re.IGNORECASE,
)
EMPTY_SUMMARY_RE = re.compile(r"^\s*(?:none|n/a|no comments?)\.?\s*$", re.IGNORECASE)
QUIET_PRIORITY_RE = re.compile(
    r"\b(?:there\s+(?:are|were)\s+)?no\s+(?:findings|issues)\s+(?:above|at or above)\s+\[(?:P[0-3])\]|\b(?:there\s+(?:are|were)\s+)?no\s+(?:\[(?:P[0-3])\](?:(?:\s*,\s*|\s+(?:or|and)\s+|\s*,\s*(?:or|and)\s+)\[(?:P[0-3])\])*\s+)?(?:findings|issues)(?:\s+remain)?|\bi did not find\s+(?:any\s+)?(?:\[(?:P[0-3])\](?:(?:\s*,\s*|\s+(?:or|and)\s+|\s*,\s*(?:or|and)\s+)\[(?:P[0-3])\])*\s+)?(?:findings|issues|bugs|problems|defects)",
    re.IGNORECASE,
)


class ReviewStateError(RuntimeError):
    """Raised when review state is invalid or a requested mutation is rejected."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def session_id() -> str:
    timestamp = now_iso().replace(":", "-")
    return f"{timestamp}-{secrets.token_hex(4)}"


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def repo_root(path: Path | None = None) -> Path:
    start = Path.cwd() if path is None else path
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return start.resolve()
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    return start.resolve()


def create_review_dir(root: Path) -> Path:
    review_root = root.resolve() / ".review"
    for _ in range(10):
        review_dir = review_root / session_id()
        try:
            review_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return review_dir
    raise ReviewStateError("could not create a unique review directory after 10 attempts")


def init_review_state(root: Path) -> Path:
    review_dir = create_review_dir(root)
    state = ReviewState.new(review_dir=review_dir, root=root.resolve())
    state.save()
    return review_dir


def classify_output(text: str) -> str:
    """Return findings, quiet, or uncertain for a successful review output."""
    if count_findings(text) > 0:
        return "findings"

    if (QUIET_RE.search(text) and not CONTRAST_RE.search(text)) or any(
        QUIET_PRIORITY_RE.search(line) and not CONTRAST_RE.search(line) for line in text.splitlines()
    ):
        return "quiet"

    return "uncertain"


def count_findings(text: str) -> int:
    """Count recognizable review findings in successful Codex review output."""
    full_comments = FULL_REVIEW_COMMENTS_RE.search(text)
    if full_comments:
        body = full_comments.group(1).strip()
        if not body or re.fullmatch(r"(none\.?|n/a|no comments?\.?|no findings\.?)", body, re.IGNORECASE):
            return 0

        raw_lines = [line.rstrip() for line in body.splitlines() if line.strip()]
        lines = [line.strip() for line in raw_lines]
        bullet_lines = [line for line in raw_lines if re.match(r"^([-*]|\d+[.)])\s+", line)]
        if bullet_lines:
            finding_count = 0
            for line in bullet_lines:
                bullet_text = re.sub(r"^([-*]|\d+[.)])\s+", "", line)
                quiet_stripped = QUIET_PRIORITY_RE.sub("", bullet_text).strip(" \t.,;:")
                if EMPTY_SUMMARY_RE.fullmatch(bullet_text) or QUIET_SUMMARY_RE.fullmatch(bullet_text):
                    continue
                if not CONTRAST_RE.search(bullet_text) and QUIET_PRIORITY_RE.match(bullet_text) and not quiet_stripped:
                    continue
                finding_count += 1
            for line in raw_lines:
                if line.startswith((" ", "\t")) or re.match(r"^([-*]|\d+[.)])\s+", line):
                    continue
                line_without_quiet_priority = QUIET_PRIORITY_RE.sub("", line)
                finding_count += len(PRIORITY_RE.findall(line_without_quiet_priority))
            return finding_count
        body_priority_count = 0
        for line in lines:
            line_without_quiet_priority = QUIET_PRIORITY_RE.sub("", line)
            body_priority_count += len(PRIORITY_RE.findall(line_without_quiet_priority))
        if body_priority_count:
            return body_priority_count
        if all(_line_is_quiet_summary(line) for line in lines):
            return 0
        return 1

    priority_count = 0
    review_comment_count = 0
    for line in text.splitlines():
        line_without_quiet_priority = QUIET_PRIORITY_RE.sub("", line)
        priority_count += len(PRIORITY_RE.findall(line_without_quiet_priority))
        if REVIEW_COMMENT_RE.search(line_without_quiet_priority) and not PRIORITY_RE.search(line_without_quiet_priority):
            comment_text = REVIEW_COMMENT_RE.sub("", line_without_quiet_priority).strip()
            if comment_text:
                review_comment_count += 1
    if priority_count:
        return priority_count + review_comment_count

    review_comment_count = sum(
        1
        for line in text.splitlines()
        for _match in REVIEW_COMMENT_RE.finditer(line)
        if not _review_comment_is_quiet(line)
    )
    if review_comment_count:
        return review_comment_count
    return 0


def _review_comment_is_quiet(line: str) -> bool:
    comment_text = REVIEW_COMMENT_RE.sub("", line).strip()
    return bool(comment_text and _line_is_quiet_summary(comment_text))


def _line_is_quiet_summary(line: str) -> bool:
    return bool(
        EMPTY_SUMMARY_RE.fullmatch(line)
        or QUIET_SUMMARY_RE.fullmatch(line)
        or (QUIET_PRIORITY_RE.match(line) and not QUIET_PRIORITY_RE.sub("", line).strip(" \t.,;:"))
    )


def append_error(review_dir: Path, title: str, details: str) -> None:
    errors = review_dir / "_errors.md"
    with errors.open("a", encoding="utf-8") as fh:
        fh.write(f"## {now_iso()} {title}\n\n")
        fh.write(details.rstrip())
        fh.write("\n\n")


@dataclass(frozen=True)
class Reservation:
    run_id: str
    slice_name: str
    pass_number: int
    output_file: Path
    slice_data: dict[str, Any]


@dataclass(frozen=True)
class ReviewExecution:
    reservation: Reservation
    proc: subprocess.CompletedProcess[str]
    launch_error: OSError | None = None


class LockedReviewState(AbstractContextManager["ReviewState"]):
    def __init__(self, review_dir: Path):
        self.review_dir = review_dir.resolve()
        self._fh: Any = None
        self.state: ReviewState | None = None

    def __enter__(self) -> "ReviewState":
        try:
            self.review_dir.mkdir(parents=True, exist_ok=True)
            self._fh = (self.review_dir / "_state.lock").open("a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            self.state = ReviewState.load(self.review_dir)
            return self.state
        except OSError as exc:
            self._release()
            raise ReviewStateError(f"could not open review state lock: {exc}") from exc
        except Exception:
            self._release()
            raise

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self.state = None


class ReviewState:
    """Rich wrapper around the persisted review session state."""

    def __init__(self, review_dir: Path, data: dict[str, Any]):
        self.review_dir = review_dir.resolve()
        self.data = data
        self.validate()

    @classmethod
    def new(cls, review_dir: Path, root: Path) -> "ReviewState":
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "created_at": now_iso(),
                "review_dir": str(review_dir.resolve()),
                "root": str(root.resolve()),
            },
            "slices": {},
            "history": [],
            "completed": False,
            "last_error": None,
        }
        return cls(review_dir, data)

    @classmethod
    def load(cls, review_dir: Path) -> "ReviewState":
        state_path = review_dir.resolve() / "_state.json"
        if not state_path.exists():
            raise ReviewStateError(f"missing review state: {state_path}")
        try:
            with state_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ReviewStateError(f"invalid review state JSON: {exc}") from exc
        return cls(review_dir.resolve(), data)

    @classmethod
    def locked(cls, review_dir: Path) -> LockedReviewState:
        return LockedReviewState(review_dir)

    def save(self) -> None:
        self.validate()
        self.review_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="_state.",
            suffix=".tmp",
            dir=str(self.review_dir),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.review_dir / "_state.json")
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def validate(self) -> None:
        data = self.data
        if not isinstance(data, dict):
            raise ReviewStateError("state must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ReviewStateError(f"unsupported review state schema: {data.get('schema_version')!r}")
        if not isinstance(data.get("session"), dict):
            raise ReviewStateError("state session must be an object")
        session = data["session"]
        if not isinstance(session.get("created_at"), str) or not session["created_at"]:
            raise ReviewStateError("state session must have created_at")
        if not isinstance(session.get("review_dir"), str) or not session["review_dir"]:
            raise ReviewStateError("state session must have review_dir")
        if not isinstance(session.get("root"), str) or not session["root"]:
            raise ReviewStateError("state session must have root")
        if not isinstance(data.get("slices"), dict):
            raise ReviewStateError("state slices must be an object")
        if not isinstance(data.get("history"), list):
            raise ReviewStateError("state history must be an array")
        if not isinstance(data.get("completed"), bool):
            raise ReviewStateError("state completed must be a boolean")
        for name, item in data["slices"].items():
            self._validate_slice_name(name)
            if not isinstance(item, dict):
                raise ReviewStateError(f"slice {name!r} must be an object")
            if item.get("name") != name:
                raise ReviewStateError(f"slice {name!r} has mismatched name")
            if item.get("mode") not in {"native", "prompt"}:
                raise ReviewStateError(f"slice {name!r} has invalid mode")
            if item["mode"] == "native":
                self._validate_native_target(name, item.get("target"))
                if item.get("prompt") is not None:
                    raise ReviewStateError(f"native slice {name!r} cannot have prompt text")
            if item["mode"] == "prompt":
                if item.get("target") is not None:
                    raise ReviewStateError(f"prompt slice {name!r} cannot have native target")
                if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
                    raise ReviewStateError(f"prompt slice {name!r} must have prompt text")
            if not isinstance(item.get("cwd"), str) or not item["cwd"]:
                raise ReviewStateError(f"slice {name!r} must have cwd")
            if not isinstance(item.get("model"), str) or not item["model"]:
                raise ReviewStateError(f"slice {name!r} must have model")
            if not isinstance(item.get("reasoning"), str) or not item["reasoning"]:
                raise ReviewStateError(f"slice {name!r} must have reasoning")
            if not isinstance(item.get("next_pass"), int) or item["next_pass"] < 1:
                raise ReviewStateError(f"slice {name!r} must have a positive next_pass")
            if not isinstance(item.get("complete"), bool):
                raise ReviewStateError(f"slice {name!r} must have complete boolean")
            if not isinstance(item.get("runs"), list):
                raise ReviewStateError(f"slice {name!r} must have runs array")
            for run in item["runs"]:
                self._validate_run(name, run)

    @staticmethod
    def _validate_run(slice_name: str, run: Any) -> None:
        if not isinstance(run, dict):
            raise ReviewStateError(f"slice {slice_name!r} has non-object run entry")
        if not isinstance(run.get("id"), str) or not run["id"]:
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid id")
        if not isinstance(run.get("pass"), int) or run["pass"] < 1:
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid pass")
        if not isinstance(run.get("output_file"), str) or not run["output_file"]:
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid output_file")
        if run.get("status") not in {"running", "findings", "quiet", "uncertain", "failed", "ignored"}:
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid status")
        if not isinstance(run.get("started_at"), str) or not run["started_at"]:
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid started_at")
        if run.get("ended_at") is not None and not isinstance(run.get("ended_at"), str):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid ended_at")
        if run.get("exit_code") is not None and not isinstance(run.get("exit_code"), int):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid exit_code")
        if run.get("classification") is not None and not isinstance(run.get("classification"), str):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid classification")
        finding_count = run.get("finding_count")
        if finding_count is not None and (not isinstance(finding_count, int) or finding_count < 0):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid finding_count")
        ignored_count = run.get("ignored_count")
        if ignored_count is not None and (not isinstance(ignored_count, int) or ignored_count < 0):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid ignored_count")
        if run.get("runner_pid") is not None and not isinstance(run.get("runner_pid"), int):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid runner_pid")
        if run.get("runner_key") is not None and not isinstance(run.get("runner_key"), str):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid runner_key")
        if run.get("error") is not None and not isinstance(run.get("error"), str):
            raise ReviewStateError(f"slice {slice_name!r} has run with invalid error")

    @staticmethod
    def _validate_native_target(slice_name: str, target: Any) -> None:
        if not isinstance(target, dict):
            raise ReviewStateError(f"native slice {slice_name!r} must have target object")
        target_count = sum(
            (
                target.get("uncommitted") is True,
                isinstance(target.get("base"), str) and bool(target["base"]),
                isinstance(target.get("commit"), str) and bool(target["commit"]),
            )
        )
        if target_count != 1:
            raise ReviewStateError(f"native slice {slice_name!r} must have exactly one target")
        allowed = {"uncommitted", "base", "commit"}
        extra = set(target) - allowed
        if extra:
            raise ReviewStateError(f"native slice {slice_name!r} has unsupported target keys")
        if "uncommitted" in target and target.get("uncommitted") is not True:
            raise ReviewStateError(f"native slice {slice_name!r} has invalid uncommitted target")
        for key in ("base", "commit"):
            if key in target and (not isinstance(target[key], str) or not target[key]):
                raise ReviewStateError(f"native slice {slice_name!r} has invalid {key} target")

    @staticmethod
    def _validate_slice_name(name: str) -> None:
        if not isinstance(name, str) or not SLICE_RE.fullmatch(name):
            raise ReviewStateError(
                "slice names must be 1-64 chars of lowercase letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ReviewStateError("slice names cannot contain path separators or dot names")

    def add_slice(
        self,
        *,
        name: str,
        mode: str,
        target: dict[str, Any] | None,
        prompt: str | None,
        cwd: Path,
        model: str = DEFAULT_MODEL,
        reasoning: str = DEFAULT_REASONING,
    ) -> None:
        self._validate_slice_name(name)
        if name in self.data["slices"]:
            raise ReviewStateError(f"slice already exists: {name}")
        if mode not in {"native", "prompt"}:
            raise ReviewStateError("slice mode must be native or prompt")
        if mode == "native":
            if not target:
                raise ReviewStateError("native slices require --uncommitted, --base, or --commit")
            if prompt:
                raise ReviewStateError("native target flags cannot be combined with prompt input")
        if mode == "prompt":
            if target:
                raise ReviewStateError("prompt slices cannot be combined with native target flags")
            if not prompt or not prompt.strip():
                raise ReviewStateError("prompt slices require non-empty prompt text")
        self.data["slices"][name] = {
            "name": name,
            "mode": mode,
            "target": target,
            "prompt": prompt,
            "cwd": str(cwd.resolve()),
            "model": model,
            "reasoning": reasoning,
            "next_pass": 1,
            "complete": False,
            "runs": [],
            "last_error": None,
        }
        self.data["completed"] = False
        self.data["history"].append({"event": "slice_added", "slice": name, "at": now_iso()})

    def reserve_eligible(self) -> list[Reservation]:
        reservations: list[Reservation] = []
        self._recover_stale_running_runs()
        if self._has_running_runs():
            self._refresh_completed()
            return reservations
        for name in sorted(self.data["slices"]):
            item = self.data["slices"][name]
            if item["complete"]:
                continue
            if any(run.get("status") == "running" for run in item["runs"]):
                continue
            pass_number = item["next_pass"]
            output_file = self._next_output_file(pass_number, name, item["runs"])
            run_id = uuid.uuid4().hex
            run = {
                "id": run_id,
                "pass": pass_number,
                "output_file": str(output_file),
                "status": "running",
                "started_at": now_iso(),
                "ended_at": None,
                "exit_code": None,
                "classification": None,
                "finding_count": None,
                "ignored_count": 0,
                "runner_pid": os.getpid(),
                "runner_key": _process_key(os.getpid()),
                "error": None,
            }
            item["runs"].append(run)
            item["last_error"] = None
            self.data["history"].append(
                {"event": "run_reserved", "slice": name, "run_id": run_id, "pass": pass_number, "at": now_iso()}
            )
            reservations.append(
                Reservation(
                    run_id=run_id,
                    slice_name=name,
                    pass_number=pass_number,
                    output_file=output_file,
                    slice_data=json.loads(json.dumps(item)),
                )
            )
        self._refresh_completed()
        return reservations

    def complete_run(
        self,
        *,
        run_id: str,
        slice_name: str,
        status: str,
        exit_code: int | None,
        classification: str | None,
        finding_count: int | None = None,
        error: str | None = None,
    ) -> bool:
        item = self.data["slices"][slice_name]
        run = next((candidate for candidate in item["runs"] if candidate.get("id") == run_id), None)
        if run is None:
            raise ReviewStateError(f"run not found: {run_id}")
        if run.get("status") != "running":
            self.data["history"].append(
                {
                    "event": "late_run_completion_ignored",
                    "slice": slice_name,
                    "run_id": run_id,
                    "current_status": run.get("status"),
                    "attempted_status": status,
                    "at": now_iso(),
                }
            )
            self._refresh_completed()
            return False
        run["status"] = status
        run["ended_at"] = now_iso()
        run["exit_code"] = exit_code
        run["classification"] = classification
        run["finding_count"] = finding_count
        run["error"] = error

        if status == "findings":
            item["next_pass"] = max(item["next_pass"], int(run["pass"]) + 1)
            item["complete"] = False
            item["last_error"] = None
        elif status in {"quiet", "uncertain"}:
            item["complete"] = True
            item["last_error"] = None
        elif status == "failed":
            item["complete"] = False
            item["last_error"] = error or "review process failed"
            self.data["last_error"] = {"slice": slice_name, "run_id": run_id, "error": item["last_error"], "at": now_iso()}
        else:
            raise ReviewStateError(f"invalid run status: {status}")

        self.data["history"].append(
            {
                "event": "run_completed",
                "slice": slice_name,
                "run_id": run_id,
                "status": status,
                "classification": classification,
                "at": now_iso(),
            }
        )
        self._refresh_completed()
        return True

    def report_ignored_findings(
        self,
        *,
        ignored_count: int,
        slice_name: str | None = None,
        run_id: str | None = None,
        pass_number: int | None = None,
    ) -> tuple[bool, str]:
        if ignored_count < 0:
            raise ReviewStateError("ignored count must be zero or greater")
        candidates = self._find_ignored_report_candidates(
            slice_name=slice_name,
            run_id=run_id,
            pass_number=pass_number,
        )
        if not candidates:
            raise ReviewStateError("no matching finding run found")
        if len(candidates) > 1:
            names = ", ".join(f"{name}:pass-{run['pass']}" for name, run in candidates)
            raise ReviewStateError(f"multiple matching finding runs found; pass --slice or --run-id: {names}")

        name, run = candidates[0]
        finding_count = run.get("finding_count")
        if not isinstance(finding_count, int) or finding_count < 1:
            raise ReviewStateError("target run does not have a positive finding_count")
        if ignored_count > finding_count:
            raise ReviewStateError(
                f"ignored count {ignored_count} exceeds finding count {finding_count} for slice {name}"
            )
        if ignored_count < finding_count:
            return False, (
                f"unchanged: slice {name} has {finding_count} findings and only {ignored_count} ignored; "
                "follow-up remains required."
            )

        run["status"] = "ignored"
        run["classification"] = "ignored_findings"
        run["ignored_count"] = ignored_count
        run["ignored_at"] = now_iso()
        item = self.data["slices"][name]
        item["complete"] = True
        item["last_error"] = None
        self.data["history"].append(
            {
                "event": "findings_ignored",
                "slice": name,
                "run_id": run["id"],
                "pass": run["pass"],
                "ignored_count": ignored_count,
                "finding_count": finding_count,
                "at": now_iso(),
            }
        )
        self._refresh_completed()
        return True, f"complete: slice {name} pass {run['pass']} had {finding_count} findings and all were ignored."

    def _find_ignored_report_candidates(
        self,
        *,
        slice_name: str | None,
        run_id: str | None,
        pass_number: int | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if slice_name is not None:
            self._validate_slice_name(slice_name)
            if slice_name not in self.data["slices"]:
                raise ReviewStateError(f"slice not found: {slice_name}")
        candidates: list[tuple[str, dict[str, Any]]] = []
        for name, item in self.data["slices"].items():
            if slice_name is not None and name != slice_name:
                continue
            latest_run = item["runs"][-1] if item["runs"] else None
            for run in item["runs"]:
                if run_id is not None and run.get("id") != run_id:
                    continue
                if pass_number is not None and run.get("pass") != pass_number:
                    continue
                if run.get("status") == "findings" and run is latest_run:
                    candidates.append((name, run))
        candidates.sort(key=lambda pair: str(pair[1].get("started_at", "")), reverse=True)
        return candidates

    def _recover_stale_running_runs(self) -> None:
        for name, item in self.data["slices"].items():
            for run in item["runs"]:
                if run.get("status") != "running":
                    continue
                if _running_reservation_is_active(run):
                    continue
                error = "stale running reservation recovered; review run will be retried"
                run["status"] = "failed"
                run["ended_at"] = now_iso()
                run["exit_code"] = None
                run["classification"] = None
                run["error"] = error
                item["complete"] = False
                item["last_error"] = error
                self.data["last_error"] = {"slice": name, "run_id": run["id"], "error": error, "at": now_iso()}
                append_error(
                    self.review_dir,
                    f"stale running review recovered for {name}",
                    f"Slice: {name}\nOutput: {run.get('output_file')}\nError: {error}",
                )
                self.data["history"].append(
                    {
                        "event": "stale_run_recovered",
                        "slice": name,
                        "run_id": run["id"],
                        "pass": run.get("pass"),
                        "at": now_iso(),
                    }
                )

    def _has_running_runs(self) -> bool:
        return any(
            run.get("status") == "running"
            for item in self.data["slices"].values()
            for run in item["runs"]
        )

    def _refresh_completed(self) -> None:
        slices = self.data["slices"].values()
        self.data["completed"] = bool(self.data["slices"]) and all(
            item["complete"] and not any(run.get("status") == "running" for run in item["runs"]) for item in slices
        )
        if self.data["completed"] and all(item.get("last_error") is None for item in self.data["slices"].values()):
            self.data["last_error"] = None

    def _next_output_file(self, pass_number: int, name: str, runs: Iterable[dict[str, Any]]) -> Path:
        base = self.review_dir / f"{pass_number}-{name}.md"
        used = {str(run.get("output_file")) for run in runs}
        if str(base) not in used and not base.exists():
            return base
        attempt = 2
        while True:
            candidate = self.review_dir / f"{pass_number}-{name}-retry{attempt}.md"
            if str(candidate) not in used and not candidate.exists():
                return candidate
            attempt += 1


def build_review_command(slice_data: dict[str, Any], output_file: Path) -> tuple[list[str], str | None]:
    cmd = [
        "codex",
        "exec",
        "review",
        "--ephemeral",
        "-m",
        slice_data["model"],
        "-c",
        f'model_reasoning_effort="{slice_data["reasoning"]}"',
    ]
    if slice_data["mode"] == "native":
        target = slice_data["target"]
        if target.get("uncommitted"):
            cmd.append("--uncommitted")
        elif "base" in target:
            cmd.extend(["--base", target["base"]])
        elif "commit" in target:
            cmd.extend(["--commit", target["commit"]])
        else:
            raise ReviewStateError("native slice target is invalid")
        cmd.extend(["-o", str(output_file)])
        return cmd, None

    cmd.extend(["-o", str(output_file), "-"])
    return cmd, slice_data["prompt"]


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_key(pid: int) -> str | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except OSError:
        try:
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError:
            return None
        if proc.returncode == 0 and proc.stdout.strip():
            return f"{pid}:{proc.stdout.strip()}"
        return None
    fields = stat.rsplit(") ", 1)
    if len(fields) != 2:
        return None
    parts = fields[1].split()
    if len(parts) < 20:
        return None
    return f"{pid}:{parts[19]}"


def _running_reservation_is_active(run: dict[str, Any]) -> bool:
    runner_pid = run.get("runner_pid")
    if not isinstance(runner_pid, int) or not _pid_is_alive(runner_pid):
        return False

    runner_key = run.get("runner_key")
    if isinstance(runner_key, str) and runner_key:
        current_key = _process_key(runner_pid)
        return current_key is not None and runner_key == current_key

    started_at = run.get("started_at")
    return isinstance(started_at, str) and parse_iso(started_at) is not None


Runner = Callable[[list[str], Path, str | None, Path, dict[str, Any]], subprocess.CompletedProcess[str]]


def default_runner(
    cmd: list[str],
    cwd: Path,
    input_text: str | None,
    output_file: Path,
    slice_data: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    del output_file, slice_data
    return subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_reserved_review(
    reservation: Reservation,
    command_runner: Runner,
) -> ReviewExecution:
    cmd, input_text = build_review_command(reservation.slice_data, reservation.output_file)
    try:
        proc = command_runner(
            cmd,
            Path(reservation.slice_data["cwd"]),
            input_text,
            reservation.output_file,
            reservation.slice_data,
        )
    except OSError as exc:
        proc = subprocess.CompletedProcess(cmd, 1, "", str(exc))
        return ReviewExecution(reservation=reservation, proc=proc, launch_error=exc)
    return ReviewExecution(reservation=reservation, proc=proc)


def evaluate_completed_process(
    review_dir: Path,
    reservation: Reservation,
    proc: subprocess.CompletedProcess[str],
) -> tuple[str, str | None, int | None, str | None]:
    output_file = reservation.output_file
    if proc.returncode != 0:
        error = (
            f"Slice: {reservation.slice_name}\n"
            f"Output: {output_file}\n"
            f"Exit code: {proc.returncode}\n\n"
            f"stdout:\n{proc.stdout or ''}\n\n"
            f"stderr:\n{proc.stderr or ''}"
        )
        append_error(review_dir, f"failed review for {reservation.slice_name}", error)
        return "failed", None, None, f"review command exited with {proc.returncode}"

    try:
        text = output_file.read_text(encoding="utf-8")
    except OSError as exc:
        error = f"Slice: {reservation.slice_name}\nOutput: {output_file}\nError: {exc}"
        append_error(review_dir, f"unreadable review output for {reservation.slice_name}", error)
        return "failed", None, None, f"review output is unreadable: {exc}"

    if not text.strip():
        error = f"Slice: {reservation.slice_name}\nOutput: {output_file}\nError: empty review output"
        append_error(review_dir, f"empty review output for {reservation.slice_name}", error)
        return "failed", None, None, "review output is empty"

    classification = classify_output(text)
    finding_count = count_findings(text) if classification == "findings" else 0
    if classification == "uncertain":
        append_error(
            review_dir,
            f"uncertain successful review output for {reservation.slice_name}",
            f"Slice: {reservation.slice_name}\nOutput: {output_file}\nAction: marked complete, but classifier was uncertain.",
        )
    return classification, classification, finding_count, None


def run_reviews(
    review_dir: Path,
    *,
    command_runner: Runner = default_runner,
    stdout: Any = sys.stdout,
) -> int:
    review_dir = review_dir.resolve()
    with ReviewState.locked(review_dir) as state:
        reservations = state.reserve_eligible()
        state.save()
        completed = state.data["completed"]
        any_running = any(
            run.get("status") == "running"
            for item in state.data["slices"].values()
            for run in item.get("runs", [])
            if all(run.get("id") != reservation.run_id for reservation in reservations)
        )

    if not reservations:
        if completed:
            print("done: all review slices are complete; no further review runs are needed.", file=stdout)
        elif any_running:
            print("waiting: no eligible slices; review runs are already in progress. call again later.", file=stdout)
        else:
            print("waiting: no review slices have been registered. add slices, then call again.", file=stdout)
        return 0

    state_completed = False
    with ThreadPoolExecutor(max_workers=len(reservations)) as executor:
        futures = [executor.submit(run_reserved_review, reservation, command_runner) for reservation in reservations]
        for future in as_completed(futures):
            execution = future.result()
            reservation = execution.reservation
            proc = execution.proc
            if execution.launch_error is not None:
                exc = execution.launch_error
                append_error(
                    review_dir,
                    f"failed to launch review for {reservation.slice_name}",
                    f"Slice: {reservation.slice_name}\nOutput: {reservation.output_file}\nError: {exc}",
                )
                status, classification, finding_count, error = (
                    "failed",
                    None,
                    None,
                    f"review command failed to launch: {exc}",
                )
            else:
                status, classification, finding_count, error = evaluate_completed_process(review_dir, reservation, proc)
            with ReviewState.locked(review_dir) as state:
                completion_applied = state.complete_run(
                    run_id=reservation.run_id,
                    slice_name=reservation.slice_name,
                    status=status,
                    exit_code=proc.returncode,
                    classification=classification,
                    finding_count=finding_count,
                    error=error,
                )
                state.save()
                state_completed = state.data["completed"]
            display_status = status if completion_applied else "skipped-late-completion"
            print(
                f"{reservation.slice_name}: pass {reservation.pass_number} {display_status} -> {reservation.output_file}",
                file=stdout,
                flush=True,
            )

    if state_completed:
        print("done: all review slices are complete; no further review runs are needed.", file=stdout)
    else:
        print("call again: at least one review slice remains eligible for another pass.", file=stdout)
    return 0


def parse_add_slice_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register one review slice in an initialized review state.")
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--uncommitted", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--commit")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--cwd", type=Path)
    return parser.parse_args(argv)


def add_slice_from_args(args: argparse.Namespace, *, stdin: Any = sys.stdin) -> None:
    native_count = sum(bool(value) for value in (args.uncommitted, args.base, args.commit))
    prompt_requested = args.prompt_file is not None
    if native_count > 1:
        raise ReviewStateError("choose only one native target flag: --uncommitted, --base, or --commit")
    if native_count and prompt_requested:
        raise ReviewStateError("native target flags cannot be combined with --prompt-file")

    target: dict[str, Any] | None = None
    prompt: str | None = None
    mode: str
    if native_count:
        mode = "native"
        if args.uncommitted:
            target = {"uncommitted": True}
        elif args.base:
            target = {"base": args.base}
        else:
            target = {"commit": args.commit}
    else:
        mode = "prompt"
        if args.prompt_file is not None:
            if str(args.prompt_file) == "-":
                prompt = stdin.read()
            else:
                prompt = args.prompt_file.read_text(encoding="utf-8")
        else:
            prompt = stdin.read()

    with ReviewState.locked(args.review_dir) as state:
        cwd = args.cwd.resolve() if args.cwd is not None else Path(state.data["session"]["root"]).resolve()
        state.add_slice(
            name=args.name,
            mode=mode,
            target=target,
            prompt=prompt,
            cwd=cwd,
            model=args.model,
            reasoning=args.reasoning,
        )
        state.save()


def parse_report_ignored_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report how many findings were ignored for a finding run; state rules decide the next step."
    )
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--count", required=True, type=int, help="Number of findings from the run that were ignored.")
    parser.add_argument("--slice", dest="slice_name")
    parser.add_argument("--run-id")
    parser.add_argument("--pass", dest="pass_number", type=int)
    return parser.parse_args(argv)


def report_ignored_from_args(args: argparse.Namespace) -> tuple[bool, str]:
    with ReviewState.locked(args.review_dir) as state:
        changed, message = state.report_ignored_findings(
            ignored_count=args.count,
            slice_name=args.slice_name,
            run_id=args.run_id,
            pass_number=args.pass_number,
        )
        if changed:
            state.save()
        return changed, message
