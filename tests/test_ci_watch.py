from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sase.axe.chop_script_context import ChopScriptContext
from sase.chops import ChopArguments, ChopInvocation, ChopLogger, validate_chop_report
from sase.core.axe_chop_facade import validate_chop_result

import bugyi_chops.ci_watch as ci_watch_module
from bugyi_chops.ci_watch import (
    LEGACY_RELEASE_LEDGER_FILE_NAME,
    REPORT_FILE_NAME,
    STATE_FILE_NAME,
    ActstatClient,
    BranchHead,
    CiWatchError,
    CommandResult,
    Config,
    FailingJobEvidence,
    GitHubReader,
    HeadCiEvidence,
    MergePlan,
    ReleasePr,
    ReleaseSettings,
    RepoObservation,
    RepoState,
    SaseNotifier,
    actionably_red,
    build_ci_watch_result,
    classify_repo,
    decide_repo,
    main,
    plan_release_merge,
    run_command,
)

REPO = "sase-org/sase"
CORE = "sase-org/sase-core"
TELEGRAM = "sase-org/sase-telegram"
SHA = "a" * 40
CORE_SHA = "b" * 40
TELEGRAM_SHA = "c" * 40
FIXED_NOW = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clear_dry_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SASE_CHOP_DRY_RUN", raising=False)


def _clock_at(value: datetime = FIXED_NOW) -> Callable[[], datetime]:
    return lambda: value


def _job(
    name: str = "test token=do-not-leak",
    *,
    conclusion: str = "failure",
    steps: Sequence[str] = ("Run tests",),
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": conclusion,
        "html_url": url or f"https://github.com/{REPO}/actions/runs/12/job/34",
        "steps": [{"name": step, "conclusion": "failure"} for step in steps],
    }


def _commit(
    repo: str,
    sha: str,
    conclusion: str = "success",
    *,
    run_conclusion: str | None = None,
    jobs: Sequence[dict[str, Any]] = (),
    workflow: str = "CI",
) -> dict[str, Any]:
    return {
        "type": "commit",
        "repo": repo,
        "sha": sha[:7],
        "branch": "master",
        "conclusion": conclusion,
        "runs": [
            {
                "name": workflow,
                "workflow_name": workflow,
                "conclusion": run_conclusion or conclusion,
                "url": f"https://github.com/{repo}/actions/runs/12",
                "jobs": list(jobs),
            }
        ],
    }


