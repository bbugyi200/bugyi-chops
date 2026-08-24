from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from sase.agent.names import iter_agent_name_key_markers
from sase.axe.chop_proposal_launch import launch_chop_proposals
from sase.axe.chop_proposals import plan_chop_proposals, prepare_chop_proposals
from sase.core.axe_chop_facade import validate_chop_result
from sase.feature_flags import override_flags
from sase.xprompt.directives import extract_prompt_directives

from bugyi_chops._common import safe_fragment
from bugyi_chops.toobig_split import (
    CLAN_SUMMARY_FACTS_STYLE,
    CLAN_SUMMARY_FYI_STYLE,
    CLAN_SUMMARY_HEADER_STYLE,
    CLAN_SUMMARY_MAX_ROWS,
    CLAN_SUMMARY_MISSION_STYLE,
    CLAN_SUMMARY_NEUTRAL_STYLE,
    CLAN_SUMMARY_SECTION_STYLE,
    CLAN_SUMMARY_VIOLATION_STYLE,
    CLAN_SUMMARY_WARNING_STYLE,
    CLAN_SUMMARY_WIDTH,
    PROPOSAL_MODEL,
    FileEntry,
    _admission_prompt,
    _agent_name,
    _elide_path,
    _line_count,
    _path_digest,
    _render_clan_summary,
    main,
)

MODEL_DIRECTIVE = f"%model:{PROPOSAL_MODEL}"

MISSION_LINES = [
    "MISSION",
    "Decompose oversized Python modules into focused, reviewable units",
    "without changing behavior.",
]


def _parse_condition_prompt(prompt: str, path: str, floor: int) -> tuple[str, str]:
    assert prompt.count("%if::") == 1
    assert prompt.count("```bash") == 1
    assert prompt.endswith(f"%auto %wait(priority=20) #split_file:{path}")
    with override_flags(typed_launch_units=True):
        cleaned, directives = extract_prompt_directives(prompt)
    assert directives.if_code is not None
    assert directives.if_code.language == "bash"
    body = directives.if_code.source
    assert f"path={shlex.quote(path)}" in body
    assert f"line_count >= {floor}" in body
    assert "%if" not in cleaned
    assert "line_count" not in cleaned
    assert f"#split_file:{path}" in cleaned
    return cleaned, body


def _condition_body(path: str, floor: int) -> str:
    return _parse_condition_prompt(_admission_prompt(path, floor), path, floor)[1]


def _run_condition(repo: Path, body: str) -> int:
    return subprocess.run(
        ["bash", "-c", body],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n" * count, encoding="utf-8")


def _known_project_resolver(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_type="git",
        ref="demo",
        workspace_dir=str(repo),
        project_file=str(repo / "demo.sase"),
    )


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
exit "${BUGYI_TEST_TOOBIG_EXIT:-0}"
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


def _assert_raw_proposals_use_medium_model(proposals: list[dict[str, Any]]) -> None:
    assert proposals
    assert all(proposal["model"] == PROPOSAL_MODEL for proposal in proposals)


def _assert_planned_prompts_use_medium_model(prompts: list[str]) -> None:
    assert prompts
    assert all(prompt.count(MODEL_DIRECTIVE) == 1 for prompt in prompts)
    assert all(prompt.count("%model:") == 1 for prompt in prompts)
    parsed = [extract_prompt_directives(prompt)[1] for prompt in prompts]
    assert all(directives.model == "medium" for directives in parsed)
    assert all(getattr(directives, "model_alias", "medium") == "medium" for directives in parsed)


def _keyed_markers(value: str) -> list[str]:
    return [
        marker.id
        for marker in iter_agent_name_key_markers(value)
        if marker.braced and marker.id is not None
    ]


def _assert_keyed_basename_template(agent_name: str, path: str) -> None:
    assert agent_name == _agent_name(path)
    assert _keyed_markers(agent_name) == [_path_digest(path)]
    slug = safe_fragment(Path(path).stem, fallback="file")
    assert agent_name == f"{slug}.{{@{_path_digest(path)}}}"
    assert "split_file." not in agent_name
    assert "/" not in agent_name
    assert "\\" not in agent_name


def _freeze_agent_name_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    import sase.agent.names as agent_names

    monkeypatch.setattr(agent_names, "get_reserved_agent_names", set)
    monkeypatch.setattr(agent_names, "get_reserved_clan_names", set)
    monkeypatch.setattr(agent_names, "get_reserved_family_names", set)
    monkeypatch.setattr(agent_names, "agent_name_allocation_lock", nullcontext)


def _capturing_launcher(
    dispatched: list[str],
    tmp_path: Path,
    repo: Path,
) -> Callable[..., list[SimpleNamespace]]:
    def _launch(prompt: str, *, extra_env: dict[str, str]) -> list[SimpleNamespace]:
        dispatched.append(prompt)
        assert extra_env["SASE_CHOP_NAME"] == "toobig_split"
        directives = extract_prompt_directives(prompt)[1]
        name = directives.name or f"captured-{len(dispatched)}"
        return [
            SimpleNamespace(
                pid=500 + len(dispatched),
                agent_name=name,
                workspace_num=2,
                workspace_dir=str(repo),
                project_name="demo",
                workflow_name="ace(run)-260823_120000",
                cl_name="demo",
                timestamp="260823_120000",
                artifacts_dir=str(tmp_path / "artifacts" / "20260823120000"),
            )
        ]

    return _launch


def _prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/pkg/large.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src/pkg/shared.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "tests/large.py").write_text("z = 3\n", encoding="utf-8")
    return repo


