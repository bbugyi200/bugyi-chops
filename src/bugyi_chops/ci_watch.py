"""Watch SASE CI health, notify on failures, and merge guarded release-please PRs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
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

from bugyi_chops._common import context_vars, result_with_summary, run_chop
from bugyi_chops._report import add_facts_footer, start_report

CHOP_NAME = "ci_watch"
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
GREEN_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
RELEASE_BRANCH_PREFIXES = ("release-please--", "release-please/")
IN_FLIGHT_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
KNOWN_RUN_STATUSES = IN_FLIGHT_STATUSES | {"completed"}
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+\Z")
TOKEN_PATTERN = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_|bearer\s+|token\s*[=:]|secret\s*[=:])\S*"
)
MAX_REPOS = 50
MAX_HEAD_RUNS = 20
MAX_HEAD_JOBS = 100
MAX_HEAD_EVIDENCE_REPOS_PER_TICK = 10
MAX_RELEASE_PR_DETAILS = 8
MAX_STATE_RELEASES = 50
MAX_FAILURE_JOBS_PER_NOTIFICATION = 8
MAX_STEPS_PER_JOB = 5
MAX_TEXT = 240
STATE_FILE_NAME = "ci_watch_state.json"
LEGACY_RELEASE_LEDGER_FILE_NAME = "ci_watch_releases.json"
LEGACY_RELEASE_REPORT_FILE_NAME = "ci_watch_releases.report.json"
REPORT_FILE_NAME = "ci_watch.report.json"
STATE_RETENTION_DAYS = 90
REPORT_RECENT_DAYS = 30
RELEASE_VERSION_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)"
)
REMOVED_CONFIG_KEYS = frozenset({"max_fix_proposals_per_tick", "red_debounce_ticks", "fix_enabled"})
_MERGE_METHOD_FLAGS = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}
_MERGE_METHOD_METADATA_KEYS = {
    "merge": "allow_merge_commit",
    "squash": "allow_squash_merge",
    "rebase": "allow_rebase_merge",
}
DEFAULT_HEAVY_MAX_AGE_HOURS = 6.0
GITHUB_JSON_ENV = {
    "GH_FORCE_TTY": "0",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
}

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


def github_command_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy *base* (or the process environment) and force plain ``gh`` JSON output."""

    env = dict(os.environ if base is None else base)
    env.update(GITHUB_JSON_ENV)
    return env


@contextmanager
def _github_json_env() -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in GITHUB_JSON_ENV}
    os.environ.update(GITHUB_JSON_ENV)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env=None if env is None else dict(env),
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


def _safe_github_url(value: object) -> str | None:
    if isinstance(value, str) and value.startswith("https://github.com/"):
        return _bounded(value, limit=512)
    return None


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
        if str(item.get("conclusion", "")).lower() in RED_CONCLUSIONS:
            return True
        steps = item.get("steps")
        if isinstance(steps, list) and any(
            isinstance(step, dict) and str(step.get("conclusion", "")).lower() in RED_CONCLUSIONS
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
class FailingJobEvidence:
    workflow: str
    job: str
    conclusion: str
    url: str | None = None
    steps: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.workflow, self.job, self.conclusion, self.steps)

    @property
    def label(self) -> str:
        return f"{self.workflow} › {self.job} — {self.conclusion}"


@dataclass(frozen=True)
class HeadCiEvidence:
    sha: str
    has_in_flight: bool
    all_completed_green: bool
    failing_jobs: tuple[FailingJobEvidence, ...]
    successful_jobs: tuple[str, ...]
    observed_workflows: tuple[str, ...]
    in_flight_workflows: tuple[str, ...]


@dataclass(frozen=True)
class FailureEvidence:
    sha: str
    jobs: tuple[FailingJobEvidence, ...]
    head_unsettled: bool = False
    current_head_sha: str | None = None

    @property
    def fingerprint(self) -> tuple[str, ...]:
        return tuple(
            "\0".join((job.workflow, job.job, job.conclusion, "\0".join(job.steps)))
            for job in sorted(self.jobs, key=lambda item: item.fingerprint)
        )

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


def _run_workflow_name(run: Mapping[str, Any]) -> str:
    for key in ("workflow_name", "workflow", "name", "display_title"):
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded(value, limit=120)
    return "workflow"


def _failing_steps(job: Mapping[str, Any]) -> tuple[str, ...]:
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        return ()
    names: list[str] = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        conclusion = str(step.get("conclusion", "")).lower()
        if conclusion not in RED_CONCLUSIONS:
            continue
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            names.append(_bounded(name, limit=120))
    return tuple(names[:MAX_STEPS_PER_JOB])


def _failed_jobs_from_runs(runs: object) -> tuple[FailingJobEvidence, ...]:
    if not isinstance(runs, list):
        return ()
    failures: dict[tuple[str, str, str, tuple[str, ...]], FailingJobEvidence] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_conclusion = str(run.get("conclusion", "")).lower()
        run_red = run_conclusion in RED_CONCLUSIONS or (
            run_conclusion == "cancelled" and _nested_failure(run)
        )
        if not run_red:
            continue
        workflow = _run_workflow_name(run)
        run_url = _safe_github_url(run.get("html_url")) or _safe_github_url(run.get("url"))
        jobs = run.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            name = job.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            steps = _failing_steps(job)
            raw_conclusion = str(job.get("conclusion", "")).lower()
            if raw_conclusion in RED_CONCLUSIONS:
                conclusion = raw_conclusion
            elif steps and run_conclusion == "cancelled":
                conclusion = "failure"
            else:
                continue
            job_url = _safe_github_url(job.get("html_url")) or _safe_github_url(job.get("url"))
            evidence = FailingJobEvidence(
                workflow=workflow,
                job=_bounded(name, limit=120),
                conclusion=conclusion,
                url=job_url or run_url,
                steps=steps,
            )
            failures[evidence.fingerprint] = evidence
    return tuple(failures[key] for key in sorted(failures))


def _commit_sha(observation: RepoObservation) -> str:
    if observation.commit is None:
        raise CiWatchError(f"{observation.repo} has no settled commit")
    return _sha(observation.commit.get("sha"))


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
        settled_jobs = _failed_jobs_from_runs(observation.commit.get("runs"))
        if not settled_jobs:
            return RepoDecision(RepoState.ERROR, "red_evidence_missing_job_identity")
        if current:
            return RepoDecision(
                RepoState.RED,
                "actionable_failure",
                head,
                FailureEvidence(head.sha, settled_jobs),
            )
        if head_evidence is None or head_evidence.sha != head.sha:
            return RepoDecision(RepoState.ERROR, "missing_head_ci_evidence")
        if head_evidence.failing_jobs:
            return RepoDecision(
                RepoState.RED,
                "actionable_failure",
                head,
                FailureEvidence(head.sha, head_evidence.failing_jobs),
            )
        remaining = tuple(
            job for job in settled_jobs if job.job not in set(head_evidence.successful_jobs)
        )
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
                    head_unsettled=True,
                    current_head_sha=head.sha,
                ),
            )
        return RepoDecision(RepoState.PENDING, "superseded_or_unsettled", head)
    if base_state is RepoState.GREEN:
        if current:
            return RepoDecision(RepoState.GREEN, "green", head)
        return RepoDecision(RepoState.PENDING, "newer_head_unsettled", head)
    return RepoDecision(RepoState.PENDING, "superseded_or_unsettled", head)


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
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
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
class ReleaseSettings:
    merge_method: str
    gating_workflows: tuple[str, ...]
    heavy_workflows: tuple[str, ...]
    heavy_max_age_hours: float


