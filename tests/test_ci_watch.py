from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sase.axe.chop_script_context import ChopScriptContext
from sase.chops import ChopArguments, ChopInvocation, ChopLogger, validate_chop_report
from sase.core.axe_chop_facade import validate_chop_result

import bugyi_chops.ci_watch as ci_watch_module
from bugyi_chops.ci_watch import (
    FIX_LEDGER_FILE_NAME,
    FIX_LEDGER_RETENTION_DAYS,
    MAX_GATE_POLLS_PER_TICK,
    MAX_GATED_FIXES,
    MAX_LEDGER_MERGES,
    MAX_LEDGER_NAMES,
    RELEASE_LEDGER_FILE_NAME,
    RELEASE_REPORT_FILE_NAME,
    ActstatClient,
    AgentProbe,
    AgentsGate,
    BranchHead,
    CiWatchError,
    CommandResult,
    Config,
    GitHubReader,
    HeadCiEvidence,
    LaunchGateClient,
    MergePlan,
    ReleasePr,
    RepoObservation,
    RepoState,
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
SHA = "a" * 40
CORE_SHA = "b" * 40
FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _clock_at(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _commit(
    repo: str,
    sha: str,
    conclusion: str = "success",
    *,
    run_conclusion: str | None = None,
    failed_job: bool = False,
    failed_job_name: str = "test token=do-not-leak",
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "conclusion": run_conclusion or conclusion,
        "url": f"https://github.com/{repo}/actions/runs/12",
        "jobs": [],
    }
    if failed_job:
        run["jobs"] = [
            {
                "name": failed_job_name,
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


def _head_evidence(
    sha: str,
    *,
    in_flight: bool = False,
    green: bool = False,
    failing_jobs: Sequence[str] = (),
    successful_jobs: Sequence[str] = (),
) -> HeadCiEvidence:
    return HeadCiEvidence(
        sha=sha,
        has_in_flight=in_flight,
        all_completed_green=green,
        failing_jobs=tuple(failing_jobs),
        successful_jobs=tuple(successful_jobs),
        run_url=f"https://github.com/{REPO}/actions/runs/99" if failing_jobs else None,
    )


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
    title: str = "chore(main): release 1.2.3",
    created_at: str = "2026-07-29T10:00:00Z",
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
        self.heads = {
            REPO: BranchHead("master", SHA),
            CORE: BranchHead("master", CORE_SHA),
        }
        self.in_flight: set[str] = set()
        self.workflow_counts: dict[str, int | Exception] = {REPO: 1, CORE: 1}
        self.head_evidence: dict[str, HeadCiEvidence | Exception] = {}
        self.head_evidence_calls: list[tuple[str, str]] = []
        self.numbers: dict[str, list[int] | Exception] = {REPO: [], CORE: []}
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

    def generator_busy(self, repo: str, branch: str, generator: str) -> bool:
        assert branch == "master"
        assert generator in {"release-please", "release-plz"}
        return repo in self.busy

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
        self.notifications: list[dict[str, Any]] = []

    def probe(self) -> AgentProbe:
        self.probes += 1
        if self.error:
            raise CiWatchError("probe failed")
        return AgentProbe(self.names)

    def notify(
        self,
        notes: Sequence[str],
        *,
        icon: str | None = None,
        action: str | None = None,
        action_data: dict[str, str] | None = None,
        tags: Sequence[str] = (),
    ) -> bool:
        self.notifications.append(
            {
                "notes": list(notes),
                "icon": icon,
                "action": action,
                "action_data": action_data,
                "tags": list(tags),
            }
        )
        return self.notify_ok


class FakeLaunchGate:
    def __init__(
        self,
        *,
        request_ids: Sequence[str | None] | None = None,
        create_error: str | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self._request_ids = list(request_ids) if request_ids is not None else None
        self._counter = 0
        self.create_error = create_error
        self.statuses = dict(statuses or {})
        self.payloads: list[dict[str, Any]] = []
        self.status_calls: list[str] = []

    def create(self, payload: dict[str, Any]) -> str | None:
        self.payloads.append(dict(payload))
        if self.create_error is not None:
            raise CiWatchError(self.create_error)
        self._counter += 1
        if self._request_ids is not None:
            index = self._counter - 1
            return self._request_ids[index] if index < len(self._request_ids) else None
        return f"launch-{self._counter}"

    def status(self, request_id: str) -> str | None:
        self.status_calls.append(request_id)
        return self.statuses.get(request_id)


def _vars(
    *,
    repos: Sequence[str] = (REPO, CORE),
    releases: dict[str, str] | None = None,
    merge_order: Sequence[str] = ("sase-core", "sase"),
    fix_enabled: bool = True,
    merge_enabled: bool = False,
    max_fixes: int = 1,
    max_merges: int = 1,
    red_debounce_ticks: int = 1,
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
        "red_debounce_ticks": red_debounce_ticks,
        "fix_enabled": fix_enabled,
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
        all_changespecs_file=str(tmp_path / "all.json"),
        filtered_changespecs_file=str(tmp_path / "filtered.json"),
        result_file=str(tmp_path / result_file),
        vars=variables,
    )
    return ChopInvocation(ChopArguments("context.json", False), context, ChopLogger())


def _build(
    tmp_path: Path,
    observations: dict[str, RepoObservation],
    *,
    github: FakeGitHub | None = None,
    agents: FakeAgents | None = None,
    launch_gate: FakeLaunchGate | None = None,
    variables: dict[str, Any] | None = None,
    result_file: str = "result.json",
    clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], FakeGitHub, FakeAgents]:
    github = github or FakeGitHub()
    agents = agents or FakeAgents()
    launch_gate = launch_gate or FakeLaunchGate()
    result = build_ci_watch_result(
        _invocation(tmp_path, variables or _vars(), result_file=result_file),
        actstat=FakeActstat(observations),  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        agents=agents,  # type: ignore[arg-type]
        launch_gate=launch_gate,  # type: ignore[arg-type]
        **({"clock": clock} if clock is not None else {}),
    ).to_dict()
    evidence = result["evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) == 1
    assert isinstance(evidence[0], str)
    ledger = json.loads((tmp_path / evidence[0]).read_text())
    return result, ledger, github, agents


def test_all_green_without_release_prs_is_noop(tmp_path: Path) -> None:
    result, ledger, _, agents = _build(tmp_path, _observations())

    assert result["status"] == "no_op"
    assert result["reason"] == "no_actions"
    assert result["counters"] == {
        "repos": 2,
        "green": 2,
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
    assert result["proposed_launches"] == []
    assert result["evidence"] == ["result.decisions.json"]
    assert ledger["repositories"][REPO]["reason"] == "green"
    assert agents.probes == 0

    validated = validate_chop_result(result)
    assert validated["report"]["title"] == "CI WATCH"


def test_ci_watch_report_rows_follow_repository_state(tmp_path: Path) -> None:
    result, _, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
    )

    report = result["report"]
    repositories = next(
        block
        for block in report["blocks"]
        if block["kind"] == "rows" and block["columns"][0] == "REPOSITORY"
    )
    rows = {row["cells"][0]: row for row in repositories["rows"]}
    assert (rows[REPO]["cells"][1], rows[REPO]["tone"], rows[REPO]["glyph"]) == (
        "red",
        "error",
        "▲",
    )
    assert (rows[CORE]["cells"][1], rows[CORE]["tone"], rows[CORE]["glyph"]) == (
        "green",
        "ok",
        "✓",
    )
    assert "streak 1/1" in rows[REPO]["cells"][2]
    validate_chop_result(result)


def test_ci_watch_check_error_still_emits_valid_report(
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


def test_ledger_file_is_unique_per_result_file(tmp_path: Path) -> None:
    first_result, first_ledger, _, _ = _build(
        tmp_path,
        _observations(),
        result_file="tick-one.json",
    )
    second_result, second_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(CORE,)),
        result_file="tick-two.json",
    )

    assert first_result["evidence"] == ["tick-one.decisions.json"]
    assert second_result["evidence"] == ["tick-two.decisions.json"]
    assert (tmp_path / "tick-one.decisions.json").is_file()
    assert (tmp_path / "tick-two.decisions.json").is_file()
    assert first_ledger["repositories"][CORE]["reason"] == "green"
    assert second_ledger["repositories"][CORE]["reason"] == "fix_gated"


def test_red_idle_files_one_pinned_sanitized_gate(tmp_path: Path) -> None:
    agents = FakeAgents()
    launch_gate = FakeLaunchGate()
    observations = _observations(red=(REPO,), active=(REPO,))
    result, ledger, _, agents = _build(
        tmp_path,
        observations,
        agents=agents,
        launch_gate=launch_gate,
    )

    assert result["status"] == "ok"
    assert result["counters"]["fix_gated"] == 1
    assert result["proposed_launches"] == []
    assert agents.notifications == []
    assert len(launch_gate.payloads) == 1
    payload = launch_gate.payloads[0]
    assert payload["schema_version"] == 1
    assert payload["approval"] == "required"
    assert payload["max_slots"] == 1
    assert REPO in payload["reason"]
    assert SHA[:7] in payload["reason"]
    prompt = payload["prompt"]
    assert prompt.startswith(f"#gh:{REPO} %i:ci_fix.sase.@ %w(runners=0)")
    assert "#pr(ci_fix_sase_aaaaaaa, status=ready)" in prompt
    assert f"#actstat(repo={REPO})" in prompt
    assert SHA in prompt
    assert "token=do-not-leak" not in prompt
    assert "[redacted]" in prompt
    gate = ledger["repositories"][REPO]["gate"]
    assert gate["agent_name"] == "ci_fix.sase.@"
    assert gate["agent_name"] != "ci_fix.sase"
    assert gate["request_id"] == "launch-1"
    dedupe_key = gate["dedupe_key"]
    assert SHA not in dedupe_key
    assert "token=" not in json.dumps(ledger).lower()
    fix_ledger = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())
    assert fix_ledger["version"] == 2
    assert fix_ledger["gates"][dedupe_key]["request_id"] == "launch-1"


def test_unrelated_agents_do_not_block_fixes_and_gates_are_capped(
    tmp_path: Path,
) -> None:
    launch_gate = FakeLaunchGate()
    observations = _observations(red=(REPO, CORE))
    result, ledger, _, _ = _build(
        tmp_path,
        observations,
        agents=FakeAgents(["interactive", "toobig-worker", "audit.waiting"]),
        launch_gate=launch_gate,
    )

    assert result["counters"]["agents_running"] == 3
    assert result["counters"]["fix_gated"] == 1
    assert result["counters"]["fix_suppressed"] == 1
    assert len(launch_gate.payloads) == 1
    assert ledger["repositories"][REPO]["reason"] == "fix_gated"
    assert ledger["repositories"][CORE]["reason"] == "fix_cap_reached"


def test_live_ci_fix_agent_suppresses_all_mature_red_repos(tmp_path: Path) -> None:
    launch_gate = FakeLaunchGate()
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO, CORE)),
        agents=FakeAgents(["interactive", "ci_fix.sase"]),
        launch_gate=launch_gate,
    )

    assert result["counters"]["agents_running"] == 2
    assert result["counters"]["fix_suppressed"] == 2
    assert result["proposed_launches"] == []
    assert launch_gate.payloads == []
    assert ledger["repositories"][REPO]["reason"] == "fix_in_flight"
    assert ledger["repositories"][CORE]["in_flight_agents"] == ["ci_fix.sase"]


