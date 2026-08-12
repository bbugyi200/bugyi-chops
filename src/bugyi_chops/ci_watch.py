"""Watch SASE CI health, avoid overlapping repairs, and merge guarded release PRs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from sase.chops import (
    ChopInvocation,
    ChopReport,
    ChopResultBuilder,
    Tone,
    validate_chop_report,
)

from bugyi_chops._common import context_vars, result_with_summary, run_chop, safe_fragment
from bugyi_chops._report import add_facts_footer, start_report

CHOP_NAME = "ci_watch"
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
GREEN_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
RELEASE_BRANCH_PREFIXES = ("release-please--", "release-plz-")
IN_FLIGHT_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
KNOWN_RUN_STATUSES = IN_FLIGHT_STATUSES | {"completed"}
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+\Z")
TOKEN_PATTERN = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_|bearer\s+|token\s*[=:]|secret\s*[=:])\S*"
)
MAX_REPOS = 50
MAX_LEDGER_NAMES = 10
MAX_HEAD_RUNS = 20
MAX_HEAD_JOBS = 100
MAX_HEAD_EVIDENCE_REPOS_PER_TICK = 10
MAX_RELEASE_PR_DETAILS = 8
MAX_LEDGER_MERGES = 50
MAX_GATED_FIXES = 100
MAX_GATE_POLLS_PER_TICK = 10
MAX_TEXT = 240
STREAK_FILE_NAME = "ci_watch_red_streaks.json"
FIX_LEDGER_FILE_NAME = "ci_watch_fixes.json"
RELEASE_LEDGER_FILE_NAME = "ci_watch_releases.json"
RELEASE_REPORT_FILE_NAME = "ci_watch_releases.report.json"
FIX_LEDGER_RETENTION_DAYS = 90
RELEASE_LEDGER_RETENTION_DAYS = 90
RELEASE_REPORT_WINDOW_DAYS = 30
RELEASE_VERSION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)"
)

type JsonObject = dict[str, Any]


class CiWatchError(RuntimeError):
    """A fail-closed adapter or configuration error."""


class RepoState(StrEnum):
    ERROR = "error"
    NO_CI = "no_ci"
    PENDING = "pending"
    RED = "red"
    GREEN = "green"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> CommandResult: ...


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    cwd: str | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except OSError as error:
        raise CiWatchError(f"failed to execute {argv[0]}: {error}") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _bounded(value: object, *, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value).split())
    text = TOKEN_PATTERN.sub("[redacted]", text)
    return text[:limit]


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CiWatchError(f"missing or invalid {key!r}")
    return value.strip()


def _repo(value: object) -> str:
    if not isinstance(value, str) or not REPO_PATTERN.fullmatch(value):
        raise CiWatchError(f"invalid repository name: {_bounded(value)!r}")
    return value


def _branch(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_TEXT
        or not BRANCH_PATTERN.fullmatch(value)
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value.split("/")
    ):
        raise CiWatchError(f"invalid branch name: {_bounded(value)!r}")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise CiWatchError(f"invalid commit SHA: {_bounded(value)!r}")
    return value.lower()


def _json_object(raw: str, *, source: str) -> JsonObject:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CiWatchError(f"{source} returned malformed JSON: {error}") from error
    if not isinstance(value, dict):
        raise CiWatchError(f"{source} returned non-object JSON")
    return cast(JsonObject, value)


def _json_array(raw: str, *, source: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CiWatchError(f"{source} returned malformed JSON: {error}") from error
    if not isinstance(value, list):
        raise CiWatchError(f"{source} returned non-array JSON")
    return value


@dataclass(frozen=True)
class RepoObservation:
    repo: str
    active: JsonObject | None = None
    commit: JsonObject | None = None
    error: str | None = None


class ActstatClient:
    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner

    def sweep(self, repos: Sequence[str]) -> dict[str, RepoObservation]:
        allowed = set(repos)
        rows: dict[str, dict[str, JsonObject | str]] = {repo: {} for repo in repos}
        result = self._runner([self.executable, "-f", "jsonl"])
        parsed_rows = 0
        for line_number, line in enumerate(result.stdout.splitlines(), start=1):
            if not line.strip():
                continue
            row = _json_object(line, source=f"actstat line {line_number}")
            kind = _required_string(row, "type")
            if kind not in {"active_commit", "commit", "repo_error"}:
                raise CiWatchError(f"actstat returned unknown record type {kind!r}")
            raw_repo = row.get("repo")
            if (
                kind == "repo_error"
                and isinstance(raw_repo, str)
                and OWNER_PATTERN.fullmatch(raw_repo)
            ):
                repo = raw_repo
            else:
                repo = _repo(raw_repo)
            parsed_rows += 1
            if repo not in allowed:
                continue
            key = {
                "active_commit": "active",
                "commit": "commit",
                "repo_error": "error",
            }[kind]
            if key in rows[repo]:
                raise CiWatchError(f"actstat returned duplicate {kind} rows for {repo}")
            rows[repo][key] = _required_string(row, "error") if kind == "repo_error" else row
        if result.returncode != 0 and parsed_rows == 0:
            detail = _bounded(result.stderr or result.stdout) or "-"
            raise CiWatchError(f"actstat failed: exit_code={result.returncode} detail={detail}")
        return {
            repo: RepoObservation(
                repo=repo,
                active=cast(JsonObject | None, row.get("active")),
                commit=cast(JsonObject | None, row.get("commit")),
                error=cast(str | None, row.get("error"))
                or (None if row else "missing_observation"),
            )
            for repo, row in rows.items()
        }


def _nested_failure(run: Mapping[str, Any]) -> bool:
    jobs = run.get("jobs")
    if not isinstance(jobs, list):
        return False
    for item in jobs:
        if not isinstance(item, dict):
            continue
        if str(item.get("conclusion", "")).lower() == "failure":
            return True
        steps = item.get("steps")
        if isinstance(steps, list) and any(
            isinstance(step, dict) and str(step.get("conclusion", "")).lower() == "failure"
            for step in steps
        ):
            return True
    return False


def actionably_red(commit: Mapping[str, Any]) -> bool:
    runs = commit.get("runs")
    if not isinstance(runs, list):
        return False
    for item in runs:
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion", "")).lower()
        if conclusion in RED_CONCLUSIONS:
            return True
        if conclusion == "cancelled" and _nested_failure(item):
            return True
    return False


def classify_repo(observation: RepoObservation) -> RepoState:
    if observation.error or observation.commit is None:
        return RepoState.ERROR
    if actionably_red(observation.commit):
        return RepoState.RED
    conclusion = str(observation.commit.get("conclusion", "")).lower()
    return RepoState.GREEN if conclusion in GREEN_CONCLUSIONS else RepoState.PENDING


def _classification_reason(observation: RepoObservation, state: RepoState) -> str:
    if state is RepoState.ERROR:
        return observation.error or "missing_commit"
    if state is RepoState.RED:
        return "actionable_failure"
    if state is RepoState.GREEN:
        return "green"
    return "superseded_or_unsettled"


@dataclass(frozen=True)
class BranchHead:
    branch: str
    sha: str


@dataclass(frozen=True)
class HeadCiEvidence:
    sha: str
    has_in_flight: bool
    all_completed_green: bool
    failing_jobs: tuple[str, ...]
    successful_jobs: tuple[str, ...]
    run_url: str | None = None


@dataclass(frozen=True)
class FailureEvidence:
    sha: str
    failing_jobs: tuple[str, ...]
    run_url: str | None
    head_unsettled: bool = False
    current_head_sha: str | None = None

    @property
    def fingerprint(self) -> tuple[str, ...]:
        return self.failing_jobs

    @property
    def fingerprint_key(self) -> str:
        payload = "\0".join(self.fingerprint).encode()
        return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(frozen=True)
class RepoDecision:
    state: RepoState
    reason: str
    head: BranchHead | None = None
    failure: FailureEvidence | None = None


def _failed_job_names(commit: Mapping[str, Any]) -> tuple[str, ...]:
    names: set[str] = set()
    runs = commit.get("runs")
    if not isinstance(runs, list):
        return ()
    for run in runs:
        if not isinstance(run, dict):
            continue
        jobs = run.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            conclusion = str(job.get("conclusion", "")).lower()
            if conclusion not in RED_CONCLUSIONS:
                continue
            name = job.get("name")
            if isinstance(name, str) and name.strip():
                names.add(_bounded(name, limit=100))
    return tuple(sorted(names))


def _failure_run_url(commit: Mapping[str, Any]) -> str | None:
    runs = commit.get("runs")
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict):
            continue
        url = run.get("url")
        if isinstance(url, str) and url.startswith("https://github.com/"):
            return url[:MAX_TEXT]
    return None


def decide_repo(
    observation: RepoObservation,
    head: BranchHead,
    *,
    head_evidence: HeadCiEvidence | None = None,
) -> RepoDecision:
    """Classify one observed repository from terminal CI evidence."""
    base_state = classify_repo(observation)
    if base_state is RepoState.ERROR:
        return RepoDecision(base_state, _classification_reason(observation, base_state))
    if observation.commit is None:
        return RepoDecision(RepoState.ERROR, "missing_commit")
    settled_sha = _commit_sha(observation)
    current = head.sha.startswith(settled_sha)
    if base_state is RepoState.RED:
        settled_jobs = _failed_job_names(observation.commit)
        if not settled_jobs:
            return RepoDecision(RepoState.ERROR, "red_evidence_missing_job_identity")
        if current:
            return RepoDecision(
                RepoState.RED,
                "actionable_failure",
                head,
                FailureEvidence(
                    head.sha,
                    settled_jobs,
                    _failure_run_url(observation.commit),
                ),
            )
        if head_evidence is None or head_evidence.sha != head.sha:
            return RepoDecision(RepoState.ERROR, "missing_head_ci_evidence")
        if head_evidence.failing_jobs:
            return RepoDecision(
                RepoState.RED,
                "actionable_failure",
                head,
                FailureEvidence(
                    head.sha,
                    head_evidence.failing_jobs,
                    head_evidence.run_url,
                ),
            )
        remaining = tuple(sorted(set(settled_jobs).difference(head_evidence.successful_jobs)))
        if not remaining:
            return RepoDecision(RepoState.GREEN, "superseded_by_newer_success", head)
        if head_evidence.all_completed_green:
            return RepoDecision(RepoState.GREEN, "green", head)
        if head_evidence.has_in_flight:
            return RepoDecision(
                RepoState.RED,
                "head_unsettled",
                head,
                FailureEvidence(
                    settled_sha,
                    remaining,
                    _failure_run_url(observation.commit),
                    head_unsettled=True,
                    current_head_sha=head.sha,
                ),
            )
        return RepoDecision(RepoState.PENDING, "superseded_or_unsettled")
    if base_state is RepoState.GREEN:
        if current:
            return RepoDecision(RepoState.GREEN, "green", head)
        return RepoDecision(RepoState.PENDING, "newer_head_unsettled")
    return RepoDecision(RepoState.PENDING, "superseded_or_unsettled")


@dataclass(frozen=True)
class ReleasePr:
    number: int
    head_ref_name: str
    base_ref_name: str
    head_oid: str
    is_draft: bool
    mergeable: str
    merge_state_status: str
    checks: tuple[tuple[str, str], ...]
    url: str
    title: str
    created_at: str

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ReleasePr:
        number = value.get("number")
        if not isinstance(number, int) or number <= 0:
            raise CiWatchError("release PR has an invalid number")
        rollup = value.get("statusCheckRollup")
        if not isinstance(rollup, list):
            raise CiWatchError("release PR has an invalid statusCheckRollup")
        checks: list[tuple[str, str]] = []
        for check in rollup:
            if not isinstance(check, dict):
                raise CiWatchError("release PR check is not an object")
            checks.append(
                (
                    str(check.get("status", "")).upper(),
                    str(check.get("conclusion", "")).upper(),
                )
            )
        url = str(value.get("url", ""))
        if url and not url.startswith("https://github.com/"):
            raise CiWatchError("release PR URL is not a GitHub URL")
        title = _bounded(_required_string(value, "title"))
        created_at = _required_string(value, "createdAt")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise CiWatchError("release PR has an invalid createdAt") from error
        if parsed_created_at.tzinfo is None:
            raise CiWatchError("release PR has an invalid createdAt")
        return cls(
            number=number,
            head_ref_name=_branch(value.get("headRefName")),
            base_ref_name=_branch(value.get("baseRefName")),
            head_oid=_sha(value.get("headRefOid")),
            is_draft=value.get("isDraft") is True,
            mergeable=str(value.get("mergeable", "")).upper(),
            merge_state_status=str(value.get("mergeStateStatus", "")).upper(),
            checks=tuple(checks),
            url=url,
            title=title,
            created_at=_bounded(created_at),
        )


@dataclass(frozen=True)
class MergePlan:
    repo: str
    pr: ReleasePr


@dataclass(frozen=True)
class ReleaseObservation:
    repo: str
    numbers: tuple[int, ...] | None
    prs: tuple[ReleasePr, ...] = ()
    error: str | None = None


def plan_release_merge(
    repo: str,
    branch_state: RepoState,
    default_branch: str,
    candidates: Sequence[ReleasePr],
    *,
    generator_busy: bool,
) -> tuple[MergePlan | None, str]:
    if branch_state is not RepoState.GREEN:
        return None, "default_branch_not_green"
    if not candidates:
        return None, "no_release_pr"
    if len(candidates) != 1:
        return None, "ambiguous_release_prs"
    pr = candidates[0]
    if not pr.head_ref_name.startswith(RELEASE_BRANCH_PREFIXES):
        return None, "not_release_pr"
    if pr.is_draft:
        return None, "release_pr_draft"
    if pr.base_ref_name != default_branch:
        return None, "release_pr_wrong_base"
    if pr.mergeable != "MERGEABLE":
        return None, "release_pr_not_mergeable"
    if pr.merge_state_status != "CLEAN":
        return None, "release_pr_not_clean"
    if not pr.checks:
        return None, "release_pr_empty_rollup"
    if any(
        status != "COMPLETED" or conclusion not in GREEN_CHECK_CONCLUSIONS
        for status, conclusion in pr.checks
    ):
        return None, "release_pr_checks_not_green"
    if generator_busy:
        return None, "release_generator_busy"
    return MergePlan(repo, pr), "eligible"


class GitHubReader:
    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner

    def _json(self, argv: Sequence[str], *, source: str) -> Any:
        result = self._runner([self.executable, *argv])
        if result.returncode != 0:
            detail = _bounded(result.stderr or result.stdout) or "-"
            raise CiWatchError(f"{source} failed: exit_code={result.returncode} detail={detail}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CiWatchError(f"{source} returned malformed JSON: {error}") from error

    def default_branch_head(self, repo: str) -> BranchHead:
        repo = _repo(repo)
        metadata = self._json(["api", f"repos/{repo}"], source=f"{repo} metadata")
        if not isinstance(metadata, dict):
            raise CiWatchError(f"{repo} metadata is not an object")
        branch = _branch(metadata.get("default_branch"))
        commit = self._json(
            ["api", f"repos/{repo}/commits/{quote(branch, safe='')}"],
            source=f"{repo} default branch head",
        )
        if not isinstance(commit, dict):
            raise CiWatchError(f"{repo} default branch head is not an object")
        return BranchHead(branch, _sha(commit.get("sha")))

    def workflow_count(self, repo: str) -> int:
        repo = _repo(repo)
        data = self._json(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repo}/actions/workflows",
                "-f",
                "per_page=1",
            ],
            source=f"{repo} workflows",
        )
        if not isinstance(data, dict):
            raise CiWatchError(f"{repo} workflows are not an object")
        count = data.get("total_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise CiWatchError(f"{repo} workflows have an invalid total_count")
        return count

    def head_ci_evidence(self, repo: str, sha: str) -> HeadCiEvidence:
        repo = _repo(repo)
        sha = _sha(sha)
        data = self._json(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repo}/actions/runs",
                "-f",
                f"head_sha={sha}",
                "-f",
                f"per_page={MAX_HEAD_RUNS}",
            ],
            source=f"{repo} HEAD workflow runs",
        )
        runs = self._bounded_collection(
            repo,
            data,
            collection_key="workflow_runs",
            limit=MAX_HEAD_RUNS,
            source="HEAD workflow runs",
        )
        has_in_flight = False
        all_completed_green = bool(runs)
        latest_job_conclusions: dict[str, str] = {}
        red_run_url: str | None = None
        terminal_red_without_job = False
        for run in runs:
            status = run.get("status")
            if not isinstance(status, str) or status.lower() not in KNOWN_RUN_STATUSES:
                raise CiWatchError(f"{repo} HEAD workflow runs contain an invalid status")
            normalized_status = status.lower()
            if normalized_status in IN_FLIGHT_STATUSES:
                has_in_flight = True
                all_completed_green = False
                continue
            conclusion = run.get("conclusion")
            if not isinstance(conclusion, str):
                raise CiWatchError(f"{repo} HEAD workflow runs contain an invalid conclusion")
            normalized_conclusion = conclusion.lower()
            if normalized_conclusion not in GREEN_CONCLUSIONS:
                all_completed_green = False
            run_id = run.get("id")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise CiWatchError(f"{repo} HEAD workflow run has an invalid id")
            jobs = self._run_jobs(repo, run_id)
            run_has_red_job = False
            for job in jobs:
                job_status = job.get("status")
                conclusion_value = job.get("conclusion")
                name = job.get("name")
                if (
                    not isinstance(job_status, str)
                    or job_status.lower() not in KNOWN_RUN_STATUSES
                    or not isinstance(name, str)
                    or not name.strip()
                ):
                    raise CiWatchError(f"{repo} HEAD jobs contain invalid fields")
                if job_status.lower() != "completed":
                    continue
                if not isinstance(conclusion_value, str):
                    raise CiWatchError(f"{repo} HEAD jobs contain an invalid conclusion")
                bounded_name = _bounded(name, limit=100)
                normalized_job_conclusion = conclusion_value.lower()
                latest_job_conclusions.setdefault(bounded_name, normalized_job_conclusion)
                if normalized_job_conclusion in RED_CONCLUSIONS:
                    run_has_red_job = True
            if normalized_conclusion in RED_CONCLUSIONS or (
                normalized_conclusion == "cancelled" and run_has_red_job
            ):
                terminal_red_without_job = terminal_red_without_job or not run_has_red_job
                if red_run_url is None:
                    candidate_url = run.get("html_url")
                    if isinstance(candidate_url, str) and candidate_url.startswith(
                        "https://github.com/"
                    ):
                        red_run_url = candidate_url[:MAX_TEXT]
        failing_jobs = tuple(
            sorted(
                name
                for name, conclusion in latest_job_conclusions.items()
                if conclusion in RED_CONCLUSIONS
            )
        )
        if terminal_red_without_job and not failing_jobs:
            raise CiWatchError(f"{repo} HEAD red evidence has no failing job identity")
        successful_jobs = tuple(
            sorted(
                name
                for name, conclusion in latest_job_conclusions.items()
                if conclusion == "success"
            )
        )
        return HeadCiEvidence(
            sha=sha,
            has_in_flight=has_in_flight,
            all_completed_green=all_completed_green,
            failing_jobs=failing_jobs,
            successful_jobs=successful_jobs,
            run_url=red_run_url,
        )

    def has_in_flight_runs(self, repo: str, branch: str) -> bool:
        data = self._workflow_runs(repo, branch)
        return any(str(run.get("status", "")).lower() in IN_FLIGHT_STATUSES for run in data)

    def generator_busy(self, repo: str, branch: str, generator: str) -> bool:
        needles = (
            ("publish", "release-please")
            if generator == "release-please"
            else ("release", "release-plz")
        )
        for run in self._workflow_runs(repo, branch):
            if str(run.get("status", "")).lower() not in IN_FLIGHT_STATUSES:
                continue
            labels = [run.get(key, "") for key in ("name", "path")]
            if not all(isinstance(label, str) and len(label) <= MAX_TEXT for label in labels):
                raise CiWatchError(f"{repo} workflow run has an invalid name or path")
            label = " ".join(item.lower() for item in labels)
            if any(needle in label for needle in needles):
                return True
        return False

    def _bounded_collection(
        self,
        repo: str,
        data: object,
        *,
        collection_key: str,
        limit: int,
        source: str,
    ) -> list[JsonObject]:
        if not isinstance(data, dict) or not isinstance(data.get(collection_key), list):
            raise CiWatchError(f"{repo} {source} have an invalid shape")
        total_count = data.get("total_count")
        if (
            not isinstance(total_count, int)
            or isinstance(total_count, bool)
            or total_count < 0
            or total_count > limit
        ):
            raise CiWatchError(f"{repo} {source} exceed the bounded query limit")
        values = data[collection_key]
        if len(values) != total_count or not all(isinstance(value, dict) for value in values):
            raise CiWatchError(f"{repo} {source} contain an invalid collection")
        return cast(list[JsonObject], values)

    def _run_jobs(self, repo: str, run_id: int) -> list[JsonObject]:
        data = self._json(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repo}/actions/runs/{run_id}/jobs",
                "-f",
                f"per_page={MAX_HEAD_JOBS}",
            ],
            source=f"{repo} run {run_id} jobs",
        )
        return self._bounded_collection(
            repo,
            data,
            collection_key="jobs",
            limit=MAX_HEAD_JOBS,
            source=f"run {run_id} jobs",
        )

    def _workflow_runs(self, repo: str, branch: str) -> list[JsonObject]:
        repo = _repo(repo)
        branch = _branch(branch)
        data = self._json(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repo}/actions/runs",
                "-f",
                f"branch={branch}",
                "-f",
                "per_page=100",
            ],
            source=f"{repo} workflow runs",
        )
        if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
            raise CiWatchError(f"{repo} workflow runs have an invalid shape")
        runs = data["workflow_runs"]
        if not all(isinstance(run, dict) for run in runs):
            raise CiWatchError(f"{repo} workflow runs contain a non-object")
        if any(
            not isinstance(run.get("status"), str)
            or str(run["status"]).lower() not in KNOWN_RUN_STATUSES
            for run in runs
        ):
            raise CiWatchError(f"{repo} workflow runs contain an invalid status")
        return cast(list[JsonObject], runs)

    def release_pr_numbers(self, repo: str) -> list[int]:
        repo = _repo(repo)
        data = self._json(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,headRefName",
            ],
            source=f"{repo} release PR list",
        )
        if not isinstance(data, list):
            raise CiWatchError(f"{repo} release PR list is not an array")
        numbers: list[int] = []
        for item in data:
            if not isinstance(item, dict):
                raise CiWatchError(f"{repo} release PR list contains a non-object")
            head = item.get("headRefName")
            number = item.get("number")
            if isinstance(head, str) and head.startswith(RELEASE_BRANCH_PREFIXES):
                if not isinstance(number, int) or number <= 0:
                    raise CiWatchError(f"{repo} release PR has an invalid number")
                numbers.append(number)
        return numbers

    def release_pr(self, repo: str, number: int) -> ReleasePr:
        data = self._json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                _repo(repo),
                "--json",
                (
                    "number,isDraft,baseRefName,headRefName,headRefOid,"
                    "mergeable,mergeStateStatus,statusCheckRollup,url,title,createdAt"
                ),
            ],
            source=f"{repo} PR #{number}",
        )
        if not isinstance(data, dict):
            raise CiWatchError(f"{repo} PR #{number} is not an object")
        return ReleasePr.from_json(data)

    def merge(self, plan: MergePlan) -> CommandResult:
        return self._runner(
            [
                self.executable,
                "pr",
                "merge",
                str(plan.pr.number),
                "--repo",
                _repo(plan.repo),
                "--squash",
                "--match-head-commit",
                _sha(plan.pr.head_oid),
            ]
        )


@dataclass(frozen=True)
class AgentProbe:
    names: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.names)


class AgentsGate:
    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner

    def probe(self) -> AgentProbe:
        result = self._runner([self.executable, "agent", "list", "-j"])
        if result.returncode != 0:
            raise CiWatchError(
                f"agent probe failed: exit_code={result.returncode} "
                f"detail={_bounded(result.stderr or result.stdout) or '-'}"
            )
        data = _json_array(result.stdout, source="agent probe")
        names: list[str] = []
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise CiWatchError(f"agent probe row {index} is not an object")
            name = row.get("name") or row.get("agent_name")
            if not isinstance(name, str) or not name.strip():
                raise CiWatchError(f"agent probe row {index} has no name")
            names.append(_bounded(name, limit=100))
        return AgentProbe(tuple(names))

    def notify(
        self,
        notes: Sequence[str],
        *,
        icon: str | None = None,
        action: str | None = None,
        action_data: Mapping[str, str] | None = None,
        tags: Sequence[str] = (),
    ) -> bool:
        payload_data: JsonObject = {
            "notes": [_bounded(note) for note in notes],
            "tags": [_bounded(tag, limit=64) for tag in tags],
        }
        if icon is not None:
            payload_data["icon"] = _bounded(icon, limit=16)
        if action is not None:
            payload_data["action"] = _bounded(action, limit=64)
        if action_data is not None:
            payload_data["action_data"] = {
                _bounded(key, limit=64): _bounded(value, limit=4096)
                for key, value in action_data.items()
            }
        payload = json.dumps(payload_data)
        result = self._runner(
            [self.executable, "notify", "create", "-s", CHOP_NAME],
            input_text=payload,
        )
        return result.returncode == 0


GATE_STATUSES = frozenset({"pending", "answered", "cancelled", "timeout"})


class LaunchGateClient:
    """Create durable LaunchApproval gates and poll their status."""

    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner

    def create(self, payload: Mapping[str, Any]) -> str | None:
        """Create a LaunchApproval request; return its request id if parseable.

        Raises CiWatchError only when the command itself fails (non-zero exit
        or exec failure). A successful exit with an unparsable descriptor
        returns None so the caller can still record the dedupe key: the gate
        may well have been created, and not duplicating it matters more than
        being able to poll it.
        """
        fd, temp_name = tempfile.mkstemp(prefix=".ci-watch-launch-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(dict(payload), stream)
            result = self._runner(
                [
                    self.executable,
                    "launch",
                    "request",
                    "-f",
                    temp_name,
                    "-o",
                    "json",
                    "-s",
                    CHOP_NAME,
                ],
                cwd=str(Path.home()),
            )
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temp_name)
        if result.returncode != 0:
            detail = _bounded(result.stderr or result.stdout) or "-"
            raise CiWatchError(
                f"launch request failed: exit_code={result.returncode} detail={detail}"
            )
        try:
            data = _json_object(result.stdout, source="launch request")
        except CiWatchError:
            return None
        request_id = data.get("request_id")
        return request_id if isinstance(request_id, str) and request_id.strip() else None

    def status(self, request_id: str) -> str | None:
        """Return a gate's status, or None if it cannot be read or parsed."""
        result = self._runner(
            [self.executable, "gate", "show", "-k", "launch", "-i", request_id, "-j"]
        )
        if result.returncode != 0:
            return None
        try:
            data = _json_object(result.stdout, source="gate show")
        except CiWatchError:
            return None
        value = data.get("status")
        return value if isinstance(value, str) and value in GATE_STATUSES else None


