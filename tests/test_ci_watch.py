from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sase.axe.chop_script_context import ChopScriptContext
from sase.chops import ChopArguments, ChopInvocation, ChopLogger

from bugyi_chops.ci_watch import (
    ActstatClient,
    AgentProbe,
    AgentsGate,
    BranchHead,
    CiWatchError,
    CommandResult,
    Config,
    GitHubReader,
    MergePlan,
    ReleasePr,
    RepoObservation,
    RepoState,
    actionably_red,
    build_ci_watch_result,
    classify_repo,
    plan_release_merge,
    run_command,
)

REPO = "sase-org/sase"
CORE = "sase-org/sase-core"
SHA = "a" * 40
CORE_SHA = "b" * 40


def _commit(
    repo: str,
    sha: str,
    conclusion: str = "success",
    *,
    run_conclusion: str | None = None,
    failed_job: bool = False,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "conclusion": run_conclusion or conclusion,
        "url": f"https://github.com/{repo}/actions/runs/12",
        "jobs": [],
    }
    if failed_job:
        run["jobs"] = [
            {
                "name": "test token=do-not-leak",
                "conclusion": "failure",
                "steps": [{"name": "Run tests", "conclusion": "failure"}],
            }
        ]
    return {
        "type": "commit",
        "repo": repo,
        "sha": sha[:7],
        "branch": "master",
        "conclusion": conclusion,
        "runs": [run],
    }