def _scan_three_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
    repo: Path,
) -> dict[str, Any]:
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\nsrc/pkg/shared.py\\n")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_TESTS", "tests/large.py\\n")
    return run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )


def test_clan_summary_has_canonical_text_styles_and_width() -> None:
    summary = _render_clan_summary(
        [
            FileEntry("sase/ace/tui/app.py", 1_214),
            FileEntry("sase/axe/run_agent_runner.py", 902),
            FileEntry("tests/deep/path/test_foo.py", 731),
        ],
        2,
        (1000, 850, 700),
    )
    rendered = Text.from_markup(summary)

    assert rendered.plain.splitlines() == [
        "◆ TOOBIG SPLIT · 3 FILES",
        *MISSION_LINES,
        "",
        "TARGETS",
        "▲ 1,214  sase/ace/tui/app.py",
        "◆   902  sase/axe/run_agent_runner.py",
        "•   731  tests/deep/path/test_foo.py",
        "",
        "2 scan roots · limits 1,000 / 850 / 700 lines · sequential queue",
    ]
    lines = rendered.split("\n")
    styled_lines = [line for line in lines if line.plain]
    assert [Style.parse(str(line.spans[0].style)) for line in styled_lines] == [
        Style.parse(CLAN_SUMMARY_HEADER_STYLE),
        Style.parse(CLAN_SUMMARY_SECTION_STYLE),
        Style.parse(CLAN_SUMMARY_MISSION_STYLE),
        Style.parse(CLAN_SUMMARY_MISSION_STYLE),
        Style.parse(CLAN_SUMMARY_SECTION_STYLE),
        Style.parse(CLAN_SUMMARY_VIOLATION_STYLE),
        Style.parse(CLAN_SUMMARY_WARNING_STYLE),
        Style.parse(CLAN_SUMMARY_FYI_STYLE),
        Style.parse(CLAN_SUMMARY_FACTS_STYLE),
    ]
    assert all(
        len(line.spans) == 1 and line.spans[0].start == 0 and line.spans[0].end == len(line)
        for line in styled_lines
    )
    assert max(line.cell_len for line in lines) <= CLAN_SUMMARY_WIDTH
    assert "]]" not in summary


def test_clan_summary_handles_one_file_and_formats_custom_limits() -> None:
    summary = _render_clan_summary(
        [FileEntry("src/only.py", 1_002)],
        3,
        (12_000, 3_456, 1_001),
    )
    rendered = Text.from_markup(summary)

    assert rendered.plain.splitlines() == [
        "◆ TOOBIG SPLIT · 1 FILE",
        *MISSION_LINES,
        "",
        "TARGETS",
        "• 1,002  src/only.py",
        "",
        "3 scan roots · limits 12,000 / 3,456 / 1,001 lines · sequential queue",
    ]
    assert max(line.cell_len for line in rendered.split("\n")) <= CLAN_SUMMARY_WIDTH


