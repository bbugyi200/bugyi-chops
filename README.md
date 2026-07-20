# bugyi-chops

`bugyi-chops` is Bryan Bugyi's community [SASE](https://sase.sh/) plugin for
scheduled Axe jobs that need to propose coding-agent work. It supplies three console
scripts:

| Script | What it proposes |
| --- | --- |
| `bugyi_chop_toobig_split` | One `%auto #split_file:<path>` agent per oversized Python file, chained in scan order |
| `bugyi_chop_recent_bug_audit` | One recent-commit audit for confirmed correctness bugs |
| `bugyi_chop_recent_improvement_audit` | One recent-commit audit for narrow, objective improvements |

The scripts never launch agents themselves. They scan or assemble a prompt, then use
the public `sase.chops` SDK to atomically write a validated result document. Axe owns
guard and trigger evaluation, deduplication, workspace allocation, proposal launches,
and the final action lifecycle.

## Installation

Install the published package into the same managed environment as SASE:

```bash
sase plugin install bugyi-chops
```

For development against the repository rather than PyPI:

```bash
sase plugin install bugyi-chops -g
```

All scripts require Python 3.12 or newer and `sase>=0.12,<0.13`. SASE 0.12 is the
first release with clan-scoped chop proposals. The package also
installs the `toobig` scanner used by `bugyi_chop_toobig_split`.

## The chop result contract

Axe invokes a configured script as `<script> --context <context.json>` and supplies
`SASE_CHOP_RESULT_FILE`. A successful script writes schema-versioned JSON like:

```json
{
  "schema_version": 1,
  "status": "ok",
  "summary": "recent_bug_audit: targets=1 proposals=1",
  "counters": {"targets": 1, "proposals": 1},
  "proposed_launches": [
    {
      "id": "audit",
      "prompt": "#pr(recent_bug_audit_sase_current)\n\nAudit recent commits...",
      "workspace": "gh:sase-org/sase"
    }
  ]
}
```

`status` is `ok`, `no_op`, or `check_error`. Axe validates the entire result before it
launches anything, injects the workspace/name/tribe scaffold, honors `wait_on`
dependencies, filters duplicate proposals, and tracks linked agents through
`action_succeeded` or `action_failed`. The prompts here may use inline xprompts such
as `#pr` and `#split_file`; they never use forbidden standalone `#!workflow`
references.

## `toobig_split`

`bugyi_chop_toobig_split` runs `toobig --files-only` for each configured tree,
normalizes and de-duplicates its paths, and emits one proposal per file. Each proposal
has:

- the shared `toobig-@` clan template, with a stable marker-free `split_file.*`
  member ID;
- `%auto #split_file:<path>` as its prompt;
- a content-sensitive dedupe key, so an unchanged file is not relaunched;
- `wait_on` pointing to the prior file, preserving sequential workspace allocation.

SASE allocates the clan template once per actionable scan, so concrete agent names
look like `toobig-<token>.split_file.<path-slug>.<digest>`. All proposals from that
scan belong to the same clan generation, while later scans can allocate a new one.

The script deliberately has no flock, no `sase agent list`, and no `sase run`. Those
responsibilities now belong to Axe:

```yaml
axe:
  lumberjacks:
    maintenance:
      interval: 60
      chops:
        toobig_split:
          script: bugyi_chop_toobig_split
          description: Split oversized Python files in sase
          run_every: 60m
          inhibit_if:
            agent_clan: {name_prefix: toobig-}
          for_each:
            source: projects
            names: [sase]
          vars:
            trees: [src, tests]
            limits: [1000, 850, 700]
```

The projects target source supplies both `target.workspace` and
`target.workspace_dir`. For a literal target, provide the same fields. Compatibility
environment variables are also accepted: `SASE_TOOBIG_SPLIT_PROJECT`,
`SASE_TOOBIG_SPLIT_REPO_ROOT`, `SASE_TOOBIG_SPLIT_LAUNCH_REF`,
`SASE_TOOBIG_SPLIT_TREES`, `SASE_TOOBIG_SPLIT_LIMITS`, and
`SASE_TOOBIG_SPLIT_TOOBIG`.

## Recent-commit audits

The audit scripts only assemble the audit proposal. The shared
`git.commits_since` trigger owns commit thresholds and checkpoints, so there are no
private marker files to corrupt or race. `on_action_success` advances the checkpoint
only after the proposed audit agent completes successfully.

```yaml
axe:
  lumberjacks:
    audits:
      interval: 300
      chops:
        recent_bug_audit:
          script: bugyi_chop_recent_bug_audit
          run_every: 1h
          trigger:
            git.commits_since:
              project: "{target.name}"
              threshold: 200
              checkpoint: on_action_success
          for_each:
            source: projects
            names: [sase]

        recent_improvement_audit:
          script: bugyi_chop_recent_improvement_audit
          run_every: 1h
          trigger:
            git.commits_since:
              project: "{target.name}"
              threshold: 200
              checkpoint: on_action_success
          for_each:
            source: projects
            names: [sase]
```

Each prompt records the current HEAD when `target.workspace_dir` is available, uses a
stable `audit_bugs.*` or `audit_improvements.*` agent name, and includes a project- and
revision-specific `#pr(...)` rollover.

## Debugging

Preview a configured script and the exact scaffolded proposals without launching
agents:

```bash
sase axe chop run 'toobig_split[sase]' -L maintenance --dry-run --chop-verbose
# Short flags:
sase axe chop run 'toobig_split[sase]' -L maintenance -n -V
```

`-V` sets `SASE_CHOP_VERBOSE` and prints scanner commands and target diagnostics.
Every actual invocation also emits a compact summary line with bounded integer
counters and an explicit reason for no-op/error outcomes.

For lower-level diagnosis, reuse a context JSON written by Axe and invoke a script
directly. This writes proposals but never launches them:

```bash
SASE_CHOP_RESULT_FILE=/tmp/toobig-result.json \
  bugyi_chop_toobig_split --context /path/to/context.json --verbose
jq . /tmp/toobig-result.json
```

## Development and releases

```bash
just install
just check
```

When developing before the matching SASE release is available on PyPI, first run
`just install` in a current SASE source checkout (with its linked `sase-core`), then
reuse that environment:

```bash
BUGYI_CHOPS_VENV_BIN=/path/to/sase/.venv/bin just install
BUGYI_CHOPS_VENV_BIN=/path/to/sase/.venv/bin just check
```

`just check` runs formatting/lint/type checks, pytest with branch coverage, builds the
wheel and source distribution, and validates both artifacts with Twine. Pull requests
and pushes to `master` run the same checks on Python 3.12 and 3.13.

Releases are tag-driven. Set the package version, push the matching `v<version>` tag,
and the publish workflow rebuilds and tests the package before uploading to PyPI via
GitHub trusted publishing. No long-lived PyPI token is stored in the repository.

## License

[MIT](LICENSE)
