# bugyi-chops

`bugyi-chops` is Bryan Bugyi's community [SASE](https://sase.sh/) plugin for
scheduled Axe jobs that watch repository health, submit guarded release PRs, and
propose maintenance work. It supplies two console scripts:

| Script | Responsibility |
| --- | --- |
| `bugyi_chop_ci_watch` | Notify on CI failures and merge explicitly enabled, guarded release-please PRs |
| `bugyi_chop_toobig_split` | One sequential condition-gated `%auto #split_file:<path>` agent per oversized Python file, routed through `@medium` |

The scripts never launch agents themselves. `bugyi_chop_toobig_split` scans and
assembles prompts, then uses the public `sase.chops` SDK to atomically write a
validated result document; Axe owns guard and trigger evaluation, keyed-proposal
deduplication, workspace allocation, proposal launches, and the final action lifecycle
for those proposals.

`bugyi_chop_ci_watch` is deliberately narrower. It never creates gates, launch
requests, repair prompts, or Axe proposals. Its only mutation outside its own state and
report files is a guarded `gh pr merge --merge|--squash|--rebase --match-head-commit`
(the flag follows `vars.merge_method`, default `merge`) for an eligible release-please
PR when `vars.merge_enabled` is true and `SASE_CHOP_DRY_RUN=0` is explicitly present.
Missing or true dry-run context suppresses merging and notifications.

## Installation

Install the published package into the same managed environment as SASE:

```bash
sase plugin install bugyi-chops
```