@pytest.mark.parametrize(
    ("agent_names", "expected_reason"),
    [
        (("ci_fix",), "fix_in_flight"),
        (("ci_fix.sase",), "fix_in_flight"),
        (("ci_fix.sase.child",), "fix_in_flight"),
        (("ci_fixer",), "fix_gated"),
        (("ci_fixing.sase",), "fix_gated"),
    ],
)
def test_ci_fix_hood_matching(
    tmp_path: Path,
    agent_names: tuple[str, ...],
    expected_reason: str,
) -> None:
    launch_gate = FakeLaunchGate()
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        agents=FakeAgents(agent_names),
        launch_gate=launch_gate,
    )

    assert ledger["repositories"][REPO]["reason"] == expected_reason
    assert bool(launch_gate.payloads) is (expected_reason == "fix_gated")
    assert result["proposed_launches"] == []


def test_ci_fix_ledger_names_are_bounded_and_redacted(tmp_path: Path) -> None:
    secret_name = f"ci_fix.token=do-not-leak{'.x' * 100}"
    _, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        agents=FakeAgents([secret_name] * (MAX_LEDGER_NAMES + 1)),
    )

    names = ledger["repositories"][REPO]["in_flight_agents"]
    assert len(names) == MAX_LEDGER_NAMES
    assert all(len(name) <= 100 for name in names)
    assert "do-not-leak" not in json.dumps(names)