def _run(
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    created_at: str = "2026-08-21T10:00:00Z",
    updated_at: str = "2026-08-21T10:05:00Z",
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _failure_job(
    name: str = "test token=do-not-leak",
    *,
    workflow: str = "CI",
    conclusion: str = "failure",
    steps: Sequence[str] = ("Run tests",),
    url: str | None = None,
) -> FailingJobEvidence:
    return FailingJobEvidence(
        workflow=workflow,
        job=ci_watch_module._bounded(name, limit=120),
        conclusion=conclusion,
        url=url or f"https://github.com/{REPO}/actions/runs/99/job/1",
        steps=tuple(steps),
    )


def _head_evidence(
    sha: str,
    *,
    in_flight: bool = False,
    green: bool = False,
    failing_jobs: Sequence[FailingJobEvidence] = (),
    successful_jobs: Sequence[str] = (),
    observed_workflows: Sequence[str] = (),
    in_flight_workflows: Sequence[str] = (),
) -> HeadCiEvidence:
    return HeadCiEvidence(
        sha=sha,
        has_in_flight=in_flight,
        all_completed_green=green,
        failing_jobs=tuple(failing_jobs),
        successful_jobs=tuple(successful_jobs),
        observed_workflows=tuple(observed_workflows),
        in_flight_workflows=tuple(in_flight_workflows),
    )


def _observations(
    *,
    red: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> dict[str, RepoObservation]:
    result: dict[str, RepoObservation] = {}
    for repo, sha in ((REPO, SHA), (CORE, CORE_SHA), (TELEGRAM, TELEGRAM_SHA)):
        failed = repo in red
        result[repo] = RepoObservation(
            repo,
            commit=_commit(
                repo,
                sha,
                "failure" if failed else "success",
                jobs=[_job(url=f"https://github.com/{repo}/actions/runs/12/job/34")]
                if failed
                else [],
            ),
            error="forbidden" if repo in errors else None,
        )
    return result


def _pr(
    number: int = 10,
    *,
    repo: str = REPO,
    draft: bool = False,
    head: str | None = None,
    base: str = "master",
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    checks: tuple[tuple[str, str], ...] = (("COMPLETED", "SUCCESS"),),
    head_ref: str = "release-please--branches--master",
    title: str = "chore(main): release 1.2.3",
    created_at: str = "2026-08-21T15:00:00Z",
) -> ReleasePr:
    return ReleasePr(
        number=number,
        head_ref_name=head_ref,
        base_ref_name=base,
        head_oid=head or {REPO: SHA, CORE: CORE_SHA, TELEGRAM: TELEGRAM_SHA}[repo],
        is_draft=draft,
        mergeable=mergeable,
        merge_state_status=merge_state,
        checks=checks,
        url=f"https://github.com/{repo}/pull/{number}",
        title=title,
        created_at=created_at,
    )


class FakeActstat:
    def __init__(self, observations: dict[str, RepoObservation]) -> None:
        self.observations = observations
        self.calls = 0

    def sweep(self, repos: Sequence[str]) -> dict[str, RepoObservation]:
        self.calls += 1
        return {repo: self.observations[repo] for repo in repos}


class FakeGitHub:
    def __init__(self) -> None:
        self.heads: dict[str, BranchHead | Exception] = {
            REPO: BranchHead("master", SHA),
            CORE: BranchHead("master", CORE_SHA),
            TELEGRAM: BranchHead("master", TELEGRAM_SHA),
        }
        self.workflow_counts: dict[str, int | Exception] = {REPO: 1, CORE: 1, TELEGRAM: 1}
        self.head_evidence: dict[str, HeadCiEvidence | Exception] = {}
        self.head_evidence_calls: list[tuple[str, str]] = []
        self.numbers: dict[str, list[int] | Exception] = {REPO: [], CORE: [], TELEGRAM: []}
        self.prs: dict[tuple[str, int], list[ReleasePr]] = {}
        self.branch_runs: dict[str, list[dict[str, Any]] | Exception] = {
            REPO: [],
            CORE: [],
            TELEGRAM: [],
        }
        self.branch_run_calls: list[tuple[str, str]] = []
        self.merge_results: list[CommandResult] = []
        self.merges: list[MergePlan] = []
        self.allowed_merge_methods: dict[str, set[str] | Exception] = {
            REPO: {"merge", "squash", "rebase"},
            CORE: {"merge", "squash", "rebase"},
            TELEGRAM: {"merge", "squash", "rebase"},
        }
        self.merge_method_allowed_calls: list[tuple[str, str]] = []

    def default_branch_head(self, repo: str) -> BranchHead:
        value = self.heads[repo]
        if isinstance(value, Exception):
            raise value
        return value

    def workflow_count(self, repo: str) -> int:
        value = self.workflow_counts[repo]
        if isinstance(value, Exception):
            raise value
        return value

    def head_ci_evidence(self, repo: str, sha: str) -> HeadCiEvidence:
        self.head_evidence_calls.append((repo, sha))
        value = self.head_evidence[repo]
        if isinstance(value, Exception):
            raise value
        return value

    def workflow_runs(self, repo: str, branch: str) -> list[dict[str, Any]]:
        assert branch == "master"
        self.branch_run_calls.append((repo, branch))
        value = self.branch_runs[repo]
        if isinstance(value, Exception):
            raise value
        return list(value)

    def release_pr_numbers(self, repo: str) -> list[int]:
        value = self.numbers[repo]
        if isinstance(value, Exception):
            raise value
        return list(value)

    def release_pr(self, repo: str, number: int) -> ReleasePr:
        values = self.prs[(repo, number)]
        return values.pop(0) if len(values) > 1 else values[0]

    def merge(self, plan: MergePlan) -> CommandResult:
        self.merges.append(plan)
        return self.merge_results.pop(0) if self.merge_results else CommandResult(0)

    def merge_method_allowed(self, repo: str, merge_method: str) -> bool:
        self.merge_method_allowed_calls.append((repo, merge_method))
        value = self.allowed_merge_methods[repo]
        if isinstance(value, Exception):
            raise value
        return merge_method in value


class FakeNotifier:
    def __init__(self, *, fail_count: int = 0) -> None:
        self.fail_count = fail_count
        self.notifications: list[dict[str, Any]] = []

    def notify(
        self,
        notes: Sequence[str],
        *,
        icon: str,
        tags: Sequence[str],
        action: str | None = None,
        action_data: Mapping[str, str] | None = None,
    ) -> None:
        self.notifications.append(
            {
                "notes": list(notes),
                "icon": icon,
                "tags": list(tags),
                "action": action,
                "action_data": dict(action_data) if action_data is not None else None,
            }
        )
        if self.fail_count > 0:
            self.fail_count -= 1
            raise CiWatchError("notification failed: synthetic")


def _vars(
    *,
    repos: Sequence[str] = (REPO, CORE, TELEGRAM),
    releases: Sequence[str] = (),
    merge_order: Sequence[str] | None = None,
    merge_enabled: bool = False,
    max_merges: int = 1,
) -> dict[str, Any]:
    return {
        "actstat_bin": "/fake/actstat",
        "gh_bin": "/fake/gh",
        "sase_bin": "/fake/sase",
        "repos": list(repos),
        "release_repositories": list(releases),
        "merge_order": list(merge_order if merge_order is not None else releases),
        "max_merges_per_tick": max_merges,
        "merge_enabled": merge_enabled,
    }


def _invocation(
    tmp_path: Path,
    variables: dict[str, Any],
    *,
    result_file: str = "result.json",
) -> ChopInvocation:
    context = ChopScriptContext(
        max_hook_runners=1,
        max_agent_runners=1,
        zombie_timeout_seconds=60,
        query="",
        lumberjack_name="test",
        state_dir=str(tmp_path),
        all_patches_file=str(tmp_path / "all.json"),
        filtered_patches_file=str(tmp_path / "filtered.json"),
        result_file=str(tmp_path / result_file),
        vars=variables,
    )
    return ChopInvocation(ChopArguments("context.json", False), context, ChopLogger())


def _build(
    tmp_path: Path,
    observations: dict[str, RepoObservation],
    *,
    github: FakeGitHub | None = None,
    notifier: FakeNotifier | None = None,
    variables: dict[str, Any] | None = None,
    result_file: str = "result.json",
    clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeGitHub, FakeNotifier]:
    github = github or FakeGitHub()
    notifier = notifier or FakeNotifier()
    result = build_ci_watch_result(
        _invocation(tmp_path, variables or _vars(), result_file=result_file),
        actstat=FakeActstat(observations),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        **({"clock": clock} if clock is not None else {}),
    ).to_dict()
    evidence = result["evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) == 1
    ledger = json.loads((tmp_path / cast(str, evidence[0])).read_text(encoding="utf-8"))
    return result, ledger, github, notifier


def _state(tmp_path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((tmp_path / STATE_FILE_NAME).read_text()))


def _report(tmp_path: Path) -> dict[str, Any]:
    report = cast(dict[str, Any], json.loads((tmp_path / REPORT_FILE_NAME).read_text()))
    assert validate_chop_report(report) == report
    return report


def _rows(report: dict[str, Any], columns: list[str]) -> list[dict[str, Any]]:
    block = next(
        block
        for block in report["blocks"]
        if block.get("kind") == "rows" and block.get("columns") == columns
    )
    return cast(list[dict[str, Any]], block["rows"])


def test_all_green_without_live_context_is_noop_and_does_not_publish_state(
    tmp_path: Path,
) -> None:
    result, ledger, _, notifier = _build(tmp_path, _observations(), clock=_clock_at())

    assert result["status"] == "no_op"
    assert result["reason"] == "no_actions"
    assert result["proposed_launches"] == []
    assert result["counters"]["green"] == 3
    assert result["counters"]["notifications_sent"] == 0
    assert ledger["mode"] == "unavailable"
    assert ledger["repositories"][REPO]["reason"] == "green"
    assert notifier.notifications == []
    assert not (tmp_path / STATE_FILE_NAME).exists()
    assert not (tmp_path / REPORT_FILE_NAME).exists()
    validate_chop_result(result)


def test_live_red_repository_sends_actionable_notification_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    result, ledger, _, notifier = _build(
        tmp_path,
        _observations(red=(REPO,)),
        clock=_clock_at(),
    )

    assert result["status"] == "ok"
    assert result["counters"]["red"] == 1
    assert result["counters"]["notifications_attempted"] == 1
    assert result["counters"]["notifications_sent"] == 1
    assert result["counters"]["reports_published"] == 1
    assert "token=do-not-leak" not in json.dumps(ledger)
    row = ledger["repositories"][REPO]
    assert row["failing_sha"] == SHA
    assert row["failing_steps"] == {"CI › test [redacted] — failure": ["Run tests"]}

    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification["icon"] == "🚨"
    assert notification["tags"] == ["ci", "failure"]
    assert notification["action"] == "ViewReport"
    assert notification["action_data"]["report_path"] == str(
        (tmp_path / REPORT_FILE_NAME).resolve()
    )
    assert notification["notes"][0] == f"CI failure: {REPO} master@{SHA[:12]}"
    assert "CI › test [redacted] — failure" in notification["notes"]
    assert "Steps: Run tests" in notification["notes"]
    assert "token=do-not-leak" not in json.dumps(notification)

    state = _state(tmp_path)
    assert state["failures"][REPO]["notification_sent"] is True
    report = _report(tmp_path)
    failure_rows = _rows(report, ["REPOSITORY", "SHA", "JOB", "STEPS", "URL"])
    assert any(
        row["cells"][0] == REPO and row["cells"][2] == "CI › test [redacted] — failure"
        for row in failure_rows
    )
    validate_chop_result(result)


def test_multiple_red_repositories_and_jobs_each_notify_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    observations = _observations(red=(REPO, CORE))
    observations[REPO] = RepoObservation(
        REPO,
        commit=_commit(
            REPO,
            SHA,
            "failure",
            jobs=[
                _job("lint", steps=("ruff",)),
                _job(
                    "test",
                    steps=("pytest",),
                    url=f"https://github.com/{REPO}/actions/runs/12/job/35",
                ),
            ],
        ),
    )

    result, _, _, notifier = _build(tmp_path, observations, clock=_clock_at())

    assert result["counters"]["red"] == 2
    assert result["counters"]["notifications_sent"] == 2
    assert [item["tags"] for item in notifier.notifications] == [
        ["ci", "failure"],
        ["ci", "failure"],
    ]
    first = notifier.notifications[0]
    assert "CI › lint — failure" in first["notes"]
    assert "CI › test — failure" in first["notes"]
    assert "Steps: ruff" in first["notes"]
    assert "Steps: pytest" in first["notes"]


def test_failure_incident_dedupes_changes_resets_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    failing = _observations(red=(REPO,))
    notifier = FakeNotifier()
    _build(tmp_path, failing, notifier=notifier, result_file="first.json", clock=_clock_at())
    _build(
        tmp_path,
        failing,
        notifier=notifier,
        result_file="same.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=5)),
    )
    assert len(notifier.notifications) == 1

    changed = _observations(red=(REPO,))
    changed[REPO] = RepoObservation(
        REPO,
        commit=_commit(REPO, SHA, "failure", jobs=[_job("test", steps=("different",))]),
    )
    _build(
        tmp_path,
        changed,
        notifier=notifier,
        result_file="changed.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=10)),
    )
    assert len(notifier.notifications) == 2

    _build(
        tmp_path,
        _observations(),
        notifier=notifier,
        result_file="green.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=15)),
    )
    assert _state(tmp_path)["failures"] == {}
    _build(
        tmp_path,
        failing,
        notifier=notifier,
        result_file="recurs.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=20)),
    )
    assert len(notifier.notifications) == 3

    retry_path = tmp_path / "retry"
    retry_notifier = FakeNotifier(fail_count=1)
    failed, _, _, retry_notifier = _build(
        retry_path,
        failing,
        notifier=retry_notifier,
        result_file="fail.json",
        clock=_clock_at(),
    )
    assert failed["status"] == "check_error"
    assert failed["reason"] == "notification_failed"
    assert _state(retry_path)["failures"][REPO]["notification_sent"] is False
    ok, _, _, retry_notifier = _build(
        retry_path,
        failing,
        notifier=retry_notifier,
        result_file="retry.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=5)),
    )
    assert ok["status"] == "ok"
    assert len(retry_notifier.notifications) == 2
    assert _state(retry_path)["failures"][REPO]["notification_sent"] is True


def test_older_settled_failure_remains_visible_while_head_is_unsettled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    new_head = "d" * 40
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_head)
    github.head_evidence[REPO] = _head_evidence(new_head, in_flight=True)

    result, ledger, _, notifier = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
        clock=_clock_at(),
    )

    assert result["counters"]["red"] == 1
    row = ledger["repositories"][REPO]
    assert row["classification_reason"] == "head_unsettled"
    assert row["failing_sha"] == SHA[:7]
    assert row["current_head_sha"] == new_head
    notes = notifier.notifications[0]["notes"]
    assert any(SHA[:7] in note and new_head[:12] in note for note in notes)


def test_newer_head_failure_takes_precedence_and_success_can_supersede() -> None:
    newer = "d" * 40
    head = BranchHead("master", newer)
    older = RepoObservation(REPO, commit=_commit(REPO, SHA, "failure", jobs=[_job("test")]))

    red = decide_repo(
        older,
        head,
        head_evidence=_head_evidence(newer, failing_jobs=(_failure_job("new failing job"),)),
    )
    assert red.state is RepoState.RED
    assert red.failure is not None
    assert red.failure.sha == newer
    assert red.failure.jobs[0].job == "new failing job"

    green = decide_repo(
        older,
        head,
        head_evidence=_head_evidence(newer, in_flight=True, successful_jobs=("test",)),
    )
    assert (green.state, green.reason) == (RepoState.GREEN, "superseded_by_newer_success")

    pending = decide_repo(older, head, head_evidence=_head_evidence(newer))
    assert (pending.state, pending.reason) == (RepoState.PENDING, "superseded_or_unsettled")


def test_dry_run_and_missing_live_context_never_merge_notify_or_write_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]
    variables = _vars(releases=(REPO,), merge_enabled=True)

    missing, missing_ledger, github, missing_notifier = _build(
        tmp_path / "missing",
        _observations(red=(CORE,)),
        github=github,
        variables=variables,
        clock=_clock_at(),
    )
    assert missing["counters"]["merged"] == 0
    assert missing_ledger["repositories"][REPO]["reason"] == "merge_context_unavailable"
    assert github.merges == []
    assert missing_notifier.notifications == []
    assert not (tmp_path / "missing" / STATE_FILE_NAME).exists()

    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "1")
    dry_github = FakeGitHub()
    dry_github.numbers[REPO] = [10]
    dry_github.prs[(REPO, 10)] = [_pr()]
    dry, dry_ledger, dry_github, dry_notifier = _build(
        tmp_path / "dry",
        _observations(red=(CORE,)),
        github=dry_github,
        variables=variables,
        clock=_clock_at(),
    )
    assert dry["counters"]["merged"] == 0
    assert dry_ledger["repositories"][REPO]["reason"] == "dry_run"
    assert dry_github.merges == []
    assert dry_notifier.notifications == []
    assert not (tmp_path / "dry" / STATE_FILE_NAME).exists()