def _observations(
    *,
    red: Sequence[str] = (),
    active: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> dict[str, RepoObservation]:
    result: dict[str, RepoObservation] = {}
    for repo, sha in ((REPO, SHA), (CORE, CORE_SHA)):
        commit = _commit(
            repo,
            sha,
            "failure" if repo in red else "success",
            failed_job=repo in red,
        )
        result[repo] = RepoObservation(
            repo,
            active={"sha": sha[:7]} if repo in active else None,
            commit=commit,
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
) -> ReleasePr:
    return ReleasePr(
        number=number,
        head_ref_name=head_ref,
        base_ref_name=base,
        head_oid=head or (SHA if repo == REPO else CORE_SHA),
        is_draft=draft,
        mergeable=mergeable,
        merge_state_status=merge_state,
        checks=checks,
        url=f"https://github.com/{repo}/pull/{number}",
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
        self.heads = {
            REPO: BranchHead("master", SHA),
            CORE: BranchHead("master", CORE_SHA),
        }
        self.in_flight: set[str] = set()
        self.numbers: dict[str, list[int]] = {REPO: [], CORE: []}
        self.prs: dict[tuple[str, int], list[ReleasePr]] = {}
        self.busy: set[str] = set()
        self.merge_results: list[CommandResult] = []
        self.merges: list[MergePlan] = []

    def default_branch_head(self, repo: str) -> BranchHead:
        value = self.heads[repo]
        if isinstance(value, Exception):
            raise value
        return value

    def has_in_flight_runs(self, repo: str, branch: str) -> bool:
        assert branch == "master"
        return repo in self.in_flight

    def generator_busy(self, repo: str, branch: str, generator: str) -> bool:
        assert branch == "master"
        assert generator in {"release-please", "release-plz"}
        return repo in self.busy

    def release_pr_numbers(self, repo: str) -> list[int]:
        return list(self.numbers[repo])

    def release_pr(self, repo: str, number: int) -> ReleasePr:
        values = self.prs[(repo, number)]
        return values.pop(0) if len(values) > 1 else values[0]

    def merge(self, plan: MergePlan) -> CommandResult:
        self.merges.append(plan)
        return self.merge_results.pop(0) if self.merge_results else CommandResult(0)


class FakeAgents:
    def __init__(
        self,
        names: Sequence[str] = (),
        *,
        error: bool = False,
        notify_ok: bool = True,
    ) -> None:
        self.names = tuple(names)
        self.error = error
        self.notify_ok = notify_ok
        self.probes = 0
        self.notifications: list[tuple[str, str]] = []

    def probe(self) -> AgentProbe:
        self.probes += 1
        if self.error:
            raise CiWatchError("probe failed")
        return AgentProbe(self.names)

    def notify(self, note: str, tag: str) -> bool:
        self.notifications.append((note, tag))
        return self.notify_ok


def _vars(
    *,
    repos: Sequence[str] = (REPO, CORE),
    releases: dict[str, str] | None = None,
    merge_order: Sequence[str] = ("sase-core", "sase"),
    fix_enabled: bool = True,
    merge_enabled: bool = False,
    max_fixes: int = 1,
    max_merges: int = 1,
) -> dict[str, Any]:
    return {
        "actstat_bin": "/fake/actstat",
        "gh_bin": "/fake/gh",
        "sase_bin": "/fake/sase",
        "repos": list(repos),
        "release_repositories": releases or {},
        "merge_order": list(merge_order),
        "max_fix_proposals_per_tick": max_fixes,
        "max_merges_per_tick": max_merges,
        "fix_enabled": fix_enabled,
        "merge_enabled": merge_enabled,
    }


def _invocation(tmp_path: Path, variables: dict[str, Any]) -> ChopInvocation:
    context = ChopScriptContext(
        max_hook_runners=1,
        max_agent_runners=1,
        zombie_timeout_seconds=60,
        query="",
        lumberjack_name="test",
        state_dir=str(tmp_path),
        all_changespecs_file=str(tmp_path / "all.json"),
        filtered_changespecs_file=str(tmp_path / "filtered.json"),
        result_file=str(tmp_path / "result.json"),
        vars=variables,
    )
    return ChopInvocation(ChopArguments("context.json", False), context, ChopLogger())


def _build(
    tmp_path: Path,
    observations: dict[str, RepoObservation],
    *,
    github: FakeGitHub | None = None,
    agents: FakeAgents | None = None,
    variables: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeGitHub, FakeAgents]:
    github = github or FakeGitHub()
    agents = agents or FakeAgents()
    result = build_ci_watch_result(
        _invocation(tmp_path, variables or _vars()),
        actstat=FakeActstat(observations),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        agents=agents,  # type: ignore[arg-type]
    ).to_dict()
    ledger = json.loads((tmp_path / "ci_watch_decisions.json").read_text())
    return result, ledger, github, agents


def test_all_green_without_release_prs_is_noop(tmp_path: Path) -> None:
    result, ledger, _, agents = _build(tmp_path, _observations())

    assert result["status"] == "no_op"
    assert result["reason"] == "no_actions"
    assert result["counters"] == {
        "repos": 2,
        "green": 2,
        "red": 0,
        "pending": 0,
        "errors": 0,
        "agents_running": 0,
        "fix_proposed": 0,
        "fix_suppressed": 0,
        "release_candidates": 0,
        "merged": 0,
        "merge_skipped": 0,
    }
    assert result["proposed_launches"] == []
    assert result["evidence"] == ["ci_watch_decisions.json"]
    assert ledger["repositories"][REPO]["reason"] == "green"
    assert agents.probes == 0


def test_red_idle_emits_one_pinned_sanitized_proposal(tmp_path: Path) -> None:
    agents = FakeAgents(notify_ok=False)
    result, ledger, _, agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        agents=agents,
    )

    assert result["status"] == "ok"
    assert result["counters"]["fix_proposed"] == 1
    proposal = result["proposed_launches"][0]
    assert proposal["workspace"] == f"gh:{REPO}"
    assert proposal["agent_name"] == "ci_fix.sase"
    assert proposal["dedupe_key"] == f"ci_fix:{REPO}:{SHA}"
    assert "#pr(ci_fix_sase_aaaaaaa, status=ready)" in proposal["prompt"]
    assert f"#actstat(repo={REPO})" in proposal["prompt"]
    assert SHA in proposal["prompt"]
    assert "token=do-not-leak" not in proposal["prompt"]
    assert "[redacted]" in proposal["prompt"]
    assert agents.notifications[0][1] == "ci"
    assert ledger["repositories"][REPO]["notification"] == "failed"
    assert "token=" not in json.dumps(ledger).lower()


def test_red_repos_are_globally_gated_and_capped(tmp_path: Path) -> None:
    observations = _observations(red=(REPO, CORE))
    busy_result, busy_ledger, _, _ = _build(
        tmp_path / "busy",
        observations,
        agents=FakeAgents(["one", "two"]),
    )
    assert busy_result["counters"]["agents_running"] == 2
    assert busy_result["counters"]["fix_suppressed"] == 2
    assert busy_result["proposed_launches"] == []
    assert busy_ledger["repositories"][CORE]["reason"] == "agents_busy"
    assert busy_ledger["repositories"][CORE]["busy_agents"] == ["one", "two"]

    failed_result, failed_ledger, _, _ = _build(
        tmp_path / "failed",
        observations,
        agents=FakeAgents(error=True),
    )
    assert failed_result["counters"]["fix_suppressed"] == 2
    assert failed_ledger["repositories"][REPO]["reason"] == "agents_check_failed"

    capped_result, capped_ledger, _, _ = _build(tmp_path / "capped", observations)
    assert capped_result["counters"]["fix_proposed"] == 1
    assert capped_result["counters"]["fix_suppressed"] == 1
    assert [item["workspace"] for item in capped_result["proposed_launches"]] == [f"gh:{REPO}"]
    assert capped_ledger["repositories"][CORE]["reason"] == "fix_cap_reached"


def test_fix_disabled_does_not_probe_agents(tmp_path: Path) -> None:
    agents = FakeAgents()
    result, ledger, _, agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        agents=agents,
        variables=_vars(fix_enabled=False),
    )
    assert result["counters"]["fix_suppressed"] == 1
    assert agents.probes == 0
    assert ledger["repositories"][REPO]["reason"] == "fix_disabled"


