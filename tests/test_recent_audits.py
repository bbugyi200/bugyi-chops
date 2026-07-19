from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bugyi_chops.recent_audits import bug_main, improvement_main


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Bugyi Chops Tests")
    _git(path, "config", "user.email", "tests@example.com")
    (path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "sample.py")
    _git(path, "commit", "-qm", "initial")
    return _git(path, "rev-parse", "HEAD")


def test_bug_audit_ports_recent_commit_prompt_and_head_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    result = run_chop_main(
        bug_main,
        tmp_path,
        monkeypatch,
        target={
            "name": "demo-project",
            "workspace": "gh:example/demo",
            "workspace_dir": str(repo),
        },
        verbose=True,
    )

    assert result["status"] == "ok"
    proposal = result["proposed_launches"][0]
    assert proposal["workspace"] == "gh:example/demo"
    assert proposal["agent_name"] == f"audit_bugs.demo-project.{head[:12]}"
    assert f"through {head}" in proposal["prompt"]
    assert "correctness regressions" in proposal["prompt"]
    assert "#pr(recent_bug_audit_demo-project_" in proposal["prompt"]
    assert "git.commits_since" in proposal["prompt"]
    assert "#!" not in proposal["prompt"]


def test_improvement_audit_uses_vars_and_current_head_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(
        improvement_main,
        tmp_path,
        monkeypatch,
        variables={"project": "demo/project", "workspace": "#git:demo"},
    )

    proposal = result["proposed_launches"][0]
    assert proposal["workspace"] == "git:demo"
    assert proposal["agent_name"] == "audit_improvements.demo_project.current"
    assert "objective wins" in proposal["prompt"]
    assert "current HEAD" in proposal["prompt"]
    assert "#pr(recent_improvement_audit_demo_project_current)" in proposal["prompt"]


def test_audit_fails_closed_for_missing_workspace_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(
        bug_main,
        tmp_path,
        monkeypatch,
        target={"workspace_dir": str(tmp_path / "missing")},
    )
    assert result["status"] == "check_error"
    assert result["proposed_launches"] == []
