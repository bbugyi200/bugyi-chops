"""Emit one agent proposal that repairs the SASE just gates."""

from __future__ import annotations

from sase.chops import ChopInvocation, ChopResultBuilder

from bugyi_chops._common import proposal_workspace, result_with_summary, run_chop

CHOP_NAME = "fix_just"
DEFAULT_WORKSPACE = "gh:sase-org/sase"

FIX_JUST_PROMPT = """\
#pr(fix_just)

Repair every failing repository gate exposed by the justfile.

Run `just install`, then run `just fmt-check`, `just lint`, and `just test`. Fix all
confirmed formatting, lint, type-checking, and test failures. Re-run the complete set
until it passes. Keep changes focused on making those gates green, preserve existing
behavior unless a failing test proves it is wrong, and use the `#pr(fix_just)` rollover
workflow when the work needs a follow-up pull request.
"""


def build_result(invocation: ChopInvocation) -> ChopResultBuilder:
    workspace = proposal_workspace(invocation, default=DEFAULT_WORKSPACE)
    result = result_with_summary(
        invocation,
        CHOP_NAME,
        {"targets": 1, "proposals": 1},
    )
    return result.propose(
        FIX_JUST_PROMPT.strip(),
        workspace,
        proposal_id="fix",
        agent_name="sase_fix_just-@",
    )


def main() -> None:
    run_chop(CHOP_NAME, "Propose an agent to repair failing just gates", build_result)


if __name__ == "__main__":
    main()