@pytest.mark.parametrize(
    ("candidates", "state", "busy", "reason"),
    [
        ([], RepoState.GREEN, False, "no_release_pr"),
        ([_pr(), _pr(11)], RepoState.GREEN, False, "ambiguous_release_prs"),
        ([_pr(head_ref="release-plz-2026-08-21")], RepoState.GREEN, False, "not_release_pr"),
        ([_pr(head_ref="release-please/main/sase")], RepoState.GREEN, False, "eligible"),
        ([_pr(draft=True)], RepoState.GREEN, False, "release_pr_draft"),
        ([_pr(base="main")], RepoState.GREEN, False, "release_pr_wrong_base"),
        ([_pr(mergeable="UNKNOWN")], RepoState.GREEN, False, "release_pr_not_mergeable"),
        ([_pr(merge_state="DIRTY")], RepoState.GREEN, False, "release_pr_not_clean"),
        ([_pr(checks=())], RepoState.GREEN, False, "release_pr_empty_rollup"),
        (
            [_pr(checks=(("IN_PROGRESS", ""),))],
            RepoState.GREEN,
            False,
            "release_pr_checks_not_green",
        ),
        ([_pr()], RepoState.GREEN, True, "release_generator_busy"),
        ([_pr()], RepoState.RED, False, "default_branch_not_green"),
    ],
)
def test_release_merge_guard_matrix(
    candidates: list[ReleasePr],
    state: RepoState,
    busy: bool,
    reason: str,
) -> None:
    plan, actual = plan_release_merge(REPO, state, "master", candidates, generator_busy=busy)
    assert actual == reason
    assert (plan is not None) is (reason == "eligible")


def test_release_please_live_merge_uses_dependency_order_cap_and_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.numbers[TELEGRAM] = [20]
    github.prs[(REPO, 10)] = [_pr()]
    github.prs[(TELEGRAM, 20)] = [_pr(20, repo=TELEGRAM)]
    variables = _vars(
        releases=(REPO, TELEGRAM),
        merge_order=("sase-telegram", "sase"),
        merge_enabled=True,
        max_merges=1,
    )

    result, ledger, github, notifier = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=variables,
        clock=_clock_at(),
    )

    assert result["counters"]["release_candidates"] == 2
    assert result["counters"]["merged"] == 1
    assert result["counters"]["merge_skipped"] == 1
    assert [plan.repo for plan in github.merges] == [TELEGRAM]
    assert ledger["repositories"][TELEGRAM]["reason"] == "merged"
    assert ledger["repositories"][REPO]["reason"] == "merge_cap_reached"
    assert notifier.notifications[0]["icon"] == "🚢"
    assert notifier.notifications[0]["tags"] == ["ci", "release"]
    assert f"Release submitted: {TELEGRAM} #20 1.2.3" in notifier.notifications[0]["notes"]
    state = _state(tmp_path)
    assert state["releases"][0]["repo"] == TELEGRAM
    assert state["releases"][0]["notification_sent"] is True


def test_one_tick_uses_per_repository_release_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.numbers[TELEGRAM] = [20]
    github.prs[(REPO, 10)] = [_pr()]
    github.prs[(TELEGRAM, 20)] = [_pr(20, repo=TELEGRAM)]
    github.head_evidence[REPO] = _head_evidence(SHA, observed_workflows=("Master Gate",))
    github.head_evidence[TELEGRAM] = _head_evidence(
        TELEGRAM_SHA, observed_workflows=("Plugin Gate",)
    )
    github.branch_runs[REPO] = [
        _run(
            "Full CI",
            created_at="2026-08-21T14:00:00Z",
            updated_at="2026-08-21T15:00:00Z",
        )
    ]
    github.branch_runs[TELEGRAM] = [
        _run(
            "Smoke",
            created_at="2026-08-21T15:00:00Z",
            updated_at="2026-08-21T15:30:00Z",
        )
    ]
    variables = _vars(
        releases=(REPO, TELEGRAM),
        merge_order=("sase-telegram", "sase"),
        merge_enabled=True,
        max_merges=2,
    )
    variables.update(
        {
            "merge_method": {REPO: "merge", TELEGRAM: "squash"},
            "gating_workflows": {REPO: ["Master Gate"], TELEGRAM: ["Plugin Gate"]},
            "heavy_workflows": {REPO: ["Full CI"], TELEGRAM: ["Smoke"]},
            "heavy_max_age_hours": {REPO: 6, TELEGRAM: 1},
        }
    )

    result, ledger, github, _ = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=variables,
        clock=_clock_at(),
    )

    assert result["counters"]["merged"] == 2
    assert [(plan.repo, plan.merge_method) for plan in github.merges] == [
        (TELEGRAM, "squash"),
        (REPO, "merge"),
    ]
    assert github.merge_method_allowed_calls == [(TELEGRAM, "squash"), (REPO, "merge")]
    assert set(github.head_evidence_calls) == {(REPO, SHA), (TELEGRAM, TELEGRAM_SHA)}
    assert ledger["release_plans"] == [
        {"repo": TELEGRAM, "number": 20, "head_oid": TELEGRAM_SHA, "merge_method": "squash"},
        {"repo": REPO, "number": 10, "head_oid": SHA, "merge_method": "merge"},
    ]
    state = _state(tmp_path)
    assert [row["outcome"] for row in state["releases"]] == [
        "squash_merge_submitted",
        "merge_merge_submitted",
    ]


def test_simultaneous_merge_and_failure_send_both_notification_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]

    result, _, _, notifier = _build(
        tmp_path,
        _observations(red=(CORE,)),
        github=github,
        variables=_vars(releases=(REPO,), merge_enabled=True),
        clock=_clock_at(),
    )

    assert result["counters"]["merged"] == 1
    assert result["counters"]["red"] == 1
    assert [item["tags"] for item in notifier.notifications] == [
        ["ci", "release"],
        ["ci", "failure"],
    ]


def test_live_merge_reread_and_command_failures_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "false")
    variables = _vars(releases=(REPO,), merge_enabled=True)

    changed_pr = FakeGitHub()
    changed_pr.numbers[REPO] = [10]
    changed_pr.prs[(REPO, 10)] = [_pr(), _pr(head="d" * 40)]
    _, ledger, changed_pr, _ = _build(
        tmp_path / "pr",
        _observations(),
        github=changed_pr,
        variables=variables,
    )
    assert changed_pr.merges == []
    assert ledger["repositories"][REPO]["reason"] == "release_pr_head_changed"

    class FlippingHeadGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.head_calls = 0

        def default_branch_head(self, repo: str) -> BranchHead:
            if repo == REPO:
                self.head_calls += 1
                if self.head_calls == 1:
                    return BranchHead("master", SHA)
                return BranchHead("master", "d" * 40)
            return super().default_branch_head(repo)

    changed_branch = FlippingHeadGitHub()
    changed_branch.numbers[REPO] = [10]
    changed_branch.prs[(REPO, 10)] = [_pr()]
    changed_branch.heads[REPO] = BranchHead("master", "d" * 40)
    _, ledger, _, _ = _build(
        tmp_path / "branch",
        _observations(),
        github=changed_branch,
        variables=variables,
    )
    assert changed_branch.merges == []
    assert ledger["repositories"][REPO]["reason"] == "default_branch_changed"

    failed = FakeGitHub()
    failed.numbers[REPO] = [10]
    failed.prs[(REPO, 10)] = [_pr()]
    failed.merge_results = [CommandResult(1, stderr="head conflict")]
    result, ledger, failed, _ = _build(
        tmp_path / "failed",
        _observations(),
        github=failed,
        variables=variables,
    )
    assert result["counters"]["merge_skipped"] == 1
    assert len(failed.merges) == 1
    assert ledger["repositories"][REPO]["reason"] == "merge_failed"
    assert ledger["repositories"][REPO]["merge_error"] == "head conflict"


