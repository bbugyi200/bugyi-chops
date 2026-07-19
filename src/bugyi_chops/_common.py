"""Shared result and target helpers for bugyi-chops."""

from __future__ import annotations

import os
import re
import subprocess
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sase.chops import (
    ChopInvocation,
    ChopResultBuilder,
    ChopResultStatus,
    emit_summary,
    load_chop_invocation,
)

ChopBody = Callable[[ChopInvocation], ChopResultBuilder]


def first_nonblank(*values: object) -> str | None:
    """Return the first non-blank string from *values*."""

    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def context_target(invocation: ChopInvocation) -> Mapping[str, Any]:
    return invocation.context.target or {}


def context_vars(invocation: ChopInvocation) -> Mapping[str, Any]:
    return invocation.context.vars or {}


def target_label(invocation: ChopInvocation, *, default: str = "sase") -> str:
    target = context_target(invocation)
    variables = context_vars(invocation)
    return (
        first_nonblank(
            variables.get("project"),
            target.get("name"),
            target.get("project"),
            os.getenv("BUGYI_CHOPS_PROJECT"),
        )
        or default
    )


def normalize_workspace(value: str) -> str:
    """Normalize a proposal workspace ref and reject malformed values."""

    workspace = value.strip().removeprefix("#")
    if not workspace or ":" not in workspace or any(char.isspace() for char in workspace):
        raise ValueError(f"invalid workspace ref: {value!r}")
    return workspace


def proposal_workspace(
    invocation: ChopInvocation,
    *,
    default: str | None = None,
) -> str:
    target = context_target(invocation)
    variables = context_vars(invocation)
    value = first_nonblank(
        variables.get("workspace"),
        variables.get("launch_ref"),
        target.get("workspace"),
        os.getenv("BUGYI_CHOPS_WORKSPACE"),
        default,
    )
    if value is None:
        raise ValueError(
            "a workspace ref is required in target.workspace, vars.workspace, "
            "or BUGYI_CHOPS_WORKSPACE"
        )
    return normalize_workspace(value)


def target_workspace_dir(invocation: ChopInvocation) -> Path | None:
    target = context_target(invocation)
    variables = context_vars(invocation)
    value = first_nonblank(
        variables.get("repo_root"),
        variables.get("workspace_dir"),
        target.get("workspace_dir"),
    )
    if value is None:
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"failed to resolve workspace directory {value!r}: {error}") from error
    if not path.is_dir():
        raise ValueError(f"workspace directory is not a directory: {path}")
    return path


def safe_fragment(value: str, *, fallback: str = "repo") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-") or fallback


def git_head(repo_root: Path | None) -> tuple[str | None, str | None]:
    """Return full and abbreviated HEAD without making audit proposals depend on git."""

    if repo_root is None:
        return None, None
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    head = result.stdout.strip()
    return head, head[:12]


def result_with_summary(
    invocation: ChopInvocation,
    name: str,
    counters: Mapping[str, int],
    *,
    status: ChopResultStatus = "ok",
    reason: str | None = None,
) -> ChopResultBuilder:
    line = emit_summary(name, counters, reason=reason, logger=invocation.logger)
    return ChopResultBuilder(
        status=status,
        summary=line,
        reason=reason,
        counters=dict(counters),
    )


def run_chop(name: str, description: str, body: ChopBody) -> None:
    """Load a chop invocation, fail closed into a typed result, and write it."""

    invocation = load_chop_invocation(description=description)
    try:
        result = body(invocation)
    except Exception as error:
        invocation.logger.error(f"{name} check failed: {error}")
        invocation.logger.debug(traceback.format_exc().rstrip())
        result = result_with_summary(
            invocation,
            name,
            {"proposals": 0},
            status="check_error",
            reason="check_failed",
        )
    result.write(context=invocation.context)