@pytest.mark.parametrize(
    ("observation", "github_change", "expected_state", "expected_reason"),
    [
        (
            RepoObservation(REPO, active={"sha": "a" * 7}, commit=_commit(REPO, SHA)),
            None,
            "pending",
            "run_in_flight",
        ),
        (
            RepoObservation(
                REPO,
                commit=_commit(
                    REPO,
                    SHA,
                    "failure",
                    run_conclusion="cancelled",
                ),
            ),
            None,
            "pending",
            "superseded_or_unsettled",
        ),
        (
            RepoObservation(REPO, commit=_commit(REPO, SHA), error="forbidden"),
            None,
            "error",
            "forbidden",
        ),
        (
            RepoObservation(REPO, commit=_commit(REPO, SHA)),
            "new_head",
            "pending",
            "newer_head_or_run_in_flight",
        ),
        (
            RepoObservation(REPO, commit=_commit(REPO, SHA)),
            "in_flight",
            "pending",
            "newer_head_or_run_in_flight",
        ),
        (
            RepoObservation(REPO, commit=_commit(REPO, SHA)),
            "github_error",
            "error",
            "API unavailable",
        ),
    ],
)
def test_pending_and_error_classification_is_fail_closed(
    tmp_path: Path,
    observation: RepoObservation,
    github_change: str | None,
    expected_state: str,
    expected_reason: str,
) -> None:
    observations = _observations()
    observations[REPO] = observation
    github = FakeGitHub()
    if github_change == "new_head":
        github.heads[REPO] = BranchHead("master", "c" * 40)
    elif github_change == "in_flight":
        github.in_flight.add(REPO)
    elif github_change == "github_error":
        github.heads[REPO] = CiWatchError("API unavailable")  # type: ignore[assignment]

    result, ledger, _, _ = _build(tmp_path, observations, github=github)
    counter_name = "errors" if expected_state == "error" else expected_state
    assert result["counters"][counter_name] >= 1
    assert ledger["repositories"][REPO]["reason"] == expected_reason
    assert all(item["workspace"] != f"gh:{REPO}" for item in result["proposed_launches"])


@pytest.mark.parametrize(
    ("candidates", "state", "busy", "reason"),
    [
        ([], RepoState.GREEN, False, "no_release_pr"),
        ([_pr(), _pr(11)], RepoState.GREEN, False, "ambiguous_release_prs"),
        ([_pr(head_ref="feature")], RepoState.GREEN, False, "not_release_pr"),
        ([_pr(draft=True)], RepoState.GREEN, False, "release_pr_draft"),
        ([_pr(base="main")], RepoState.GREEN, False, "release_pr_wrong_base"),
        (
            [_pr(mergeable="UNKNOWN")],
            RepoState.GREEN,
            False,
            "release_pr_not_mergeable",
        ),
        (
            [_pr(merge_state="DIRTY")],
            RepoState.GREEN,
            False,
            "release_pr_not_clean",
        ),
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
def test_release_merge_guards(
    candidates: list[ReleasePr],
    state: RepoState,
    busy: bool,
    reason: str,
) -> None:
    plan, actual_reason = plan_release_merge(
        REPO,
        state,
        "master",
        candidates,
        generator_busy=busy,
    )
    assert plan is None
    assert actual_reason == reason


def _release_github(*, two_repos: bool = False) -> FakeGitHub:
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr()]
    if two_repos:
        github.numbers[CORE] = [20]
        github.prs[(CORE, 20)] = [
            _pr(
                20,
                repo=CORE,
                head=CORE_SHA,
                head_ref="release-plz-2026-07-27",
            )
        ]
    return github


@pytest.mark.parametrize(
    ("merge_enabled", "dry_run", "reason"),
    [
        (False, None, "merge_disabled"),
        (True, None, "merge_context_unavailable"),
        (True, "1", "dry_run"),
    ],
)
def test_merge_modes_render_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    merge_enabled: bool,
    dry_run: str | None,
    reason: str,
) -> None:
    if dry_run is None:
        monkeypatch.delenv("SASE_CHOP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("SASE_CHOP_DRY_RUN", dry_run)
    github = _release_github()
    result, ledger, github, _ = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=_vars(
            releases={REPO: "release-please"},
            merge_enabled=merge_enabled,
        ),
    )
    assert result["status"] == "ok"
    assert result["counters"]["release_candidates"] == 1
    assert result["counters"]["merge_skipped"] == 1
    assert result["counters"]["merged"] == 0
    assert github.merges == []
    assert ledger["repositories"][REPO]["reason"] == reason
    assert ledger["release_plans"][0]["number"] == 10