def test_disallowed_repository_merge_method_skips_without_merge_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]
    github.allowed_merge_methods[REPO] = {"merge"}
    variables = _vars(releases=(REPO,), merge_enabled=True)
    variables["merge_method"] = "squash"

    result, ledger, github, _ = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=variables,
    )

    assert result["counters"]["merged"] == 0
    assert result["counters"]["merge_skipped"] == 1
    assert github.merge_method_allowed_calls == [(REPO, "squash")]
    assert github.merges == []
    assert ledger["repositories"][REPO]["release_reason"] == "merge_method_not_allowed"


def test_empty_gate_allowlists_preserve_default_branch_green_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]

    result, ledger, github, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
        variables=_vars(releases=(REPO,), merge_enabled=True),
        clock=_clock_at(),
    )

    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "default_branch_not_green"
    assert github.merges == []
    assert github.head_evidence_calls == []


def test_gating_workflows_gate_release_independent_of_actstat_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    variables = _vars(releases=(REPO,), merge_enabled=True)
    variables["gating_workflows"] = ["Fast"]

    green_gate = FakeGitHub()
    green_gate.numbers[REPO] = [10]
    green_gate.prs[(REPO, 10)] = [_pr()]
    green_gate.head_evidence[REPO] = _head_evidence(SHA, observed_workflows=("Fast",))
    result, ledger, green_gate, _ = _build(
        tmp_path / "red-actstat-green-gate",
        _observations(red=(REPO,)),
        github=green_gate,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 1
    assert ledger["repositories"][REPO]["release_reason"] == "merged"
    assert (REPO, SHA) in green_gate.head_evidence_calls

    missing = FakeGitHub()
    missing.numbers[REPO] = [10]
    missing.prs[(REPO, 10)] = [_pr()]
    missing.head_evidence[REPO] = _head_evidence(SHA, observed_workflows=("Other",))
    result, ledger, _, _ = _build(
        tmp_path / "missing",
        _observations(),
        github=missing,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert result["counters"]["merge_skipped"] == 1
    assert ledger["repositories"][REPO]["release_reason"] == "gating_workflow_missing"

    in_flight = FakeGitHub()
    in_flight.numbers[REPO] = [10]
    in_flight.prs[(REPO, 10)] = [_pr()]
    in_flight.head_evidence[REPO] = _head_evidence(
        SHA, observed_workflows=("Fast",), in_flight_workflows=("Fast",)
    )
    result, ledger, _, _ = _build(
        tmp_path / "in-flight",
        _observations(),
        github=in_flight,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "gating_workflow_in_flight"

    red_gate = FakeGitHub()
    red_gate.numbers[REPO] = [10]
    red_gate.prs[(REPO, 10)] = [_pr()]
    red_gate.head_evidence[REPO] = _head_evidence(
        SHA,
        observed_workflows=("Fast",),
        failing_jobs=(_failure_job("lint", workflow="Fast"),),
    )
    result, ledger, _, _ = _build(
        tmp_path / "red-gate",
        _observations(),
        github=red_gate,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "gating_workflow_red"


def test_heavy_lane_requires_a_recent_green_run_and_reuses_one_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    variables = _vars(releases=(REPO,), merge_enabled=True)
    variables["heavy_workflows"] = ["E2E"]
    variables["heavy_max_age_hours"] = 6

    fresh = FakeGitHub()
    fresh.numbers[REPO] = [10]
    fresh.prs[(REPO, 10)] = [_pr()]
    fresh.branch_runs[REPO] = [
        _run("E2E", created_at="2026-08-21T14:00:00Z", updated_at="2026-08-21T15:00:00Z")
    ]
    result, ledger, fresh, _ = _build(
        tmp_path / "fresh",
        _observations(),
        github=fresh,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 1
    assert ledger["repositories"][REPO]["release_reason"] == "merged"
    # One branch-run query drives both the generator-busy and heavy-lane checks per
    # decision: the initial plan and the pre-merge reread.
    assert fresh.branch_run_calls == [(REPO, "master"), (REPO, "master")]

    stale = FakeGitHub()
    stale.numbers[REPO] = [10]
    stale.prs[(REPO, 10)] = [_pr()]
    stale.branch_runs[REPO] = [
        _run("E2E", created_at="2026-08-21T08:00:00Z", updated_at="2026-08-21T08:00:00Z")
    ]
    result, ledger, _, _ = _build(
        tmp_path / "stale",
        _observations(),
        github=stale,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "heavy_lane_stale"

    missing = FakeGitHub()
    missing.numbers[REPO] = [10]
    missing.prs[(REPO, 10)] = [_pr()]
    result, ledger, _, _ = _build(
        tmp_path / "missing",
        _observations(),
        github=missing,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "heavy_lane_not_green"

    red = FakeGitHub()
    red.numbers[REPO] = [10]
    red.prs[(REPO, 10)] = [_pr()]
    red.branch_runs[REPO] = [
        _run(
            "E2E",
            conclusion="failure",
            created_at="2026-08-21T14:00:00Z",
            updated_at="2026-08-21T15:00:00Z",
        )
    ]
    result, ledger, _, _ = _build(
        tmp_path / "red",
        _observations(),
        github=red,
        variables=variables,
        clock=_clock_at(),
    )
    assert result["counters"]["merged"] == 0
    assert ledger["repositories"][REPO]["release_reason"] == "heavy_lane_not_green"


@pytest.mark.parametrize(
    ("merge_method", "flag"),
    [("merge", "--merge"), ("squash", "--squash"), ("rebase", "--rebase")],
)
def test_configured_merge_method_threads_through_plan_and_gh_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    merge_method: str,
    flag: str,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]
    variables = _vars(releases=(REPO,), merge_enabled=True)
    variables["merge_method"] = merge_method

    result, _, github, _ = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=variables,
        clock=_clock_at(),
    )

    assert result["counters"]["merged"] == 1
    assert len(github.merges) == 1
    assert github.merges[0].merge_method == merge_method
    state = _state(tmp_path)
    assert state["releases"][0]["outcome"] == f"{merge_method}_merge_submitted"

    runner = QueueRunner(CommandResult(0))
    real_github = GitHubReader("/gh", runner)
    pr = _pr()
    assert real_github.merge(MergePlan(REPO, pr, merge_method)).returncode == 0
    assert runner.calls[-1][0] == [
        "/gh",
        "pr",
        "merge",
        str(pr.number),
        "--repo",
        REPO,
        flag,
        "--match-head-commit",
        pr.head_oid,
    ]


def test_release_gate_reason_distinguishes_missing_in_flight_and_red() -> None:
    missing = _head_evidence(SHA, observed_workflows=("CI",))
    assert (
        ci_watch_module._release_gate_reason(missing, ("CI", "Fast")) == "gating_workflow_missing"
    )

    in_flight = _head_evidence(
        SHA, observed_workflows=("CI", "Fast"), in_flight_workflows=("Fast",)
    )
    assert (
        ci_watch_module._release_gate_reason(in_flight, ("CI", "Fast"))
        == "gating_workflow_in_flight"
    )

    red = _head_evidence(
        SHA,
        observed_workflows=("CI", "Fast"),
        failing_jobs=(_failure_job("lint", workflow="Fast"),),
    )
    assert ci_watch_module._release_gate_reason(red, ("CI", "Fast")) == "gating_workflow_red"

    clean = _head_evidence(SHA, observed_workflows=("CI", "Fast"))
    assert ci_watch_module._release_gate_reason(clean, ("CI", "Fast")) is None
    assert ci_watch_module._release_gate_reason(clean, ()) is None


def test_evaluate_heavy_lane_freshness_and_conclusion_gates() -> None:
    now = FIXED_NOW
    fresh_green = [_run("E2E", updated_at="2026-08-21T15:00:00Z")]
    assert ci_watch_module._evaluate_heavy_lane(REPO, fresh_green, ("E2E",), 6, now) is None

    stale_green = [_run("E2E", updated_at="2026-08-21T08:00:00Z")]
    assert (
        ci_watch_module._evaluate_heavy_lane(REPO, stale_green, ("E2E",), 6, now)
        == "heavy_lane_stale"
    )

    red = [_run("E2E", conclusion="failure", updated_at="2026-08-21T15:00:00Z")]
    assert (
        ci_watch_module._evaluate_heavy_lane(REPO, red, ("E2E",), 6, now) == "heavy_lane_not_green"
    )

    assert (
        ci_watch_module._evaluate_heavy_lane(REPO, [], ("E2E",), 6, now) == "heavy_lane_not_green"
    )

    mixed = [
        _run(
            "E2E",
            conclusion="failure",
            created_at="2026-08-21T09:00:00Z",
            updated_at="2026-08-21T09:05:00Z",
        ),
        _run("E2E", created_at="2026-08-21T14:00:00Z", updated_at="2026-08-21T15:30:00Z"),
        {
            "name": "E2E",
            "status": "in_progress",
            "conclusion": None,
            "created_at": "2026-08-21T15:50:00Z",
        },
    ]
    assert ci_watch_module._evaluate_heavy_lane(REPO, mixed, ("E2E",), 6, now) is None

    with pytest.raises(CiWatchError, match="missing or invalid 'updated_at'"):
        ci_watch_module._evaluate_heavy_lane(
            REPO,
            [{"name": "E2E", "status": "completed", "conclusion": "success"}],
            ("E2E",),
            6,
            now,
        )

    with pytest.raises(CiWatchError, match="invalid completion timestamp"):
        ci_watch_module._evaluate_heavy_lane(
            REPO,
            [_run("E2E", updated_at="not-a-timestamp")],
            ("E2E",),
            6,
            now,
        )


def test_release_gate_config_validation_and_defaults(tmp_path: Path) -> None:
    with pytest.raises(CiWatchError, match="merge_method must be one of"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "merge_method": "fast-forward"}))
    with pytest.raises(CiWatchError, match="gating_workflows must be a list"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "gating_workflows": "CI"}))
    with pytest.raises(CiWatchError, match="heavy_workflows must be a list"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "heavy_workflows": [1]}))
    with pytest.raises(CiWatchError, match="heavy_max_age_hours must be a positive number"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "heavy_max_age_hours": 0}))
    with pytest.raises(CiWatchError, match="heavy_max_age_hours must be a positive number"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "heavy_max_age_hours": True}))

    defaults = Config.from_invocation(_invocation(tmp_path, _vars()))
    assert defaults.merge_method == "merge"
    assert defaults.gating_workflows == ()
    assert defaults.heavy_workflows == ()
    assert defaults.heavy_max_age_hours == 6.0

    configured = Config.from_invocation(
        _invocation(
            tmp_path,
            {
                **_vars(),
                "merge_method": "rebase",
                "gating_workflows": ["CI", "Lint"],
                "heavy_workflows": ["E2E"],
                "heavy_max_age_hours": 12,
            },
        )
    )
    assert configured.merge_method == "rebase"
    assert configured.gating_workflows == ("CI", "Lint")
    assert configured.heavy_workflows == ("E2E",)
    assert configured.heavy_max_age_hours == 12.0