def test_agent_probe_failure_suppresses_fixes(tmp_path: Path) -> None:
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO, CORE)),
        agents=FakeAgents(error=True),
    )

    assert result["counters"]["fix_suppressed"] == 2
    assert result["proposed_launches"] == []
    assert ledger["repositories"][REPO]["reason"] == "agents_check_failed"


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
            "green",
            "green",
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
            "newer_head_unsettled",
        ),
        (
            RepoObservation(REPO, commit=_commit(REPO, SHA)),
            "in_flight",
            "green",
            "green",
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
    assert result["proposed_launches"] == []


def test_older_red_stays_actionable_while_head_is_unsettled(tmp_path: Path) -> None:
    new_head = "c" * 40
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_head)
    github.head_evidence[REPO] = _head_evidence(new_head, in_flight=True)
    launch_gate = FakeLaunchGate()

    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,), active=(REPO,)),
        github=github,
        launch_gate=launch_gate,
    )

    assert result["counters"]["red"] == 1
    prompt = launch_gate.payloads[0]["prompt"]
    assert f"Pinned failing commit: {SHA[:7]}" in prompt
    assert new_head in prompt
    assert "older than the current unsettled HEAD" in prompt
    assert ledger["repositories"][REPO]["classification_reason"] == "head_unsettled"
    assert ledger["repositories"][REPO]["head_unsettled"] is True


def test_newer_head_red_evidence_takes_precedence(tmp_path: Path) -> None:
    new_head = "c" * 40
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_head)
    github.head_evidence[REPO] = _head_evidence(
        new_head,
        failing_jobs=("new failing job",),
    )
    launch_gate = FakeLaunchGate()

    _, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
        launch_gate=launch_gate,
    )

    prompt = launch_gate.payloads[0]["prompt"]
    assert f"Pinned failing commit: {new_head}" in prompt
    assert "new failing job" in prompt
    assert ledger["repositories"][REPO]["failing_jobs"] == ["new failing job"]


def test_newer_success_supersedes_same_failing_job(tmp_path: Path) -> None:
    new_head = "c" * 40
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_head)
    github.head_evidence[REPO] = _head_evidence(
        new_head,
        in_flight=True,
        successful_jobs=("test [redacted]",),
    )

    result, ledger, _, agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
    )

    assert result["counters"]["green"] == 2
    assert result["proposed_launches"] == []
    assert agents.probes == 0
    assert ledger["repositories"][REPO]["reason"] == "superseded_by_newer_success"


def test_supersession_query_failure_is_isolated_to_repo(tmp_path: Path) -> None:
    new_head = "c" * 40
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_head)
    github.head_evidence[REPO] = CiWatchError("HEAD query failed")

    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
    )

    assert result["counters"]["errors"] == 1
    assert result["counters"]["green"] == 1
    assert ledger["repositories"][REPO]["reason"] == "HEAD query failed"


def test_head_evidence_queries_have_a_per_tick_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "bugyi_chops.ci_watch.MAX_HEAD_EVIDENCE_REPOS_PER_TICK",
        0,
    )
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", "c" * 40)

    result, ledger, github, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
    )

    assert result["counters"]["errors"] == 1
    assert ledger["repositories"][REPO]["reason"] == "HEAD evidence per-tick query limit reached"
    assert github.head_evidence_calls == []


def test_pure_terminal_decision_fail_closed_and_fallback_matrix() -> None:
    head = BranchHead("master", "c" * 40)
    assert decide_repo(RepoObservation(REPO, error="forbidden"), head).state is RepoState.ERROR

    red_without_jobs = RepoObservation(
        REPO,
        commit=_commit(REPO, SHA, "failure", run_conclusion="failure"),
    )
    decision = decide_repo(red_without_jobs, BranchHead("master", SHA))
    assert (decision.state, decision.reason) == (
        RepoState.ERROR,
        "red_evidence_missing_job_identity",
    )

    older_red = RepoObservation(
        REPO,
        commit=_commit(REPO, SHA, "failure", failed_job=True),
    )
    decision = decide_repo(older_red, head)
    assert (decision.state, decision.reason) == (
        RepoState.ERROR,
        "missing_head_ci_evidence",
    )

    decision = decide_repo(
        older_red,
        head,
        head_evidence=_head_evidence(head.sha, green=True),
    )
    assert (decision.state, decision.reason) == (RepoState.GREEN, "green")

    decision = decide_repo(
        older_red,
        head,
        head_evidence=_head_evidence(head.sha),
    )
    assert (decision.state, decision.reason) == (
        RepoState.PENDING,
        "superseded_or_unsettled",
    )

    pending = RepoObservation(REPO, commit=_commit(REPO, SHA, "cancelled"))
    decision = decide_repo(pending, BranchHead("master", SHA))
    assert (decision.state, decision.reason) == (
        RepoState.PENDING,
        "superseded_or_unsettled",
    )


def test_red_debounce_persists_and_resets_on_changed_fingerprint(
    tmp_path: Path,
) -> None:
    variables = _vars(red_debounce_ticks=2)
    launch_gate = FakeLaunchGate()
    first, first_ledger, _, first_agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        variables=variables,
        launch_gate=launch_gate,
    )
    _, second_ledger, _, second_agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        variables=variables,
        launch_gate=launch_gate,
    )

    assert first["proposed_launches"] == []
    assert first["counters"]["red_debounce_suppressed"] == 1
    assert first_ledger["repositories"][REPO]["reason"] == "red_debounce"
    assert first_agents.probes == 0
    assert len(launch_gate.payloads) == 1
    assert second_ledger["repositories"][REPO]["streak"] == 2
    assert second_agents.probes == 1

    changed_path = tmp_path / "changed"
    _build(
        changed_path,
        _observations(red=(REPO,)),
        variables=variables,
    )
    changed = _observations(red=(REPO,))
    changed[REPO] = RepoObservation(
        REPO,
        commit=_commit(
            REPO,
            SHA,
            "failure",
            failed_job=True,
            failed_job_name="different job",
        ),
    )
    changed_result, changed_ledger, _, _ = _build(
        changed_path,
        changed,
        variables=variables,
    )
    assert changed_result["proposed_launches"] == []
    assert changed_ledger["repositories"][REPO]["streak"] == 1


