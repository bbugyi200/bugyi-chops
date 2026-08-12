# bugyi-chops

`bugyi-chops` is Bryan Bugyi's community [SASE](https://sase.sh/) plugin for
scheduled Axe jobs that need to propose coding-agent work. It supplies two console
scripts:

| Script | What it proposes |
| --- | --- |
| `bugyi_chop_ci_watch` | Idle-gated CI repairs and explicitly enabled, guarded release merges |
| `bugyi_chop_toobig_split` | One `%auto #split_file:<path>` agent per oversized Python file, chained in scan order |

The scripts never launch agents themselves. They scan or assemble a prompt, then use
the public `sase.chops` SDK to atomically write a validated result document. Axe owns
guard and trigger evaluation, deduplication, workspace allocation, proposal launches,
and the final action lifecycle.

`bugyi_chop_ci_watch` additionally owns a tightly guarded direct side effect: when
`vars.merge_enabled` is true and `SASE_CHOP_DRY_RUN=0` is explicitly present, it may
squash-merge one fully green release-please or release-plz PR. Missing or true dry-run
context always suppresses merging. Its CI-fix proposals are emitted only after a
`sase agent list -j` probe reports no live agent in the `ci_fix` hood. Proposed repair
agents use the `ci_fix.<slug>.@` template so SASE assigns a unique launch token; the
small race between the probe and Axe launching the proposal is accepted.

## Installation

Install the published package into the same managed environment as SASE:

```bash
sase plugin install bugyi-chops
```

For development against the repository rather than PyPI:

```bash
sase plugin install bugyi-chops -g
```

All scripts require Python 3.12 or newer and `sase>=0.13.2,<0.14`. SASE 0.13.2 is the
first release with structured chop reports. The package also
installs the `toobig` scanner used by `bugyi_chop_toobig_split`.

## The chop result contract

Axe invokes a configured script as `<script> --context <context.json>` and supplies
`SASE_CHOP_RESULT_FILE`. A successful script writes schema-versioned JSON like:

```json
{
  "schema_version": 1,
  "status": "ok",
  "summary": "toobig_split: files=1 proposals=1 skipped=0",
  "counters": {"files": 1, "proposals": 1, "skipped": 0},
  "proposed_launches": [
    {
      "id": "split_file-src-large_py",
      "prompt": "%auto #split_file:src/large.py",
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
- the same Rich `clan_summary` metadata, describing the mission, file and scan-root
  counts, configured limits, and sequential queue;
- `%auto #split_file:<path>` as its prompt;
- a content-sensitive dedupe key, so an unchanged file is not relaunched;
- `wait_on` pointing to the prior file, preserving sequential workspace allocation.

The repeated summary metadata is deliberate: after deduplication, any surviving
proposal can safely become the clan declarer. Axe allocates the concrete clan template
once per actionable scan and emits the summary exactly once on that declaration; clan
joiners do not redeclare it. In ACE the default scan reads as:

```text
◆ TOOBIG SPLIT · 3 FILES
MISSION
Decompose oversized Python modules into focused, reviewable units
without changing behavior.
2 scan roots · limits 1,000 / 850 / 700 lines · sequential queue
```

Concrete agent names look like
`toobig-<token>.split_file.<path-slug>.<digest>`. All proposals from that scan
belong to the same clan generation, while later scans can allocate a new one. The
plugin authors only the `toobig-@` template and proposal metadata; Axe alone chooses
the concrete clan name, injects declaration/join directives, and launches agents.

The script deliberately has no flock, no `sase agent list`, and no `sase run`. Those
responsibilities now belong to Axe:

```yaml
axe:
  lumberjacks:
    maintenance:
      description: |-
        Propose split-file maintenance for oversized Python files once a minute

        Runs every 60 seconds so the chop can notice when its hourly `run_every` window opens. Use this lane for
        low-cost maintenance proposals guarded by AXE, not for commit-threshold audits.

        The `toobig-` clan inhibit guard prevents a new split-file scan while an earlier split swarm is still active.
      interval: 60
      chops:
        toobig_split:
          script: bugyi_chop_toobig_split
          description: |-
            Split oversized Python files in sase

            Runs `bugyi_chop_toobig_split` once per hour for the `sase` target and scans the configured `src` and
            `tests` trees with the `1000`, `850`, and `700` line limits.

            Each actionable scan emits content-deduped `%auto #split_file:<path>` proposals in a sequential `toobig-`
            clan, so one oversized file is handled before the next claim starts.
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