def test_flat_release_settings_apply_to_every_release_repository(tmp_path: Path) -> None:
    variables = _vars(releases=(REPO, TELEGRAM))
    variables.update(
        {
            "merge_method": "squash",
            "gating_workflows": ["CI", "Lint"],
            "heavy_workflows": ["E2E"],
            "heavy_max_age_hours": 12,
        }
    )

    config = Config.from_invocation(_invocation(tmp_path, variables))

    expected = ReleaseSettings(
        merge_method="squash",
        gating_workflows=("CI", "Lint"),
        heavy_workflows=("E2E",),
        heavy_max_age_hours=12.0,
    )
    assert config.default_release_settings == expected
    assert config.merge_method == "squash"
    assert config.gating_workflows == ("CI", "Lint")
    assert config.heavy_workflows == ("E2E",)
    assert config.heavy_max_age_hours == 12.0
    assert config.release_settings_for(REPO) == expected
    assert config.release_settings_for(TELEGRAM) == expected


def test_mapped_release_settings_precedence_and_builtin_fallback(tmp_path: Path) -> None:
    variables = _vars(releases=(REPO, CORE, TELEGRAM))
    variables.update(
        {
            "merge_method": {"default": "squash", REPO: "rebase"},
            "gating_workflows": {REPO: ["Master Gate"]},
            "heavy_workflows": {"default": ["Full CI"], TELEGRAM: []},
            "heavy_max_age_hours": {TELEGRAM: 12},
        }
    )

    config = Config.from_invocation(_invocation(tmp_path, variables))

    assert config.default_release_settings == ReleaseSettings(
        merge_method="squash",
        gating_workflows=(),
        heavy_workflows=("Full CI",),
        heavy_max_age_hours=6.0,
    )
    assert config.release_settings_for(REPO) == ReleaseSettings(
        merge_method="rebase",
        gating_workflows=("Master Gate",),
        heavy_workflows=("Full CI",),
        heavy_max_age_hours=6.0,
    )
    assert config.release_settings_for(CORE) == ReleaseSettings(
        merge_method="squash",
        gating_workflows=(),
        heavy_workflows=("Full CI",),
        heavy_max_age_hours=6.0,
    )
    assert config.release_settings_for(TELEGRAM) == ReleaseSettings(
        merge_method="squash",
        gating_workflows=(),
        heavy_workflows=(),
        heavy_max_age_hours=12.0,
    )


def test_mapped_release_settings_reject_unknown_keys_and_bad_values(tmp_path: Path) -> None:
    with pytest.raises(CiWatchError, match="mapping keys must be repository strings"):
        Config.from_invocation(
            _invocation(tmp_path, {**_vars(releases=(REPO,)), "merge_method": {1: "merge"}})
        )
    with pytest.raises(CiWatchError, match="unknown release repository"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                {**_vars(releases=(REPO,)), "merge_method": {CORE: "merge"}},
            )
        )
    with pytest.raises(CiWatchError, match="merge_method.*must be one of"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                {**_vars(releases=(REPO,)), "merge_method": {REPO: "fast-forward"}},
            )
        )
    with pytest.raises(CiWatchError, match="gating_workflows.*must be a list"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                {**_vars(releases=(REPO,)), "gating_workflows": {REPO: "CI"}},
            )
        )
    with pytest.raises(CiWatchError, match="heavy_max_age_hours.*must be a positive number"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                {**_vars(releases=(REPO,)), "heavy_max_age_hours": {REPO: True}},
            )
        )


def test_release_notification_retry_and_legacy_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    legacy = {
        "version": 1,
        "merges": [
            {
                "repo": REPO,
                "number": 9,
                "url": f"https://github.com/{REPO}/pull/9",
                "head_oid": SHA,
                "version": "1.0.0",
                "generator": "release-please",
                "merged_at": FIXED_NOW.isoformat(),
            }
        ],
        "announced_pending": {},
    }
    (tmp_path / LEGACY_RELEASE_LEDGER_FILE_NAME).write_text(json.dumps(legacy), encoding="utf-8")
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]
    notifier = FakeNotifier(fail_count=1)

    result, _, _, notifier = _build(
        tmp_path,
        _observations(),
        github=github,
        notifier=notifier,
        variables=_vars(releases=(REPO,), merge_enabled=True),
        clock=_clock_at(),
    )
    assert result["status"] == "check_error"
    state = _state(tmp_path)
    assert [row["number"] for row in state["releases"]] == [9, 10]
    assert state["releases"][0]["notification_sent"] is True
    assert state["releases"][1]["notification_sent"] is False

    retry, _, _, notifier = _build(
        tmp_path,
        _observations(),
        notifier=notifier,
        variables=_vars(releases=(REPO,), merge_enabled=True),
        result_file="retry.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=5)),
    )
    assert retry["status"] == "ok"
    assert len(notifier.notifications) == 2
    assert _state(tmp_path)["releases"][1]["notification_sent"] is True


def test_report_publish_failure_preserves_last_good_and_notifications_still_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    _build(tmp_path, _observations(), clock=_clock_at())
    previous = (tmp_path / REPORT_FILE_NAME).read_bytes()

    def reject_report(report: dict[str, Any]) -> dict[str, Any]:
        del report
        raise ValueError("synthetic invalid report")

    monkeypatch.setattr(ci_watch_module, "validate_chop_report", reject_report)
    result, _, _, notifier = _build(
        tmp_path,
        _observations(red=(REPO,)),
        result_file="bad.json",
        clock=_clock_at(FIXED_NOW + timedelta(minutes=5)),
    )

    assert result["status"] == "check_error"
    assert result["reason"] == "report_publish_failed"
    assert result["counters"]["reports_failed"] == 1
    assert (tmp_path / REPORT_FILE_NAME).read_bytes() == previous
    assert notifier.notifications[0]["action"] is None
    assert notifier.notifications[0]["action_data"] is None


def test_missing_observation_distinguishes_no_ci_and_errors(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.workflow_counts[REPO] = 0
    github.workflow_counts[CORE] = CiWatchError("probe failed")
    observations = _observations()
    observations[REPO] = RepoObservation(REPO, error="missing_observation")
    observations[CORE] = RepoObservation(CORE, error="missing_observation")

    result, ledger, _, _ = _build(tmp_path, observations, github=github)

    assert result["counters"]["no_ci"] == 1
    assert result["counters"]["errors"] == 1
    assert ledger["repositories"][REPO]["reason"] == "no_ci"
    assert ledger["repositories"][CORE]["reason"] == "missing_observation"
    assert "probe failed" in ledger["repositories"][CORE]["workflow_probe_error"]


def test_config_validation_rejects_stale_shapes_and_removed_fix_keys(tmp_path: Path) -> None:
    with pytest.raises(CiWatchError, match="release_repositories must be a list"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                {
                    **_vars(),
                    "release_repositories": {REPO: "release-please"},
                },
            )
        )
    with pytest.raises(CiWatchError, match="CI fix gates were removed"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "fix_enabled": True}))
    with pytest.raises(CiWatchError, match="is not release-enabled"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                _vars(releases=(REPO,), merge_order=("sase-core",)),
            )
        )
    with pytest.raises(CiWatchError, match="repos contains duplicates"):
        Config.from_invocation(_invocation(tmp_path, _vars(repos=(REPO, REPO))))
    with pytest.raises(CiWatchError, match="merge_enabled must be a boolean"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "merge_enabled": "yes"}))