DEFAULT_RELEASE_SETTINGS = ReleaseSettings(
    merge_method="merge",
    gating_workflows=(),
    heavy_workflows=(),
    heavy_max_age_hours=DEFAULT_HEAVY_MAX_AGE_HOURS,
)


@dataclass(frozen=True)
class MergePlan:
    repo: str
    pr: ReleasePr
    merge_method: str = "merge"


@dataclass(frozen=True)
class ReleaseObservation:
    repo: str
    numbers: tuple[int, ...] | None
    prs: tuple[ReleasePr, ...] = ()
    error: str | None = None


def _is_release_please_branch(value: str) -> bool:
    return value.startswith(RELEASE_BRANCH_PREFIXES)


def plan_release_merge(
    repo: str,
    branch_state: RepoState,
    default_branch: str,
    candidates: Sequence[ReleasePr],
    *,
    generator_busy: bool,
    merge_method: str = "merge",
) -> tuple[MergePlan | None, str]:
    if branch_state is not RepoState.GREEN:
        return None, "default_branch_not_green"
    if not candidates:
        return None, "no_release_pr"
    if len(candidates) != 1:
        return None, "ambiguous_release_prs"
    pr = candidates[0]
    if not _is_release_please_branch(pr.head_ref_name):
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
    return MergePlan(repo, pr, merge_method), "eligible"


def _is_generator_busy(repo: str, runs: Sequence[JsonObject]) -> bool:
    for run in runs:
        if str(run.get("status", "")).lower() not in IN_FLIGHT_STATUSES:
            continue
        labels = [run.get(key, "") for key in ("name", "path")]
        if not all(isinstance(label, str) and len(label) <= MAX_TEXT for label in labels):
            raise CiWatchError(f"{repo} workflow run has an invalid name or path")
        label = " ".join(item.lower() for item in labels)
        if "release-please" in label or "publish" in label:
            return True
    return False


def _release_gate_reason(evidence: HeadCiEvidence, gating_workflows: Sequence[str]) -> str | None:
    """Fail-closed release gate over HEAD CI evidence restricted to named workflows."""
    observed = set(evidence.observed_workflows)
    if any(workflow not in observed for workflow in gating_workflows):
        return "gating_workflow_missing"
    in_flight = set(evidence.in_flight_workflows)
    if any(workflow in in_flight for workflow in gating_workflows):
        return "gating_workflow_in_flight"
    red = {job.workflow for job in evidence.failing_jobs}
    if any(workflow in red for workflow in gating_workflows):
        return "gating_workflow_red"
    return None


def _newest_completed_run(runs: Sequence[JsonObject], workflow: str) -> JsonObject | None:
    candidates = [
        run
        for run in runs
        if _run_workflow_name(run) == workflow and str(run.get("status", "")).lower() == "completed"
    ]
    if not candidates:
        return None

    def _created_at(run: JsonObject) -> str:
        value = run.get("created_at")
        return value if isinstance(value, str) else ""

    return max(candidates, key=_created_at)


def _evaluate_heavy_lane(
    repo: str,
    runs: Sequence[JsonObject],
    heavy_workflows: Sequence[str],
    max_age_hours: float,
    now: datetime,
) -> str | None:
    """Fail-closed freshness gate: every heavy workflow's newest run must be a recent green."""
    for workflow in heavy_workflows:
        run = _newest_completed_run(runs, workflow)
        if run is None or str(run.get("conclusion", "")).lower() not in GREEN_CONCLUSIONS:
            return "heavy_lane_not_green"
        try:
            completed_at = _parse_timestamp(_required_string(run, "updated_at"))
        except ValueError as error:
            raise CiWatchError(
                f"{repo} heavy workflow {workflow!r} run has an invalid completion timestamp"
            ) from error
        if now - completed_at > timedelta(hours=max_age_hours):
            return "heavy_lane_stale"
    return None


@dataclass(frozen=True)
class RepositoryMetadata:
    default_branch: str
    allowed_merge_methods: frozenset[str]

    @classmethod
    def from_json(cls, repo: str, value: Mapping[str, Any]) -> RepositoryMetadata:
        allowed: set[str] = set()
        for method, key in _MERGE_METHOD_METADATA_KEYS.items():
            raw_allowed = value.get(key)
            if raw_allowed is not None and not isinstance(raw_allowed, bool):
                raise CiWatchError(f"{repo} metadata has an invalid {key}")
            if raw_allowed is True:
                allowed.add(method)
        return cls(
            default_branch=_branch(value.get("default_branch")),
            allowed_merge_methods=frozenset(allowed),
        )

    def allows_merge_method(self, merge_method: str) -> bool:
        if merge_method not in _MERGE_METHOD_FLAGS:
            raise CiWatchError("merge_method must be one of: merge, squash, rebase")
        return merge_method in self.allowed_merge_methods