def test_live_merge_uses_dependency_order_and_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = _release_github(two_repos=True)
    agents = FakeAgents()
    result, ledger, github, agents = _build(
        tmp_path,
        _observations(),
        github=github,
        agents=agents,
        variables=_vars(
            releases={REPO: "release-please", CORE: "release-plz"},
            merge_enabled=True,
        ),
    )
    assert result["counters"]["release_candidates"] == 2
    assert result["counters"]["merged"] == 1
    assert result["counters"]["merge_skipped"] == 1
    assert [plan.repo for plan in github.merges] == [CORE]
    assert ledger["repositories"][CORE]["reason"] == "merged"
    assert ledger["repositories"][REPO]["reason"] == "merge_cap_reached"
    assert agents.notifications == [(f"Merged release PR #20 for {CORE}", "release")]


def test_live_merge_reread_fails_closed_on_changed_head_and_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "false")
    changed = _release_github()
    changed.prs[(REPO, 10)] = [_pr(), _pr(head="c" * 40)]
    result, ledger, changed, _ = _build(
        tmp_path / "changed",
        _observations(),
        github=changed,
        variables=_vars(releases={REPO: "release-please"}, merge_enabled=True),
    )
    assert result["counters"]["merged"] == 0
    assert changed.merges == []
    assert ledger["repositories"][REPO]["reason"] == "release_pr_head_changed"

    failed = _release_github()
    failed.merge_results = [CommandResult(1, stderr="head conflict")]
    result, ledger, failed, _ = _build(
        tmp_path / "failed",
        _observations(),
        github=failed,
        variables=_vars(releases={REPO: "release-please"}, merge_enabled=True),
    )
    assert len(failed.merges) == 1
    assert result["counters"]["merge_skipped"] == 1
    assert ledger["repositories"][REPO]["reason"] == "merge_failed"


def test_failed_first_merge_does_not_consume_success_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = _release_github(two_repos=True)
    github.merge_results = [CommandResult(1), CommandResult(0)]
    result, ledger, github, _ = _build(
        tmp_path,
        _observations(),
        github=github,
        variables=_vars(
            releases={REPO: "release-please", CORE: "release-plz"},
            merge_enabled=True,
        ),
    )
    assert [plan.repo for plan in github.merges] == [CORE, REPO]
    assert result["counters"]["merged"] == 1
    assert result["counters"]["merge_skipped"] == 1
    assert ledger["repositories"][CORE]["reason"] == "merge_failed"
    assert ledger["repositories"][REPO]["reason"] == "merged"


def test_release_candidate_errors_and_busy_generator_are_isolated(tmp_path: Path) -> None:
    github = _release_github()
    github.numbers[REPO] = [10, 11]
    github.prs[(REPO, 11)] = [_pr(11)]
    result, ledger, _, _ = _build(
        tmp_path / "ambiguous",
        _observations(),
        github=github,
        variables=_vars(releases={REPO: "release-please"}),
    )
    assert result["counters"]["release_candidates"] == 2
    assert result["counters"]["merge_skipped"] == 1
    assert ledger["repositories"][REPO]["reason"] == "ambiguous_release_prs"

    busy = _release_github()
    busy.busy.add(REPO)
    _, ledger, _, _ = _build(
        tmp_path / "busy",
        _observations(),
        github=busy,
        variables=_vars(releases={REPO: "release-please"}),
    )
    assert ledger["repositories"][REPO]["reason"] == "release_generator_busy"