def test_clan_summary_renders_mixed_severities_with_redundant_glyphs() -> None:
    summary = _render_clan_summary(
        [
            FileEntry("src/neutral.py", 700),
            FileEntry("src/fyi.py", 701),
            FileEntry("src/warning.py", 851),
            FileEntry("src/violation.py", 1_001),
        ],
        1,
        (1000, 850, 700),
    )
    lines = Text.from_markup(summary).split("\n")
    target_rows = lines[6:10]

    assert [line.plain for line in target_rows] == [
        "▲ 1,001  src/violation.py",
        "◆   851  src/warning.py",
        "•   701  src/fyi.py",
        "·   700  src/neutral.py",
    ]
    assert [Style.parse(str(line.spans[0].style)) for line in target_rows] == [
        Style.parse(CLAN_SUMMARY_VIOLATION_STYLE),
        Style.parse(CLAN_SUMMARY_WARNING_STYLE),
        Style.parse(CLAN_SUMMARY_FYI_STYLE),
        Style.parse(CLAN_SUMMARY_NEUTRAL_STYLE),
    ]


def test_clan_summary_elides_a_long_path_from_the_left() -> None:
    long_path = "src/" + "/".join(["deeply_nested_package"] * 6) + "/test_foo.py"
    summary = _render_clan_summary(
        [FileEntry(long_path, 1_234)],
        1,
        (1000, 850, 700),
    )
    target_row = Text.from_markup(summary).split("\n")[6]
    rendered_path = target_row.plain.split("  ", 1)[1]

    assert rendered_path.startswith("…/")
    assert rendered_path.endswith("/test_foo.py")
    assert long_path not in target_row.plain
    assert target_row.cell_len <= CLAN_SUMMARY_WIDTH
    assert _elide_path("src/short.py", 20) == "src/short.py"
    assert cell_len(_elide_path(long_path, 20)) <= 20


def test_clan_summary_sorts_unknown_counts_last_and_aligns_count_column() -> None:
    summary = _render_clan_summary(
        [
            FileEntry("src/missing.py", None),
            FileEntry("src/small.py", 9),
            FileEntry("src/largest.py", 12_345),
        ],
        1,
        (1000, 850, 700),
    )
    target_rows = Text.from_markup(summary).split("\n")[6:9]

    assert [line.plain for line in target_rows] == [
        "▲ 12,345  src/largest.py",
        "·      9  src/small.py",
        "·      ?  src/missing.py",
    ]
    assert Style.parse(str(target_rows[-1].spans[0].style)) == Style.parse(
        CLAN_SUMMARY_NEUTRAL_STYLE
    )
    assert len({line.plain.index("src/") for line in target_rows}) == 1


def test_clan_summary_caps_target_rows_and_reports_overflow() -> None:
    entries = [
        FileEntry(f"src/file_{index:02}.py", 2_000 - index)
        for index in range(CLAN_SUMMARY_MAX_ROWS + 2)
    ]
    summary = _render_clan_summary(entries, 2, (1000, 850, 700))
    lines = Text.from_markup(summary).split("\n")
    target_block = lines[6:-2]

    assert lines[0].plain == f"◆ TOOBIG SPLIT · {len(entries)} FILES"
    assert len(target_block) == CLAN_SUMMARY_MAX_ROWS + 1
    assert target_block[-1].plain == "…and 2 more"
    assert Style.parse(str(target_block[-1].spans[0].style)) == Style.parse(
        CLAN_SUMMARY_FACTS_STYLE
    )
    assert max(line.cell_len for line in lines) <= CLAN_SUMMARY_WIDTH


