from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from rich.style import Style
from rich.text import Text
from sase.axe.chop_proposals import plan_chop_proposals, prepare_chop_proposals
from sase.xprompt.directives import extract_prompt_directives

from bugyi_chops.toobig_split import (
    CLAN_SUMMARY_FACTS_STYLE,
    CLAN_SUMMARY_HEADER_STYLE,
    CLAN_SUMMARY_MISSION_STYLE,
    CLAN_SUMMARY_SECTION_STYLE,
    CLAN_SUMMARY_WIDTH,
    _render_clan_summary,
    main,
)

CANONICAL_SUMMARY_PLAIN = """\
◆ TOOBIG SPLIT · 3 FILES
MISSION
Decompose oversized Python modules into focused, reviewable units
without changing behavior.
2 scan roots · limits 1,000 / 850 / 700 lines · sequential queue"""


def _fake_toobig(tmp_path: Path) -> Path:
    script = tmp_path / "fake-toobig"
    script.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$BUGYI_TEST_TOOBIG_CALLS"
if [ "${BUGYI_TEST_TOOBIG_FAIL_TREE:-}" = "$2" ]; then
    printf '%s\\n' "${BUGYI_TEST_TOOBIG_FAIL_DETAIL:-scanner exploded for $2}" >&2
    exit 23