class QueueRunner:
    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), input_text))
        return self.results.pop(0)


def test_actstat_client_parses_rows_filters_scope_and_accepts_isolated_errors() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "type": "active_commit",
                    "repo": REPO,
                    "sha": SHA[:7],
                    "runs": [],
                }
            ),
            json.dumps(_commit(REPO, SHA)),
            json.dumps({"type": "repo_error", "repo": CORE, "error": "forbidden"}),
            json.dumps({"type": "repo_error", "repo": "missing-org", "error": "not found"}),
            json.dumps(_commit("other-org/other", "c" * 40)),
        ]
    )
    runner = QueueRunner(CommandResult(1, output))
    observations = ActstatClient("/actstat", runner).sweep([REPO, CORE])
    assert observations[REPO].active is not None
    assert observations[REPO].commit is not None
    assert observations[CORE].error == "forbidden"
    assert runner.calls[0][0] == ["/actstat", "-f", "jsonl"]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(1, stderr="auth failed"), "actstat failed"),
        (CommandResult(0, "{bad"), "malformed JSON"),
        (
            CommandResult(0, '{"type":"surprise","repo":"sase-org/sase"}'),
            "unknown record type",
        ),
        (
            CommandResult(
                0,
                "\n".join(
                    [
                        json.dumps(_commit(REPO, SHA)),
                        json.dumps(_commit(REPO, SHA)),
                    ]
                ),
            ),
            "duplicate commit",
        ),
        (CommandResult(0, '{"type":"commit","repo":"bad repo"}'), "invalid repository"),
    ],
)
def test_actstat_client_rejects_process_and_schema_failures(
    result: CommandResult,
    message: str,
) -> None:
    with pytest.raises(CiWatchError, match=message):
        ActstatClient("actstat", QueueRunner(result)).sweep([REPO])


def test_actstat_missing_expected_repo_becomes_row_error() -> None:
    observations = ActstatClient("actstat", QueueRunner(CommandResult(0))).sweep([REPO])
    assert observations[REPO].error == "missing_observation"


def test_github_reader_covers_queries_and_mutation_argv() -> None:
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
    }
    runner = QueueRunner(
        CommandResult(0, '{"default_branch":"master"}'),
        CommandResult(0, json.dumps({"sha": SHA})),
        CommandResult(0, '{"workflow_runs":[{"status":"queued"}]}'),
        CommandResult(
            0,
            '{"workflow_runs":[{"status":"in_progress","name":"Publish","path":"publish.yml"}]}',
        ),
        CommandResult(
            0,
            json.dumps(
                [
                    {"number": 10, "headRefName": "release-please--branches--master"},
                    {"number": 9, "headRefName": "feature"},
                ]
            ),
        ),
        CommandResult(0, json.dumps(pr_json)),
        CommandResult(0),
    )
    github = GitHubReader("/gh", runner)
    assert github.default_branch_head(REPO) == BranchHead("master", SHA)
    assert github.has_in_flight_runs(REPO, "master")
    assert github.generator_busy(REPO, "master", "release-please")
    assert github.release_pr_numbers(REPO) == [10]
    pr = github.release_pr(REPO, 10)
    assert pr == _pr()
    result = github.merge(MergePlan(REPO, pr))
    assert result.returncode == 0
    assert runner.calls[-1][0] == [
        "/gh",
        "pr",
        "merge",
        "10",
        "--repo",
        REPO,
        "--squash",
        "--match-head-commit",
        SHA,
    ]


@pytest.mark.parametrize(
    ("result", "method", "message"),
    [
        (CommandResult(1, stderr="no auth"), "head", "metadata failed"),
        (CommandResult(0, "[]"), "head", "metadata is not an object"),
        (CommandResult(0, "{bad"), "numbers", "malformed JSON"),
        (CommandResult(0, "{}"), "numbers", "not an array"),
        (
            CommandResult(0, '{"workflow_runs":[1]}'),
            "runs",
            "contain a non-object",
        ),
    ],
)
def test_github_reader_fails_closed_on_command_and_json_shapes(
    result: CommandResult,
    method: str,
    message: str,
) -> None:
    github = GitHubReader("gh", QueueRunner(result))
    with pytest.raises(CiWatchError, match=message):
        if method == "head":
            github.default_branch_head(REPO)
        elif method == "numbers":
            github.release_pr_numbers(REPO)
        else:
            github.has_in_flight_runs(REPO, "master")


