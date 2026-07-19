"""Shared integration helpers for console-script tests."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def run_chop(
    main: Callable[[], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    result_path = tmp_path / "result.json"
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "max_hook_runners": 1,
                "max_agent_runners": 1,
                "zombie_timeout_seconds": 60,
                "query": "",
                "lumberjack_name": "bugyi-chops-test",
                "state_dir": str(tmp_path / "state"),
                "all_changespecs_file": str(tmp_path / "all.json"),
                "filtered_changespecs_file": str(tmp_path / "filtered.json"),
                "verbose_lumberjack_diagnostics": False,
                "result_file": str(result_path),
                "target": target or {},
                "vars": variables or {},
            }
        ),
        encoding="utf-8",
    )
    argv = ["chop", "--context", str(context_path)]
    if verbose:
        argv.append("--verbose")
    monkeypatch.setattr(sys, "argv", argv)
    main()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


@pytest.fixture
def run_chop_main() -> Callable[..., dict[str, Any]]:
    return run_chop