def test_line_count_uses_newline_semantics_and_handles_missing_files(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"first\nsecond\nunterminated")

    assert _line_count(source) == 2
    assert _line_count(tmp_path / "missing.py") is None


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
    _assert_raw_proposals_use_medium_model(proposals)
    paths = ["src/pkg/large.py", "src/pkg/shared.py", "tests/large.py"]
    for proposal, path in zip(proposals, paths, strict=True):
        _parse_condition_prompt(proposal["prompt"], path, 700)
    assert all(not proposal.get("dedupe_key") for proposal in proposals)
    assert proposals[0]["wait_on"] is None
    assert proposals[1]["wait_on"] == proposals[0]["id"]
    assert proposals[2]["wait_on"] == proposals[1]["id"]
    for proposal, path in zip(proposals, paths, strict=True):
        _assert_keyed_basename_template(proposal["agent_name"], path)
    assert proposals[0]["agent_name"] != proposals[2]["agent_name"]
    assert proposals[0]["agent_name"].startswith("large.{@")
    assert proposals[2]["agent_name"].startswith("large.{@")
    assert all(proposal["clan"] == "toobig-@" for proposal in proposals)
    assert {proposal["clan_summary"] for proposal in proposals} == {proposals[0]["clan_summary"]}
    summary_plain = Text.from_markup(proposals[0]["clan_summary"]).plain
    assert "◆ TOOBIG SPLIT · 3 FILES" in summary_plain
    assert "2 scan roots · limits 1,000 / 850 / 700 lines · sequential queue" in summary_plain
    assert "src/pkg/large.py" in summary_plain
    assert "src/pkg/shared.py" in summary_plain
    assert "tests/large.py" in summary_plain
    assert all(proposal["workspace"] == "gh:example/demo" for proposal in proposals)
    report_rows = next(block for block in result["report"]["blocks"] if block["kind"] == "rows")[
        "rows"
    ]
    assert {row["cells"][1] for row in report_rows} == {
        "src/pkg/large.py",
        "src/pkg/shared.py",
        "tests/large.py",
    }
    assert all(row["tone"] == "muted" and row["glyph"] == "·" for row in report_rows)
    validate_chop_result(result)
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "--files-only src 1000 850 700",
        "--files-only tests 1000 850 700",
    ]


def test_scan_agent_name_uses_keyed_basename_not_parent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    long_path = "src/pkg/section_abcdefghijklmnopqrstuvwxyz0123456789/trailing_module.py"
    (repo / long_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / long_path).write_text("value = 1\n", encoding="utf-8")
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", f"{long_path}\n")

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner)},
    )

    assert result["status"] == "ok"
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
    name = result["proposed_launches"][0]["agent_name"]
    _assert_keyed_basename_template(name, long_path)
    assert name.startswith("trailing_module.{@")
    assert "section_abcdefghijklmnopqrstuvwxyz0123456789" not in name


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
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
    prepared = prepare_chop_proposals("toobig_split", result)
    assert {proposal.clan_summary for proposal in prepared} == {authored_summary}
    assert all(proposal.model == PROPOSAL_MODEL for proposal in prepared)

    _freeze_agent_name_allocation(monkeypatch)

    plans = plan_chop_proposals(prepared)
    assert [plan.clan for plan in plans] == ["toobig-0"] * 3
    assert [plan.agent_name for plan in plans] == [
        "toobig-0.large.0",
        "toobig-0.shared.0",
        "toobig-0.large.1",
    ]
    assert [plan.declares_clan for plan in plans] == [True, False, False]
    assert [plan.clan_summary for plan in plans] == [authored_summary, None, None]
    assert sum(plan.prompt.count("%clan(") for plan in plans) == 1
    assert sum(plan.prompt.count("summary=[[") for plan in plans) == 1
    assert f"%clan(toobig-0, tribe=chop, summary=[[{authored_summary}]])" in plans[0].prompt
    assert all("summary=[[" not in plan.prompt for plan in plans[1:])
    _assert_planned_prompts_use_medium_model([plan.prompt for plan in plans])

    parsed = [extract_prompt_directives(plan.prompt)[1] for plan in plans]
    assert parsed[0].clan_declared
    assert parsed[0].clan == "toobig-0"
    assert parsed[0].clan_tribe == "chop"
    assert parsed[0].clan_summary == authored_summary
    assert all(not directives.clan_declared for directives in parsed[1:])
    assert all(directives.clan_summary is None for directives in parsed[1:])

    accepted_tail = [replace(prepared[1], wait_on=None), *prepared[2:]]
    tail_plans = plan_chop_proposals(accepted_tail)
    assert [plan.agent_name for plan in tail_plans] == [
        "toobig-0.shared.0",
        "toobig-0.large.0",
    ]
    assert [plan.declares_clan for plan in tail_plans] == [True, False]
    assert [plan.clan_summary for plan in tail_plans] == [authored_summary, None]
    assert extract_prompt_directives(tail_plans[0].prompt)[1].clan_summary == authored_summary
    _assert_planned_prompts_use_medium_model([plan.prompt for plan in tail_plans])


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
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
    assert proposal["workspace"] == "git:demo"
    _parse_condition_prompt(proposal["prompt"], "lib/large.py", 70)
    assert not proposal.get("dedupe_key")
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
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
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
    assert result["report"]["title"] == "TOOBIG SPLIT"
    assert result["report"]["blocks"][0] == {
        "kind": "headline",
        "text": "Every scanned file is within limits",
        "tone": "ok",
    }
    validate_chop_result(result)