def test_intervening_green_and_corrupt_streak_reset_debounce(tmp_path: Path) -> None:
    variables = _vars(red_debounce_ticks=2)
    _build(tmp_path, _observations(red=(REPO,)), variables=variables)
    _build(tmp_path, _observations(), variables=variables)
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        variables=variables,
    )
    assert result["proposed_launches"] == []
    assert ledger["repositories"][REPO]["streak"] == 1

    (tmp_path / "ci_watch_red_streaks.json").write_text("{broken", encoding="utf-8")
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        variables=variables,
    )
    assert result["proposed_launches"] == []
    assert ledger["repositories"][REPO]["streak"] == 1


def test_dedupe_key_is_stable_across_head_shas(tmp_path: Path) -> None:
    launch_gate = FakeLaunchGate()
    _, first_ledger, _, _ = _build(tmp_path, _observations(red=(REPO,)), launch_gate=launch_gate)
    new_sha = "c" * 40
    observations = _observations()
    observations[REPO] = RepoObservation(
        REPO,
        commit=_commit(REPO, new_sha, "failure", failed_job=True),
    )
    github = FakeGitHub()
    github.heads[REPO] = BranchHead("master", new_sha)
    _, second_ledger, _, _ = _build(tmp_path, observations, github=github, launch_gate=launch_gate)

    assert first_ledger["repositories"][REPO]["reason"] == "fix_gated"
    assert second_ledger["repositories"][REPO]["reason"] == "already_gated"
    assert len(launch_gate.payloads) == 1


def test_green_starts_a_new_fix_episode(tmp_path: Path) -> None:
    first_launch_gate = FakeLaunchGate()
    first, first_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        clock=_clock_at(FIXED_NOW),
        launch_gate=first_launch_gate,
    )
    _, green_ledger, _, _ = _build(
        tmp_path,
        _observations(),
        clock=_clock_at(FIXED_NOW + timedelta(minutes=5)),
    )
    second_launch_gate = FakeLaunchGate()
    second, second_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        clock=_clock_at(FIXED_NOW + timedelta(minutes=10)),
        launch_gate=second_launch_gate,
    )

    first_key = first_ledger["repositories"][REPO]["gate"]["dedupe_key"]
    second_key = second_ledger["repositories"][REPO]["gate"]["dedupe_key"]
    assert first_key.endswith(":e0")
    assert second_key.endswith(":e1")
    assert first_key != second_key
    assert first_ledger["repositories"][REPO]["fix_episode"] == 0
    assert green_ledger["repositories"][REPO]["fix_episode"] == 1
    assert second_ledger["repositories"][REPO]["fix_episode"] == 1
    assert first["counters"]["fix_gated"] == 1
    assert second["counters"]["fix_gated"] == 1
    fix_ledger = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())
    assert fix_ledger["version"] == 2
    assert fix_ledger["repos"][REPO] == {"episode": 1, "red": True}


@pytest.mark.parametrize("middle_state", ["pending", "no_ci"])
def test_unsettled_state_does_not_start_a_new_fix_episode(
    tmp_path: Path,
    middle_state: str,
) -> None:
    launch_gate = FakeLaunchGate()
    _, first_ledger, _, _ = _build(tmp_path, _observations(red=(REPO,)), launch_gate=launch_gate)
    observations = _observations()
    github = FakeGitHub()
    if middle_state == "pending":
        observations[REPO] = RepoObservation(
            REPO,
            commit=_commit(REPO, SHA, "cancelled"),
        )
    else:
        observations[REPO] = RepoObservation(REPO, error="missing_observation")
        github.workflow_counts[REPO] = 0
    _, middle_ledger, _, _ = _build(tmp_path, observations, github=github)
    _, second_ledger, _, _ = _build(tmp_path, _observations(red=(REPO,)), launch_gate=launch_gate)

    assert middle_ledger["repositories"][REPO]["state"] == middle_state
    assert middle_ledger["repositories"][REPO]["fix_episode"] == 0
    assert second_ledger["repositories"][REPO]["fix_episode"] == 0
    assert first_ledger["repositories"][REPO]["reason"] == "fix_gated"
    assert second_ledger["repositories"][REPO]["reason"] == "already_gated"
    assert len(launch_gate.payloads) == 1


def test_pending_gate_suppresses_a_different_red_repo(tmp_path: Path) -> None:
    launch_gate = FakeLaunchGate(statuses={"launch-1": "pending"})
    _, first_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )
    assert first_ledger["repositories"][REPO]["reason"] == "fix_gated"

    second, second_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(CORE,)),
        launch_gate=launch_gate,
    )

    assert second["counters"]["gate_pending_suppressed"] == 1
    assert second_ledger["repositories"][CORE]["reason"] == "gate_pending"
    assert len(launch_gate.payloads) == 1
    assert launch_gate.status_calls == ["launch-1"]


@pytest.mark.parametrize("terminal_status", ["answered", "cancelled", "timeout"])
def test_terminal_gate_status_does_not_resurrect_dedupe_key(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    launch_gate = FakeLaunchGate(statuses={"launch-1": terminal_status})
    _, first_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )
    assert first_ledger["repositories"][REPO]["reason"] == "fix_gated"

    _, second_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )

    assert second_ledger["repositories"][REPO]["reason"] == "already_gated"
    assert len(launch_gate.payloads) == 1


