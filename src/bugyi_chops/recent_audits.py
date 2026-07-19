"""Recent-commit bug and objective-improvement audit chops."""

from __future__ import annotations

from dataclasses import dataclass

from sase.chops import ChopInvocation, ChopResultBuilder

from bugyi_chops._common import (
    git_head,
    proposal_workspace,
    result_with_summary,
    run_chop,
    safe_fragment,
    target_label,
    target_workspace_dir,
)

DEFAULT_WORKSPACE = "gh:sase-org/sase"


@dataclass(frozen=True)
class AuditKind:
    name: str
    agent_hood: str
    pr_prefix: str
    subject: str
    instructions: str


BUG_AUDIT = AuditKind(
    name="recent_bug_audit",
    agent_hood="audit_bugs",
    pr_prefix="recent_bug_audit",
    subject="confirmed bugs",
    instructions="""\
Inspect the commits in scope for correctness regressions, broken edge cases, unsafe
error handling, race conditions, data-loss risks, and test failures introduced by
those commits.

Fix confirmed issues only. Avoid unrelated improvements, style-only edits,
speculative refactors, broad rewrites, and preference changes. If no confirmed bug is
found, leave the worktree untouched and report that outcome.
""",
)

IMPROVEMENT_AUDIT = AuditKind(
    name="recent_improvement_audit",
    agent_hood="audit_improvements",
    pr_prefix="recent_improvement_audit",
    subject="objective improvements",
    instructions="""\
Inspect the commits in scope for clear, objective wins: a small
correctness-preserving simplification, a plainly better error path, targeted test
coverage for changed behavior, or an obvious low-risk performance fix.

Only change files when the value is objective and narrowly scoped. Do not perform
style churn, speculative refactors, preference changes, broad rewrites, renames,
formatting-only edits, or subjective cleanup. If no objectively valuable change is
found, leave the worktree untouched and report that outcome.
""",
)


def _prompt(kind: AuditKind, project: str, head: str | None, pr_name: str) -> str:
    head_description = head or "the current HEAD"
    return f"""\
#pr({pr_name})

Audit recent commits in {project} for {kind.subject}.

The Axe `git.commits_since` trigger owns the threshold and checkpoint for scheduled
runs. Review the commit history through {head_description}, identify the recent
since-last-audit scope from the available history and task context, and inspect every
commit in that scope.

{kind.instructions.strip()}

When you change files, run the focused checks appropriate to the affected code. Use
the `#pr({pr_name})` rollover workflow if follow-up work is required.
""".strip()


def build_audit_result(
    invocation: ChopInvocation,
    kind: AuditKind,
) -> ChopResultBuilder:
    project = target_label(invocation)
    workspace = proposal_workspace(invocation, default=DEFAULT_WORKSPACE)
    repo_root = target_workspace_dir(invocation)
    head, head_short = git_head(repo_root)
    safe_project = safe_fragment(project)
    revision = safe_fragment(head_short or "current", fallback="current")
    pr_name = f"{kind.pr_prefix}_{safe_project.replace('.', '_')}_{revision}"
    agent_name = f"{kind.agent_hood}.{safe_project}.{revision}"

    invocation.logger.debug(f"project={project} workspace={workspace} head={head or '-'}")
    result = result_with_summary(
        invocation,
        kind.name,
        {"targets": 1, "proposals": 1},
    )
    return result.propose(
        _prompt(kind, project, head, pr_name),
        workspace,
        proposal_id="audit",
        agent_name=agent_name,
    )


def bug_main() -> None:
    run_chop(
        BUG_AUDIT.name,
        "Propose a recent-commit correctness audit",
        lambda invocation: build_audit_result(invocation, BUG_AUDIT),
    )


def improvement_main() -> None:
    run_chop(
        IMPROVEMENT_AUDIT.name,
        "Propose a recent-commit objective-improvement audit",
        lambda invocation: build_audit_result(invocation, IMPROVEMENT_AUDIT),
    )