@dataclass(frozen=True)
class Config:
    actstat_bin: str
    gh_bin: str
    sase_bin: str
    repos: tuple[str, ...]
    release_repositories: dict[str, str]
    merge_order: tuple[str, ...]
    max_merges: int
    max_fixes: int
    red_debounce_ticks: int
    fix_enabled: bool
    merge_enabled: bool

    @classmethod
    def from_invocation(cls, invocation: ChopInvocation) -> Config:
        values = context_vars(invocation)
        repos = _string_list(values.get("repos"), "repos")
        if not repos or len(repos) > MAX_REPOS:
            raise CiWatchError(f"repos must contain between 1 and {MAX_REPOS} entries")
        normalized_repos = tuple(_repo(repo) for repo in repos)
        if len(set(normalized_repos)) != len(normalized_repos):
            raise CiWatchError("repos contains duplicates")
        releases_value = values.get("release_repositories", {})
        if not isinstance(releases_value, dict):
            raise CiWatchError("release_repositories must be an object")
        releases: dict[str, str] = {}
        for raw_repo, raw_generator in releases_value.items():
            repo = _repo(raw_repo)
            if repo not in normalized_repos:
                raise CiWatchError(f"release repository {repo!r} is not in repos")
            if raw_generator not in {"release-please", "release-plz"}:
                raise CiWatchError(f"unsupported release generator for {repo}")
            releases[repo] = raw_generator
        raw_order = _string_list(values.get("merge_order", list(normalized_repos)), "merge_order")
        merge_order = tuple(_resolve_order_repo(item, normalized_repos) for item in raw_order)
        if len(set(merge_order)) != len(merge_order):
            raise CiWatchError("merge_order contains duplicates")
        return cls(
            actstat_bin=_binary(values, "actstat_bin", "actstat"),
            gh_bin=_binary(values, "gh_bin", "gh"),
            sase_bin=_binary(values, "sase_bin", "sase"),
            repos=normalized_repos,
            release_repositories=releases,
            merge_order=merge_order,
            max_merges=_positive_int(values, "max_merges_per_tick", 1),
            max_fixes=_positive_int(values, "max_fix_proposals_per_tick", 1),
            red_debounce_ticks=_positive_int(values, "red_debounce_ticks", 2),
            fix_enabled=_bool(values, "fix_enabled", True),
            merge_enabled=_bool(values, "merge_enabled", False),
        )


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise CiWatchError(f"{name} must be a list of non-blank strings")
    return tuple(item.strip() for item in value)