def test_gate_pending_probe_is_bounded_per_tick(tmp_path: Path) -> None:
    launch_gate = FakeLaunchGate()
    gates = {
        f"ci_fix:{REPO}:{index:016x}:e0": {
            "request_id": f"launch-{index}",
            "created_at": FIXED_NOW.isoformat(),
        }
        for index in range(MAX_GATE_POLLS_PER_TICK + 5)
    }
    (tmp_path / FIX_LEDGER_FILE_NAME).write_text(
        json.dumps({"version": 2, "repos": {}, "gates": gates}),
        encoding="utf-8",
    )

    _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
        clock=_clock_at(FIXED_NOW),
    )

    assert len(launch_gate.status_calls) == MAX_GATE_POLLS_PER_TICK


def test_gate_creation_failure_counts_errors_and_leaves_key_unrecorded(
    tmp_path: Path,
) -> None:
    launch_gate = FakeLaunchGate(create_error="exit_code=2 detail=boom")
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )

    assert result["status"] == "no_op"
    assert result["counters"]["gate_errors"] == 1
    assert result["counters"]["fix_gated"] == 0
    assert ledger["repositories"][REPO]["reason"] == "gate_failed"
    assert "boom" in ledger["repositories"][REPO]["gate_error"]
    fix_ledger = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())
    assert fix_ledger["gates"] == {}

    retry_gate = FakeLaunchGate()
    retry_result, retry_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=retry_gate,
    )
    assert retry_result["counters"]["fix_gated"] == 1
    assert retry_ledger["repositories"][REPO]["reason"] == "fix_gated"


def test_gate_with_unparsable_descriptor_still_records_dedupe_key(tmp_path: Path) -> None:
    launch_gate = FakeLaunchGate(request_ids=[None])
    result, ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )

    assert result["counters"]["fix_gated"] == 1
    assert result["counters"]["gate_errors"] == 0
    gate = ledger["repositories"][REPO]["gate"]
    assert gate["request_id"] is None
    dedupe_key = gate["dedupe_key"]
    fix_ledger = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())
    assert fix_ledger["gates"][dedupe_key]["request_id"] is None

    _, second_ledger, _, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        launch_gate=launch_gate,
    )
    assert second_ledger["repositories"][REPO]["reason"] == "already_gated"


def test_fix_ledger_absence_and_corruption_fall_back_to_empty(tmp_path: Path) -> None:
    ledger_path = tmp_path / FIX_LEDGER_FILE_NAME
    assert not ledger_path.exists()
    first_launch_gate = FakeLaunchGate()
    _, first_ledger, _, _ = _build(
        tmp_path, _observations(red=(REPO,)), launch_gate=first_launch_gate
    )
    assert first_ledger["repositories"][REPO]["reason"] == "fix_gated"
    assert ledger_path.is_file()

    ledger_path.write_text('{"version":2,"repos":', encoding="utf-8")
    second_launch_gate = FakeLaunchGate()
    _, second_ledger, _, _ = _build(
        tmp_path, _observations(red=(REPO,)), launch_gate=second_launch_gate
    )
    assert second_ledger["repositories"][REPO]["reason"] == "fix_gated"
    recovered = json.loads(ledger_path.read_text())
    assert recovered["version"] == 2
    assert recovered["repos"][REPO] == {"episode": 0, "red": True}


def test_v1_fix_ledger_loads_as_empty_and_is_rewritten_as_v2(tmp_path: Path) -> None:
    (tmp_path / FIX_LEDGER_FILE_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {REPO: {"episode": 3, "red": True}},
                "announced": {f"ci_fix:{REPO}:{'a' * 16}:e3": FIXED_NOW.isoformat()},
            }
        ),
        encoding="utf-8",
    )

    _, ledger, _, _ = _build(
        tmp_path,
        _observations(),
        clock=_clock_at(FIXED_NOW),
    )

    assert ledger["repositories"][REPO]["fix_episode"] == 0
    migrated = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())
    assert migrated["version"] == 2
    assert migrated["gates"] == {}


def test_fix_ledger_prunes_unconfigured_expired_and_overflow_gates(
    tmp_path: Path,
) -> None:
    extra_repo = "example/unconfigured"
    oldest = FIXED_NOW - timedelta(days=FIX_LEDGER_RETENTION_DAYS + 1)
    valid_keys = [f"ci_fix:{REPO}:{index:016x}:e0" for index in range(MAX_GATED_FIXES + 3)]
    gates = {
        key: {
            "request_id": f"launch-{index}",
            "created_at": (FIXED_NOW - timedelta(minutes=len(valid_keys) - index)).isoformat(),
        }
        for index, key in enumerate(valid_keys)
    }
    gates[f"ci_fix:{REPO}:ffffffffffffffff:e99"] = {
        "request_id": "launch-old",
        "created_at": oldest.isoformat(),
    }
    gates[f"ci_fix:{extra_repo}:eeeeeeeeeeeeeeee:e0"] = {
        "request_id": "launch-extra",
        "created_at": FIXED_NOW.isoformat(),
    }
    (tmp_path / FIX_LEDGER_FILE_NAME).write_text(
        json.dumps(
            {
                "version": 2,
                "repos": {
                    REPO: {"episode": 0, "red": False},
                    extra_repo: {"episode": 7, "red": True},
                },
                "gates": gates,
            }
        ),
        encoding="utf-8",
    )

    _build(
        tmp_path,
        _observations(),
        clock=_clock_at(FIXED_NOW),
    )
    pruned = json.loads((tmp_path / FIX_LEDGER_FILE_NAME).read_text())

    assert extra_repo not in pruned["repos"]
    assert len(pruned["gates"]) == MAX_GATED_FIXES
    assert set(pruned["gates"]) == set(valid_keys[-MAX_GATED_FIXES:])
    assert all(f":{extra_repo}:" not in key for key in pruned["gates"])