class GitHubReader:
    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner
        self._metadata: dict[str, RepositoryMetadata] = {}

    def _run(self, argv: Sequence[str], *, input_text: str | None = None) -> CommandResult:
        command = [self.executable, *argv]
        env = github_command_env()
        with _github_json_env():
            if self._runner is run_command:
                return run_command(command, input_text=input_text, env=env)
            return self._runner(command, input_text=input_text)

    def _json(self, argv: Sequence[str], *, source: str) -> Any:
        result = self._run(argv)
        if result.returncode != 0:
            detail = _bounded(result.stderr or result.stdout) or "-"
            raise CiWatchError(f"{source} failed: exit_code={result.returncode} detail={detail}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CiWatchError(f"{source} returned malformed JSON: {error}") from error

    def _repository_metadata(self, repo: str) -> RepositoryMetadata:
        repo = _repo(repo)
        cached = self._metadata.get(repo)
        if cached is not None:
            return cached
        metadata = self._json(["api", f"repos/{repo}"], source=f"{repo} metadata")
        if not isinstance(metadata, dict):
            raise CiWatchError(f"{repo} metadata is not an object")
        parsed = RepositoryMetadata.from_json(repo, metadata)
        self._metadata[repo] = parsed
        return parsed

    def default_branch_head(self, repo: str) -> BranchHead:
        repo = _repo(repo)
        branch = self._repository_metadata(repo).default_branch
        commit = self._json(
            ["api", f"repos/{repo}/commits/{quote(branch, safe='')}"],
            source=f"{repo} default branch head",
        )
        if not isinstance(commit, dict):
            raise CiWatchError(f"{repo} default branch head is not an object")
        return BranchHead(branch, _sha(commit.get("sha")))

    def merge_method_allowed(self, repo: str, merge_method: str) -> bool:
        return self._repository_metadata(repo).allows_merge_method(merge_method)

    def workflow_count(self, repo: str) -> int:
        repo = _repo(repo)
        data = self._json(
            ["api", "-X", "GET", f"repos/{repo}/actions/workflows", "-f", "per_page=1"],
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
        failing_jobs: dict[tuple[str, str, str, tuple[str, ...]], FailingJobEvidence] = {}
        successful_jobs: dict[str, None] = {}
        observed_workflows: dict[str, None] = {}
        in_flight_workflows: dict[str, None] = {}
        terminal_red_without_job = False
        for run in runs:
            status = run.get("status")
            if not isinstance(status, str) or status.lower() not in KNOWN_RUN_STATUSES:
                raise CiWatchError(f"{repo} HEAD workflow runs contain an invalid status")
            normalized_status = status.lower()
            workflow_name = _run_workflow_name(run)
            observed_workflows[workflow_name] = None
            if normalized_status in IN_FLIGHT_STATUSES:
                has_in_flight = True
                all_completed_green = False
                in_flight_workflows[workflow_name] = None
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
            synthetic_run = {
                "name": run.get("name"),
                "workflow_name": run.get("name"),
                "conclusion": normalized_conclusion,
                "html_url": run.get("html_url"),
                "jobs": jobs,
            }
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
                normalized_job_conclusion = conclusion_value.lower()
                if normalized_job_conclusion in RED_CONCLUSIONS:
                    run_has_red_job = True
                elif normalized_job_conclusion == "success":
                    successful_jobs[_bounded(name, limit=120)] = None
            for failure in _failed_jobs_from_runs([synthetic_run]):
                failing_jobs[failure.fingerprint] = failure
            if normalized_conclusion in RED_CONCLUSIONS or (
                normalized_conclusion == "cancelled" and run_has_red_job
            ):
                terminal_red_without_job = terminal_red_without_job or not run_has_red_job
        if terminal_red_without_job and not failing_jobs:
            raise CiWatchError(f"{repo} HEAD red evidence has no failing job identity")
        return HeadCiEvidence(
            sha=sha,
            has_in_flight=has_in_flight,
            all_completed_green=all_completed_green,
            failing_jobs=tuple(failing_jobs[key] for key in sorted(failing_jobs)),
            successful_jobs=tuple(sorted(successful_jobs)),
            observed_workflows=tuple(sorted(observed_workflows)),
            in_flight_workflows=tuple(sorted(in_flight_workflows)),
        )

    def generator_busy(self, repo: str, branch: str) -> bool:
        return _is_generator_busy(repo, self.workflow_runs(repo, branch))

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

    def workflow_runs(self, repo: str, branch: str) -> list[JsonObject]:
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
            if isinstance(head, str) and _is_release_please_branch(head):
                if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
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
        return self._run(
            [
                "pr",
                "merge",
                str(plan.pr.number),
                "--repo",
                _repo(plan.repo),
                _MERGE_METHOD_FLAGS[plan.merge_method],
                "--match-head-commit",
                _sha(plan.pr.head_oid),
            ]
        )


class SaseNotifier:
    """Send ci_watch SASE notifications and nothing else."""

    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self._runner = runner

    def notify(
        self,
        notes: Sequence[str],
        *,
        icon: str,
        tags: Sequence[str],
        action: str | None = None,
        action_data: Mapping[str, str] | None = None,
    ) -> None:
        payload_data: JsonObject = {
            "notes": [_bounded(note, limit=512) for note in notes],
            "tags": [_bounded(tag, limit=64) for tag in tags],
            "icon": _bounded(icon, limit=16),
        }
        if action is not None:
            payload_data["action"] = _bounded(action, limit=64)
        if action_data is not None:
            payload_data["action_data"] = {
                _bounded(key, limit=64): _bounded(value, limit=4096)
                for key, value in action_data.items()
            }
        result = self._runner(
            [self.executable, "notify", "create", "-s", CHOP_NAME],
            input_text=json.dumps(payload_data),
        )
        if result.returncode != 0:
            detail = _bounded(result.stderr or result.stdout) or "-"
            raise CiWatchError(
                f"notification failed: exit_code={result.returncode} detail={detail}"
            )


@dataclass(frozen=True)
class Config:
    actstat_bin: str
    gh_bin: str
    sase_bin: str
    repos: tuple[str, ...]
    release_repositories: tuple[str, ...]
    merge_order: tuple[str, ...]
    max_merges: int
    merge_enabled: bool
    default_release_settings: ReleaseSettings
    release_settings: tuple[tuple[str, ReleaseSettings], ...]

    @property
    def merge_method(self) -> str:
        return self.default_release_settings.merge_method

    @property
    def gating_workflows(self) -> tuple[str, ...]:
        return self.default_release_settings.gating_workflows

    @property
    def heavy_workflows(self) -> tuple[str, ...]:
        return self.default_release_settings.heavy_workflows

    @property
    def heavy_max_age_hours(self) -> float:
        return self.default_release_settings.heavy_max_age_hours

    def release_settings_for(self, repo: str) -> ReleaseSettings:
        repo = _repo(repo)
        for candidate, settings in self.release_settings:
            if candidate == repo:
                return settings
        raise CiWatchError(f"release repository {repo!r} has no release settings")

    @classmethod
    def from_invocation(cls, invocation: ChopInvocation) -> Config:
        values = context_vars(invocation)
        removed = sorted(key for key in REMOVED_CONFIG_KEYS if key in values)
        if removed:
            raise CiWatchError(
                "unsupported ci_watch config keys: "
                + ", ".join(removed)
                + "; CI fix gates were removed"
            )
        repos = _string_list(values.get("repos"), "repos")
        if not repos or len(repos) > MAX_REPOS:
            raise CiWatchError(f"repos must contain between 1 and {MAX_REPOS} entries")
        normalized_repos = tuple(_repo(repo) for repo in repos)
        if len(set(normalized_repos)) != len(normalized_repos):
            raise CiWatchError("repos contains duplicates")
        releases = _string_list(values.get("release_repositories", []), "release_repositories")
        release_repositories = tuple(_repo(repo) for repo in releases)
        if len(set(release_repositories)) != len(release_repositories):
            raise CiWatchError("release_repositories contains duplicates")
        for repo in release_repositories:
            if repo not in normalized_repos:
                raise CiWatchError(f"release repository {repo!r} is not in repos")
        raw_order = _string_list(
            values.get("merge_order", list(release_repositories)), "merge_order"
        )
        merge_order = tuple(_resolve_order_repo(item, normalized_repos) for item in raw_order)
        if len(set(merge_order)) != len(merge_order):
            raise CiWatchError("merge_order contains duplicates")
        for repo in merge_order:
            if repo not in release_repositories:
                raise CiWatchError(f"merge_order repository {repo!r} is not release-enabled")
        default_release_settings, release_settings = _release_settings(values, release_repositories)
        return cls(
            actstat_bin=_binary(values, "actstat_bin", "actstat"),
            gh_bin=_binary(values, "gh_bin", "gh"),
            sase_bin=_binary(values, "sase_bin", "sase"),
            repos=normalized_repos,
            release_repositories=release_repositories,
            merge_order=merge_order,
            max_merges=_positive_int(values, "max_merges_per_tick", 1),
            merge_enabled=_bool(values, "merge_enabled", False),
            default_release_settings=default_release_settings,
            release_settings=release_settings,
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


def _merge_method_value(value: object, name: str) -> str:
    if not isinstance(value, str) or value not in _MERGE_METHOD_FLAGS:
        raise CiWatchError(f"{name} must be one of: merge, squash, rebase")
    return value


def _positive_number_value(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise CiWatchError(f"{name} must be a positive number")
    return float(value)


def _release_setting_key(
    value: object,
    *,
    field_name: str,
    release_repositories: Sequence[str],
) -> str | None:
    if not isinstance(value, str):
        raise CiWatchError(f"{field_name} mapping keys must be repository strings or 'default'")
    key = value.strip()
    if key == "default":
        return None
    repo = _repo(key)
    if repo not in release_repositories:
        raise CiWatchError(f"{field_name} contains unknown release repository {repo!r}")
    return repo


def _release_setting_values[T](
    values: Mapping[str, Any],
    key: str,
    release_repositories: Sequence[str],
    built_in: T,
    parser: Callable[[object, str], T],
) -> tuple[T, dict[str, T]]:
    if key not in values:
        return built_in, {}
    raw = values[key]
    if isinstance(raw, Mapping):
        default = built_in
        by_repo: dict[str, T] = {}
        seen: set[str] = set()
        for raw_key, raw_value in raw.items():
            repo = _release_setting_key(
                raw_key,
                field_name=key,
                release_repositories=release_repositories,
            )
            label = f"{key}.{raw_key}" if isinstance(raw_key, str) else key
            parsed = parser(raw_value, label)
            if repo is None:
                if "default" in seen:
                    raise CiWatchError(f"{key} mapping contains duplicate default")
                seen.add("default")
                default = parsed
                continue
            if repo in seen:
                raise CiWatchError(f"{key} mapping contains duplicate repository {repo!r}")
            seen.add(repo)
            by_repo[repo] = parsed
        return default, by_repo
    return parser(raw, key), {}


def _release_settings(
    values: Mapping[str, Any],
    release_repositories: Sequence[str],
) -> tuple[ReleaseSettings, tuple[tuple[str, ReleaseSettings], ...]]:
    default_merge_method, merge_methods = _release_setting_values(
        values,
        "merge_method",
        release_repositories,
        DEFAULT_RELEASE_SETTINGS.merge_method,
        _merge_method_value,
    )
    default_gating_workflows, gating_workflows = _release_setting_values(
        values,
        "gating_workflows",
        release_repositories,
        DEFAULT_RELEASE_SETTINGS.gating_workflows,
        _string_list,
    )
    default_heavy_workflows, heavy_workflows = _release_setting_values(
        values,
        "heavy_workflows",
        release_repositories,
        DEFAULT_RELEASE_SETTINGS.heavy_workflows,
        _string_list,
    )
    default_heavy_max_age_hours, heavy_max_age_hours = _release_setting_values(
        values,
        "heavy_max_age_hours",
        release_repositories,
        DEFAULT_RELEASE_SETTINGS.heavy_max_age_hours,
        _positive_number_value,
    )
    default_settings = ReleaseSettings(
        merge_method=default_merge_method,
        gating_workflows=default_gating_workflows,
        heavy_workflows=default_heavy_workflows,
        heavy_max_age_hours=default_heavy_max_age_hours,
    )
    return default_settings, tuple(
        (
            repo,
            ReleaseSettings(
                merge_method=merge_methods.get(repo, default_merge_method),
                gating_workflows=gating_workflows.get(repo, default_gating_workflows),
                heavy_workflows=heavy_workflows.get(repo, default_heavy_workflows),
                heavy_max_age_hours=heavy_max_age_hours.get(repo, default_heavy_max_age_hours),
            ),
        )
        for repo in release_repositories
    )


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
        "release_candidates": 0,
        "merged": 0,
        "merge_skipped": 0,
        "notifications_attempted": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "reports_published": 0,
        "reports_failed": 0,
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


def _empty_state() -> JsonObject:
    return {"version": 1, "failures": {}, "releases": []}


def _release_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def _legacy_release_rows(invocation: ChopInvocation, now: datetime) -> list[JsonObject]:
    path = Path(invocation.context.state_dir) / LEGACY_RELEASE_LEDGER_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(raw.get("merges"), list)
    ):
        return []
    rows: list[JsonObject] = []
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    for row in raw["merges"]:
        if not isinstance(row, dict):
            continue
        try:
            repo = _repo(row.get("repo"))
            number = row.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise CiWatchError("invalid release number")
            url = _required_string(row, "url")
            if not url.startswith("https://github.com/"):
                raise CiWatchError("invalid release URL")
            head_oid = _sha(row.get("head_oid"))
            version = _bounded(_required_string(row, "version"), limit=80)
            submitted_at = _required_string(row, "merged_at")
            parsed = _parse_timestamp(submitted_at).astimezone(now.tzinfo)
        except (CiWatchError, ValueError):
            continue
        if parsed < cutoff:
            continue
        rows.append(
            {
                "repo": repo,
                "number": number,
                "url": url,
                "head_oid": head_oid,
                "version": version,
                "title": f"release {version}",
                "target_branch": "master",
                "submitted_at": submitted_at,
                "notification_sent": True,
                "outcome": "legacy_merge",
            }
        )
    return rows[-MAX_STATE_RELEASES:]


def _failure_to_json(failure: FailureEvidence) -> JsonObject:
    payload: JsonObject = {
        "sha": failure.sha,
        "head_unsettled": failure.head_unsettled,
        "jobs": [
            {
                "workflow": job.workflow,
                "job": job.job,
                "conclusion": job.conclusion,
                "url": job.url,
                "steps": list(job.steps),
            }
            for job in failure.jobs
        ],
    }
    if failure.current_head_sha is not None:
        payload["current_head_sha"] = failure.current_head_sha
    return payload


def _failure_from_json(value: Mapping[str, Any]) -> FailureEvidence:
    sha = _sha(value.get("sha"))
    jobs_value = value.get("jobs")
    if not isinstance(jobs_value, list) or not jobs_value:
        raise CiWatchError("failure evidence has no jobs")
    jobs: list[FailingJobEvidence] = []
    for row in jobs_value:
        if not isinstance(row, dict):
            raise CiWatchError("failure job is not an object")
        steps = row.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
            raise CiWatchError("failure job has invalid steps")
        url = row.get("url")
        if url is not None and _safe_github_url(url) is None:
            raise CiWatchError("failure job has invalid URL")
        jobs.append(
            FailingJobEvidence(
                workflow=_bounded(_required_string(row, "workflow"), limit=120),
                job=_bounded(_required_string(row, "job"), limit=120),
                conclusion=_bounded(_required_string(row, "conclusion"), limit=80),
                url=cast(str | None, url),
                steps=tuple(_bounded(step, limit=120) for step in steps[:MAX_STEPS_PER_JOB]),
            )
        )
    raw_current_head_sha = value.get("current_head_sha")
    current_head_sha = _sha(raw_current_head_sha) if raw_current_head_sha is not None else None
    return FailureEvidence(
        sha=sha,
        jobs=tuple(jobs),
        head_unsettled=value.get("head_unsettled") is True,
        current_head_sha=current_head_sha,
    )


def _sanitize_failure_row(repo: str, row: Mapping[str, Any]) -> JsonObject | None:
    fingerprint = row.get("fingerprint")
    notification_sent = row.get("notification_sent")
    evidence = row.get("evidence")
    last_seen = row.get("last_seen")
    if (
        not isinstance(fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{16}", fingerprint)
        or not isinstance(notification_sent, bool)
        or not isinstance(evidence, dict)
        or not isinstance(last_seen, str)
    ):
        return None
    try:
        _repo(repo)
        _parse_timestamp(last_seen)
        failure = _failure_from_json(evidence)
    except (CiWatchError, ValueError):
        return None
    return {
        "fingerprint": fingerprint,
        "notification_sent": notification_sent,
        "last_seen": last_seen,
        "evidence": _failure_to_json(failure),
    }


def _sanitize_release_row(row: Mapping[str, Any], now: datetime) -> JsonObject | None:
    try:
        repo = _repo(row.get("repo"))
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise CiWatchError("invalid release number")
        url = _required_string(row, "url")
        if not url.startswith("https://github.com/"):
            raise CiWatchError("invalid release URL")
        head_oid = _sha(row.get("head_oid"))
        title = _bounded(_required_string(row, "title"))
        version = _bounded(_required_string(row, "version"), limit=80)
        target_branch = _branch(row.get("target_branch"))
        submitted_at = _required_string(row, "submitted_at")
        parsed = _parse_timestamp(submitted_at).astimezone(now.tzinfo)
        notification_sent = row.get("notification_sent")
        if not isinstance(notification_sent, bool):
            raise CiWatchError("invalid notification flag")
        outcome = _bounded(row.get("outcome", "squash_merge_submitted"), limit=80)
    except (CiWatchError, ValueError):
        return None
    if parsed < now - timedelta(days=STATE_RETENTION_DAYS):
        return None
    sanitized: JsonObject = {
        "repo": repo,
        "number": number,
        "url": url,
        "head_oid": head_oid,
        "title": title,
        "version": version,
        "target_branch": target_branch,
        "submitted_at": submitted_at,
        "notification_sent": notification_sent,
        "outcome": outcome,
    }
    notified_at = row.get("notified_at")
    if isinstance(notified_at, str):
        with suppress(ValueError):
            _parse_timestamp(notified_at)
            sanitized["notified_at"] = notified_at
    return sanitized


def _load_state(invocation: ChopInvocation, now: datetime, repos: Sequence[str]) -> JsonObject:
    path = Path(invocation.context.state_dir) / STATE_FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        state = _empty_state()
        state["releases"] = _legacy_release_rows(invocation, now)
        return state
    allowed = set(repos)
    failures: JsonObject = {}
    raw_failures = raw.get("failures")
    if isinstance(raw_failures, dict):
        for repo, row in raw_failures.items():
            if not isinstance(repo, str) or repo not in allowed or not isinstance(row, dict):
                continue
            sanitized = _sanitize_failure_row(repo, row)
            if sanitized is not None:
                failures[repo] = sanitized
    releases: list[JsonObject] = []
    raw_releases = raw.get("releases")
    if isinstance(raw_releases, list):
        for row in raw_releases:
            if not isinstance(row, dict):
                continue
            sanitized = _sanitize_release_row(row, now)
            if sanitized is not None:
                releases.append(sanitized)
    releases.sort(key=lambda row: _parse_timestamp(str(row["submitted_at"])))
    return {"version": 1, "failures": failures, "releases": releases[-MAX_STATE_RELEASES:]}


def _write_state(invocation: ChopInvocation, state: Mapping[str, Any]) -> None:
    destination = Path(invocation.context.state_dir) / STATE_FILE_NAME
    _atomic_write_json(destination, state)


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


def _release_order(config: Config) -> list[str]:
    ordered = [repo for repo in config.merge_order if repo in config.release_repositories]
    ordered.extend(repo for repo in config.release_repositories if repo not in ordered)
    return ordered


_RELEASE_REASON_LABELS = {
    "eligible": "ready to merge",
    "default_branch_not_green": "base branch not green",
    "ambiguous_release_prs": "multiple release PRs",
    "not_release_pr": "not a release-please PR",
    "release_pr_draft": "draft",
    "release_pr_wrong_base": "wrong base branch",
    "release_pr_not_mergeable": "not mergeable",
    "release_pr_not_clean": "merge state not clean",
    "release_pr_empty_rollup": "checks unavailable",
    "release_pr_checks_not_green": "checks not green",
    "release_generator_busy": "release-please publish running",
    "release_pr_head_changed": "PR changed during merge",
    "default_branch_changed": "default branch changed",
    "gating_workflow_missing": "gating workflow has not run",
    "gating_workflow_in_flight": "gating workflow running",
    "gating_workflow_red": "gating workflow red",
    "heavy_lane_not_green": "heavy workflow not green",
    "heavy_lane_stale": "heavy workflow evidence stale",
    "merge_cap_reached": "merge cap reached",
    "merge_disabled": "merge disabled",
    "merge_context_unavailable": "merge context unavailable",
    "merge_method_not_allowed": "merge method disabled",
    "dry_run": "dry run",
    "merge_failed": "merge failed",
    "merged": "merged",
}
_NON_BLOCKING_RELEASE_REASONS = frozenset(
    {"eligible", "release_generator_busy", "gating_workflow_in_flight"}
)


def _humanize_release_reason(reason: str) -> str:
    return _RELEASE_REASON_LABELS.get(reason, _bounded(reason.replace("_", " ")))


def _release_reason_tone(reason: str) -> Tone:
    if reason == "merged":
        return "ok"
    if reason == "no_release_pr":
        return "muted"
    if reason in _NON_BLOCKING_RELEASE_REASONS:
        return "warn"
    return "error"


def _format_timestamp(value: str, now: datetime) -> str:
    try:
        parsed = _parse_timestamp(value).astimezone(now.tzinfo)
    except ValueError:
        return "-"
    return parsed.strftime("%m-%d %H:%M")


def _failure_summary(failure: FailureEvidence) -> str:
    labels = [job.label for job in failure.jobs[:2]]
    remaining = len(failure.jobs) - len(labels)
    if remaining > 0:
        labels.append(f"+{remaining} more")
    return _bounded("; ".join(labels), limit=512)


def _build_ci_watch_report(
    config: Config,
    counters: Mapping[str, int],
    states: Mapping[str, RepoState],
    heads: Mapping[str, BranchHead],
    failures: Mapping[str, FailureEvidence],
    release_observations: Mapping[str, ReleaseObservation],
    release_decisions: Mapping[str, str],
    state: Mapping[str, Any],
    mode: str,
    now: datetime,
) -> ChopReport:
    report = start_report("CI WATCH")
    headline = (
        f"{counters['green']} green · {counters['red']} red · "
        f"{counters['pending']} pending · {counters['errors']} error"
    )
    headline_tone: Tone = (
        "error"
        if counters["red"] or counters["errors"]
        else "warn"
        if counters["pending"]
        else "ok"
    )
    report.headline(headline, tone=headline_tone)

    report.heading("REPOSITORIES")
    repositories = report.rows(columns=("REPOSITORY", "STATE", "DEFAULT", "EVIDENCE"))
    for repo in config.repos:
        state_value = states[repo]
        tone, glyph = _repo_state_presentation(state_value)
        head = heads.get(repo)
        evidence = str(state_value.value)
        if state_value is RepoState.GREEN and head is not None:
            evidence = head.sha[:12]
        elif state_value is RepoState.RED and repo in failures:
            evidence = _failure_summary(failures[repo])
        repositories.row(
            (
                repo,
                state_value.value,
                f"{head.branch}@{head.sha[:12]}" if head is not None else "-",
                evidence,
            ),
            tone=tone,
            glyph=glyph,
        )

    report.heading("FAILING JOBS")
    failing_rows = report.rows(columns=("REPOSITORY", "SHA", "JOB", "STEPS", "URL"))
    for repo in config.repos:
        failure = failures.get(repo)
        if failure is None:
            continue
        for job in failure.jobs:
            failing_rows.row(
                (
                    repo,
                    failure.sha[:12],
                    job.label,
                    ", ".join(job.steps) if job.steps else "-",
                    job.url or "-",
                ),
                tone="error",
                glyph="▲",
            )
        if failure.head_unsettled and failure.current_head_sha is not None:
            failing_rows.row(
                (
                    repo,
                    failure.current_head_sha[:12],
                    "current HEAD still unsettled",
                    f"settled failure remains at {failure.sha[:12]}",
                    "-",
                ),
                tone="warn",
                glyph="◆",
            )
    if not failures:
        failing_rows.row(("-", "-", "no current failures", "-", "-"), tone="muted", glyph="·")

    report.heading("RELEASES")
    release_rows = report.rows(columns=("REPOSITORY", "PR", "DECISION", "HEAD"))
    for repo in _release_order(config):
        observation = release_observations.get(repo)
        reason = release_decisions.get(repo, "not_observed")
        if observation is None or observation.numbers is None:
            release_rows.row((repo, "-", _humanize_release_reason(reason), "-"), tone="error")
            continue
        if not observation.numbers:
            release_rows.row((repo, "-", "no release PR", "-"), tone="muted", glyph="·")
            continue
        pr = observation.prs[0] if observation.prs else None
        release_rows.row(
            (
                repo,
                f"#{observation.numbers[0]}",
                _humanize_release_reason(reason),
                pr.head_oid[:12] if pr is not None else "-",
            ),
            tone=_release_reason_tone(reason),
            glyph="✓" if reason == "merged" else "◆",
        )

    report.heading("RECENT SUBMISSIONS")
    recent_rows = report.rows(columns=("REPOSITORY", "PR", "VERSION", "SUBMITTED", "NOTICE"))
    raw_releases = state.get("releases")
    releases = (
        [row for row in raw_releases if isinstance(row, dict)]
        if isinstance(raw_releases, list)
        else []
    )
    recent_cutoff = now - timedelta(days=REPORT_RECENT_DAYS)
    recent = [
        row
        for row in releases
        if isinstance(row.get("submitted_at"), str)
        and _parse_timestamp(str(row["submitted_at"])).astimezone(now.tzinfo) >= recent_cutoff
    ]
    recent.sort(key=lambda row: _parse_timestamp(str(row["submitted_at"])), reverse=True)
    for row in recent[:8]:
        recent_rows.row(
            (
                str(row.get("repo", "-")),
                f"#{row.get('number')}",
                str(row.get("version", "-")),
                _format_timestamp(str(row.get("submitted_at", "")), now),
                "sent" if row.get("notification_sent") is True else "pending",
            ),
            tone="ok" if row.get("notification_sent") is True else "warn",
            glyph="✓" if row.get("notification_sent") is True else "◆",
        )
    if not recent:
        recent_rows.row(("-", "-", "-", "none", "-"), tone="muted", glyph="·")

    add_facts_footer(
        report,
        {
            "mode": mode.replace("_", " "),
            "updated": now.strftime("%m-%d %H:%M"),
            "release cap": str(config.max_merges),
            "legacy report": LEGACY_RELEASE_REPORT_FILE_NAME,
        },
        tone="neutral" if mode == "live" else "warn",
    )
    return report


def _publish_report(invocation: ChopInvocation, report: ChopReport) -> Path:
    destination = Path(invocation.context.state_dir).resolve() / REPORT_FILE_NAME
    document = validate_chop_report(report.to_dict())
    _atomic_write_json(destination, document)
    return destination


def _release_notification_notes(row: Mapping[str, Any]) -> list[str]:
    return [
        f"Release submitted: {row['repo']} #{row['number']} {row.get('version', '-')}",
        (
            f"Target {row.get('target_branch', '-')} · "
            f"head {str(row.get('head_oid', ''))[:12]} · {row.get('outcome', 'merged')}"
        ),
        _bounded(row.get("title", "-"), limit=512),
        str(row.get("url", "-")),
    ]


def _failure_notification_notes(
    repo: str,
    failure: FailureEvidence,
    head: BranchHead | None,
) -> list[str]:
    branch = head.branch if head is not None else "default"
    notes = [f"CI failure: {repo} {branch}@{failure.sha[:12]}"]
    for job in failure.jobs[:MAX_FAILURE_JOBS_PER_NOTIFICATION]:
        notes.append(job.label)
        if job.steps:
            notes.append("Steps: " + ", ".join(job.steps))
        if job.url:
            notes.append(job.url)
    remaining = len(failure.jobs) - MAX_FAILURE_JOBS_PER_NOTIFICATION
    if remaining > 0:
        notes.append(f"+{remaining} more failing job{'s' if remaining != 1 else ''}")
    if failure.head_unsettled and failure.current_head_sha is not None:
        notes.append(
            "Settled failure is older than current HEAD: "
            f"{failure.sha[:12]} red, {failure.current_head_sha[:12]} still unsettled"
        )
    return notes


def _notification_action(report_path: Path | None) -> tuple[str | None, dict[str, str] | None]:
    if report_path is None:
        return None, None
    return "ViewReport", {"report_path": str(report_path), "report_title": "CI WATCH"}


def _update_failure_state(
    state: JsonObject,
    failures: Mapping[str, FailureEvidence],
    states: Mapping[str, RepoState],
    now: datetime,
) -> None:
    raw_failures = state.setdefault("failures", {})
    if not isinstance(raw_failures, dict):
        raw_failures = {}
        state["failures"] = raw_failures
    for repo, repo_state in states.items():
        if repo_state is not RepoState.RED:
            raw_failures.pop(repo, None)
            continue
        failure = failures[repo]
        fingerprint = failure.fingerprint_key
        previous = raw_failures.get(repo)
        notification_sent = (
            isinstance(previous, dict)
            and previous.get("fingerprint") == fingerprint
            and previous.get("notification_sent") is True
        )
        raw_failures[repo] = {
            "fingerprint": fingerprint,
            "notification_sent": notification_sent,
            "last_seen": now.isoformat(),
            "evidence": _failure_to_json(failure),
        }


def _append_release_record(
    state: JsonObject,
    repo: str,
    pr: ReleasePr,
    now: datetime,
    merge_method: str,
) -> None:
    raw_releases = state.setdefault("releases", [])
    if not isinstance(raw_releases, list):
        raw_releases = []
        state["releases"] = raw_releases
    key = _release_key(repo, pr.number)
    raw_releases[:] = [
        row
        for row in raw_releases
        if not (
            isinstance(row, dict)
            and isinstance(row.get("repo"), str)
            and isinstance(row.get("number"), int)
            and _release_key(row["repo"], row["number"]) == key
        )
    ]
    raw_releases.append(
        {
            "repo": repo,
            "number": pr.number,
            "url": pr.url,
            "head_oid": pr.head_oid,
            "title": pr.title,
            "version": _extract_release_version(pr.title),
            "target_branch": pr.base_ref_name,
            "submitted_at": now.isoformat(),
            "notification_sent": False,
            "outcome": f"{merge_method}_merge_submitted",
        }
    )
    raw_releases[:] = raw_releases[-MAX_STATE_RELEASES:]


def _send_required_notifications(
    *,
    invocation: ChopInvocation,
    notifier: SaseNotifier,
    state: JsonObject,
    failures: Mapping[str, FailureEvidence],
    heads: Mapping[str, BranchHead],
    report_path: Path | None,
    now: datetime,
    counters: dict[str, int],
) -> list[str]:
    errors: list[str] = []
    action, action_data = _notification_action(report_path)
    raw_releases = state.get("releases")
    releases = raw_releases if isinstance(raw_releases, list) else []
    for row in releases:
        if not isinstance(row, dict) or row.get("notification_sent") is True:
            continue
        counters["notifications_attempted"] += 1
        try:
            notifier.notify(
                _release_notification_notes(row),
                icon="🚢",
                tags=("ci", "release"),
                action=action,
                action_data=action_data,
            )
        except CiWatchError as error:
            counters["notifications_failed"] += 1
            errors.append(str(error))
            continue
        counters["notifications_sent"] += 1
        row["notification_sent"] = True
        row["notified_at"] = now.isoformat()

    raw_failures = state.get("failures")
    failure_rows = raw_failures if isinstance(raw_failures, dict) else {}
    for repo, row in failure_rows.items():
        if (
            not isinstance(repo, str)
            or not isinstance(row, dict)
            or row.get("notification_sent") is True
            or repo not in failures
        ):
            continue
        failure = failures[repo]
        counters["notifications_attempted"] += 1
        try:
            notifier.notify(
                _failure_notification_notes(repo, failure, heads.get(repo)),
                icon="🚨",
                tags=("ci", "failure"),
                action=action,
                action_data=action_data,
            )
        except CiWatchError as error:
            counters["notifications_failed"] += 1
            errors.append(str(error))
            continue
        counters["notifications_sent"] += 1
        row["notification_sent"] = True
        row["notified_at"] = now.isoformat()

    if counters["notifications_sent"]:
        try:
            _write_state(invocation, state)
        except OSError as error:
            errors.append(f"state write failed after notification: {_bounded(error)}")
    return errors


def _evaluate_release_branch(
    github: GitHubReader,
    settings: ReleaseSettings,
    repo: str,
    actstat_state: RepoState,
    actstat_head: BranchHead | None,
    now: datetime,
) -> tuple[BranchHead | None, bool, str | None]:
    """Resolve the release-decision HEAD, generator-busy flag, and blocking reason.

    When `gating_workflows` is configured, release readiness is decided from named
    workflow evidence at the exact current HEAD instead of the actstat sweep, so a
    repository ineligible only because of unrelated actstat noise can still release.
    """
    head: BranchHead | None
    if settings.gating_workflows:
        head = actstat_head or github.default_branch_head(repo)
        evidence = github.head_ci_evidence(repo, head.sha)
        reason = _release_gate_reason(evidence, settings.gating_workflows)
    else:
        head = actstat_head
        reason = None if actstat_state is RepoState.GREEN else "default_branch_not_green"
    if reason is not None or head is None:
        return head, False, reason or "default_branch_not_green"
    runs = github.workflow_runs(repo, head.branch)
    busy = _is_generator_busy(repo, runs)
    if settings.heavy_workflows:
        heavy_reason = _evaluate_heavy_lane(
            repo, runs, settings.heavy_workflows, settings.heavy_max_age_hours, now
        )
        if heavy_reason is not None:
            return head, busy, heavy_reason
    return head, busy, None


def build_ci_watch_result(
    invocation: ChopInvocation,
    *,
    actstat: ActstatClient | None = None,
    github: GitHubReader | None = None,
    notifier: SaseNotifier | None = None,
    clock: Callable[[], datetime] = _local_now,
) -> ChopResultBuilder:
    config = Config.from_invocation(invocation)
    actstat = actstat or ActstatClient(config.actstat_bin)
    github = github or GitHubReader(config.gh_bin)
    notifier = notifier or SaseNotifier(config.sase_bin)
    now = clock().astimezone()
    mode = _dry_run_mode()
    state = _load_state(invocation, now, config.repos)
    observations = actstat.sweep(config.repos)
    counters = _new_counters(len(config.repos))
    states: dict[str, RepoState] = {}
    heads: dict[str, BranchHead] = {}
    failures: dict[str, FailureEvidence] = {}
    ledger_repos: dict[str, JsonObject] = {}
    head_evidence_repos = 0
    operational_errors: list[str] = []

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
                decision = RepoDecision(RepoState.ERROR, "missing_observation")
                _mark(ledger_repos, repo, workflow_probe_error=str(error))
        elif observation.error or observation.commit is None:
            state_value = classify_repo(observation)
            decision = RepoDecision(
                state_value,
                _classification_reason(observation, state_value),
            )
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
                decision = decide_repo(observation, head, head_evidence=head_evidence)
            except CiWatchError as error:
                decision = RepoDecision(RepoState.ERROR, str(error))
        state_value = decision.state
        states[repo] = state_value
        if decision.head is not None:
            heads[repo] = decision.head
        if decision.failure is not None:
            failures[repo] = decision.failure
        counter_name = "errors" if state_value is RepoState.ERROR else state_value.value
        counters[counter_name] += 1
        _mark(
            ledger_repos,
            repo,
            state=state_value,
            reason=decision.reason,
            classification_reason=decision.reason,
            head_sha=heads[repo].sha if repo in heads else None,
            failing_sha=decision.failure.sha if decision.failure is not None else None,
            failing_jobs=(
                [job.label for job in decision.failure.jobs]
                if decision.failure is not None
                else None
            ),
            failing_steps=(
                {job.label: list(job.steps) for job in decision.failure.jobs if job.steps}
                if decision.failure is not None
                else None
            ),
            failing_job_fingerprint=(
                decision.failure.fingerprint_key if decision.failure is not None else None
            ),
            head_unsettled=(
                decision.failure.head_unsettled if decision.failure is not None else None
            ),
            current_head_sha=(
                decision.failure.current_head_sha if decision.failure is not None else None
            ),
        )

    release_observations: dict[str, ReleaseObservation] = {}
    remaining_details = MAX_RELEASE_PR_DETAILS
    for repo in _release_order(config):
        try:
            numbers = tuple(github.release_pr_numbers(repo))
        except CiWatchError as error:
            release_observations[repo] = ReleaseObservation(repo, None, error=_bounded(error))
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
    release_heads: dict[str, BranchHead] = {}
    for repo in _release_order(config):
        release_observation = release_observations[repo]
        settings = config.release_settings_for(repo)
        plan = None
        evaluated = False
        if release_observation.numbers is None:
            reason = release_observation.error or "release observation failed"
        elif not release_observation.numbers:
            reason = "no_release_pr"
        elif release_observation.error is not None:
            reason = release_observation.error
        elif states[repo] is not RepoState.GREEN and not settings.gating_workflows:
            reason = "default_branch_not_green"
        else:
            evaluated = True
            try:
                release_head, busy, gate_reason = _evaluate_release_branch(
                    github, settings, repo, states[repo], heads.get(repo), now
                )
                if gate_reason is not None:
                    plan, reason = None, gate_reason
                elif release_head is None:
                    plan, reason = None, "default_branch_not_green"
                else:
                    plan, reason = plan_release_merge(
                        repo,
                        RepoState.GREEN,
                        release_head.branch,
                        release_observation.prs,
                        generator_busy=busy,
                        merge_method=settings.merge_method,
                    )
                    if plan is not None:
                        release_heads[repo] = release_head
            except CiWatchError as error:
                plan, reason = None, _bounded(error)
        release_decisions[repo] = reason
        ledger_repos.setdefault(repo, {})["release_reason"] = reason
        surfaced = states[repo] is RepoState.GREEN or evaluated
        if surfaced:
            ledger_repos[repo]["reason"] = reason
        if plan is None:
            if release_observation.numbers and surfaced:
                counters["merge_skipped"] += 1
            continue
        release_plans.append(plan)

    for plan in release_plans:
        repo = plan.repo
        settings = config.release_settings_for(repo)
        if counters["merged"] >= config.max_merges:
            counters["merge_skipped"] += 1
            reason = "merge_cap_reached"
        elif not config.merge_enabled:
            counters["merge_skipped"] += 1
            reason = "merge_disabled"
        elif mode == "unavailable":
            counters["merge_skipped"] += 1
            reason = "merge_context_unavailable"
        elif mode == "dry_run":
            counters["merge_skipped"] += 1
            reason = "dry_run"
        else:
            reason = ""
        if reason:
            _mark(
                ledger_repos,
                repo,
                reason=reason,
                release_reason=reason,
                planned_pr=plan.pr.number,
            )
            release_decisions[repo] = reason
            continue
        try:
            current_head = github.default_branch_head(repo)
            stale_head = release_heads[repo]
            if current_head.sha != stale_head.sha or current_head.branch != stale_head.branch:
                current_plan = None
                current_reason = "default_branch_changed"
                current = plan.pr
            else:
                gate_reason = None
                if settings.gating_workflows:
                    evidence = github.head_ci_evidence(repo, current_head.sha)
                    gate_reason = _release_gate_reason(evidence, settings.gating_workflows)
                if gate_reason is not None:
                    current_plan, current_reason, current = None, gate_reason, plan.pr
                else:
                    runs = github.workflow_runs(repo, current_head.branch)
                    heavy_reason = (
                        _evaluate_heavy_lane(
                            repo,
                            runs,
                            settings.heavy_workflows,
                            settings.heavy_max_age_hours,
                            now,
                        )
                        if settings.heavy_workflows
                        else None
                    )
                    if heavy_reason is not None:
                        current_plan, current_reason, current = None, heavy_reason, plan.pr
                    else:
                        current = github.release_pr(repo, plan.pr.number)
                        current_plan, current_reason = plan_release_merge(
                            repo,
                            RepoState.GREEN,
                            current_head.branch,
                            [current],
                            generator_busy=_is_generator_busy(repo, runs),
                            merge_method=plan.merge_method,
                        )
            if current_plan is None or current.head_oid != plan.pr.head_oid:
                reason = (
                    "release_pr_head_changed"
                    if current.head_oid != plan.pr.head_oid
                    else current_reason
                )
                counters["merge_skipped"] += 1
                _mark(ledger_repos, repo, reason=reason, release_reason=reason)
                release_decisions[repo] = reason
                continue
            if not github.merge_method_allowed(repo, current_plan.merge_method):
                counters["merge_skipped"] += 1
                reason = "merge_method_not_allowed"
                _mark(
                    ledger_repos,
                    repo,
                    reason=reason,
                    release_reason=reason,
                    planned_pr=current_plan.pr.number,
                    merge_method=current_plan.merge_method,
                )
                release_decisions[repo] = reason
                continue
            merge_result = github.merge(current_plan)
        except CiWatchError as error:
            counters["merge_skipped"] += 1
            reason = _bounded(error)
            _mark(ledger_repos, repo, reason=reason, release_reason=reason)
            release_decisions[repo] = reason
            continue
        if merge_result.returncode != 0:
            counters["merge_skipped"] += 1
            reason = "merge_failed"
            _mark(
                ledger_repos,
                repo,
                reason=reason,
                release_reason=reason,
                merge_error=_bounded(merge_result.stderr or merge_result.stdout),
            )
            release_decisions[repo] = reason
            continue
        counters["merged"] += 1
        _append_release_record(state, repo, current, now, current_plan.merge_method)
        try:
            _write_state(invocation, state)
        except OSError as error:
            operational_errors.append(f"state write failed after merge: {_bounded(error)}")
        _mark(
            ledger_repos,
            repo,
            reason="merged",
            release_reason="merged",
            merged_pr=current.number,
            head_oid=current.head_oid,
            merge_method=current_plan.merge_method,
        )
        release_decisions[repo] = "merged"

    if mode == "live":
        _update_failure_state(state, failures, states, now)
        try:
            _write_state(invocation, state)
        except OSError as error:
            operational_errors.append(f"state write failed: {_bounded(error)}")

    report = _build_ci_watch_report(
        config,
        counters,
        states,
        heads,
        failures,
        release_observations,
        release_decisions,
        state,
        mode,
        now,
    )
    report_path: Path | None = None
    if mode == "live":
        try:
            report_path = _publish_report(invocation, report)
            counters["reports_published"] += 1
        except (OSError, TypeError, ValueError) as error:
            counters["reports_failed"] += 1
            operational_errors.append(f"report publish failed: {_bounded(error)}")

    state_write_failed = any(error.startswith("state write failed") for error in operational_errors)
    if mode == "live" and not state_write_failed:
        operational_errors.extend(
            _send_required_notifications(
                invocation=invocation,
                notifier=notifier,
                state=state,
                failures=failures,
                heads=heads,
                report_path=report_path,
                now=now,
                counters=counters,
            )
        )

    status: str = "ok" if counters["merged"] or counters["notifications_sent"] else "no_op"
    result_reason: str | None = None if status == "ok" else "no_actions"
    if operational_errors or counters["notifications_failed"] or counters["reports_failed"]:
        status = "check_error"
        result_reason = (
            "notification_failed"
            if counters["notifications_failed"]
            else "report_publish_failed"
            if counters["reports_failed"]
            else "state_write_failed"
        )
    result = result_with_summary(
        invocation,
        CHOP_NAME,
        counters,
        status=cast(Any, status),
        reason=result_reason,
        report=report,
    )
    ledger = {
        "mode": mode,
        "repositories": ledger_repos,
        "release_plans": [
            {
                "repo": plan.repo,
                "number": plan.pr.number,
                "head_oid": plan.pr.head_oid,
                "merge_method": plan.merge_method,
            }
            for plan in release_plans
        ],
        "notification_errors": operational_errors,
    }
    result.add_evidence(_write_ledger(invocation, ledger))
    return result


def main() -> None:
    run_chop(
        CHOP_NAME,
        """\
Sweep SASE CI, send durable per-incident notifications, and guard release-please
merges.

The chop never creates gates, launches agents, or emits repair proposals. It may
only query actstat and GitHub, merge an explicitly eligible release-please PR
using the configured merge method in live mode, publish the combined CI WATCH
report, and call `sase notify create` for required release or failure
notifications.
""".strip(),
        build_ci_watch_result,
    )