def test_hard_limit_exit_1_emits_actionable_violation_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = _prepare_repo(tmp_path)
    oversized = repo / "src/pkg/large.py"
    oversized.write_text("x\n" * 1001, encoding="utf-8")
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "src/pkg/large.py\\n")
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_EXIT", "1")

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner), "trees": ["src"]},
    )

    assert result["status"] == "ok"
    assert result["counters"] == {"files": 1, "proposals": 1, "trees": 1}
    proposal = result["proposed_launches"][0]
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
    _parse_condition_prompt(proposal["prompt"], "src/pkg/large.py", 700)
    assert not proposal.get("dedupe_key")
    _assert_keyed_basename_template(proposal["agent_name"], "src/pkg/large.py")
    assert proposal["clan"] == "toobig-@"
    summary_plain = Text.from_markup(proposal["clan_summary"]).plain
    assert "◆ TOOBIG SPLIT · 1 FILE" in summary_plain
    assert "▲ 1,001  src/pkg/large.py" in summary_plain
    assert result["report"]["blocks"][0] == {
        "kind": "headline",
        "text": "1 files over limits",
        "tone": "error",
    }
    report_rows = next(block for block in result["report"]["blocks"] if block["kind"] == "rows")[
        "rows"
    ]
    assert report_rows == [
        {
            "cells": ["1001", "src/pkg/large.py"],
            "tone": "error",
            "glyph": "▲",
        }
    ]
    validate_chop_result(result)


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("", "1"),
        ("\\n", "1"),
        ("src/pkg/large.py\\n", "2"),
    ],
)
def test_empty_exit_1_and_other_scanner_failures_are_check_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
    output: str,
    exit_code: str,
) -> None:
    repo = _prepare_repo(tmp_path)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", output)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_EXIT", exit_code)

    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner), "trees": ["src"]},
    )

    assert result["status"] == "check_error"
    assert result["reason"] == "check_failed"
    assert result["proposed_launches"] == []
    assert result["counters"] == {"proposals": 0}
    headline = result["report"]["blocks"][0]
    assert headline["tone"] == "error"
    assert f"exit_code={exit_code}" in headline["text"]
    assert "scanner failed" in headline["text"]
    validate_chop_result(result)


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
    assert result["report"]["blocks"][0]["tone"] == "error"
    validate_chop_result(result)