@pytest.mark.parametrize(
    ("workflow_count", "expected_state", "expected_reason"),
    [
        (0, "no_ci", "no_ci"),
        (1, "error", "missing_observation"),
        (CiWatchError("probe failed"), "error", "missing_observation"),
    ],
)
def test_missing_observation_distinguishes_no_ci(
    tmp_path: Path,
    workflow_count: int | Exception,
    expected_state: str,
    expected_reason: str,
) -> None:
    github = FakeGitHub()
    github.workflow_counts[REPO] = workflow_count
    observations = _observations()
    observations[REPO] = RepoObservation(REPO, error="missing_observation")

    result, ledger, _, _ = _build(tmp_path, observations, github=github)

    counter = "errors" if expected_state == "error" else expected_state
    assert result["counters"][counter] == 1
    assert ledger["repositories"][REPO]["state"] == expected_state
    assert ledger["repositories"][REPO]["reason"] == expected_reason


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
    assert result["status"] == "no_op"
    assert result["reason"] == "no_actions"
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
        clock=lambda: FIXED_NOW,
    )
    assert result["counters"]["release_candidates"] == 2
    assert result["counters"]["merged"] == 1
    assert result["counters"]["merge_skipped"] == 1
    assert [plan.repo for plan in github.merges] == [CORE]
    assert ledger["repositories"][CORE]["reason"] == "merged"
    assert ledger["repositories"][REPO]["reason"] == "merge_cap_reached"
    assert agents.notifications == [
        {
            "notes": [
                f"Merged release PR #20 for {CORE}",
                "1 merged today · 1 pending",
            ],
            "icon": "🚢",
            "action": "ViewReport",
            "action_data": {
                "report_path": str((tmp_path / RELEASE_REPORT_FILE_NAME).resolve()),
                "report_title": "Releases",
            },
            "tags": ["ci", "release"],
        }
    ]


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


def _published_release_report(tmp_path: Path) -> dict[str, Any]:
    document = json.loads((tmp_path / RELEASE_REPORT_FILE_NAME).read_text())
    assert isinstance(document, dict)
    assert validate_chop_report(document) == document
    return cast(dict[str, Any], document)


def _report_rows(
    report: dict[str, Any],
    columns: list[str],
) -> list[dict[str, Any]]:
    block = next(
        block
        for block in report["blocks"]
        if block.get("kind") == "rows" and block.get("columns") == columns
    )
    rows = block["rows"]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    return cast(list[dict[str, Any]], rows)


def test_red_repo_release_pr_is_observed_and_reported_without_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = _release_github()
    result, ledger, github, _ = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
        variables=_vars(
            releases={REPO: "release-please"},
            fix_enabled=False,
            merge_enabled=True,
        ),
        clock=lambda: FIXED_NOW,
    )

    assert result["counters"]["release_candidates"] == 1
    assert result["counters"]["merged"] == 0
    assert github.merges == []
    assert ledger["repositories"][REPO]["release_reason"] == "default_branch_not_green"
    pending = _report_rows(
        _published_release_report(tmp_path),
        ["REPOSITORY", "PR", "STATE", "AGE"],
    )
    assert pending[0]["cells"][:3] == [REPO, "#10", "base branch not green"]
    assert pending[0]["tone"] == "error"


def test_release_ledger_accumulates_prunes_and_recovers_from_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    variables = _vars(
        releases={REPO: "release-please"},
        merge_enabled=True,
    )
    first = _release_github()
    _build(
        tmp_path,
        _observations(),
        github=first,
        variables=variables,
        result_file="first.json",
        clock=lambda: FIXED_NOW,
    )
    second = FakeGitHub()
    second.numbers[REPO] = [11]
    second.prs[(REPO, 11)] = [
        _pr(11, title="chore: release v2.3.4", created_at="2026-07-29T11:00:00Z")
    ]
    _build(
        tmp_path,
        _observations(),
        github=second,
        variables=variables,
        result_file="second.json",
        clock=lambda: FIXED_NOW + timedelta(minutes=5),
    )
    ledger_path = tmp_path / RELEASE_LEDGER_FILE_NAME
    ledger = json.loads(ledger_path.read_text())
    assert [row["number"] for row in ledger["merges"]] == [10, 11]
    assert [row["version"] for row in ledger["merges"]] == ["1.2.3", "2.3.4"]

    many_merges = [
        {
            "repo": REPO,
            "number": index + 1,
            "url": f"https://github.com/{REPO}/pull/{index + 1}",
            "head_oid": SHA,
            "version": f"1.0.{index}",
            "generator": "release-please",
            "merged_at": (FIXED_NOW - timedelta(days=91 if index == 0 else 1)).isoformat(),
        }
        for index in range(MAX_LEDGER_MERGES + 3)
    ]
    ledger_path.write_text(
        json.dumps({"version": 1, "merges": many_merges, "announced_pending": {}})
    )
    _build(
        tmp_path,
        _observations(),
        variables=_vars(),
        result_file="prune.json",
        clock=lambda: FIXED_NOW,
    )
    pruned = json.loads(ledger_path.read_text())
    assert len(pruned["merges"]) == MAX_LEDGER_MERGES
    assert all(row["number"] != 1 for row in pruned["merges"])

    ledger_path.write_text("{corrupt")
    _build(
        tmp_path,
        _observations(),
        variables=_vars(),
        result_file="corrupt.json",
        clock=lambda: FIXED_NOW,
    )
    assert json.loads(ledger_path.read_text()) == {
        "announced_pending": {},
        "merges": [],
        "version": 1,
    }


def test_invalid_release_report_does_not_replace_last_good_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build(
        tmp_path,
        _observations(),
        variables=_vars(),
        result_file="good.json",
        clock=lambda: FIXED_NOW,
    )
    report_path = tmp_path / RELEASE_REPORT_FILE_NAME
    previous = report_path.read_bytes()

    def reject_report(report: dict[str, Any]) -> dict[str, Any]:
        del report
        raise ValueError("synthetic invalid report")

    monkeypatch.setattr(ci_watch_module, "validate_chop_report", reject_report)
    _build(
        tmp_path,
        _observations(),
        variables=_vars(),
        result_file="invalid.json",
        clock=lambda: FIXED_NOW + timedelta(minutes=5),
    )
    assert report_path.read_bytes() == previous


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("chore(main): release 0.9.3", "0.9.3"),
        ("chore: release v0.9.3", "0.9.3"),
        ("refresh release metadata", "-"),
    ],
)
def test_release_version_extraction(title: str, expected: str) -> None:
    assert ci_watch_module._extract_release_version(title) == expected