def test_more_config_validation_edges(tmp_path: Path) -> None:
    with pytest.raises(CiWatchError, match="release_repositories contains duplicates"):
        Config.from_invocation(_invocation(tmp_path, _vars(releases=(REPO, REPO))))
    with pytest.raises(CiWatchError, match="is not in repos"):
        Config.from_invocation(_invocation(tmp_path, _vars(repos=(REPO,), releases=(CORE,))))
    with pytest.raises(CiWatchError, match="merge_order contains duplicates"):
        Config.from_invocation(
            _invocation(
                tmp_path,
                _vars(releases=(REPO, CORE), merge_order=("sase", "sase")),
            )
        )
    with pytest.raises(CiWatchError, match="unknown or ambiguous"):
        Config.from_invocation(
            _invocation(tmp_path, _vars(releases=(REPO,), merge_order=("missing",)))
        )
    with pytest.raises(CiWatchError, match="max_merges_per_tick must be an integer"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "max_merges_per_tick": 0}))
    with pytest.raises(CiWatchError, match="gh_bin must be a non-blank executable"):
        Config.from_invocation(_invocation(tmp_path, {**_vars(), "gh_bin": ""}))


def test_pure_classification_and_json_validation_matrix() -> None:
    cancelled = _commit(REPO, SHA, "failure", run_conclusion="cancelled")
    cancelled_failed = _commit(
        REPO,
        SHA,
        "failure",
        run_conclusion="cancelled",
        jobs=[_job("test")],
    )
    timed_out = _commit(REPO, SHA, "failure", run_conclusion="timed_out")
    assert not actionably_red(cancelled)
    assert actionably_red(cancelled_failed)
    assert actionably_red(timed_out)
    assert classify_repo(RepoObservation(REPO, commit=_commit(REPO, SHA))) is RepoState.GREEN
    assert classify_repo(RepoObservation(REPO, commit=cancelled_failed)) is RepoState.RED
    assert classify_repo(RepoObservation(REPO)) is RepoState.ERROR

    base = {
        "number": 1,
        "headRefName": "release-please-branches-master",
        "baseRefName": "master",
        "headRefOid": SHA,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "url": f"https://github.com/{REPO}/pull/1",
        "title": "chore(main): release 1.2.3",
        "createdAt": "2026-08-21T10:00:00Z",
    }
    assert (
        plan_release_merge(
            REPO,
            RepoState.GREEN,
            "master",
            [ReleasePr.from_json(base)],
            generator_busy=False,
        )[1]
        == "not_release_pr"
    )
    with pytest.raises(CiWatchError, match="invalid number"):
        ReleasePr.from_json({**base, "number": 0})
    with pytest.raises(CiWatchError, match="invalid statusCheckRollup"):
        ReleasePr.from_json({**base, "statusCheckRollup": {}})
    with pytest.raises(CiWatchError, match="check is not an object"):
        ReleasePr.from_json({**base, "statusCheckRollup": [1]})
    with pytest.raises(CiWatchError, match="not a GitHub URL"):
        ReleasePr.from_json({**base, "url": "https://evil.example/token=bad"})
    with pytest.raises(CiWatchError, match="invalid createdAt"):
        ReleasePr.from_json({**base, "createdAt": "not-a-date"})
    assert ci_watch_module._extract_release_version("chore: release v2.3.4") == "2.3.4"
    assert ci_watch_module._extract_release_version("refresh release metadata") == "-"


def test_low_level_validation_and_failure_extraction_edges() -> None:
    with pytest.raises(CiWatchError, match="missing or invalid"):
        ci_watch_module._required_string({}, "name")
    with pytest.raises(CiWatchError, match="invalid repository"):
        ci_watch_module._repo("bad repo")
    with pytest.raises(CiWatchError, match="invalid branch"):
        ci_watch_module._branch("../main")
    with pytest.raises(CiWatchError, match="invalid commit SHA"):
        ci_watch_module._sha("not-sha")
    with pytest.raises(CiWatchError, match="non-object JSON"):
        ci_watch_module._json_object("[]", source="unit")

    assert not actionably_red({"runs": "bad"})
    assert not actionably_red({"runs": [1]})
    assert not ci_watch_module._nested_failure({"jobs": "bad"})
    assert not ci_watch_module._nested_failure({"jobs": [1]})
    assert ci_watch_module._nested_failure(
        {"jobs": [{"conclusion": "cancelled", "steps": [{"name": "x", "conclusion": "failure"}]}]}
    )
    assert ci_watch_module._failed_jobs_from_runs("bad") == ()
    assert ci_watch_module._failed_jobs_from_runs([1, {"conclusion": "success"}]) == ()

    cancelled = {
        "conclusion": "cancelled",
        "display_title": "fallback workflow",
        "url": "https://github.com/sase-org/sase/actions/runs/1",
        "jobs": [
            {
                "name": "cancelled job",
                "conclusion": "cancelled",
                "url": "https://example.invalid/not-github",
                "steps": [
                    {"name": "setup", "conclusion": "success"},
                    {"name": "fail", "conclusion": "failure"},
                ],
            },
            {"name": "", "conclusion": "failure"},
        ],
    }
    jobs = ci_watch_module._failed_jobs_from_runs([cancelled])
    assert jobs == (
        FailingJobEvidence(
            workflow="fallback workflow",
            job="cancelled job",
            conclusion="failure",
            url="https://github.com/sase-org/sase/actions/runs/1",
            steps=("fail",),
        ),
    )

    with pytest.raises(CiWatchError, match="no settled commit"):
        ci_watch_module._commit_sha(RepoObservation(REPO))


def test_release_pr_validation_additional_edges() -> None:
    base = {
        "number": 1,
        "headRefName": "release-please--branches--master",
        "baseRefName": "master",
        "headRefOid": SHA,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "url": f"https://github.com/{REPO}/pull/1",
        "title": "chore(main): release 1.2.3",
        "createdAt": "2026-08-21T10:00:00Z",
    }
    with pytest.raises(CiWatchError, match="missing or invalid 'title'"):
        ReleasePr.from_json({**base, "title": ""})
    with pytest.raises(CiWatchError, match="invalid branch"):
        ReleasePr.from_json({**base, "headRefName": "/bad"})
    with pytest.raises(CiWatchError, match="invalid commit SHA"):
        ReleasePr.from_json({**base, "headRefOid": "bad"})
    with pytest.raises(CiWatchError, match="invalid createdAt"):
        ReleasePr.from_json({**base, "createdAt": "2026-08-21T10:00:00"})


def test_state_loading_sanitizes_corruption_invalid_rows_and_expiry(tmp_path: Path) -> None:
    valid_failure = ci_watch_module._failure_to_json(
        ci_watch_module.FailureEvidence(SHA, (_failure_job("lint"),))
    )
    state = {
        "version": 1,
        "failures": {
            REPO: {
                "fingerprint": ci_watch_module.FailureEvidence(
                    SHA, (_failure_job("lint"),)
                ).fingerprint_key,
                "notification_sent": False,
                "last_seen": FIXED_NOW.isoformat(),
                "evidence": valid_failure,
            },
            CORE: {"fingerprint": "bad"},
            "bad repo": {},
        },
        "releases": [
            {
                "repo": REPO,
                "number": 10,
                "url": f"https://github.com/{REPO}/pull/10",
                "head_oid": SHA,
                "title": "release",
                "version": "1.2.3",
                "target_branch": "master",
                "submitted_at": FIXED_NOW.isoformat(),
                "notification_sent": False,
                "outcome": "squash_merge_submitted",
                "notified_at": "not-a-date",
            },
            {
                "repo": REPO,
                "number": 9,
                "url": f"https://github.com/{REPO}/pull/9",
                "head_oid": SHA,
                "title": "old",
                "version": "1.0.0",
                "target_branch": "master",
                "submitted_at": (FIXED_NOW - timedelta(days=100)).isoformat(),
                "notification_sent": True,
            },
            {"repo": "bad repo"},
        ],
    }
    (tmp_path / STATE_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")
    loaded = ci_watch_module._load_state(_invocation(tmp_path, _vars()), FIXED_NOW, [REPO, CORE])
    assert set(loaded["failures"]) == {REPO}
    assert [row["number"] for row in loaded["releases"]] == [10]
    assert "notified_at" not in loaded["releases"][0]

    (tmp_path / STATE_FILE_NAME).write_text("{broken", encoding="utf-8")
    (tmp_path / LEGACY_RELEASE_LEDGER_FILE_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "merges": [
                    {
                        "repo": REPO,
                        "number": 8,
                        "url": f"https://github.com/{REPO}/pull/8",
                        "head_oid": SHA,
                        "version": "0.9.0",
                        "merged_at": FIXED_NOW.isoformat(),
                    },
                    {"repo": "bad repo"},
                ],
            }
        ),
        encoding="utf-8",
    )
    migrated = ci_watch_module._load_state(
        _invocation(tmp_path, _vars()),
        FIXED_NOW,
        [REPO],
    )
    assert migrated["releases"][0]["number"] == 8
    assert migrated["releases"][0]["notification_sent"] is True


