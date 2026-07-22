"""Scan oversized Python files and emit one chained proposal per file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from sase.chops import ChopInvocation, ChopResultBuilder

from bugyi_chops._common import (
    context_target,
    context_vars,
    first_nonblank,
    normalize_workspace,
    result_with_summary,
    run_chop,
    safe_fragment,
)

CHOP_NAME = "toobig_split"
CLAN_TEMPLATE = "toobig-@"
DEFAULT_TREES = ("src", "tests")
DEFAULT_LIMITS = (1000, 850, 700)
DETAIL_LIMIT_CHARS = 500
ENV_PREFIX = "SASE_TOOBIG_SPLIT_"
LAUNCH_PRIORITY = 20
CLAN_SUMMARY_WIDTH = 76
CLAN_SUMMARY_HEADER_STYLE = "bold #D75FFF"
CLAN_SUMMARY_SECTION_STYLE = "bold #87D7FF"
CLAN_SUMMARY_MISSION_STYLE = "dim #D7D7FF"
CLAN_SUMMARY_FACTS_STYLE = "dim #A8A8A8"


@dataclass(frozen=True)
class ScanTarget:
    repo_root: Path
    workspace: str


def _env(name: str) -> str | None:
    return first_nonblank(os.getenv(f"{ENV_PREFIX}{name}"))


def _compact_detail(detail: str) -> str:
    compacted = " ".join(detail.strip().split())
    if len(compacted) <= DETAIL_LIMIT_CHARS:
        return compacted
    return compacted[: DETAIL_LIMIT_CHARS - 3].rstrip() + "..."


def _run_command(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RuntimeError(f"failed to execute {args[0]}: {error}") from error


def _project_data(project: str) -> dict[str, Any]:
    result = _run_command(["sase", "project", "show", project, "--json"])
    if result.returncode != 0:
        detail = _compact_detail(result.stderr or result.stdout)
        raise RuntimeError(
            f"project resolution failed: project={project!r} "
            f"exit_code={result.returncode} detail={detail or '-'}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"project resolution returned invalid JSON for {project!r}: {error}"
        ) from error
    if not isinstance(data, dict):
        raise RuntimeError(f"project resolution returned non-object JSON for {project!r}")
    return data


def _required_project_string(data: dict[str, Any], key: str, project: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"project {project!r} is missing {key!r}")
    return value.strip()


def _resolve_target(invocation: ChopInvocation) -> ScanTarget:
    target = context_target(invocation)
    variables = context_vars(invocation)
    project = first_nonblank(
        variables.get("project"),
        _env("PROJECT"),
        target.get("project"),
        target.get("name"),
    )
    repo_root_value = first_nonblank(
        variables.get("repo_root"),
        variables.get("workspace_dir"),
        _env("REPO_ROOT"),
        target.get("workspace_dir"),
    )
    workspace_value = first_nonblank(
        variables.get("workspace"),
        variables.get("launch_ref"),
        _env("LAUNCH_REF"),
        target.get("workspace"),
    )

    project_data: dict[str, Any] | None = None
    if (repo_root_value is None or workspace_value is None) and project is not None:
        project_data = _project_data(project)
    if repo_root_value is None and project_data is not None:
        repo_root_value = _required_project_string(project_data, "workspace_dir", project or "-")
    if workspace_value is None and project_data is not None:
        vcs_kind = _required_project_string(project_data, "vcs_kind", project or "-")
        effective_name = _required_project_string(
            project_data, "effective_project_name", project or "-"
        )
        workspace_value = f"{vcs_kind}:{effective_name}"

    if repo_root_value is None:
        raise RuntimeError(
            "a repository root is required in target.workspace_dir, vars.repo_root, "
            f"or {ENV_PREFIX}REPO_ROOT"
        )
    try:
        repo_root = Path(repo_root_value).expanduser().resolve(strict=True)
    except OSError as error:
        raise RuntimeError(
            f"failed to resolve repository root {repo_root_value!r}: {error}"
        ) from error
    if not repo_root.is_dir():
        raise RuntimeError(f"repository root is not a directory: {repo_root}")
    if workspace_value is None:
        raise RuntimeError(
            "a launch workspace is required in target.workspace, vars.workspace, "
            f"or {ENV_PREFIX}LAUNCH_REF"
        )
    return ScanTarget(repo_root=repo_root, workspace=normalize_workspace(workspace_value))


def _words(value: object, *, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            items = tuple(shlex.split(value))
        except ValueError as error:
            raise ValueError(f"{name} is invalid: {error}") from error
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = tuple(item.strip() for item in value)
    else:
        raise ValueError(f"{name} must be a string or list of strings")
    if not items or any(not item for item in items):
        raise ValueError(f"{name} must contain at least one non-blank value")
    return items


def _trees(invocation: ChopInvocation) -> tuple[str, ...]:
    variables = context_vars(invocation)
    value = variables.get("trees")
    if value is None:
        value = _env("TREES")
    return _words(value, name="trees", default=DEFAULT_TREES)


def _limits(invocation: ChopInvocation) -> tuple[int, int, int]:
    variables = context_vars(invocation)
    value: object = variables.get("limits")
    if value is None:
        value = _env("LIMITS")
    words = _words(
        value,
        name="limits",
        default=tuple(str(limit) for limit in DEFAULT_LIMITS),
    )
    if len(words) != 3:
        raise ValueError("limits must contain exactly three integers")
    try:
        limits = tuple(int(word) for word in words)
    except ValueError as error:
        raise ValueError("limits must contain only integers") from error
    if any(limit <= 0 for limit in limits):
        raise ValueError("limits values must be positive")
    return limits[0], limits[1], limits[2]


def _find_toobig(invocation: ChopInvocation, repo_root: Path) -> Path:
    variables = context_vars(invocation)
    override_value = first_nonblank(variables.get("toobig"), _env("TOOBIG"))
    candidates: list[Path] = []
    if override_value is not None:
        candidates.append(Path(override_value).expanduser())
    candidates.extend(
        [
            repo_root / ".venv" / "bin" / "toobig",
            repo_root / ".venv" / "Scripts" / "toobig.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    executable = shutil.which("toobig")
    if executable:
        return Path(executable).resolve()
    if override_value is not None:
        raise RuntimeError(f"toobig executable is unavailable: {override_value}")
    raise RuntimeError(f"toobig executable is unavailable for repository {repo_root}")


def _normalize_scanned_path(raw_path: str, repo_root: Path) -> str:
    path = Path(raw_path)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo_root)
        except ValueError as error:
            raise RuntimeError(
                f"scanner returned a path outside the repository: {raw_path}"
            ) from error
    else:
        path = Path(os.path.normpath(raw_path))
        if path == Path("..") or ".." in path.parts:
            raise RuntimeError(f"scanner returned a path outside the repository: {raw_path}")
    normalized = path.as_posix()
    if not normalized or normalized == "." or any(char.isspace() for char in normalized):
        raise RuntimeError(f"scanner returned an unsupported path: {raw_path!r}")
    return normalized


def _scan_files(
    executable: Path,
    repo_root: Path,
    trees: tuple[str, ...],
    limits: tuple[int, int, int],
    invocation: ChopInvocation,
) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for tree in trees:
        command = [str(executable), "--files-only", tree, *(str(limit) for limit in limits)]
        invocation.logger.debug(f"running in {repo_root}: {shlex.join(command)}")
        result = _run_command(command, cwd=repo_root)
        if result.returncode != 0:
            detail = _compact_detail(result.stderr or result.stdout)
            raise RuntimeError(
                f"scanner failed: tree={tree!r} exit_code={result.returncode} "
                f"detail={detail or '-'}"
            )
        for line in result.stdout.splitlines():
            raw_path = line.strip()
            if not raw_path:
                continue
            path = _normalize_scanned_path(raw_path, repo_root)
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _path_digest(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def _agent_name(path: str) -> str:
    stem = Path(path).with_suffix("").as_posix()
    slug = safe_fragment(re.sub(r"[/\\]+", ".", stem), fallback="file")[:48]
    return f"split_file.{slug}.{_path_digest(path)[:8]}"


def _dedupe_key(repo_root: Path, workspace: str, path: str) -> str:
    target = repo_root / path
    try:
        content_digest = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    except OSError:
        content_digest = "missing"
    return f"toobig_split:{workspace}:{path}:{content_digest}"


def _render_clan_summary(
    file_count: int,
    tree_count: int,
    limits: tuple[int, int, int],
) -> str:
    file_label = "FILE" if file_count == 1 else "FILES"
    limit_text = " / ".join(f"{limit:,}" for limit in limits)
    lines = (
        Text(
            f"◆ TOOBIG SPLIT · {file_count} {file_label}",
            style=CLAN_SUMMARY_HEADER_STYLE,
        ),
        Text("MISSION", style=CLAN_SUMMARY_SECTION_STYLE),
        Text(
            "Decompose oversized Python modules into focused, reviewable units",
            style=CLAN_SUMMARY_MISSION_STYLE,
        ),
        Text("without changing behavior.", style=CLAN_SUMMARY_MISSION_STYLE),
        Text(
            f"{tree_count} scan roots · limits {limit_text} lines · sequential queue",
            style=CLAN_SUMMARY_FACTS_STYLE,
        ),
    )
    return "\n".join(line.markup for line in lines)


def build_result(invocation: ChopInvocation) -> ChopResultBuilder:
    target = _resolve_target(invocation)
    trees = _trees(invocation)
    limits = _limits(invocation)
    executable = _find_toobig(invocation, target.repo_root)
    files = _scan_files(executable, target.repo_root, trees, limits, invocation)
    if not files:
        return result_with_summary(
            invocation,
            CHOP_NAME,
            {"trees": len(trees), "files": 0, "proposals": 0},
            status="no_op",
            reason="no_files_over_limits",
        )

    result = result_with_summary(
        invocation,
        CHOP_NAME,
        {"trees": len(trees), "files": len(files), "proposals": len(files)},
    )
    clan_summary = _render_clan_summary(len(files), len(trees), limits)
    prior_id: str | None = None
    for path in files:
        proposal_id = f"split-{_path_digest(path)}"
        result.propose(
            f"%auto %wait(priority={LAUNCH_PRIORITY}) #split_file:{path}",
            target.workspace,
            proposal_id=proposal_id,
            agent_name=_agent_name(path),
            clan=CLAN_TEMPLATE,
            clan_summary=clan_summary,
            dedupe_key=_dedupe_key(target.repo_root, target.workspace, path),
            wait_on=prior_id,
        )
        prior_id = proposal_id
    return result


def main() -> None:
    run_chop(
        CHOP_NAME,
        "Propose chained split-file agents for oversized Python files",
        build_result,
    )


if __name__ == "__main__":
    main()