def test_absolute_scanner_paths_are_normalized_and_missing_files_are_condition_gated(
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
    _assert_raw_proposals_use_medium_model(proposals)
    _parse_condition_prompt(proposals[0]["prompt"], "src/pkg/large.py", 700)
    _, missing_condition = _parse_condition_prompt(
        proposals[1]["prompt"], "src/pkg/missing.py", 700
    )
    assert _run_condition(repo, missing_condition) == 1
    assert all(not proposal.get("dedupe_key") for proposal in proposals)
    assert "· ?" in Text.from_markup(proposals[1]["clan_summary"]).plain


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
    _assert_raw_proposals_use_medium_model(result["proposed_launches"])
    assert list((tmp_path / "state").glob("*.lock")) == []


@pytest.mark.parametrize(
    ("line_count", "exit_code"),
    [
        (699, 1),
        (700, 0),
        (701, 0),
    ],
)
def test_condition_body_gates_at_configured_floor(
    tmp_path: Path,
    line_count: int,
    exit_code: int,
) -> None:
    repo = tmp_path / "repo"
    path = "src/pkg/large.py"
    _write_lines(repo / path, line_count)

    assert _run_condition(repo, _condition_body(path, 700)) == exit_code


def test_condition_body_skips_deleted_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = "src/pkg/large.py"
    source = repo / path
    _write_lines(source, 700)
    body = _condition_body(path, 700)
    source.unlink()

    assert _run_condition(repo, body) == 1


def test_condition_body_surfaces_read_failures(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 files")
    repo = tmp_path / "repo"
    path = "src/pkg/unreadable.py"
    source = repo / path
    _write_lines(source, 700)
    source.chmod(0)
    try:
        assert _run_condition(repo, _condition_body(path, 700)) not in {0, 1}
    finally:
        source.chmod(0o600)


def test_condition_body_quotes_metacharacter_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = "src/pkg/$(touch${IFS}INJECTED).py"
    _write_lines(repo / path, 700)

    body = _condition_body(path, 700)

    assert f"path={shlex.quote(path)}" in body
    assert _run_condition(repo, body) == 0
    assert not (repo / "INJECTED").exists()


def test_agent_name_key_is_stable_and_collision_safe_for_shared_basenames() -> None:
    first = "src/pkg/large.py"
    second = "tests/large.py"
    first_name = _agent_name(first)
    second_name = _agent_name(second)

    _assert_keyed_basename_template(first_name, first)
    _assert_keyed_basename_template(second_name, second)
    assert first_name == _agent_name(first)
    assert first_name != second_name
    assert _keyed_markers(first_name) != _keyed_markers(second_name)


def test_sase_bridge_skips_stale_queued_files_without_agent_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    pytest.importorskip("sase_core_rs")
    repo = _prepare_repo(tmp_path)
    paths = ["src/pkg/large.py", "src/pkg/shared.py"]
    for path in paths:
        _write_lines(repo / path, 701)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "".join(f"{path}\n" for path in paths))
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner), "trees": ["src"]},
    )
    for path in paths:
        _write_lines(repo / path, 699)
    monkeypatch.setenv("SASE_HOME", str(tmp_path / ".sase"))
    monkeypatch.setattr(
        "sase.agent.launch_cwd_common.resolve_known_project_vcs_launch_ref",
        lambda _prompt: _known_project_resolver(repo),
    )

    def _unexpected_launch(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("stale toobig_split proposal allocated an agent")

    with override_flags(typed_launch_units=True):
        launches = launch_chop_proposals(
            lumberjack_name="maintenance",
            chop_name="toobig_split",
            run_id="run-stale",
            proposals=prepare_chop_proposals("toobig_split", result),
            launch_agent_from_cwd_fn=_unexpected_launch,
            launch_agents_from_cwd_fn=_unexpected_launch,
        )

    summary = launches.admission_result.summary
    assert list(launches) == []
    assert launches.typed_admission is not None
    assert launches.admission_result.admission_complete
    assert summary is not None
    assert summary.total == 2
    assert summary.launched == 0
    assert summary.skipped == 2
    assert summary.condition_errors == 0
    assert summary.launch_errors == 0


def test_sase_bridge_launches_eligible_file_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    pytest.importorskip("sase_core_rs")
    repo = _prepare_repo(tmp_path)
    path = "src/pkg/large.py"
    _write_lines(repo / path, 700)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", f"{path}\n")
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner), "trees": ["src"]},
    )
    monkeypatch.setenv("SASE_HOME", str(tmp_path / ".sase"))
    monkeypatch.setattr(
        "sase.agent.launch_cwd_common.resolve_known_project_vcs_launch_ref",
        lambda _prompt: _known_project_resolver(repo),
    )
    dispatched: list[str] = []
    authored_summary = result["proposed_launches"][0]["clan_summary"]
    _freeze_agent_name_allocation(monkeypatch)
    prepared = prepare_chop_proposals("toobig_split", result)
    plans = plan_chop_proposals(prepared)
    planned = extract_prompt_directives(plans[0].prompt)[1]
    assert plans[0].agent_name == "toobig-0.large.0"
    assert planned.clan == "toobig-0"
    assert planned.clan_declared is True
    assert planned.clan_tribe == "chop"
    assert planned.clan_summary == authored_summary

    with override_flags(typed_launch_units=True):
        launches = launch_chop_proposals(
            lumberjack_name="maintenance",
            chop_name="toobig_split",
            run_id="run-eligible",
            proposals=prepared,
            launch_plans=plans,
            launch_agent_from_cwd_fn=lambda *_args, **_kwargs: None,
            launch_agents_from_cwd_fn=_capturing_launcher(dispatched, tmp_path, repo),
        )

    summary = launches.admission_result.summary
    assert len(launches) == 1
    assert launches.typed_admission is not None
    assert launches.admission_result.admission_complete
    assert summary is not None
    assert summary.launched == 1
    assert summary.skipped == 0
    assert summary.condition_errors == 0
    assert summary.launch_errors == 0
    assert len(dispatched) == 1
    assert "%if" not in dispatched[0]
    assert "line_count" not in dispatched[0]
    directives = extract_prompt_directives(dispatched[0])[1]
    assert directives.name == "toobig-0.large.0"
    assert "split_file" not in (directives.name or "")
    assert launches[0]["clan"] == "toobig-0"
    assert launches[0]["member_id"] == "large.0"
    assert launches[0]["agent_name"] == directives.name