def test_blocked_release_notification_is_debounced_and_not_repeated(
    tmp_path: Path,
) -> None:
    agents = FakeAgents()
    github = FakeGitHub()
    github.numbers[REPO] = [10]
    github.prs[(REPO, 10)] = [_pr(checks=(("IN_PROGRESS", ""),), created_at="2026-07-29T10:00:00Z")]
    variables = _vars(releases={REPO: "release-please"})

    for tick in range(3):
        _build(
            tmp_path,
            _observations(),
            github=github,
            agents=agents,
            variables=variables,
            result_file=f"blocked-{tick}.json",
            clock=_clock_at(FIXED_NOW + timedelta(minutes=5 * tick)),
        )
    assert len(agents.notifications) == 1
    assert agents.notifications[0]["notes"] == [
        f"Release PR #10 for {REPO} needs attention: checks not green"
    ]
    assert agents.notifications[0]["action"] == "ViewReport"
    assert agents.notifications[0]["tags"] == ["ci", "release"]

    github.numbers[REPO] = []
    _build(
        tmp_path,
        _observations(),
        github=github,
        agents=agents,
        variables=variables,
        result_file="closed.json",
        clock=lambda: FIXED_NOW + timedelta(minutes=15),
    )
    github.numbers[REPO] = [11]
    github.prs[(REPO, 11)] = [
        _pr(11, checks=(("IN_PROGRESS", ""),), created_at="2026-07-29T12:00:00Z")
    ]
    for tick in range(2):
        _build(
            tmp_path,
            _observations(),
            github=github,
            agents=agents,
            variables=variables,
            result_file=f"new-{tick}.json",
            clock=_clock_at(FIXED_NOW + timedelta(minutes=20 + 5 * tick)),
        )
    assert [notification["notes"][0] for notification in agents.notifications] == [
        f"Release PR #10 for {REPO} needs attention: checks not green",
        f"Release PR #11 for {REPO} needs attention: checks not green",
    ]


def test_release_observation_failure_is_report_only_and_does_not_block_other_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SASE_CHOP_DRY_RUN", "0")
    github = _release_github(two_repos=True)
    github.numbers[REPO] = CiWatchError("gh unavailable")
    agents = FakeAgents()
    result, ledger, github, agents = _build(
        tmp_path,
        _observations(red=(REPO,)),
        github=github,
        agents=agents,
        variables=_vars(
            releases={REPO: "release-please", CORE: "release-plz"},
            merge_enabled=True,
        ),
        clock=lambda: FIXED_NOW,
    )

    assert result["counters"]["fix_gated"] == 1
    assert result["counters"]["merged"] == 1
    assert [plan.repo for plan in github.merges] == [CORE]
    assert ledger["repositories"][REPO]["reason"] == "fix_gated"
    assert ledger["repositories"][REPO]["release_reason"] == "gh unavailable"
    assert {notification["tags"][0] for notification in agents.notifications} == {"ci"}
    assert any(notification["tags"] == ["ci", "release"] for notification in agents.notifications)
    pending = _report_rows(
        _published_release_report(tmp_path),
        ["REPOSITORY", "PR", "STATE", "AGE"],
    )
    repo_row = next(row for row in pending if row["cells"][0] == REPO)
    assert repo_row["tone"] == "error"
    assert "gh unavailable" in repo_row["cells"][2]


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
        "title": "chore(main): release 1.2.3",
        "createdAt": "2026-07-29T10:00:00Z",
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


def test_github_reader_probes_workflows_and_bounded_head_job_evidence() -> None:
    runs = {
        "total_count": 3,
        "workflow_runs": [
            {"id": 3, "status": "in_progress", "conclusion": None},
            {
                "id": 2,
                "status": "completed",
                "conclusion": "failure",
                "html_url": f"https://github.com/{REPO}/actions/runs/2",
            },
            {"id": 1, "status": "completed", "conclusion": "success"},
        ],
    }
    failed_jobs = {
        "total_count": 2,
        "jobs": [
            {"name": "lint", "status": "completed", "conclusion": "failure"},
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
    assert github.head_ci_evidence(REPO, SHA) == HeadCiEvidence(
        sha=SHA,
        has_in_flight=True,
        all_completed_green=False,
        failing_jobs=("lint",),
        successful_jobs=("test",),
        run_url=f"https://github.com/{REPO}/actions/runs/2",
    )
    assert runner.calls[0][0] == [
        "/gh",
        "api",
        "-X",
        "GET",
        f"repos/{REPO}/actions/workflows",
        "-f",
        "per_page=1",
    ]
    assert f"head_sha={SHA}" in runner.calls[1][0]
    assert runner.calls[2][0][-3:] == [
        f"repos/{REPO}/actions/runs/2/jobs",
        "-f",
        "per_page=100",
    ]


def test_github_reader_head_evidence_recognizes_all_green_and_latest_job_result() -> None:
    runs = {
        "total_count": 1,
        "workflow_runs": [{"id": 2, "status": "completed", "conclusion": "success"}],
    }
    jobs = {
        "total_count": 1,
        "jobs": [{"name": "test", "status": "completed", "conclusion": "success"}],
    }
    github = GitHubReader(
        "gh",
        QueueRunner(
            CommandResult(0, json.dumps(runs)),
            CommandResult(0, json.dumps(jobs)),
        ),
    )
    evidence = github.head_ci_evidence(REPO, SHA)
    assert evidence.all_completed_green is True
    assert evidence.successful_jobs == ("test",)
    assert evidence.failing_jobs == ()

    retry_runs = {
        "total_count": 2,
        "workflow_runs": [
            {"id": 2, "status": "completed", "conclusion": "success"},
            {"id": 1, "status": "completed", "conclusion": "failure"},
        ],
    }
    success = {
        "total_count": 1,
        "jobs": [{"name": "test", "status": "completed", "conclusion": "success"}],
    }
    failure = {
        "total_count": 1,
        "jobs": [{"name": "test", "status": "completed", "conclusion": "failure"}],
    }
    github = GitHubReader(
        "gh",
        QueueRunner(
            CommandResult(0, json.dumps(retry_runs)),
            CommandResult(0, json.dumps(success)),
            CommandResult(0, json.dumps(failure)),
        ),
    )
    evidence = github.head_ci_evidence(REPO, SHA)
    assert evidence.failing_jobs == ()
    assert evidence.successful_jobs == ("test",)


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        (['{"total_count":"zero"}'], "invalid total_count"),
        (["[]"], "workflows are not an object"),
    ],
)
def test_github_reader_rejects_invalid_workflow_probe(
    payloads: list[str],
    message: str,
) -> None:
    github = GitHubReader("gh", QueueRunner(*(CommandResult(0, raw) for raw in payloads)))
    with pytest.raises(CiWatchError, match=message):
        github.workflow_count(REPO)