def test_notification_note_overflow_and_state_helpers() -> None:
    many_jobs = tuple(_failure_job(f"job-{index}", steps=()) for index in range(10))
    failure = ci_watch_module.FailureEvidence(SHA, many_jobs)
    notes = ci_watch_module._failure_notification_notes(REPO, failure, None)
    assert notes[0] == f"CI failure: {REPO} default@{SHA[:12]}"
    assert notes[-1] == "+2 more failing jobs"
    assert "Steps:" not in notes

    state: dict[str, Any] = {"version": 1, "failures": [], "releases": {}}
    ci_watch_module._update_failure_state(
        state,
        {REPO: failure},
        {REPO: RepoState.RED, CORE: RepoState.GREEN},
        FIXED_NOW,
    )
    assert state["failures"][REPO]["notification_sent"] is False
    ci_watch_module._append_release_record(state, REPO, _pr(), FIXED_NOW, "squash")
    ci_watch_module._append_release_record(
        state, REPO, _pr(), FIXED_NOW + timedelta(minutes=1), "rebase"
    )
    assert len(state["releases"]) == 1
    assert state["releases"][0]["outcome"] == "rebase_merge_submitted"


class QueueRunner:
    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), input_text, cwd))
        return self.results.pop(0)


def test_actstat_client_parses_rows_filters_scope_and_rejects_bad_input() -> None:
    output = "\n".join(
        [
            json.dumps({"type": "active_commit", "repo": REPO, "sha": SHA[:7], "runs": []}),
            json.dumps(_commit(REPO, SHA)),
            json.dumps({"type": "repo_error", "repo": CORE, "error": "forbidden"}),
            json.dumps({"type": "repo_error", "repo": "missing-org", "error": "not found"}),
            json.dumps(_commit("other-org/other", "d" * 40)),
        ]
    )
    runner = QueueRunner(CommandResult(1, output))
    observations = ActstatClient("/actstat", runner).sweep([REPO, CORE])
    assert observations[REPO].active is not None
    assert observations[REPO].commit is not None
    assert observations[CORE].error == "forbidden"
    assert runner.calls[0][0] == ["/actstat", "-f", "jsonl"]

    with pytest.raises(CiWatchError, match="actstat failed"):
        ActstatClient("actstat", QueueRunner(CommandResult(1, stderr="auth failed"))).sweep([REPO])
    with pytest.raises(CiWatchError, match="malformed JSON"):
        ActstatClient("actstat", QueueRunner(CommandResult(0, "{bad"))).sweep([REPO])
    with pytest.raises(CiWatchError, match="unknown record type"):
        ActstatClient(
            "actstat",
            QueueRunner(CommandResult(0, '{"type":"surprise","repo":"sase-org/sase"}')),
        ).sweep([REPO])
    with pytest.raises(CiWatchError, match="duplicate commit"):
        ActstatClient(
            "actstat",
            QueueRunner(CommandResult(0, "\n".join([json.dumps(_commit(REPO, SHA))] * 2))),
        ).sweep([REPO])
    assert (
        ActstatClient("actstat", QueueRunner(CommandResult(0))).sweep([REPO])[REPO].error
        == "missing_observation"
    )


def test_github_reader_queries_release_prs_merges_and_detects_generator_busy() -> None:
    metadata = {
        "default_branch": "master",
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": False,
    }
    pr_json = {
        "number": 10,
        "isDraft": False,
        "baseRefName": "master",
        "headRefName": "release-please--branches--master",
        "headRefOid": SHA,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "url": f"https://github.com/{REPO}/pull/10",
        "title": "chore(main): release 1.2.3",
        "createdAt": "2026-08-21T10:00:00Z",
    }
    runner = QueueRunner(
        CommandResult(0, json.dumps(metadata)),
        CommandResult(0, json.dumps({"sha": SHA})),
        CommandResult(
            0,
            '{"workflow_runs":[{"status":"in_progress","name":"release-please","path":"publish.yml"}]}',
        ),
        CommandResult(
            0,
            json.dumps(
                [
                    {"number": 10, "headRefName": "release-please--branches--master"},
                    {"number": 11, "headRefName": "release-plz-2026-08-21"},
                    {"number": 9, "headRefName": "feature"},
                ]
            ),
        ),
        CommandResult(0, json.dumps(pr_json)),
        CommandResult(0),
    )
    github = GitHubReader("/gh", runner)
    assert github.default_branch_head(REPO) == BranchHead("master", SHA)
    assert github.merge_method_allowed(REPO, "squash")
    assert not github.merge_method_allowed(REPO, "rebase")
    assert [call[0] for call in runner.calls].count(["/gh", "api", f"repos/{REPO}"]) == 1
    assert github.generator_busy(REPO, "master")
    assert github.release_pr_numbers(REPO) == [10]
    pr = github.release_pr(REPO, 10)
    assert pr == _pr(created_at="2026-08-21T10:00:00Z")
    assert github.merge(MergePlan(REPO, pr)).returncode == 0
    assert runner.calls[-1][0] == [
        "/gh",
        "pr",
        "merge",
        "10",
        "--repo",
        REPO,
        "--merge",
        "--match-head-commit",
        SHA,
    ]


def test_github_reader_parses_allowed_merge_metadata_without_default_head_query() -> None:
    runner = QueueRunner(
        CommandResult(
            0,
            json.dumps(
                {
                    "default_branch": "main",
                    "allow_merge_commit": False,
                    "allow_squash_merge": True,
                    "allow_rebase_merge": False,
                }
            ),
        )
    )
    github = GitHubReader("/gh", runner)

    assert github.merge_method_allowed(REPO, "squash")
    assert not github.merge_method_allowed(REPO, "merge")
    assert runner.calls == [(["/gh", "api", f"repos/{REPO}"], None, None)]


def test_github_reader_bounded_head_job_evidence_and_fail_closed_shapes() -> None:
    runs = {
        "total_count": 3,
        "workflow_runs": [
            {"id": 3, "status": "in_progress", "conclusion": None, "name": "CI"},
            {
                "id": 2,
                "status": "completed",
                "conclusion": "failure",
                "html_url": f"https://github.com/{REPO}/actions/runs/2",
                "name": "CI",
            },
            {"id": 1, "status": "completed", "conclusion": "success", "name": "CI"},
        ],
    }
    failed_jobs = {
        "total_count": 2,
        "jobs": [
            {
                "name": "lint",
                "status": "completed",
                "conclusion": "failure",
                "html_url": f"https://github.com/{REPO}/actions/runs/2/job/1",
                "steps": [{"name": "ruff", "conclusion": "failure"}],
            },
            {"name": "still running", "status": "in_progress", "conclusion": None},
        ],
    }
    successful_jobs = {
        "total_count": 1,
        "jobs": [{"name": "test", "status": "completed", "conclusion": "success"}],
    }
    runner = QueueRunner(
        CommandResult(0, '{"total_count":2}'),
        CommandResult(0, json.dumps(runs)),
        CommandResult(0, json.dumps(failed_jobs)),
        CommandResult(0, json.dumps(successful_jobs)),
    )
    github = GitHubReader("/gh", runner)

    assert github.workflow_count(REPO) == 2
    evidence = github.head_ci_evidence(REPO, SHA)
    assert evidence.sha == SHA
    assert evidence.has_in_flight is True
    assert evidence.all_completed_green is False
    assert evidence.successful_jobs == ("test",)
    assert evidence.failing_jobs == (
        FailingJobEvidence(
            workflow="CI",
            job="lint",
            conclusion="failure",
            url=f"https://github.com/{REPO}/actions/runs/2/job/1",
            steps=("ruff",),
        ),
    )
    assert f"head_sha={SHA}" in runner.calls[1][0]

    with pytest.raises(CiWatchError, match="bounded query limit"):
        GitHubReader(
            "gh",
            QueueRunner(CommandResult(0, '{"workflow_runs":[],"total_count":21}')),
        ).head_ci_evidence(REPO, SHA)
    with pytest.raises(CiWatchError, match="invalid status"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"status":"mystery"}],"total_count":1}',
                )
            ),
        ).head_ci_evidence(REPO, SHA)
    with pytest.raises(CiWatchError, match="no failing job identity"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"id":1,"status":"completed","conclusion":"failure"}],"total_count":1}',
                ),
                CommandResult(
                    0,
                    '{"jobs":[{"name":"test","status":"completed","conclusion":"success"}],"total_count":1}',
                ),
            ),
        ).head_ci_evidence(REPO, SHA)


