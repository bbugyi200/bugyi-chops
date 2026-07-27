"""Watch SASE CI health, propose repairs, and merge guarded release PRs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from sase.chops import ChopInvocation, ChopResultBuilder

from bugyi_chops._common import context_vars, result_with_summary, run_chop, safe_fragment

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
MAX_TEXT = 240
STREAK_FILE_NAME = "ci_watch_red_streaks.json"

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
    ) -> CommandResult: ...


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
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
        )


@dataclass(frozen=True)
class MergePlan:
    repo: str
    pr: ReleasePr


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
                    "mergeable,mergeStateStatus,statusCheckRollup,url"
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

    def notify(self, note: str, tag: str) -> bool:
        payload = json.dumps({"notes": [_bounded(note)], "tags": [tag]})
        result = self._runner(
            [self.executable, "notify", "create", "-s", CHOP_NAME],
            input_text=payload,
        )
        return result.returncode == 0


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
        "fix_proposed": 0,
        "fix_suppressed": 0,
        "red_debounce_suppressed": 0,
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


def build_ci_watch_result(
    invocation: ChopInvocation,
    *,
    actstat: ActstatClient | None = None,
    github: GitHubReader | None = None,
    agents: AgentsGate | None = None,
) -> ChopResultBuilder:
    config = Config.from_invocation(invocation)
    actstat = actstat or ActstatClient(config.actstat_bin)
    github = github or GitHubReader(config.gh_bin)
    agents = agents or AgentsGate(config.sase_bin)
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
        if states[repo] is not RepoState.RED:
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
                if probe.count:
                    busy_names = list(probe.names[:MAX_LEDGER_NAMES])
                    for repo in mature_red_repos:
                        counters["fix_suppressed"] += 1
                        _mark(
                            ledger_repos,
                            repo,
                            reason="agents_busy",
                            busy_agents=busy_names,
                        )
                else:
                    for index, repo in enumerate(mature_red_repos):
                        if index >= config.max_fixes:
                            counters["fix_suppressed"] += 1
                            _mark(ledger_repos, repo, reason="fix_cap_reached")
                            continue
                        failure = failures[repo]
                        slug = safe_fragment(repo.rsplit("/", 1)[-1])
                        dedupe_key = f"ci_fix:{repo}:{failure.fingerprint_key}"
                        result_prompt = _fix_prompt(repo, failure)
                        counters["fix_proposed"] += 1
                        _mark(ledger_repos, repo, reason="fix_proposed")
                        if not agents.notify(
                            f"Proposed a CI repair for {repo} at {failure.sha[:7]}", "ci"
                        ):
                            ledger_repos[repo]["notification"] = "failed"
                        ledger_repos[repo]["proposal"] = {
                            "agent_name": f"ci_fix.{slug}",
                            "dedupe_key": dedupe_key,
                        }
                        # Proposals are appended after the summary builder exists below.
                        ledger_repos[repo]["_prompt"] = result_prompt
                        ledger_repos[repo]["_dedupe_key"] = dedupe_key
                        streaks.pop(repo, None)

    _write_streaks(invocation, streaks)

    release_plans: list[MergePlan] = []
    for repo in config.merge_order:
        generator = config.release_repositories.get(repo)
        if generator is None or states[repo] is not RepoState.GREEN:
            continue
        head = heads[repo]
        try:
            numbers = github.release_pr_numbers(repo)
            counters["release_candidates"] += len(numbers)
            candidates = [github.release_pr(repo, number) for number in numbers]
            plan, reason = plan_release_merge(
                repo,
                states[repo],
                head.branch,
                candidates,
                generator_busy=github.generator_busy(repo, head.branch, generator),
            )
        except CiWatchError as error:
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason=str(error))
            continue
        if plan is None:
            if numbers:
                counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason=reason)
            continue
        release_plans.append(plan)

    mode = _dry_run_mode()
    for plan in release_plans:
        repo = plan.repo
        if counters["merged"] >= config.max_merges:
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason="merge_cap_reached")
            continue
        if not config.merge_enabled:
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason="merge_disabled", planned_pr=plan.pr.number)
            continue
        if mode == "unavailable":
            counters["merge_skipped"] += 1
            _mark(
                ledger_repos,
                repo,
                reason="merge_context_unavailable",
                planned_pr=plan.pr.number,
            )
            continue
        if mode == "dry_run":
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason="dry_run", planned_pr=plan.pr.number)
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
                )
                continue
            merge_result = github.merge(current_plan)
        except CiWatchError as error:
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason=str(error))
            continue
        if merge_result.returncode != 0:
            counters["merge_skipped"] += 1
            _mark(ledger_repos, repo, reason="merge_failed")
            continue
        counters["merged"] += 1
        _mark(
            ledger_repos,
            repo,
            reason="merged",
            merged_pr=plan.pr.number,
            head_oid=plan.pr.head_oid,
        )
        if not agents.notify(f"Merged release PR #{plan.pr.number} for {repo}", "release"):
            ledger_repos[repo]["notification"] = "failed"

    actionable = bool(counters["fix_proposed"] or counters["merged"])
    result = result_with_summary(
        invocation,
        CHOP_NAME,
        counters,
        status="ok" if actionable else "no_op",
        reason=None if actionable else "no_actions",
    )
    for repo in mature_red_repos:
        prompt = ledger_repos[repo].pop("_prompt", None)
        dedupe_key = ledger_repos[repo].pop("_dedupe_key", None)
        if isinstance(prompt, str) and isinstance(dedupe_key, str):
            slug = safe_fragment(repo.rsplit("/", 1)[-1])
            result.propose(
                prompt,
                f"gh:{repo}",
                proposal_id=f"fix_{slug}",
                agent_name=f"ci_fix.{slug}",
                dedupe_key=dedupe_key,
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
Sweep SASE CI, propose idle-gated repairs, and guard release merges.

Terminal failing-job evidence stays actionable while unrelated work is in flight.
A fixer landing either changes the failing job set or turns the repository green;
either outcome resets the debounce streak and releases its SHA-independent key.
""".strip(),
        build_ci_watch_result,
    )