def test_sase_bridge_promotes_next_basename_member_when_first_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    pytest.importorskip("sase_core_rs")
    repo = _prepare_repo(tmp_path)
    paths = ["src/pkg/large.py", "src/pkg/shared.py"]
    for path in paths:
        _write_lines(repo / path, 701)
    scanner = _fake_toobig(tmp_path)
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("BUGYI_TEST_TOOBIG_SRC", "".join(f"{path}\n" for path in paths))
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target=_target(repo),
        variables={"toobig": str(scanner), "trees": ["src"]},
    )
    authored_summary = result["proposed_launches"][0]["clan_summary"]
    _write_lines(repo / paths[0], 699)
    monkeypatch.setenv("SASE_HOME", str(tmp_path / ".sase"))
    monkeypatch.setattr(
        "sase.agent.launch_cwd_common.resolve_known_project_vcs_launch_ref",
        lambda _prompt: _known_project_resolver(repo),
    )
    dispatched: list[str] = []
    _freeze_agent_name_allocation(monkeypatch)
    prepared = prepare_chop_proposals("toobig_split", result)
    plans = plan_chop_proposals(prepared)
    assert [plan.agent_name for plan in plans] == ["toobig-0.large.0", "toobig-0.shared.0"]
    assert [plan.declares_clan for plan in plans] == [True, False]
    tail_plans = plan_chop_proposals([replace(prepared[1], wait_on=None)])
    tail = extract_prompt_directives(tail_plans[0].prompt)[1]
    assert tail_plans[0].agent_name == "toobig-0.shared.0"
    assert tail_plans[0].declares_clan is True
    assert tail.clan == "toobig-0"
    assert tail.clan_declared is True
    assert tail.clan_tribe == "chop"
    assert tail.clan_summary == authored_summary

    with override_flags(typed_launch_units=True):
        launches = launch_chop_proposals(
            lumberjack_name="maintenance",
            chop_name="toobig_split",
            run_id="run-promote",
            proposals=prepared,
            launch_plans=plans,
            launch_agent_from_cwd_fn=lambda *_args, **_kwargs: None,
            launch_agents_from_cwd_fn=_capturing_launcher(dispatched, tmp_path, repo),
        )

    summary = launches.admission_result.summary
    assert len(launches) == 1
    assert launches.typed_admission is not None
    assert launches.admission_result.admission_complete
    assert summary is not None
    assert summary.total == 2
    assert summary.launched == 1
    assert summary.skipped == 1
    assert summary.condition_errors == 0
    assert summary.launch_errors == 0
    assert len(dispatched) == 1
    assert "%if" not in dispatched[0]
    directives = extract_prompt_directives(dispatched[0])[1]
    assert directives.name is not None
    assert "shared" in directives.name
    assert "large" not in directives.name
    assert "split_file" not in directives.name
    assert launches[0]["clan"] == "toobig-0"
    assert launches[0]["member_id"] == "shared.0"
    assert launches[0]["agent_name"] == directives.name