def test_github_reader_rejects_untyped_workflow_labels() -> None:
    github = GitHubReader(
        "gh",
        QueueRunner(
            CommandResult(
                0,
                '{"workflow_runs":[{"status":"in_progress","name":1,"path":"publish.yml"}]}',
            )
        ),
    )
    with pytest.raises(CiWatchError, match="invalid name or path"):
        github.generator_busy(REPO, "master", "release-please")


def test_github_reader_rejects_unknown_workflow_status() -> None:
    github = GitHubReader(
        "gh",
        QueueRunner(CommandResult(0, '{"workflow_runs":[{"status":"mystery"}]}')),
    )
    with pytest.raises(CiWatchError, match="invalid status"):
        github.has_in_flight_runs(REPO, "master")


def test_agents_gate_parses_global_list_and_sends_json_notifications() -> None:
    runner = QueueRunner(
        CommandResult(0, '[{"name":"alpha"},{"agent_name":"beta"}]'),
        CommandResult(0, "id"),
    )
    agents = AgentsGate("/sase", runner)
    assert agents.probe() == AgentProbe(("alpha", "beta"))
    assert agents.notify("Merged release", "release")
    assert runner.calls[0][0] == ["/sase", "agent", "list", "-j"]
    assert runner.calls[1][0] == [
        "/sase",
        "notify",
        "create",
        "-s",
        "ci_watch",
    ]
    assert json.loads(runner.calls[1][1] or "{}") == {
        "notes": ["Merged release"],
        "tags": ["release"],
    }


def test_default_command_runner_captures_output_and_exec_errors() -> None:
    result = run_command(["sh", "-c", "read value; printf '%s' \"$value\""], input_text="ok\n")
    assert result == CommandResult(0, "ok", "")
    with pytest.raises(CiWatchError, match="failed to execute"):
        run_command(["/definitely/missing/ci-watch-command"])


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(1, stderr="failed"), "agent probe failed"),
        (CommandResult(0, "{}"), "non-array"),
        (CommandResult(0, "[1]"), "not an object"),
        (CommandResult(0, "[{}]"), "has no name"),
    ],
)
def test_agents_gate_fails_closed(result: CommandResult, message: str) -> None:
    with pytest.raises(CiWatchError, match=message):
        AgentsGate("sase", QueueRunner(result)).probe()


def test_pure_red_and_classification_matrix() -> None:
    cancelled = _commit(REPO, SHA, "failure", run_conclusion="cancelled")
    cancelled_failed = _commit(
        REPO,
        SHA,
        "failure",
        run_conclusion="cancelled",
        failed_job=True,
    )
    timed_out = _commit(REPO, SHA, "failure", run_conclusion="timed_out")
    assert not actionably_red(cancelled)
    assert actionably_red(cancelled_failed)
    assert actionably_red(timed_out)
    assert classify_repo(RepoObservation(REPO, commit=_commit(REPO, SHA))) is RepoState.GREEN
    assert classify_repo(RepoObservation(REPO, commit=cancelled_failed)) is RepoState.RED
    assert classify_repo(RepoObservation(REPO)) is RepoState.ERROR


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"repos": []}, "repos must contain"),
        ({"repos": [REPO, REPO]}, "duplicates"),
        ({"release_repositories": []}, "must be an object"),
        (
            {"release_repositories": {"other/repo": "release-please"}},
            "is not in repos",
        ),
        (
            {"release_repositories": {REPO: "unknown"}},
            "unsupported release generator",
        ),
        ({"merge_order": ["missing"]}, "unknown or ambiguous"),
        ({"max_merges_per_tick": 0}, "between 1 and 100"),
        ({"fix_enabled": "yes"}, "must be a boolean"),
        ({"gh_bin": ""}, "non-blank executable"),
    ],
)
def test_config_validation_is_strict(
    tmp_path: Path,
    changes: dict[str, Any],
    message: str,
) -> None:
    variables = _vars()
    variables.update(changes)
    with pytest.raises(CiWatchError, match=message):
        Config.from_invocation(_invocation(tmp_path, variables))


def test_release_pr_json_validation_and_near_miss_identity() -> None:
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
    }
    near_miss = ReleasePr.from_json(base)
    assert (
        plan_release_merge(
            REPO,
            RepoState.GREEN,
            "master",
            [near_miss],
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
