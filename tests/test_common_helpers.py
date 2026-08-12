from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sase.axe.chop_script_context import ChopScriptContext
from sase.chops import ChopArguments, ChopInvocation, ChopLogger, ChopResultBuilder
from sase.core.axe_chop_facade import validate_chop_result

from bugyi_chops import _common
from bugyi_chops._report import add_facts_footer, elide_path, severity_tone, start_report


def _invocation(
    tmp_path: Path,
    *,
    target: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> ChopInvocation:
    return ChopInvocation(
        arguments=ChopArguments(context=str(tmp_path / "context.json"), verbose=False),
        context=ChopScriptContext(
            max_hook_runners=1,
            max_agent_runners=1,
            zombie_timeout_seconds=60,
            query="",
            lumberjack_name="helpers-test",
            state_dir=tmp_path / "state",
            all_changespecs_file=tmp_path / "all.json",
            filtered_changespecs_file=tmp_path / "filtered.json",
            verbose_lumberjack_diagnostics=False,
            result_file=tmp_path / "result.json",
            target=target or {},
            vars=variables or {},
        ),
        logger=ChopLogger(),
    )


def test_target_and_workspace_helpers_prefer_vars_then_target_then_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUGYI_CHOPS_PROJECT", "env-project")
    monkeypatch.setenv("BUGYI_CHOPS_WORKSPACE", "gh:env/repo")

    from_env = _invocation(tmp_path)
    assert _common.target_label(from_env) == "env-project"
    assert _common.proposal_workspace(from_env) == "gh:env/repo"

    from_target = _invocation(
        tmp_path,
        target={"name": "target-project", "workspace": "#gh:target/repo"},
    )
    assert _common.target_label(from_target) == "target-project"
    assert _common.proposal_workspace(from_target) == "gh:target/repo"

    from_vars = _invocation(
        tmp_path,
        target={"name": "target-project", "workspace": "gh:target/repo"},
        variables={"project": "vars-project", "launch_ref": "#git:vars"},
    )
    assert _common.target_label(from_vars) == "vars-project"
    assert _common.proposal_workspace(from_vars) == "git:vars"


def test_workspace_helpers_reject_missing_or_malformed_refs(tmp_path: Path) -> None:
    assert _common.target_label(_invocation(tmp_path), default="fallback") == "fallback"

    with pytest.raises(ValueError, match="workspace ref is required"):
        _common.proposal_workspace(_invocation(tmp_path))

    with pytest.raises(ValueError, match="invalid workspace ref"):
        _common.normalize_workspace("not a ref")

    with pytest.raises(ValueError, match="invalid workspace ref"):
        _common.normalize_workspace("gh:owner/repo with spaces")


def test_target_workspace_dir_and_git_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Bugyi Chops Tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    invocation = _invocation(tmp_path, variables={"repo_root": str(repo)})
    assert _common.target_workspace_dir(invocation) == repo.resolve()

    head, head_short = _common.git_head(repo)
    assert head is not None
    assert head_short == head[:12]
    assert _common.git_head(None) == (None, None)

    with pytest.raises(ValueError, match="failed to resolve workspace directory"):
        _common.target_workspace_dir(
            _invocation(tmp_path, target={"workspace_dir": str(tmp_path / "missing")})
        )

    file_path = tmp_path / "file.txt"
    file_path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="workspace directory is not a directory"):
        _common.target_workspace_dir(
            _invocation(tmp_path, target={"workspace_dir": str(file_path)})
        )


def test_result_with_summary_and_report_helpers(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path)
    report = add_facts_footer(start_report("demo report").headline("Ready"), {"repo": "demo"})
    result = _common.result_with_summary(
        invocation,
        "demo",
        {"targets": 1, "proposals": 0},
        status="no_op",
        reason="nothing_to_do",
        report=report,
    )

    assert result.summary == "demo: targets=1 proposals=0 reason=nothing_to_do"
    assert result.status == "no_op"
    assert severity_tone("violation") == "error"
    assert severity_tone("unknown-value") == "neutral"
    assert elide_path("src/pkg/module.py", 40) == "src/pkg/module.py"
    assert elide_path("src/pkg/module.py", 1) == "…"
    assert elide_path("src/pkg/module.py", 8).startswith("…/")


def test_run_chop_writes_check_error_when_body_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_path = tmp_path / "result.json"
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "max_hook_runners": 1,
                "max_agent_runners": 1,
                "zombie_timeout_seconds": 60,
                "query": "",
                "lumberjack_name": "helpers-test",
                "state_dir": str(tmp_path / "state"),
                "all_changespecs_file": str(tmp_path / "all.json"),
                "filtered_changespecs_file": str(tmp_path / "filtered.json"),
                "verbose_lumberjack_diagnostics": False,
                "result_file": str(result_path),
                "target": {},
                "vars": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["chop", "--context", str(context_path)])

    def _raise(_invocation: ChopInvocation) -> ChopResultBuilder:
        raise RuntimeError("boom")

    _common.run_chop("demo_chop", "Demo chop", _raise)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "check_error"
    assert result["summary"] == "demo_chop: proposals=0 reason=check_failed"
    assert result["report"]["title"] == "DEMO CHOP"
    validate_chop_result(result)