def test_github_reader_additional_shape_failures() -> None:
    with pytest.raises(CiWatchError, match="default branch head is not an object"):
        GitHubReader(
            "gh",
            QueueRunner(CommandResult(0, '{"default_branch":"master"}'), CommandResult(0, "[]")),
        ).default_branch_head(REPO)
    with pytest.raises(CiWatchError, match="invalid total_count"):
        GitHubReader("gh", QueueRunner(CommandResult(0, '{"total_count":"one"}'))).workflow_count(
            REPO
        )
    with pytest.raises(CiWatchError, match="release PR list contains a non-object"):
        GitHubReader("gh", QueueRunner(CommandResult(0, "[1]"))).release_pr_numbers(REPO)
    with pytest.raises(CiWatchError, match="release PR has an invalid number"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(0, '[{"number":0,"headRefName":"release-please--branches--master"}]')
            ),
        ).release_pr_numbers(REPO)
    with pytest.raises(CiWatchError, match="PR #10 is not an object"):
        GitHubReader("gh", QueueRunner(CommandResult(0, "[]"))).release_pr(REPO, 10)
    with pytest.raises(CiWatchError, match="invalid allow_squash_merge"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"default_branch":"master","allow_squash_merge":"yes"}',
                )
            ),
        ).merge_method_allowed(REPO, "squash")

    with pytest.raises(CiWatchError, match="invalid conclusion"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"id":1,"status":"completed","conclusion":null}],"total_count":1}',
                )
            ),
        ).head_ci_evidence(REPO, SHA)
    with pytest.raises(CiWatchError, match="invalid id"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"id":0,"status":"completed","conclusion":"success"}],"total_count":1}',
                )
            ),
        ).head_ci_evidence(REPO, SHA)
    with pytest.raises(CiWatchError, match="invalid fields"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"id":1,"status":"completed","conclusion":"success"}],"total_count":1}',
                ),
                CommandResult(0, '{"jobs":[{"name":1}],"total_count":1}'),
            ),
        ).head_ci_evidence(REPO, SHA)
    with pytest.raises(CiWatchError, match="invalid conclusion"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"id":1,"status":"completed","conclusion":"success"}],"total_count":1}',
                ),
                CommandResult(
                    0,
                    '{"jobs":[{"name":"test","status":"completed","conclusion":null}],"total_count":1}',
                ),
            ),
        ).head_ci_evidence(REPO, SHA)


def test_github_reader_command_json_and_workflow_label_failures() -> None:
    with pytest.raises(CiWatchError, match="metadata failed"):
        GitHubReader(
            "gh",
            QueueRunner(CommandResult(1, stderr="no auth")),
        ).default_branch_head(REPO)
    with pytest.raises(CiWatchError, match="metadata is not an object"):
        GitHubReader("gh", QueueRunner(CommandResult(0, "[]"))).default_branch_head(REPO)
    with pytest.raises(CiWatchError, match="malformed JSON"):
        GitHubReader("gh", QueueRunner(CommandResult(0, "{bad"))).release_pr_numbers(REPO)
    with pytest.raises(CiWatchError, match="not an array"):
        GitHubReader("gh", QueueRunner(CommandResult(0, "{}"))).release_pr_numbers(REPO)
    with pytest.raises(CiWatchError, match="contain a non-object"):
        GitHubReader(
            "gh",
            QueueRunner(CommandResult(0, '{"workflow_runs":[1]}')),
        ).generator_busy(REPO, "master")
    with pytest.raises(CiWatchError, match="invalid name or path"):
        GitHubReader(
            "gh",
            QueueRunner(
                CommandResult(
                    0,
                    '{"workflow_runs":[{"status":"in_progress","name":1,"path":"publish.yml"}]}',
                )
            ),
        ).generator_busy(REPO, "master")


def test_sase_notifier_only_calls_notify_create_and_fails_explicitly() -> None:
    runner = QueueRunner(CommandResult(0, "id"))
    notifier = SaseNotifier("/sase", runner)
    notifier.notify(
        ["Merged release", "token=do-not-leak"],
        icon="🚢",
        tags=["ci", "release"],
        action="ViewReport",
        action_data={"report_path": "/tmp/report.json"},
    )
    argv, payload, cwd = runner.calls[0]
    assert argv == ["/sase", "notify", "create", "-s", "ci_watch"]
    assert cwd is None
    assert json.loads(payload or "{}") == {
        "notes": ["Merged release", "[redacted]"],
        "tags": ["ci", "release"],
        "icon": "🚢",
        "action": "ViewReport",
        "action_data": {"report_path": "/tmp/report.json"},
    }

    failing = SaseNotifier("/sase", QueueRunner(CommandResult(1, stderr="boom")))
    with pytest.raises(CiWatchError, match="notification failed"):
        failing.notify(["hi"], icon="🚨", tags=["ci"])


def test_default_command_runner_captures_output_and_exec_errors() -> None:
    result = run_command(["sh", "-c", "read value; printf '%s' \"$value\""], input_text="ok\n")
    assert result == CommandResult(0, "ok", "")
    with pytest.raises(CiWatchError, match="failed to execute"):
        run_command(["/definitely/missing/ci-watch-command"])


def test_subprocess_boundary_contains_no_sase_agent_launch_gate_or_run_paths() -> None:
    runner = QueueRunner(CommandResult(0, "id"))
    notifier = SaseNotifier("/sase", runner)
    notifier.notify(["failure"], icon="🚨", tags=["ci"])
    argv = runner.calls[0][0]
    assert argv[:3] == ["/sase", "notify", "create"]
    assert not any(part in {"agent", "launch", "gate", "run"} for part in argv)


def test_release_observation_errors_detail_cap_and_generator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = FakeGitHub()
    github.numbers[REPO] = CiWatchError("gh unavailable")
    result, ledger, _, _ = _build(
        tmp_path / "numbers",
        _observations(),
        github=github,
        variables=_vars(releases=(REPO,), merge_enabled=True),
    )
    assert result["counters"]["merge_skipped"] == 0
    assert "gh unavailable" in ledger["repositories"][REPO]["release_reason"]

    detail = FakeGitHub()
    detail.numbers[REPO] = [10]
    monkeypatch.setattr(ci_watch_module, "MAX_RELEASE_PR_DETAILS", 0)
    _, ledger, _, _ = _build(
        tmp_path / "detail",
        _observations(),
        github=detail,
        variables=_vars(releases=(REPO,), merge_enabled=True),
    )
    assert "detail cap reached" in ledger["repositories"][REPO]["reason"]
    monkeypatch.setattr(ci_watch_module, "MAX_RELEASE_PR_DETAILS", 8)

    class BrokenGeneratorGitHub(FakeGitHub):
        def workflow_runs(self, repo: str, branch: str) -> list[dict[str, Any]]:
            del repo, branch
            raise CiWatchError("workflow query failed")

    broken = BrokenGeneratorGitHub()
    broken.numbers[REPO] = [10]
    broken.prs[(REPO, 10)] = [_pr()]
    _, ledger, _, _ = _build(
        tmp_path / "generator",
        _observations(),
        github=broken,
        variables=_vars(releases=(REPO,), merge_enabled=True),
    )
    assert ledger["repositories"][REPO]["reason"] == "workflow query failed"


def test_head_evidence_query_cap_and_state_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", "d" * 40)
    monkeypatch.setattr(ci_watch_module, "MAX_HEAD_EVIDENCE_REPOS_PER_TICK", 0)
    result, ledger, github, _ = _build(tmp_path / "cap", _observations(red=(REPO,)), github=github)
    assert result["counters"]["errors"] == 1
    assert ledger["repositories"][REPO]["reason"] == "HEAD evidence per-tick query limit reached"
    assert github.head_evidence_calls == []

    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    monkeypatch.setattr(ci_watch_module, "MAX_HEAD_EVIDENCE_REPOS_PER_TICK", 10)

    def fail_write(invocation: ChopInvocation, state: Mapping[str, Any]) -> None:
        del invocation, state
        raise OSError("disk full")

    monkeypatch.setattr(ci_watch_module, "_write_state", fail_write)
    result, _, _, notifier = _build(
        tmp_path / "write",
        _observations(red=(REPO,)),
        clock=_clock_at(),
    )
    assert result["status"] == "check_error"
    assert result["reason"] == "state_write_failed"
    assert notifier.notifications == []


def test_notification_state_write_failure_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {
        "version": 1,
        "failures": {
            REPO: {
                "fingerprint": "a" * 16,
                "notification_sent": False,
                "last_seen": FIXED_NOW.isoformat(),
                "evidence": ci_watch_module._failure_to_json(
                    ci_watch_module.FailureEvidence(SHA, (_failure_job("lint"),))
                ),
            }
        },
        "releases": [],
    }

    def fail_write(invocation: ChopInvocation, state: Mapping[str, Any]) -> None:
        del invocation, state
        raise OSError("disk full")

    monkeypatch.setattr(ci_watch_module, "_write_state", fail_write)
    counters = ci_watch_module._new_counters(1)
    errors = ci_watch_module._send_required_notifications(
        invocation=_invocation(tmp_path, _vars()),
        notifier=FakeNotifier(),  # type: ignore[arg-type]
        state=state,
        failures={REPO: ci_watch_module.FailureEvidence(SHA, (_failure_job("lint"),))},
        heads={},
        report_path=None,
        now=FIXED_NOW,
        counters=counters,
    )
    assert counters["notifications_sent"] == 1
    assert errors == ["state write failed after notification: disk full"]


def test_main_check_error_still_emits_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        variables={"repos": []},
    )

    assert result["status"] == "check_error"
    assert result["report"]["title"] == "CI WATCH"
    assert result["report"]["blocks"][0]["tone"] == "error"
    validate_chop_result(result)