fi
case "$2" in
    src) printf '%b' "${BUGYI_TEST_TOOBIG_SRC:-}" ;;
    tests) printf '%b' "${BUGYI_TEST_TOOBIG_TESTS:-}" ;;
    lib) printf '%b' "${BUGYI_TEST_TOOBIG_LIB:-}" ;;
    *) printf 'unexpected tree: %s\\n' "$2" >&2; exit 24 ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _fake_sase(tmp_path: Path, repo: Path) -> Path:
    fake_bin = tmp_path / "sase-bin"
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / "sase"
    project_json = json.dumps(
        {
            "workspace_dir": str(repo),
            "vcs_kind": "gh",
            "effective_project_name": "demo",
        },
        separators=(",", ":"),
    )
    script.write_text(
        f"""#!/bin/sh
set -eu
case "${{BUGYI_TEST_PROJECT_MODE:-ok}}" in
    ok)
        printf '%s\\n' '{project_json}'
        ;;
    fail) printf 'project unavailable\\n' >&2; exit 17 ;;
    invalid) printf '{{not-json\\n' ;;
    array) printf '[]\\n' ;;
    missing) printf '{{"workspace_dir":"{repo}"}}\\n' ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return fake_bin


def _target(repo: Path) -> dict[str, str]:
    return {
        "name": "demo",
        "workspace": "gh:example/demo",
        "workspace_dir": str(repo),
    }


def _prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/pkg/large.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src/pkg/shared.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "tests/large.py").write_text("z = 3\n", encoding="utf-8")
    return repo


def test_clan_summary_has_canonical_text_styles_and_width() -> None:
    summary = _render_clan_summary(3, 2, (1000, 850, 700))
    rendered = Text.from_markup(summary)

    assert rendered.plain == CANONICAL_SUMMARY_PLAIN
    lines = rendered.split("\n")
    assert len(lines) == 5
    assert [Style.parse(str(line.spans[0].style)) for line in lines] == [
        Style.parse(CLAN_SUMMARY_HEADER_STYLE),
        Style.parse(CLAN_SUMMARY_SECTION_STYLE),
        Style.parse(CLAN_SUMMARY_MISSION_STYLE),
        Style.parse(CLAN_SUMMARY_MISSION_STYLE),
        Style.parse(CLAN_SUMMARY_FACTS_STYLE),
    ]
    assert all(
        len(line.spans) == 1 and line.spans[0].start == 0 and line.spans[0].end == len(line)
        for line in lines
    )
    assert max(line.cell_len for line in lines) <= CLAN_SUMMARY_WIDTH


def test_clan_summary_handles_one_file_and_formats_custom_limits() -> None:
    summary = _render_clan_summary(1, 3, (12_000, 3_456, 1_001))
    rendered = Text.from_markup(summary)

    assert rendered.plain.splitlines() == [
        "◆ TOOBIG SPLIT · 1 FILE",
        "MISSION",
        "Decompose oversized Python modules into focused, reviewable units",
        "without changing behavior.",
        "3 scan roots · limits 12,000 / 3,456 / 1,001 lines · sequential queue",
    ]
    assert max(line.cell_len for line in rendered.split("\n")) <= CLAN_SUMMARY_WIDTH


def test_scan_deduplicates_files_and_emits_stable_wait_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    calls = tmp_path / "calls"
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(calls))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\nsrc/pkg/shared.py\\n")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_TESTS", "src/pkg/shared.py\\ntests/large.py\\n")

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
        verbose=True,
    )

    assert result["status"] == "ok"
    assert result["counters"] == {"files": 3, "proposals": 3, "trees": 2}
    proposals = result["proposed_launches"]
    assert [proposal["prompt"] for proposal in proposals] == [
        "%auto %wait(priority=20) #split_file:src/pkg/large.py",
        "%auto %wait(priority=20) #split_file:src/pkg/shared.py",
        "%auto %wait(priority=20) #split_file:tests/large.py",
    ]
    assert proposals[0]["wait_on"] is None
    assert proposals[1]["wait_on"] == proposals[0]["id"]
    assert proposals[2]["wait_on"] == proposals[1]["id"]
    assert [proposal["agent_name"] for proposal in proposals] == [
        "split_file.src.pkg.large.1a5de906",
        "split_file.src.pkg.shared.a534170a",
        "split_file.tests.large.56df040d",
    ]
    assert all("@" not in proposal["agent_name"] for proposal in proposals)
    assert all(proposal["clan"] == "toobig-@" for proposal in proposals)
    assert {proposal["clan_summary"] for proposal in proposals} == {proposals[0]["clan_summary"]}
    assert Text.from_markup(proposals[0]["clan_summary"]).plain == CANONICAL_SUMMARY_PLAIN
    assert all(proposal["workspace"] == "gh:example/demo" for proposal in proposals)
    assert len({proposal["dedupe_key"] for proposal in proposals}) == 3
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--files-only src 1000 850 700",
        "--files-only tests 1000 850 700",
    ]


def test_sase_planning_emits_one_summary_and_promotes_a_surviving_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\nsrc/pkg/shared.py\\n")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_TESTS", "tests/large.py\\n")
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )
    authored_summary = result["proposed_launches"][0]["clan_summary"]
    prepared = prepare_chop_proposals("toobig_split", result)
    assert {proposal.clan_summary for proposal in prepared} == {authored_summary}

    import sase.agent.names as agent_names

    monkeypatch.setattr(agent_names, "get_reserved_agent_names", set)
    monkeypatch.setattr(agent_names, "get_reserved_clan_names", set)
    monkeypatch.setattr(agent_names, "get_reserved_family_names", set)
    monkeypatch.setattr(agent_names, "agent_name_allocation_lock", nullcontext)

    plans = plan_chop_proposals(prepared)
    assert [plan.clan for plan in plans] == ["toobig-0"] * 3
    assert [plan.declares_clan for plan in plans] == [True, False, False]
    assert [plan.clan_summary for plan in plans] == [authored_summary, None, None]
    assert sum(plan.prompt.count("%clan(") for plan in plans) == 1
    assert sum(plan.prompt.count("summary=[[") for plan in plans) == 1
    assert f"%clan(toobig-0, tribe=chop, summary=[[{authored_summary}]])" in plans[0].prompt
    assert all("summary=[[" not in plan.prompt for plan in plans[1:])

    parsed = [extract_prompt_directives(plan.prompt)[1] for plan in plans]
    assert parsed[0].clan_declared
    assert parsed[0].clan == "toobig-0"
    assert parsed[0].clan_tribe == "chop"
    assert parsed[0].clan_summary == authored_summary
    assert all(not directives.clan_declared for directives in parsed[1:])
    assert all(directives.clan_summary is None for directives in parsed[1:])

    accepted_tail = [replace(prepared[1], wait_on=None), *prepared[2:]]
    tail_plans = plan_chop_proposals(accepted_tail)
    assert [plan.declares_clan for plan in tail_plans] == [True, False]
    assert [plan.clan_summary for plan in tail_plans] == [authored_summary, None]
    assert extract_prompt_directives(tail_plans[0].prompt)[1].clan_summary == authored_summary


def test_custom_tree_limits_and_legacy_env_target_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    (repo / "lib").mkdir()
    (repo / "lib/large.py").write_text("value = 1\n", encoding="utf-8")
    scanner = _fake_toobig(tmp_path)
    calls = tmp_path / "calls"
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(calls))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_LIB", "lib/large.py\\n")
    monkeypatch.setenv("SASE_TOOBIG_SPLIT_REPO_ROOT", str(repo))
    monkeypatch.setenv("SASE_TOOBIG_SPLIT_LAUNCH_REF", "#git:demo")
    monkeypatch.setenv("SASE_TOOBIG_SPLIT_TREES", "lib")
    monkeypatch.setenv("SASE_TOOBIG_SPLIT_LIMITS", "90 80 70")
    monkeypatch.setenv("SASE_TOOBIG_SPLIT_TOOBIG", str(scanner))

    result = run_chop_main(main, tmp_path, monkeypatch)

    proposal = result["proposed_launches"][0]
    assert proposal["workspace"] == "git:demo"
    assert proposal["prompt"] == "%auto %wait(priority=20) #split_file:lib/large.py"
    assert calls.read_text(encoding="utf-8").strip() == "--files-only lib 90 80 70"


def test_project_resolution_supplies_repo_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    fake_bin = _fake_sase(tmp_path, repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\n")

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target={"name": "demo"},
        variables={"toobig": str(scanner)},
    )

    assert result["status"] == "ok"
    assert result["proposed_launches"][0]["workspace"] == "gh:demo"


@pytest.mark.parametrize("mode", ["fail", "invalid", "array", "missing"])
def test_project_resolution_failures_are_typed_check_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
    mode: str,
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    fake_bin = _fake_sase(tmp_path, repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BUGYI_TEST_PROJECT_MODE", mode)

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target={"name": "demo"},
        variables={"toobig": str(scanner)},
    )

    assert result["status"] == "check_error"
    assert result["proposed_launches"] == []


def test_toobig_is_discovered_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    path_scanner = tmp_path / "toobig"
    scanner.rename(path_scanner)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))

    result = run_chop_main(main, tmp_path, monkeypatch, target=_target(repo))

    assert result["status"] == "no_op"


def test_no_oversized_files_is_a_typed_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "no_files_over_limits"
    assert result["proposed_launches"] == []


@pytest.mark.parametrize(
    ("variables", "output"),
    [
        ({"limits": [1, 2]}, ""),
        ({"limits": [1, 0, 3]}, ""),
        ({"limits": [1, "two", 3]}, ""),
        ({"trees": []}, ""),
        ({"trees": 42}, ""),
        ({"trees": "'unterminated"}, ""),
        ({}, "../outside.py\\n"),
        ({}, "white space.py\\n"),
    ],
)
def test_invalid_config_or_scanner_paths_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
    variables: dict[str, Any],
    output: str,
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", output)
    configured = {"toobig": str(scanner), **variables}

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables=configured,
    )

    assert result["status"] == "check_error"
    assert result["reason"] == "check_failed"
    assert result["proposed_launches"] == []


def test_scanner_failure_is_visible_as_check_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_FAIL_TREE", "src")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_FAIL_DETAIL", "x" * 600)

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )
    assert result["status"] == "check_error"
    assert result["counters"] == {"proposals": 0}


def test_absolute_scanner_paths_are_normalized_and_missing_files_still_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv(
        "BUGYI_TEST_TOOBIG_SRC",
        f"{repo / 'src/pkg/large.py'}\\n{repo / 'src/pkg/missing.py'}\\n",
    )

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )

    proposals = result["proposed_launches"]
    assert [proposal["prompt"] for proposal in proposals] == [
        "%auto %wait(priority=20) #split_file:src/pkg/large.py",
        "%auto %wait(priority=20) #split_file:src/pkg/missing.py",
    ]
    assert proposals[1]["dedupe_key"].endswith(":missing")


@pytest.mark.parametrize(
    "target",
    [
        {"workspace": "gh:example/demo"},
        {"workspace": "gh:example/demo", "workspace_dir": "/does/not/exist"},
    ],
)
def test_missing_repository_targets_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
    target: dict[str, str],
) -> None:
    result = run_chop_main(main, tmp_path, monkeypatch, target=target)
    assert result["status"] == "check_error"


def test_missing_launch_workspace_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        variables={"repo_root": str(repo)},
    )
    assert result["status"] == "check_error"


def test_toobig_never_calls_sase_or_creates_lock_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sase = fake_bin / "sase"
    fake_sase.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_sase.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\n")

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )

    assert result["status"] == "ok"
    assert list((tmp_path / "state").glob("*.lock")) == []