@pytest.mark.parametrize(
    ("runs", "jobs", "message"),
    [
        ({"workflow_runs": [], "total_count": 21}, None, "bounded query limit"),
        ({"workflow_runs": [1], "total_count": 1}, None, "invalid collection"),
        (
            {"workflow_runs": [{"status": "mystery"}], "total_count": 1},
            None,
            "invalid status",
        ),
        (
            {
                "workflow_runs": [{"id": 1, "status": "completed", "conclusion": None}],
                "total_count": 1,
            },
            None,
            "invalid conclusion",
        ),
        (
            {
                "workflow_runs": [{"id": 0, "status": "completed", "conclusion": "success"}],
                "total_count": 1,
            },
            None,
            "invalid id",
        ),
        (
            {
                "workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success"}],
                "total_count": 1,
            },
            {"jobs": [{"name": 1}], "total_count": 1},
            "invalid fields",
        ),
        (
            {
                "workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success"}],
                "total_count": 1,
            },
            {
                "jobs": [{"name": "test", "status": "completed", "conclusion": None}],
                "total_count": 1,
            },
            "invalid conclusion",
        ),
        (
            {
                "workflow_runs": [{"id": 1, "status": "completed", "conclusion": "failure"}],
                "total_count": 1,
            },
            {
                "jobs": [{"name": "test", "status": "completed", "conclusion": "success"}],
                "total_count": 1,
            },
            "no failing job identity",
        ),
    ],
)
def test_github_reader_head_evidence_fails_closed_on_bounded_shapes(
    runs: dict[str, Any],
    jobs: dict[str, Any] | None,
    message: str,
) -> None:
    results = [CommandResult(0, json.dumps(runs))]
    if jobs is not None:
        results.append(CommandResult(0, json.dumps(jobs)))
    github = GitHubReader("gh", QueueRunner(*results))
    with pytest.raises(CiWatchError, match=message):
        github.head_ci_evidence(REPO, SHA)


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
    assert agents.notify(
        ["Merged release", "1 merged today · 0 pending"],
        icon="🚢",
        tags=["release"],
        action="ViewReport",
        action_data={"/fake": "value"},
    )
    assert runner.calls[0][0] == ["/sase", "agent", "list", "-j"]
    assert runner.calls[1][0] == [
        "/sase",
        "notify",
        "create",
        "-s",
        "ci_watch",
    ]
    assert json.loads(runner.calls[1][1] or "{}") == {
        "notes": ["Merged release", "1 merged today · 0 pending"],
        "tags": ["release"],
        "icon": "🚢",
        "action": "ViewReport",
        "action_data": {"/fake": "value"},
    }


def test_default_command_runner_captures_output_and_exec_errors() -> None:
    result = run_command(["sh", "-c", "read value; printf '%s' \"$value\""], input_text="ok\n")
    assert result == CommandResult(0, "ok", "")
    with pytest.raises(CiWatchError, match="failed to execute"):
        run_command(["/definitely/missing/ci-watch-command"])


def test_launch_gate_client_creates_request_with_stable_cwd_and_reads_status() -> None:
    runner = QueueRunner(
        CommandResult(0, json.dumps({"request_id": "launch-abc"})),
        CommandResult(0, json.dumps({"status": "pending"})),
    )
    client = LaunchGateClient("/sase", runner)

    request_id = client.create(
        {
            "schema_version": 1,
            "prompt": "hi",
            "reason": "r",
            "approval": "required",
            "max_slots": 1,
        }
    )
    assert request_id == "launch-abc"
    argv, _, cwd = runner.calls[0]
    assert argv[:3] == ["/sase", "launch", "request"]
    assert argv[argv.index("-o") + 1] == "json"
    assert argv[argv.index("-s") + 1] == "ci_watch"
    assert cwd == str(Path.home())
    temp_file = Path(argv[argv.index("-f") + 1])
    assert not temp_file.exists()

    assert client.status("launch-abc") == "pending"
    assert runner.calls[1][0] == ["/sase", "gate", "show", "-k", "launch", "-i", "launch-abc", "-j"]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(0, "{bad"),
        CommandResult(0, json.dumps({"request_id": 5})),
        CommandResult(0, json.dumps({})),
    ],
)
def test_launch_gate_client_create_returns_none_on_unparsable_descriptor(
    result: CommandResult,
) -> None:
    client = LaunchGateClient("/sase", QueueRunner(result))
    assert client.create({"prompt": "hi"}) is None


def test_launch_gate_client_create_raises_on_nonzero_exit() -> None:
    client = LaunchGateClient("/sase", QueueRunner(CommandResult(1, stderr="boom")))
    with pytest.raises(CiWatchError, match="launch request failed"):
        client.create({"prompt": "hi"})


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CommandResult(1, stderr="boom"), None),
        (CommandResult(0, "{bad"), None),
        (CommandResult(0, json.dumps({"status": "mystery"})), None),
        (CommandResult(0, json.dumps({"status": "answered"})), "answered"),
    ],
)
def test_launch_gate_client_status_fails_closed(
    result: CommandResult,
    expected: str | None,
) -> None:
    client = LaunchGateClient("/sase", QueueRunner(result))
    assert client.status("launch-abc") == expected


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
        "title": "chore(main): release 1.2.3",
        "createdAt": "2026-07-29T10:00:00Z",
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
    with pytest.raises(CiWatchError, match="missing or invalid 'title'"):
        ReleasePr.from_json({**base, "title": ""})
    with pytest.raises(CiWatchError, match="invalid createdAt"):
        ReleasePr.from_json({**base, "createdAt": "not-a-date"})
