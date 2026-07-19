from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bugyi_chops.fix_just import main


def test_fix_just_emits_one_runner_launch_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        target={"workspace": "gh:sase-org/sase"},
    )

    assert result["status"] == "ok"
    assert result["counters"] == {"proposals": 1, "targets": 1}
    proposal = result["proposed_launches"][0]
    assert proposal["id"] == "fix"
    assert proposal["workspace"] == "gh:sase-org/sase"
    assert proposal["agent_name"] == "sase_fix_just-@"
    assert "#pr(fix_just)" in proposal["prompt"]
    assert "just fmt-check" in proposal["prompt"]
    assert "just lint" in proposal["prompt"]
    assert "just test" in proposal["prompt"]
    assert "#!" not in proposal["prompt"]


def test_fix_just_defaults_to_sase_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(main, tmp_path, monkeypatch)
    assert result["proposed_launches"][0]["workspace"] == "gh:sase-org/sase"


def test_fix_just_fails_closed_for_malformed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_chop_main: Callable[..., dict[str, Any]],
) -> None:
    result = run_chop_main(
        main,
        tmp_path,
        monkeypatch,
        variables={"workspace": "not a ref"},
    )
    assert result["status"] == "check_error"
    assert result["reason"] == "check_failed"
    assert result["proposed_launches"] == []
