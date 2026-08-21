# bugyi-chops

`bugyi-chops` is Bryan Bugyi's community [SASE](https://sase.sh/) plugin for
scheduled Axe jobs that need to propose coding-agent work. It supplies two console
scripts:

| Script | What it proposes |
| --- | --- |
| `bugyi_chop_ci_watch` | LaunchApproval-gated CI repairs and explicitly enabled, guarded release merges |
| `bugyi_chop_toobig_split` | One `%auto #split_file:<path>` agent per oversized Python file, chained in scan order |

The scripts never launch agents themselves. `bugyi_chop_toobig_split` scans and
assembles a prompt, then uses the public `sase.chops` SDK to atomically write a
validated result document; Axe owns guard and trigger evaluation, deduplication,
workspace allocation, proposal launches, and the final action lifecycle for that
proposal. `bugyi_chop_ci_watch`'s CI-fix path instead files a durable LaunchApproval
gate directly (see [`ci_watch` CI-fix gates](#ci_watch-ci-fix-gates) below); a human
always approves or rejects it before any repair agent launches.

`bugyi_chop_ci_watch` additionally owns a tightly guarded direct side effect: when
`vars.merge_enabled` is true and `SASE_CHOP_DRY_RUN=0` is explicitly present, it may
squash-merge one fully green release-please or release-plz PR. Missing or true dry-run
context always suppresses merging. Its CI-fix gates are filed only when a
`sase agent list -j` probe reports no live agent in the `ci_fix` hood, no earlier gate
is still pending, and the current failing-job fingerprint has not already been gated
for this red episode. Approved repair agents use the `ci_fix.<slug>.@` template so SASE
assigns a unique launch token.

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
references. This proposal contract governs `bugyi_chop_toobig_split`.
`bugyi_chop_ci_watch`'s CI-fix path never populates `proposed_launches`; it creates a
LaunchApproval gate directly through `sase launch request` instead, so its result
document always carries an empty proposal list.

## `ci_watch` CI-fix gates

Once a red repository's failing-job fingerprint has held for `red_debounce_ticks`
ticks, `bugyi_chop_ci_watch` files a durable LaunchApproval gate instead of proposing a
launch to Axe:

```bash
sase launch request -f <payload.json> -o json -s ci_watch
```

The gate kind is `launch` (`LaunchApproval`, `auto_policy: "forbidden"`), so approval is
never automatic — a human must approve or reject the gate before any `ci_fix.<slug>.@`
agent launches. Axe never scaffolds a LaunchApproval prompt the way it scaffolds a
proposal, so the prompt itself is self-sufficient:

```text
#gh:<owner>/<repo> %i:ci_fix.<slug>.@ %w(runners=0)

#pr(ci_fix_<slug>_<sha7>, status=ready)

#actstat(repo=<owner>/<repo>)

Repair the current default-branch CI failure in <owner>/<repo>.
...
```

Each tick evaluates, in order, whether a new gate is suppressed — first match wins:

1. `fix_enabled` is false → `fix_disabled`.
2. The `sase agent list -j` probe fails → `agents_check_failed`.
3. A live agent named `ci_fix` or `ci_fix.*` exists → `fix_in_flight`.
4. Any gate recorded in the fix ledger is still `pending` → `gate_pending`. This is
   global: while one CI-fix gate is unanswered, no new gate is filed for any repo.
5. The candidate's dedupe key is already recorded in the ledger → `already_gated`.
6. The per-tick cap `max_fix_proposals_per_tick` is reached → `fix_cap_reached`.
7. The tick is a dry run (`SASE_CHOP_DRY_RUN=1`) → `dry_run`. A dry run reports the
   gate it would have filed but never creates one and never records its dedupe key, so
   the next live tick still gates that failure.
8. Otherwise, the gate is created.

The durable ledger (`ci_watch_fixes.json`, schema version 2) records each gate's
request id under the dedupe key `ci_fix:{repo}:{failing_job_fingerprint}:e{episode}`,
where `episode` increments only when a repository is observed going red and then green
again. A key is recorded the instant its gate is created and is never re-gated: a
rejected, cancelled, or timed-out gate does not come back, and only a genuinely new
failure — a changed failing-job fingerprint or a new red episode — can produce another
gate. The gate itself is the only user-facing signal for a CI-fix; no separate
notification is sent.

## `toobig_split`

`bugyi_chop_toobig_split` runs `toobig --files-only` for each configured tree,
normalizes and de-duplicates its paths, and emits one proposal per file.

The scanner contract is fail-closed around two healthy outcomes. Exit `0` is a
completed scan whose stdout listing, if any, contains only informational or
warning-level paths. Exit `1` with one or more listed paths is also a completed
scan: `toobig` uses that exit code for a hard-limit hit and still writes the
matching paths to stdout, which this chop consumes as actionable findings.
Malformed invocations, missing trees, and other filesystem errors reuse exit
`1` but produce no path payload; those empty exit-`1` results, and every other
nonzero status, remain a typed `check_error`.

Each proposal has:

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
