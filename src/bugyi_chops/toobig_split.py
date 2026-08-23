"""Scan oversized Python files and emit one chained proposal per file."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from sase.chops import ChopInvocation, ChopReport, ChopResultBuilder, Tone

from bugyi_chops._common import (
    context_target,
    context_vars,
    first_nonblank,
    normalize_workspace,
    result_with_summary,
    run_chop,
    safe_fragment,
)
from bugyi_chops._report import (
    add_facts_footer,
    elide_path,
    severity_tone,
    start_report,
)

CHOP_NAME = "toobig_split"
CLAN_TEMPLATE = "toobig-@"
PROPOSAL_MODEL = "@medium"
DEFAULT_TREES = ("src", "tests")
DEFAULT_LIMITS = (1000, 850, 700)
DETAIL_LIMIT_CHARS = 500
ENV_PREFIX = "SASE_TOOBIG_SPLIT_"
LAUNCH_PRIORITY = 20
CLAN_SUMMARY_WIDTH = 76
CLAN_SUMMARY_MAX_ROWS = 10
CLAN_SUMMARY_HEADER_STYLE = "bold #D75FFF"
CLAN_SUMMARY_SECTION_STYLE = "bold #87D7FF"
CLAN_SUMMARY_MISSION_STYLE = "dim #D7D7FF"
CLAN_SUMMARY_FACTS_STYLE = "dim #A8A8A8"
CLAN_SUMMARY_VIOLATION_STYLE = "bold #FF5F87"
CLAN_SUMMARY_WARNING_STYLE = "bold #FFAF5F"
CLAN_SUMMARY_FYI_STYLE = "#87D7FF"
CLAN_SUMMARY_NEUTRAL_STYLE = "dim #A8A8A8"
CLAN_SUMMARY_VIOLATION_GLYPH = "▲"
CLAN_SUMMARY_WARNING_GLYPH = "◆"
CLAN_SUMMARY_FYI_GLYPH = "•"
CLAN_SUMMARY_NEUTRAL_GLYPH = "·"


@dataclass(frozen=True)
class ScanTarget:
    repo_root: Path
    workspace: str


@dataclass(frozen=True)
class FileEntry:
    path: str
    line_count: int | None


@dataclass(frozen=True)
class TargetRow:
    path: str
    line_count: int | None
    severity: str
    tone: Tone
    glyph: str


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


def _files_only_payload(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _healthy_files_only_result(returncode: int, payload_lines: Sequence[str]) -> bool:
    """Return whether a files-only toobig run completed with a usable listing.

    Exit 0 is always a completed scan. Exit 1 is a completed scan only when
    stdout lists at least one path (hard-limit findings). Empty exit-1 and any
    other nonzero code are scanner failures.
    """

    return returncode == 0 or (returncode == 1 and bool(payload_lines))


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
        payload_lines = _files_only_payload(result.stdout)
        if not _healthy_files_only_result(result.returncode, payload_lines):
            detail = _compact_detail(result.stderr or result.stdout)
            raise RuntimeError(
                f"scanner failed: tree={tree!r} exit_code={result.returncode} "
                f"detail={detail or '-'}"
            )
        for raw_path in payload_lines:
            path = _normalize_scanned_path(raw_path, repo_root)
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _path_digest(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def _agent_name(path: str) -> str:
    stem = Path(path).with_suffix("").as_posix()
    slug = safe_fragment(re.sub(r"[/\\]+", ".", stem), fallback="file")
    return f"split_file.{slug}.@"


def _repo_revision(repo_root: Path) -> str | None:
    try:
        result = _run_command(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    except RuntimeError:
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None


def _dedupe_key(workspace: str, path: str, revision: str | None) -> str | None:
    if revision is None:
        return None
    return f"toobig_split:{workspace}:{path}:{revision}"


def _line_count(path: Path) -> int | None:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return None


def _classify(line_count: int | None, limits: tuple[int, int, int]) -> str:
    if line_count is None:
        return "unknown"
    if line_count > limits[0]:
        return "violation"
    if line_count > limits[1]:
        return "warning"
    if line_count > limits[2]:
        return "fyi"
    return "neutral"


def _elide_path(path: str, max_cells: int) -> str:
    """Compatibility wrapper for the package-wide path elision helper."""

    return elide_path(path, max_cells)


def _severity_display(severity: str) -> tuple[str, str]:
    if severity == "violation":
        return CLAN_SUMMARY_VIOLATION_GLYPH, CLAN_SUMMARY_VIOLATION_STYLE
    if severity == "warning":
        return CLAN_SUMMARY_WARNING_GLYPH, CLAN_SUMMARY_WARNING_STYLE
    if severity == "fyi":
        return CLAN_SUMMARY_FYI_GLYPH, CLAN_SUMMARY_FYI_STYLE
    return CLAN_SUMMARY_NEUTRAL_GLYPH, CLAN_SUMMARY_NEUTRAL_STYLE


def _target_rows(
    entries: Sequence[FileEntry],
    limits: tuple[int, int, int],
    *,
    max_rows: int = CLAN_SUMMARY_MAX_ROWS,
) -> tuple[list[TargetRow], int]:
    """Compute the ordered target ledger shared by both report projections."""

    sorted_entries = sorted(
        entries,
        key=lambda entry: (
            entry.line_count is None,
            -entry.line_count if entry.line_count is not None else 0,
            entry.path,
        ),
    )
    displayed_entries = sorted_entries[: max(0, max_rows)]
    rows: list[TargetRow] = []
    for entry in displayed_entries:
        severity = _classify(entry.line_count, limits)
        glyph, _ = _severity_display(severity)
        rows.append(
            TargetRow(
                path=entry.path,
                line_count=entry.line_count,
                severity=severity,
                tone=severity_tone(severity),
                glyph=glyph,
            )
        )
    return rows, len(entries) - len(displayed_entries)


def _facts(tree_count: int, limits: tuple[int, int, int]) -> dict[str, str]:
    return {
        "scan roots": str(tree_count),
        "limits": " / ".join(f"{limit:,}" for limit in limits),
        "queue mode": "sequential",
    }


def _build_report(
    rows: Sequence[TargetRow],
    overflow: int,
    tree_count: int,
    limits: tuple[int, int, int],
) -> ChopReport:
    report = start_report("TOOBIG SPLIT")
    has_violation = any(row.severity == "violation" for row in rows)
    report.headline(
        f"{len(rows) + overflow} files over limits",
        tone="error" if has_violation else "warn",
    ).heading("TARGETS")
    table = report.rows(columns=("LINES", "FILE"))
    for row in rows:
        table.row(
            (
                str(row.line_count) if row.line_count is not None else "?",
                _elide_path(row.path, CLAN_SUMMARY_WIDTH - 12),
            ),
            tone=row.tone,
            glyph=row.glyph,
        )
    if overflow:
        report.text(f"…and {overflow} more", tone="muted")
    if rows and rows[0].line_count is not None:
        report.gauge(
            _elide_path(rows[0].path, CLAN_SUMMARY_WIDTH - 12),
            rows[0].line_count,
            limits[0],
            tone=rows[0].tone,
        )
    return add_facts_footer(report, _facts(tree_count, limits))


def _build_noop_report(
    tree_count: int,
    limits: tuple[int, int, int],
) -> ChopReport:
    report = start_report("TOOBIG SPLIT").headline(
        "Every scanned file is within limits",
        tone="ok",
    )
    return add_facts_footer(report, _facts(tree_count, limits))


def _render_clan_summary(
    entries: Sequence[FileEntry],
    tree_count: int,
    limits: tuple[int, int, int],
    *,
    max_rows: int = CLAN_SUMMARY_MAX_ROWS,
) -> str:
    file_count = len(entries)
    file_label = "FILE" if file_count == 1 else "FILES"
    limit_text = " / ".join(f"{limit:,}" for limit in limits)
    lines = [
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
        Text(""),
        Text("TARGETS", style=CLAN_SUMMARY_SECTION_STYLE),
    ]

    rows, overflow = _target_rows(entries, limits, max_rows=max_rows)
    count_strings = [f"{row.line_count:,}" if row.line_count is not None else "?" for row in rows]
    count_width = max((len(count) for count in count_strings), default=1)
    path_cells = CLAN_SUMMARY_WIDTH - (2 + count_width + 2)
    for row, count_string in zip(rows, count_strings, strict=True):
        _, style = _severity_display(row.severity)
        path = _elide_path(row.path, path_cells)
        lines.append(Text(f"{row.glyph} {count_string:>{count_width}}  {path}", style=style))

    if overflow:
        lines.append(Text(f"…and {overflow} more", style=CLAN_SUMMARY_FACTS_STYLE))
    lines.extend(
        [
            Text(""),
            Text(
                f"{tree_count} scan roots · limits {limit_text} lines · sequential queue",
                style=CLAN_SUMMARY_FACTS_STYLE,
            ),
        ]
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
            report=_build_noop_report(len(trees), limits),
        )

    entries = [
        FileEntry(path=path, line_count=_line_count(target.repo_root / path)) for path in files
    ]
    rows, overflow = _target_rows(entries, limits)
    result = result_with_summary(
        invocation,
        CHOP_NAME,
        {"trees": len(trees), "files": len(files), "proposals": len(files)},
        report=_build_report(rows, overflow, len(trees), limits),
    )
    clan_summary = _render_clan_summary(entries, len(trees), limits)
    revision = _repo_revision(target.repo_root)
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
            model=PROPOSAL_MODEL,
            dedupe_key=_dedupe_key(target.workspace, path, revision),
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