`-g`/`--git` forces a built VCS snapshot install from this repository instead of the
published index distribution; it is not a development install. For local development
against this repository, see [Development and releases](#development-and-releases)
below.

All scripts require Python 3.12 or newer and `sase>=0.16.0,<0.17`. SASE 0.16.0 is the
first compatible release series for typed Axe chop admission with `%if`. The package
also installs the `toobig` scanner used by `bugyi_chop_toobig_split`.

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
      "prompt": "%if::\n```bash\npath=src/large.py\n...\n```\n%auto #split_file:src/large.py",
      "workspace": "gh:sase-org/sase",
      "model": "@medium"
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
`bugyi_chop_ci_watch` always returns an empty `proposed_launches` list.

## `ci_watch`

`bugyi_chop_ci_watch` sweeps the configured repository allowlist through `actstat`,
reconciles each settled commit with the current default branch through bounded GitHub
queries, and publishes a combined `CI WATCH` report. Red observations keep typed job
evidence: workflow name, job name, conclusion, direct job/run URL, failing steps,
failing SHA, and current-head SHA when a stale settled failure is still actionable
while newer HEAD checks are unsettled.

Failure notifications are incident-based. The state file `ci_watch_state.json` tracks
the active failing-job fingerprint per repository, independent of SHA. The same
failure is announced once and kept active while it persists; changed job/step evidence
replaces the incident; a green or non-red observation clears it so a later recurrence
is announced again. Failed notification deliveries remain marked unsent and are retried
on the next live tick.

Release handling is release-please only. Configure `vars.release_repositories` as a
list of repositories from `vars.repos`; generator mappings are rejected and other
release branch families are ignored. A release PR is mergeable only when all guards
pass: exactly one release-please candidate for the repository, configured default base, non-draft,
mergeable and clean, non-empty fully green check rollup, a green current default branch,
idle release-please/publish workflow, deterministic dependency order,
`max_merges_per_tick`, explicit live mode, a final PR/default-branch reread, and
`--match-head-commit` race protection.

By default "a green current default branch" means the actstat sweep classified it green.
Configuring `vars.gating_workflows` (a list of workflow names, empty by default) switches
that guard to explicit HEAD evidence instead: every named workflow must have a completed,
green run at the exact current default-branch HEAD, distinguishing a workflow that never
ran (`gating_workflow_missing`), is still running (`gating_workflow_in_flight`), and ran
red (`gating_workflow_red`). This makes release eligibility independent of the broader
actstat classification, so unrelated flaky or red workflows outside the allowlist no
longer block a release. Configuring `vars.heavy_workflows` (also empty by default) adds a
freshness requirement on top: every named heavy workflow's newest completed run on the
default branch must be green and no older than `vars.heavy_max_age_hours` (default `6`),
reported as `heavy_lane_not_green` or `heavy_lane_stale` otherwise. Both allowlists are
re-checked, from a freshly fetched HEAD, during the final pre-merge reread. Leaving both
lists empty preserves the pre-existing actstat-only behavior. `vars.merge_method`
(`merge`, `squash`, or `rebase`; default `merge`) selects the `gh pr merge` flag.

Successful merge submissions are written durably with `notification_sent: false` before
any SASE notification is attempted. The release notification is marked delivered only
after `sase notify create` succeeds, so a later tick retries an unsent release notice.
If report publication fails, inline notifications still include the full evidence but
omit the `ViewReport` action and the chop returns `check_error` so the failure is
visible.

Minimal configuration:

```yaml
vars:
  actstat_bin: /home/bryan/.cargo/bin/actstat
  gh_bin: gh
  sase_bin: sase
  repos: [sase-org/sase, sase-org/sase-core, sase-org/sase-telegram]
  release_repositories: [sase-org/sase-telegram, sase-org/sase]
  merge_order: [sase-telegram, sase]
  max_merges_per_tick: 1
  merge_enabled: true
```

Configuration adding an explicit merge method and release gates:

```yaml
vars:
  # ... same as above ...
  merge_method: squash
  gating_workflows: [CI]
  heavy_workflows: [E2E]
  heavy_max_age_hours: 6
```

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

- the shared `toobig-@` clan template, with a keyed basename member template
  (`<basename>.{@<path-digest>}`);
- the same Rich `clan_summary` metadata, describing the mission, file and scan-root
  counts, configured limits, and sequential queue;
- structured `model: "@medium"`, which Axe renders as one `%model:@medium`
  directive alongside `%auto #split_file:<path>`;
- one `%if::` Bash fence followed by `%auto #split_file:<path>` as its prompt;
- no proposal `dedupe_key`; every scheduled scan may reconsider files that remain
  oversized;
- `wait_on` pointing to the prior file, preserving sequential workspace allocation.

The `%if` fence runs after the proposal's sequential wait and immediately before typed
admission. SASE briefly claims a temporary numbered workspace to evaluate it — never a
runner, agent identity, or model request — and releases that claim once the verdict is
settled, whether the proposal is eligible or skipped. This is why a queued condition
still sees a file an earlier agent already split even when the chop's own checkout is
stale: the leased checkout synchronizes from the configured upstream before `%if` runs.
The proposal skips when the target file is gone or has dropped below the configured
floor (`min(limits)`, 700 lines in the default configuration) in that freshly leased
checkout. A read/count failure for an existing file is a visible condition error. This
requires SASE's `typed_launch_units` flag and a compatible
SASE 0.16.x runtime; older SASE 0.13.x runtimes reject these directives before model
dispatch.

The structured `@medium` field is the only model source. It selects SASE's
configurable, load-balanced alias pool rather than pinning a concrete
provider/model in the prompt body, so operators retune `@medium` centrally
without a per-chop model variable.

The repeated summary metadata is deliberate: after conditional admission or any other
proposal filtering, any surviving proposal can safely become the clan declarer. Axe
allocates the concrete clan template once per actionable scan and emits the summary
exactly once on that declaration; clan joiners do not redeclare it. In ACE the default
scan reads as:

```text
◆ TOOBIG SPLIT · 3 FILES
MISSION
Decompose oversized Python modules into focused, reviewable units
without changing behavior.
2 scan roots · limits 1,000 / 850 / 700 lines · sequential queue
```

Concrete agent names look like `toobig-<token>.<basename>.<token>`, for example
`toobig-3j.test_query_profile.0`. Two files that share a basename keep the same
readable stem and allocate distinct member tokens (`.0`, `.1`) inside that clan.
All proposals from that scan belong to the same clan generation, while later scans
can allocate a new one. The plugin authors only the `toobig-@` clan template, a
keyed basename member template, and proposal metadata; it never inspects live
agents or chooses concrete tokens. Axe alone allocates the clan and member
tokens, injects declaration/join directives, and launches agents.

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

            Each actionable scan emits sequential `toobig-` clan proposals. Every member rechecks the configured
            700-line floor through `%if` after its wait and before admission, so stale files skip without allocating an
            agent while still-oversized files launch normally. The chop sets the structured proposal model to `@medium`;
            Axe emits `%model:@medium` and SASE consumes the alias pool at each real invocation.
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

Preview configured chops without side effects:

```bash
sase axe chop run 'ci_watch' -L ci_watch --dry-run --chop-verbose
sase axe chop run 'toobig_split[sase]' -L maintenance --dry-run --chop-verbose
# Short flags:
sase axe chop run 'ci_watch' -L ci_watch -n -V
sase axe chop run 'toobig_split[sase]' -L maintenance -n -V
```

`-V` sets `SASE_CHOP_VERBOSE` and prints scanner commands and target diagnostics.
Every actual invocation also emits a compact summary line with bounded integer
counters and an explicit reason for no-op/error outcomes.

For lower-level diagnosis, reuse a context JSON written by Axe and invoke a script
directly. `ci_watch` requires `SASE_CHOP_DRY_RUN=0` before it can notify or submit a
merge; all other direct invocations render decisions only:

```bash
SASE_CHOP_RESULT_FILE=/tmp/ci-watch-result.json \
  bugyi_chop_ci_watch --context /path/to/context.json --verbose
jq . /tmp/ci-watch-result.json

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