def _resolve_order_repo(value: str, repos: Sequence[str]) -> str:
    if "/" in value:
        repo = _repo(value)
        if repo not in repos:
            raise CiWatchError(f"merge_order contains unknown repository {repo!r}")
        return repo
    matches = [repo for repo in repos if repo.rsplit("/", 1)[-1] == value]
    if len(matches) != 1:
        raise CiWatchError(f"merge_order name {value!r} is unknown or ambiguous")
    return matches[0]


def _binary(values: Mapping[str, Any], key: str, default: str) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise CiWatchError(f"{key} must be a non-blank executable")
    return value.strip()


def _positive_int(values: Mapping[str, Any], key: str, default: int) -> int:
    value = values.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise CiWatchError(f"{key} must be an integer between 1 and 100")
    return value


def _bool(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise CiWatchError(f"{key} must be a boolean")
    return value


def _commit_sha(observation: RepoObservation) -> str:
    if observation.commit is None:
        raise CiWatchError(f"{observation.repo} has no settled commit")
    return _sha(observation.commit.get("sha"))


def _fix_agent_name(repo: str) -> str:
    slug = safe_fragment(repo.rsplit("/", 1)[-1])
    return f"ci_fix.{slug}.@"  # SASE replaces `@` with a concrete launch token.


def _fix_prompt(repo: str, failure: FailureEvidence) -> str:
    slug = safe_fragment(repo.rsplit("/", 1)[-1])
    pr_name = f"ci_fix_{slug.replace('-', '_')}_{failure.sha[:7]}"
    evidence = "\n".join(f"- {name}" for name in failure.failing_jobs)
    unsettled_note = ""
    if failure.head_unsettled:
        unsettled_note = f"""
The pinned failure is on a settled commit older than the current unsettled HEAD
({failure.current_head_sha}). Re-verify these job failures against current state
before changing code.
"""
    return f"""\
#pr({pr_name}, status=ready)

#actstat(repo={repo})

Repair the current default-branch CI failure in {repo}.

Pinned failing run: {failure.run_url or "unavailable"}
Pinned failing commit: {failure.sha}
Failed jobs from the sweep:
{evidence}
{unsettled_note}

First re-verify that this failure and commit are still current on the default branch.
If it was superseded or already fixed, leave the worktree unchanged and report that
outcome. Keep any fix narrowly scoped and run the relevant checks.
""".strip()


def _gate_prompt(repo: str, failure: FailureEvidence) -> str:
    """Build the self-sufficient prompt a LaunchApproval gate previews.

    A LaunchApproval prompt gets none of the workspace/agent-name/wait_runners
    scaffolding Axe used to inject around a proposal, so the prompt itself must
    carry all of it.
    """
    header = f"#gh:{repo} %i:{_fix_agent_name(repo)} %w(runners=0)"
    return f"{header}\n\n{_fix_prompt(repo, failure)}"


def _gate_reason(repo: str, failure: FailureEvidence) -> str:
    job_names = ", ".join(failure.failing_jobs[:3])
    remaining_jobs = len(failure.failing_jobs) - 3
    if remaining_jobs > 0:
        job_names = f"{job_names} (+{remaining_jobs} more)"
    return _bounded(f"CI red in {repo} at {failure.sha[:7]}: {job_names}")


def _dry_run_mode() -> str:
    value = os.getenv("SASE_CHOP_DRY_RUN")
    if value is None:
        return "unavailable"
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return "live"
    return "dry_run"


def _new_counters(repo_count: int) -> dict[str, int]:
    return {
        "repos": repo_count,
        "green": 0,
        "no_ci": 0,
        "red": 0,
        "pending": 0,
        "errors": 0,
        "agents_running": 0,
        "fix_gated": 0,
        "fix_suppressed": 0,
        "red_debounce_suppressed": 0,
        "gate_pending_suppressed": 0,
        "gate_errors": 0,
        "release_candidates": 0,
        "merged": 0,
        "merge_skipped": 0,
    }


def _atomic_write_json(destination: Path, value: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ci-watch-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def _extract_release_version(title: str) -> str:
    match = RELEASE_VERSION_PATTERN.search(_bounded(title))
    return match.group(1) if match is not None else "-"


def _empty_release_ledger() -> JsonObject:
    return {"version": 1, "merges": [], "announced_pending": {}}


def _load_release_ledger(invocation: ChopInvocation, now: datetime) -> JsonObject:
    path = Path(invocation.context.state_dir) / RELEASE_LEDGER_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_release_ledger()
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return _empty_release_ledger()

    raw_merges = raw.get("merges")
    if not isinstance(raw_merges, list):
        return _empty_release_ledger()
    cutoff = now - timedelta(days=RELEASE_LEDGER_RETENTION_DAYS)
    merges: list[JsonObject] = []
    for row in raw_merges:
        if not isinstance(row, dict):
            continue
        try:
            repo = _repo(row.get("repo"))
            number = row.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise CiWatchError("invalid release ledger PR number")
            url = _required_string(row, "url")
            if not url.startswith("https://github.com/"):
                raise CiWatchError("invalid release ledger URL")
            head_oid = _sha(row.get("head_oid"))
            version = _bounded(_required_string(row, "version"), limit=80)
            generator = _bounded(_required_string(row, "generator"), limit=80)
            merged_at = _required_string(row, "merged_at")
            parsed_merged_at = _parse_timestamp(merged_at).astimezone(now.tzinfo)
        except (CiWatchError, ValueError):
            continue
        if parsed_merged_at < cutoff:
            continue
        merges.append(
            {
                "repo": repo,
                "number": number,
                "url": url,
                "head_oid": head_oid,
                "version": version,
                "generator": generator,
                "merged_at": merged_at,
            }
        )

    announced: JsonObject = {}
    raw_announced = raw.get("announced_pending")
    if isinstance(raw_announced, dict):
        for key, row in raw_announced.items():
            if not isinstance(key, str) or not isinstance(row, dict):
                continue
            consecutive_ticks = row.get("consecutive_ticks")
            notified = row.get("notified")
            if (
                not isinstance(consecutive_ticks, int)
                or isinstance(consecutive_ticks, bool)
                or consecutive_ticks < 0
                or not isinstance(notified, bool)
            ):
                continue
            announced[_bounded(key, limit=MAX_TEXT)] = {
                "consecutive_ticks": consecutive_ticks,
                "notified": notified,
            }
    return {
        "version": 1,
        "merges": merges[-MAX_LEDGER_MERGES:],
        "announced_pending": announced,
    }


def _write_release_ledger(invocation: ChopInvocation, ledger: Mapping[str, Any]) -> None:
    destination = Path(invocation.context.state_dir) / RELEASE_LEDGER_FILE_NAME
    _atomic_write_json(destination, ledger)


def _empty_fix_ledger() -> JsonObject:
    return {"version": 2, "repos": {}, "gates": {}}


def _fix_key_repo(value: str) -> str:
    parts = value.split(":")
    if (
        len(parts) != 4
        or parts[0] != "ci_fix"
        or re.fullmatch(r"[0-9a-f]{16}", parts[2]) is None
        or re.fullmatch(r"e(?:0|[1-9][0-9]*)", parts[3]) is None
    ):
        raise CiWatchError("invalid CI fix dedupe key")
    return _repo(parts[1])


def _prune_gated_fixes(
    gates: Mapping[str, Any],
    now: datetime,
    repos: Sequence[str],
) -> dict[str, JsonObject]:
    allowed_repos = set(repos)
    cutoff = now - timedelta(days=FIX_LEDGER_RETENTION_DAYS)
    rows: list[tuple[datetime, str, JsonObject]] = []
    for key, row in gates.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        request_id = row.get("request_id")
        created_at = row.get("created_at")
        if (request_id is not None and not isinstance(request_id, str)) or not isinstance(
            created_at, str
        ):
            continue
        try:
            repo = _fix_key_repo(key)
            parsed = _parse_timestamp(created_at).astimezone(now.tzinfo)
        except (CiWatchError, ValueError):
            continue
        if repo not in allowed_repos or parsed < cutoff:
            continue
        rows.append((parsed, key, {"request_id": request_id, "created_at": created_at}))
    rows.sort(key=lambda row: (row[0], row[1]))
    return {key: row for _, key, row in rows[-MAX_GATED_FIXES:]}


def _load_fix_ledger(
    invocation: ChopInvocation,
    now: datetime,
    configured_repos: Sequence[str],
) -> JsonObject:
    path = Path(invocation.context.state_dir) / FIX_LEDGER_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_fix_ledger()
    if not isinstance(raw, dict) or raw.get("version") != 2:
        return _empty_fix_ledger()
    raw_repos = raw.get("repos")
    raw_gates = raw.get("gates")
    if not isinstance(raw_repos, dict) or not isinstance(raw_gates, dict):
        return _empty_fix_ledger()

    allowed_repos = set(configured_repos)
    repos: dict[str, JsonObject] = {}
    for repo, row in raw_repos.items():
        if not isinstance(repo, str) or not isinstance(row, dict):
            continue
        episode = row.get("episode")
        red = row.get("red")
        try:
            normalized_repo = _repo(repo)
        except CiWatchError:
            continue
        if (
            normalized_repo not in allowed_repos
            or not isinstance(episode, int)
            or isinstance(episode, bool)
            or episode < 0
            or not isinstance(red, bool)
        ):
            continue
        repos[normalized_repo] = {"episode": episode, "red": red}

    return {
        "version": 2,
        "repos": repos,
        "gates": _prune_gated_fixes(raw_gates, now, configured_repos),
    }


def _write_fix_ledger(invocation: ChopInvocation, ledger: Mapping[str, Any]) -> None:
    destination = Path(invocation.context.state_dir) / FIX_LEDGER_FILE_NAME
    _atomic_write_json(destination, ledger)


def _load_streaks(invocation: ChopInvocation) -> dict[str, tuple[tuple[str, ...], int]]:
    path = Path(invocation.context.state_dir) / STREAK_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return {}
    repos = raw.get("repositories")
    if not isinstance(repos, dict):
        return {}
    streaks: dict[str, tuple[tuple[str, ...], int]] = {}
    for repo, row in repos.items():
        if not isinstance(repo, str) or not isinstance(row, dict):
            continue
        fingerprint = row.get("fingerprint")
        streak = row.get("streak")
        if (
            not isinstance(fingerprint, list)
            or not fingerprint
            or not all(isinstance(name, str) and name for name in fingerprint)
            or not isinstance(streak, int)
            or isinstance(streak, bool)
            or streak < 1
        ):
            continue
        streaks[repo] = (tuple(sorted(set(fingerprint))), streak)
    return streaks


def _write_streaks(
    invocation: ChopInvocation,
    streaks: Mapping[str, tuple[tuple[str, ...], int]],
) -> None:
    destination = Path(invocation.context.state_dir) / STREAK_FILE_NAME
    payload: JsonObject = {
        "version": 1,
        "repositories": {
            repo: {"fingerprint": list(fingerprint), "streak": streak}
            for repo, (fingerprint, streak) in sorted(streaks.items())
        },
    }
    _atomic_write_json(destination, payload)


def _write_ledger(invocation: ChopInvocation, ledger: Mapping[str, Any]) -> str:
    result_path = Path(invocation.context.result_file).resolve()
    evidence_name = f"{result_path.stem}.decisions.json"
    destination = result_path.parent / evidence_name
    _atomic_write_json(destination, ledger)
    return evidence_name


def _mark(
    ledger_repos: dict[str, JsonObject],
    repo: str,
    *,
    state: RepoState | None = None,
    reason: str | None = None,
    **fields: object,
) -> None:
    row = ledger_repos.setdefault(repo, {})
    if state is not None:
        row["state"] = state.value
    if reason is not None:
        row["reason"] = _bounded(reason)
    for key, value in fields.items():
        if value is not None:
            row[key] = value


def _repo_state_presentation(state: RepoState) -> tuple[Tone, str]:
    return {
        RepoState.GREEN: ("ok", "✓"),
        RepoState.RED: ("error", "▲"),
        RepoState.PENDING: ("warn", "◆"),
        RepoState.ERROR: ("error", "!"),
        RepoState.NO_CI: ("muted", "·"),
    }[state]


def _repo_evidence(
    repo: str,
    state: RepoState,
    heads: Mapping[str, BranchHead],
    failures: Mapping[str, FailureEvidence],
    ledger_repos: Mapping[str, JsonObject],
    *,
    red_debounce_ticks: int,
) -> str:
    row = ledger_repos.get(repo, {})
    reason = str(row.get("reason", ""))
    if reason in {
        "red_debounce",
        "fix_in_flight",
        "fix_cap_reached",
        "fix_disabled",
        "agents_check_failed",
        "gate_pending",
        "already_gated",
        "gate_failed",
        "dry_run",
    }:
        return reason
    if state is RepoState.GREEN and repo in heads:
        return heads[repo].sha[:12]
    if state is RepoState.RED and repo in failures:
        jobs = " · ".join(failures[repo].failing_jobs)
        streak = row.get("streak")
        streak_text = (
            f"streak {streak}/{red_debounce_ticks}"
            if isinstance(streak, int)
            else f"streak ?/{red_debounce_ticks}"
        )
        return _bounded(f"{jobs} · {streak_text}", limit=512)
    return _bounded(reason or state.value, limit=512)


def _release_tone(reason: str) -> Tone:
    if reason == "merged":
        return "ok"
    if reason in {"no_release_pr", "dry_run"}:
        return "muted"
    return "warn"


_RELEASE_REASON_LABELS = {
    "eligible": "ready to merge",
    "default_branch_not_green": "base branch not green",
    "ambiguous_release_prs": "multiple release PRs",
    "not_release_pr": "not a release PR",
    "release_pr_draft": "draft",
    "release_pr_wrong_base": "wrong base branch",
    "release_pr_not_mergeable": "not mergeable",
    "release_pr_not_clean": "merge state not clean",
    "release_pr_empty_rollup": "checks unavailable",
    "release_pr_checks_not_green": "checks not green",
    "release_generator_busy": "release generator running",
    "release_pr_head_changed": "PR changed during merge",
    "merge_cap_reached": "merge cap reached",
    "merge_disabled": "merge disabled",
    "merge_context_unavailable": "merge context unavailable",
    "dry_run": "dry run",
    "merge_failed": "merge failed",
}
_NON_BLOCKING_RELEASE_REASONS = frozenset({"eligible", "release_generator_busy"})


def _humanize_release_reason(reason: str) -> str:
    return _RELEASE_REASON_LABELS.get(reason, _bounded(reason.replace("_", " ")))


def _release_reason_tone(reason: str) -> Tone:
    if reason == "no_release_pr":
        return "muted"
    if reason in _NON_BLOCKING_RELEASE_REASONS:
        return "warn"
    return "error"


def _format_release_age(created_at: str, now: datetime) -> str:
    try:
        delta = max(now - _parse_timestamp(created_at).astimezone(now.tzinfo), timedelta())
    except ValueError:
        return "-"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _release_order(config: Config) -> list[str]:
    ordered = [repo for repo in config.merge_order if repo in config.release_repositories]
    ordered.extend(repo for repo in config.release_repositories if repo not in ordered)
    return ordered


def _build_release_report(
    config: Config,
    release_ledger: Mapping[str, Any],
    observations: Mapping[str, ReleaseObservation],
    decisions: Mapping[str, str],
    now: datetime,
    merged_keys: set[str],
) -> ChopReport:
    raw_merges = release_ledger.get("merges")
    merges = (
        [row for row in raw_merges if isinstance(row, dict)] if isinstance(raw_merges, list) else []
    )
    recent_cutoff = now - timedelta(days=RELEASE_REPORT_WINDOW_DAYS)
    recent = [
        row
        for row in merges
        if isinstance(row.get("merged_at"), str)
        and _parse_timestamp(str(row["merged_at"])).astimezone(now.tzinfo) >= recent_cutoff
    ]
    recent.sort(
        key=lambda row: _parse_timestamp(str(row["merged_at"])),
        reverse=True,
    )
    merged_today = sum(
        _parse_timestamp(str(row["merged_at"])).astimezone(now.tzinfo).date() == now.date()
        for row in merges
    )

    pending_repos = [
        repo
        for repo in _release_order(config)
        if observations[repo].numbers
        and not all(
            f"{repo}#{number}" in merged_keys for number in observations[repo].numbers or ()
        )
    ]
    blocked = sum(
        decisions.get(repo, "") not in _NON_BLOCKING_RELEASE_REASONS for repo in pending_repos
    )
    headline_tone: Tone = "error" if blocked else "warn" if pending_repos else "ok"
    report = ChopReport(title="RELEASES").headline(
        f"{merged_today} merged today · {len(pending_repos)} pending · {blocked} blocked",
        tone=headline_tone,
    )

    report.heading("RECENT")
    recent_rows = report.rows(columns=("REPOSITORY", "PR", "VERSION", "MERGED"))
    for row in recent[:8]:
        merged_at = _parse_timestamp(str(row["merged_at"])).astimezone(now.tzinfo)
        recent_rows.row(
            (
                str(row["repo"]),
                f"#{row['number']}",
                str(row["version"]),
                merged_at.strftime("%m-%d %H:%M"),
            ),
            tone="ok",
            glyph="✓",
        )
    if not recent:
        recent_rows.row(("-", "-", "-", "no recent releases"), tone="muted", glyph="·")

    report.heading("PENDING")
    pending_rows = report.rows(columns=("REPOSITORY", "PR", "STATE", "AGE"))
    for repo in _release_order(config):
        observation = observations[repo]
        unmerged_numbers = [
            number for number in observation.numbers or () if f"{repo}#{number}" not in merged_keys
        ]
        if observation.numbers is None:
            pending_rows.row(
                (
                    repo,
                    "-",
                    _humanize_release_reason(observation.error or "observation failed"),
                    "-",
                ),
                tone="error",
                glyph="!",
            )
            continue
        if not unmerged_numbers:
            pending_rows.row((repo, "-", "no release PR", "-"), tone="muted", glyph="·")
            continue
        number = unmerged_numbers[0]
        pr = next((candidate for candidate in observation.prs if candidate.number == number), None)
        reason = decisions.get(repo, observation.error or "observation failed")
        tone = _release_reason_tone(reason)
        pending_rows.row(
            (
                repo,
                f"#{number}",
                _humanize_release_reason(reason),
                _format_release_age(pr.created_at, now) if pr is not None else "-",
            ),
            tone=tone,
            glyph="◆" if tone == "warn" else "▲",
        )

    report.divider().kv(
        {
            "updated": now.strftime("%m-%d %H:%M"),
            "window": f"{RELEASE_REPORT_WINDOW_DAYS}d",
            "merges recorded": str(len(merges)),
        },
        tone="muted",
    )
    return report


def _publish_release_report(invocation: ChopInvocation, report: ChopReport) -> bool:
    destination = Path(invocation.context.state_dir).resolve() / RELEASE_REPORT_FILE_NAME
    try:
        document = validate_chop_report(report.to_dict())
        _atomic_write_json(destination, document)
    except (OSError, TypeError, ValueError) as error:
        invocation.logger.warning(f"{CHOP_NAME}: release report publish skipped: {_bounded(error)}")
        return False
    return True


def _build_ci_watch_report(
    config: Config,
    counters: Mapping[str, int],
    states: Mapping[str, RepoState],
    heads: Mapping[str, BranchHead],
    failures: Mapping[str, FailureEvidence],
    ledger_repos: Mapping[str, JsonObject],
    release_plans: Sequence[MergePlan],
    mode: str,
) -> ChopReport:
    report = start_report("CI WATCH")
    headline = (
        f"{counters['green']} green · {counters['red']} red · "
        f"{counters['pending']} pending · {counters['errors']} error"
    )
    headline_tone: Tone = (
        "error"
        if counters["red"]
        else "warn"
        if counters["pending"] or counters["errors"]
        else "ok"
    )
    report.headline(headline, tone=headline_tone).heading("REPOSITORIES")
    repositories = report.rows(columns=("REPOSITORY", "STATE", "EVIDENCE"))
    for repo in config.repos:
        state = states[repo]
        tone, glyph = _repo_state_presentation(state)
        repositories.row(
            (
                repo,
                state.value,
                _repo_evidence(
                    repo,
                    state,
                    heads,
                    failures,
                    ledger_repos,
                    red_debounce_ticks=config.red_debounce_ticks,
                ),
            ),
            tone=tone,
            glyph=glyph,
        )

    if config.release_repositories:
        plans = {plan.repo: plan for plan in release_plans}
        report.heading("RELEASE")
        releases = report.rows(columns=("REPOSITORY", "PR", "DECISION"))
        release_order = [repo for repo in config.merge_order if repo in config.release_repositories]
        release_order.extend(
            repo for repo in config.release_repositories if repo not in release_order
        )
        for repo in release_order:
            ledger_row = ledger_repos.get(repo, {})
            reason = str(
                ledger_row.get("release_reason", ledger_row.get("reason", states[repo].value))
            )
            pr_number = ledger_row.get("merged_pr") or ledger_row.get("planned_pr")
            if pr_number is None and repo in plans:
                pr_number = plans[repo].pr.number
            releases.row(
                (repo, f"#{pr_number}" if pr_number is not None else "-", reason),
                tone=_release_tone(reason),
            )

    add_facts_footer(
        report,
        {"mode": mode.replace("_", " ")},
        tone="neutral" if mode == "live" else "warn",
    )
    report.kv(
        {
            "agents running": str(counters["agents_running"]),
            "fix cap": str(config.max_fixes),
            "merge cap": str(config.max_merges),
            "debounce ticks": str(config.red_debounce_ticks),
        },
        tone="muted",
    )
    return report


def _any_gate_pending(launch_gate: LaunchGateClient, gates: Mapping[str, JsonObject]) -> bool:
    """Return whether any recorded gate is still pending, bounding the polling work."""
    checked = 0
    for row in gates.values():
        if checked >= MAX_GATE_POLLS_PER_TICK:
            break
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            continue
        checked += 1
        if launch_gate.status(request_id) == "pending":
            return True
    return False


def build_ci_watch_result(
    invocation: ChopInvocation,
    *,
    actstat: ActstatClient | None = None,
    github: GitHubReader | None = None,
    agents: AgentsGate | None = None,
    launch_gate: LaunchGateClient | None = None,
    clock: Callable[[], datetime] = _local_now,
) -> ChopResultBuilder:
    config = Config.from_invocation(invocation)
    actstat = actstat or ActstatClient(config.actstat_bin)
    github = github or GitHubReader(config.gh_bin)
    agents = agents or AgentsGate(config.sase_bin)
    launch_gate = launch_gate or LaunchGateClient(config.sase_bin)
    now = clock().astimezone()
    fix_ledger = _load_fix_ledger(invocation, now, config.repos)
    fix_repos = cast(dict[str, JsonObject], fix_ledger["repos"])
    fix_gates = cast(dict[str, JsonObject], fix_ledger["gates"])
    release_ledger = _load_release_ledger(invocation, now)
    release_merges = cast(list[JsonObject], release_ledger["merges"])
    announced_pending = cast(dict[str, JsonObject], release_ledger["announced_pending"])
    observations = actstat.sweep(config.repos)
    counters = _new_counters(len(config.repos))
    states: dict[str, RepoState] = {}
    heads: dict[str, BranchHead] = {}
    failures: dict[str, FailureEvidence] = {}
    ledger_repos: dict[str, JsonObject] = {}
    head_evidence_repos = 0

    for repo in config.repos:
        observation = observations[repo]
        decision: RepoDecision
        if observation.error == "missing_observation":
            try:
                if github.workflow_count(repo) == 0:
                    decision = RepoDecision(RepoState.NO_CI, "no_ci")
                else:
                    decision = RepoDecision(RepoState.ERROR, "missing_observation")
            except CiWatchError as error:
                decision = RepoDecision(
                    RepoState.ERROR,
                    "missing_observation",
                )
                _mark(ledger_repos, repo, workflow_probe_error=str(error))
        elif observation.error or observation.commit is None:
            state = classify_repo(observation)
            decision = RepoDecision(state, _classification_reason(observation, state))
        else:
            try:
                head = github.default_branch_head(repo)
                head_evidence = None
                if classify_repo(observation) is RepoState.RED and not head.sha.startswith(
                    _commit_sha(observation)
                ):
                    if head_evidence_repos >= MAX_HEAD_EVIDENCE_REPOS_PER_TICK:
                        raise CiWatchError("HEAD evidence per-tick query limit reached")
                    head_evidence_repos += 1
                    head_evidence = github.head_ci_evidence(repo, head.sha)
                decision = decide_repo(
                    observation,
                    head,
                    head_evidence=head_evidence,
                )
            except CiWatchError as error:
                decision = RepoDecision(RepoState.ERROR, str(error))
        state = decision.state
        reason = decision.reason
        states[repo] = state
        if decision.head is not None:
            heads[repo] = decision.head
        if decision.failure is not None:
            failures[repo] = decision.failure
        counter_name = "errors" if state is RepoState.ERROR else state.value
        counters[counter_name] += 1
        _mark(
            ledger_repos,
            repo,
            state=state,
            reason=reason,
            classification_reason=reason,
            head_sha=heads[repo].sha if repo in heads else None,
            failing_sha=decision.failure.sha if decision.failure is not None else None,
            head_unsettled=(
                decision.failure.head_unsettled if decision.failure is not None else None
            ),
        )

    streaks = _load_streaks(invocation)
    mature_red_repos: list[str] = []
    for repo in config.repos:
        state = states[repo]
        fix_row = fix_repos.setdefault(repo, {"episode": 0, "red": False})
        episode = cast(int, fix_row["episode"])
        was_red = cast(bool, fix_row["red"])
        if state is RepoState.RED:
            was_red = True
        elif state is RepoState.GREEN and was_red:
            episode += 1
            was_red = False
        fix_repos[repo] = {"episode": episode, "red": was_red}
        _mark(ledger_repos, repo, fix_episode=episode)

        if state is not RepoState.RED:
            streaks.pop(repo, None)
            continue
        failure = failures[repo]
        previous = streaks.get(repo)
        streak = (
            previous[1] + 1 if previous is not None and previous[0] == failure.fingerprint else 1
        )
        streaks[repo] = (failure.fingerprint, streak)
        _mark(
            ledger_repos,
            repo,
            streak=streak,
            failing_jobs=list(failure.failing_jobs),
            failing_job_fingerprint=failure.fingerprint_key,
        )
        if streak < config.red_debounce_ticks:
            counters["fix_suppressed"] += 1
            counters["red_debounce_suppressed"] += 1
            _mark(ledger_repos, repo, reason="red_debounce")
            continue
        mature_red_repos.append(repo)

    mode = _dry_run_mode()
    if mature_red_repos:
        if not config.fix_enabled:
            for repo in mature_red_repos:
                counters["fix_suppressed"] += 1
                _mark(ledger_repos, repo, reason="fix_disabled")
        else:
            try:
                probe = agents.probe()
            except CiWatchError:
                probe = None
                for repo in mature_red_repos:
                    counters["fix_suppressed"] += 1
                    _mark(ledger_repos, repo, reason="agents_check_failed")
            if probe is not None:
                counters["agents_running"] = probe.count
                fix_agents = tuple(
                    name for name in probe.names if name == "ci_fix" or name.startswith("ci_fix.")
                )
                if fix_agents:
                    in_flight_names = [
                        _bounded(name, limit=100) for name in fix_agents[:MAX_LEDGER_NAMES]
                    ]
                    for repo in mature_red_repos:
                        counters["fix_suppressed"] += 1
                        _mark(
                            ledger_repos,
                            repo,
                            reason="fix_in_flight",
                            in_flight_agents=in_flight_names,
                        )
                elif _any_gate_pending(launch_gate, fix_gates):
                    for repo in mature_red_repos:
                        counters["fix_suppressed"] += 1
                        counters["gate_pending_suppressed"] += 1
                        _mark(ledger_repos, repo, reason="gate_pending")
                else:
                    gate_attempts = 0
                    for repo in mature_red_repos:
                        failure = failures[repo]
                        episode = cast(int, fix_repos[repo]["episode"])
                        dedupe_key = f"ci_fix:{repo}:{failure.fingerprint_key}:e{episode}"
                        if dedupe_key in fix_gates:
                            counters["fix_suppressed"] += 1
                            _mark(ledger_repos, repo, reason="already_gated")
                            continue
                        if gate_attempts >= config.max_fixes:
                            counters["fix_suppressed"] += 1
                            _mark(ledger_repos, repo, reason="fix_cap_reached")
                            continue
                        gate_attempts += 1
                        if mode == "dry_run":
                            counters["fix_suppressed"] += 1
                            _mark(ledger_repos, repo, reason="dry_run")
                            continue
                        payload = {
                            "schema_version": 1,
                            "prompt": _gate_prompt(repo, failure),
                            "reason": _gate_reason(repo, failure),
                            "approval": "required",
                            "max_slots": 1,
                        }
                        try:
                            request_id = launch_gate.create(payload)
                        except CiWatchError as error:
                            counters["gate_errors"] += 1
                            _mark(ledger_repos, repo, reason="gate_failed")
                            ledger_repos[repo]["gate_error"] = _bounded(error)
                            continue
                        fix_gates[dedupe_key] = {
                            "request_id": request_id,
                            "created_at": now.isoformat(),
                        }
                        fix_ledger["gates"] = fix_gates
                        try:
                            _write_fix_ledger(invocation, fix_ledger)
                        except OSError as error:
                            invocation.logger.warning(
                                f"{CHOP_NAME}: fix ledger write skipped: {_bounded(error)}"
                            )
                        counters["fix_gated"] += 1
                        _mark(ledger_repos, repo, reason="fix_gated")
                        ledger_repos[repo]["gate"] = {
                            "agent_name": _fix_agent_name(repo),
                            "dedupe_key": dedupe_key,
                            "request_id": request_id,
                        }
                        streaks.pop(repo, None)

    _write_streaks(invocation, streaks)
    fix_ledger["gates"] = _prune_gated_fixes(fix_gates, now, config.repos)
    try:
        _write_fix_ledger(invocation, fix_ledger)
    except OSError as error:
        invocation.logger.warning(f"{CHOP_NAME}: fix ledger write skipped: {_bounded(error)}")

    release_observations: dict[str, ReleaseObservation] = {}
    remaining_details = MAX_RELEASE_PR_DETAILS
    for repo in _release_order(config):
        try:
            numbers = tuple(github.release_pr_numbers(repo))
        except CiWatchError as error:
            release_observations[repo] = ReleaseObservation(
                repo,
                None,
                error=_bounded(error),
            )
            continue
        counters["release_candidates"] += len(numbers)
        candidates: list[ReleasePr] = []
        observation_error = None
        for number in numbers:
            if remaining_details <= 0:
                observation_error = (
                    f"release PR detail cap reached ({MAX_RELEASE_PR_DETAILS} per tick)"
                )
                break
            remaining_details -= 1
            try:
                candidates.append(github.release_pr(repo, number))
            except CiWatchError as error:
                observation_error = _bounded(error)
                break
        release_observations[repo] = ReleaseObservation(
            repo,
            numbers,
            tuple(candidates),
            observation_error,
        )

    release_plans: list[MergePlan] = []
    release_decisions: dict[str, str] = {}
    for repo in _release_order(config):
        release_observation = release_observations[repo]
        plan = None
        if release_observation.numbers is None:
            reason = release_observation.error or "release observation failed"
        elif not release_observation.numbers:
            reason = "no_release_pr"
        elif release_observation.error is not None:
            reason = release_observation.error
        elif states[repo] is not RepoState.GREEN:
            plan, reason = plan_release_merge(
                repo,
                states[repo],
                "",
                release_observation.prs,
                generator_busy=False,
            )
        else:
            head = heads[repo]
            try:
                plan, reason = plan_release_merge(
                    repo,
                    states[repo],
                    head.branch,
                    release_observation.prs,
                    generator_busy=github.generator_busy(
                        repo,
                        head.branch,
                        config.release_repositories[repo],
                    ),
                )
            except CiWatchError as error:
                reason = _bounded(error)
        release_decisions[repo] = reason
        ledger_repos[repo]["release_reason"] = reason
        if states[repo] is RepoState.GREEN:
            ledger_repos[repo]["reason"] = reason
        if plan is None:
            if release_observation.numbers and states[repo] is RepoState.GREEN:
                counters["merge_skipped"] += 1
            continue
        release_plans.append(plan)

    merged_keys: set[str] = set()
    merged_notifications: list[tuple[str, ReleasePr]] = []
    for plan in release_plans:
        repo = plan.repo
        if counters["merged"] >= config.max_merges:
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="merge_cap_reached",
                release_reason="merge_cap_reached",
            )
            continue
        if not config.merge_enabled:
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="merge_disabled",
                release_reason="merge_disabled",
                planned_pr=plan.pr.number,
            )
            continue
        if mode == "unavailable":
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="merge_context_unavailable",
                release_reason="merge_context_unavailable",
                planned_pr=plan.pr.number,
            )
            continue
        if mode == "dry_run":
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="dry_run",
                release_reason="dry_run",
                planned_pr=plan.pr.number,
            )
            continue
        try:
            current = github.release_pr(repo, plan.pr.number)
            current_plan, current_reason = plan_release_merge(
                repo,
                RepoState.GREEN,
                heads[repo].branch,
                [current],
                generator_busy=github.generator_busy(
                    repo, heads[repo].branch, config.release_repositories[repo]
                ),
            )
            if current_plan is None or current.head_oid != plan.pr.head_oid:
                counters["merge_skipped"] += 1
                _mark(
                    ledger_repos,
                    repo,
                    reason=(
                        "release_pr_head_changed"
                        if current.head_oid != plan.pr.head_oid
                        else current_reason
                    ),
                    release_reason=(
                        "release_pr_head_changed"
                        if current.head_oid != plan.pr.head_oid
                        else current_reason
                    ),
                )
                continue
            merge_result = github.merge(current_plan)
        except CiWatchError as error:
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason=str(error),
                release_reason=str(error),
            )
            continue
        if merge_result.returncode != 0:
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="merge_failed",
                release_reason="merge_failed",
            )
            continue
        counters["merged"] += 1
        merged_pr = current
        merged_key = f"{repo}#{merged_pr.number}"
        merged_keys.add(merged_key)
        merged_notifications.append((repo, merged_pr))
        release_merges.append(
            {
                "repo": repo,
                "number": merged_pr.number,
                "url": merged_pr.url,
                "head_oid": merged_pr.head_oid,
                "version": _extract_release_version(merged_pr.title),
                "generator": config.release_repositories[repo],
                "merged_at": now.isoformat(),
            }
        )
        release_merges[:] = release_merges[-MAX_LEDGER_MERGES:]
        _mark(
            ledger_repos,
            repo,
            reason="merged",
            release_reason="merged",
            merged_pr=merged_pr.number,
            head_oid=merged_pr.head_oid,
        )

    pending_notifications: list[tuple[str, int, str]] = []
    configured_release_repos = set(config.release_repositories)
    for key in list(announced_pending):
        repo, separator, raw_number = key.rpartition("#")
        stored_release_observation = release_observations.get(repo)
        if (
            not separator
            or repo not in configured_release_repos
            or not raw_number.isdigit()
            or key in merged_keys
            or (
                stored_release_observation is not None
                and stored_release_observation.numbers is not None
                and int(raw_number) not in stored_release_observation.numbers
            )
        ):
            announced_pending.pop(key, None)
    for repo, release_observation in release_observations.items():
        if release_observation.numbers is None:
            continue
        reason = release_decisions[repo]
        blocked = reason not in _NON_BLOCKING_RELEASE_REASONS
        for number in release_observation.numbers:
            key = f"{repo}#{number}"
            if key in merged_keys:
                announced_pending.pop(key, None)
                continue
            pending_row = announced_pending.get(key, {})
            previous_ticks = pending_row.get("consecutive_ticks", 0)
            notified = pending_row.get("notified") is True
            consecutive_ticks = (
                int(previous_ticks) + 1 if blocked and isinstance(previous_ticks, int) else 0
            )
            announced_pending[key] = {
                "consecutive_ticks": consecutive_ticks,
                "notified": notified,
            }
            if blocked and consecutive_ticks >= 2 and not notified:
                pending_notifications.append((repo, number, reason))

    try:
        _write_release_ledger(invocation, release_ledger)
    except OSError as error:
        invocation.logger.warning(f"{CHOP_NAME}: release ledger write skipped: {_bounded(error)}")

    try:
        release_report = _build_release_report(
            config,
            release_ledger,
            release_observations,
            release_decisions,
            now,
            merged_keys,
        )
    except (TypeError, ValueError) as error:
        invocation.logger.warning(f"{CHOP_NAME}: release report build skipped: {_bounded(error)}")
    else:
        _publish_release_report(invocation, release_report)

    merged_today = sum(
        _parse_timestamp(str(row["merged_at"])).astimezone(now.tzinfo).date() == now.date()
        for row in release_merges
    )
    pending_count = sum(
        bool(observation.numbers)
        and not all(f"{repo}#{number}" in merged_keys for number in observation.numbers or ())
        for repo, observation in release_observations.items()
    )
    report_path = str(Path(invocation.context.state_dir).resolve() / RELEASE_REPORT_FILE_NAME)
    release_action_data = {
        "report_path": report_path,
        "report_title": "Releases",
    }
    for repo, pr in merged_notifications:
        if not agents.notify(
            [
                f"Merged release PR #{pr.number} for {repo}",
                f"{merged_today} merged today · {pending_count} pending",
            ],
            icon="🚢",
            tags=["ci", "release"],
            action="ViewReport",
            action_data=release_action_data,
        ):
            ledger_repos[repo]["notification"] = "failed"
    pending_state_changed = False
    for repo, number, reason in pending_notifications:
        notification_ok = agents.notify(
            [
                (
                    f"Release PR #{number} for {repo} needs attention: "
                    f"{_humanize_release_reason(reason)}"
                )
            ],
            icon="🚢",
            tags=["ci", "release"],
            action="ViewReport",
            action_data=release_action_data,
        )
        if notification_ok:
            announced_pending[f"{repo}#{number}"]["notified"] = True
            pending_state_changed = True
        else:
            ledger_repos[repo]["release_notification"] = "failed"
    if pending_state_changed:
        try:
            _write_release_ledger(invocation, release_ledger)
        except OSError as error:
            invocation.logger.warning(
                f"{CHOP_NAME}: pending notification ledger write skipped: {_bounded(error)}"
            )

    actionable = bool(counters["fix_gated"] or counters["merged"])
    result = result_with_summary(
        invocation,
        CHOP_NAME,
        counters,
        status="ok" if actionable else "no_op",
        reason=None if actionable else "no_actions",
        report=_build_ci_watch_report(
            config,
            counters,
            states,
            heads,
            failures,
            ledger_repos,
            release_plans,
            mode,
        ),
    )
    ledger = {
        "mode": mode,
        "repositories": ledger_repos,
        "release_plans": [
            {
                "repo": plan.repo,
                "number": plan.pr.number,
                "head_oid": plan.pr.head_oid,
            }
            for plan in release_plans
        ],
    }
    result.add_evidence(_write_ledger(invocation, ledger))
    return result


def main() -> None:
    run_chop(
        CHOP_NAME,
        """\
Sweep SASE CI, gate non-overlapping repairs behind LaunchApproval, and guard
release merges.

Terminal failing-job evidence stays actionable while unrelated work is in flight.
A mature red failure files one durable LaunchApproval gate; approval is never
automatic. A new gate is suppressed while a live agent exists in the `ci_fix`
hood, while an earlier gate is still unanswered, or once the same failing-job
fingerprint has already been gated for the current red episode. Each failing-job
set gets one gate per red episode; an observed green resolution starts a new
episode. Repair agents use the `ci_fix.<slug>.@` template so SASE assigns a
concrete launch token, and the gate prompt carries `%w(runners=0)` since Axe no
longer applies lane-level `wait_runners` to a launch it did not itself propose.
""".strip(),
        build_ci_watch_result,
    )
