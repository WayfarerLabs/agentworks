# Bootstrap Packaging LLD

- Status: Draft for Phase 3 implementation
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- HLA: `docs/sdd/2026-08-05-onboarding-and-discovery/hla.md`
- Plan: `docs/sdd/2026-08-05-onboarding-and-discovery/plan.md`, Phase 3
- Verified against: Claude Code and Codex packaging documentation on 2026-08-10

## Purpose and limits

Phase 3 publishes one small onboarding bootstrap through three entry points: Claude Code, Codex, and
the repository README. The bootstrap discloses the workstation authority the agent needs,
establishes consent, installs a compatible Agentworks CLI, and hands all teaching to
`agw guide concept-onboarding --agent`.

This phase does not create another onboarding workflow, template framework, custom installer,
runtime package loader, network abstraction, or copy of guide content. Both plugins are skills-only
packages. They contain no hook, command, script, MCP server, app, or pre-approved-tool declaration.
Installing one can make the inert skill discoverable, but cannot inspect or change the workstation.

## Decisions

| Concern              | Decision                                                                                                                                                                                                                                       |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical prose      | `packaging/onboarding/bootstrap.md` is the only authored bootstrap body and owns its security posture text and links.                                                                                                                          |
| Canonical metadata   | `packaging/onboarding/metadata.json` owns machine metadata only: package identity, bootstrap version, minimum CLI version, and publisher fields.                                                                                               |
| Minimum CLI          | `agentworks-cli >=0.14.0`; no maximum. Version 0.14.0 is the first release containing the guide contract used by the bootstrap.                                                                                                                |
| Package identity     | Marketplace `agentworks`; plugin and skill `agentworks-onboarding`; independent bootstrap package version starts at `1.0.0`.                                                                                                                   |
| Cross-harness parity | A deterministic generator emits both native package layouts from the same body and metadata. Generated skill bodies are byte-identical after their generated frontmatter.                                                                      |
| README parity        | The first fenced block under `## Getting Started` contains the exact canonical body. The generator owns that fenced region.                                                                                                                    |
| Runtime behavior     | The inert skill instructs the agent to obtain consent, check or install a compatible CLI, and run the guide. The skill performs none of those actions itself and contains no day-two teaching.                                                 |
| Security posture     | Claude Code uses default/manual approvals without bypass. Codex uses `danger-full-access` with on-request approvals because Agentworks needs workstation-wide account access. Never change harness security settings on the operator's behalf. |
| Release ownership    | The Agentworks repository owns the source, wrappers, marketplaces, tests, and version. The CLI release and bootstrap version remain distinct.                                                                                                  |

The bootstrap version changes whenever an installed generated artifact changes. It is deliberately
independent of the CLI version. Claude Code and Codex cache installed plugins by version, so a body,
frontmatter, or manifest change without a bootstrap version bump is a generation error. Marketplace
entries do not repeat the version.

## Repository layout and ownership

The implementation adds this layout:

```text
packaging/onboarding/
  bootstrap.md                         # authored canonical body
  metadata.json                        # authored canonical metadata
plugins/
  claude-code/agentworks-onboarding/
    .claude-plugin/plugin.json         # generated
    skills/agentworks-onboarding/
      SKILL.md                         # generated
  codex/agentworks-onboarding/
    .codex-plugin/plugin.json          # generated
    skills/agentworks-onboarding/
      SKILL.md                         # generated
.claude-plugin/
  marketplace.json                    # generated Claude Code catalog
.agents/plugins/
  marketplace.json                    # generated Codex catalog
scripts/
  generate-onboarding-bootstrap.py    # authored deterministic generator
cli/tests/bootstrap/
  test_generation.py
  test_contract.py
  test_flows.py
```

The root `plugins/` tree is harness distribution packaging and is separate from the in-process
system plugins under `cli/agentworks/plugins/`.

Neither harness documents one dual-manifest directory as a cross-harness package contract. Two
generated wrappers under native plugin roots are therefore the smallest supported layouts; they
share the canonical body rather than sharing an undocumented runtime package root.

Only these seven regions are generated:

1. `.claude-plugin/marketplace.json` in full.
2. `.agents/plugins/marketplace.json` in full.
3. `plugins/claude-code/agentworks-onboarding/.claude-plugin/plugin.json` in full.
4. `plugins/claude-code/agentworks-onboarding/skills/agentworks-onboarding/SKILL.md` in full.
5. `plugins/codex/agentworks-onboarding/.codex-plugin/plugin.json` in full.
6. `plugins/codex/agentworks-onboarding/skills/agentworks-onboarding/SKILL.md` in full.
7. The one marked fenced bootstrap region under README `## Getting Started`.

The first six paths contain a generated-file notice where their format permits one. JSON has no
comments, so its generated ownership is proven by the generator's exact-path list and tests. No
other README content is rewritten. The generator refuses missing or duplicate README boundary
markers and refuses an unexpected file below either generated plugin root.

## Canonical bootstrap contract

`bootstrap.md` is agent-addressed Markdown without YAML frontmatter. It is short enough to appear
unchanged in the README fenced block and in both generated skills. Its headings and order are part
of the contract:

1. `Agentworks setup request`
2. `Access disclosure and consent`
3. `Strict harness posture`
4. `After approval`

### Access disclosure and consent

Before any command, probe, file read, verification, or configuration action, the body directs the
agent to restate all of these facts to the operator and wait for affirmative consent:

- The agent runs on the workstation from which the operator intends to manage Agentworks.
- It needs the ability to inspect files and execute commands with the permissions of the workstation
  account running the harness.
- That account-scoped access is not root access. Any privilege elevation is a separate action that
  must be disclosed and explicitly approved.
- The agent can reach everything Agentworks can reach, including managed resources, secret
  references, and destinations reachable over SSH from the workstation.
- Sensitive material is checked only for presence when discovery needs it. Values, private-key
  contents, and secret contents are never viewed.
- The agent asks before each workstation probe, names its exact scope, and offers a manual
  alternative when the operator refuses.

A refusal ends automation without penalty. The agent may give the operator the inert manual steps,
but it does not perform them or treat refusal as verification.

### Strict harness posture

The canonical body links to both harnesses because the same body appears in both packages and the
README. It tells the agent to identify its current harness and recommend, not set, the matching
posture:

- Claude Code: use `default` permission mode and the normal manual approval flow. Never use
  `bypassPermissions` for workstation management. See <https://code.claude.com/docs/en/permissions>
  and <https://code.claude.com/docs/en/sandboxing>.
- Codex: use `sandbox_mode = "danger-full-access"` with `approval_policy = "on-request"`. Agentworks
  manages account-scoped files, credentials, SSH destinations, and infrastructure beyond a project
  workspace, so `workspace-write` is not the operating boundary. On-request approvals preserve
  operator visibility for consequential actions. Never use `approval_policy = "never"` or the bypass
  flag. See <https://learn.chatgpt.com/docs/agent-approvals-security> and
  <https://learn.chatgpt.com/docs/config-file/config-basic>.

These policies preserve manual approval while allowing the workstation-wide account access that
Agentworks requires. No-prompt or bypass modes discard the operator visibility R12 requires.

The bootstrap never writes `.claude/settings*.json`, `.codex/config.toml`, or managed policy. It
asks the operator to select or confirm the posture using the harness's own controls. The canonical
body owns the recommendation and links. Permanent docs point to the generated README bootstrap
rather than duplicating those links, and `metadata.json` is not a second link registry.

### After approval

Only after affirmative consent does the bootstrap direct the agent to:

1. Check `agw version` without inspecting unrelated workstation state.
2. Install or upgrade from PyPI with uv when `agw` is absent, reports an unknown or malformed
   version, or is older than 0.14.0. Python 3.12 or newer is required, and uv may supply a managed
   Python runtime. The actionable repair command is
   `uv tool install --upgrade 'agentworks-cli>=0.14.0'`.
3. Confirm `agw version` satisfies the minimum after installation. A failed or unsatisfied check
   stops before guide execution.
4. Run `agw guide concept-onboarding --agent` and follow the returned action inventory exactly.

The bootstrap neither expands those actions nor predicts their current commands. It tells the agent
to consume the guide's JSON list, describe, and doctor records where each action requests them. The
installed CLI remains the authority for guided, replayable, migration, management, and bug-reporting
teaching.

## Generated skill wrappers

Both generated `SKILL.md` files have Agent Skills compatible frontmatter followed by the canonical
body verbatim:

```yaml
---
name: agentworks-onboarding
description: >-
  Install, configure, discover, and manage Agentworks from an operator workstation. Use when the
  operator asks to set up Agentworks or manage an existing installation.
compatibility:
  Requires Python 3.12+ and agentworks-cli >=0.14.0; network and approved workstation access are
  needed during setup.
metadata:
  agentworks-bootstrap-version: "1.0.0"
  agentworks-min-cli-version: "0.14.0"
---
```

There is no `allowed-tools` field. Loading the skill grants no tool permission and does not weaken
either harness's approval path. The generator rejects executable skill resources or extra files in
either package root.

### Claude Code package

The Claude Code manifest is deliberately minimal:

```json
{
  "name": "agentworks-onboarding",
  "version": "1.0.0",
  "description": "Set up and manage Agentworks through its installed guide.",
  "author": {
    "name": "Wayfarer Labs"
  },
  "homepage": "https://github.com/WayfarerLabs/agentworks",
  "repository": "https://github.com/WayfarerLabs/agentworks",
  "license": "MIT"
}
```

The root Claude Code marketplace is:

```json
{
  "name": "agentworks",
  "owner": {
    "name": "Wayfarer Labs"
  },
  "plugins": [
    {
      "name": "agentworks-onboarding",
      "source": "./plugins/claude-code/agentworks-onboarding",
      "description": "Set up and manage Agentworks through its installed guide"
    }
  ]
}
```

Generated paths never traverse above the plugin root. Claude Code copies installed plugins into its
cache, so the skill references no canonical source or repository-relative file at runtime.

### Codex package

The Codex manifest uses the same identity and common metadata, plus the native skill path:

```json
{
  "name": "agentworks-onboarding",
  "version": "1.0.0",
  "description": "Set up and manage Agentworks through its installed guide.",
  "author": {
    "name": "Wayfarer Labs",
    "url": "https://github.com/WayfarerLabs"
  },
  "homepage": "https://github.com/WayfarerLabs/agentworks",
  "repository": "https://github.com/WayfarerLabs/agentworks",
  "license": "MIT",
  "skills": "./skills/"
}
```

The root Codex marketplace is:

```json
{
  "name": "agentworks",
  "interface": {
    "displayName": "Agentworks"
  },
  "plugins": [
    {
      "name": "agentworks-onboarding",
      "source": {
        "source": "local",
        "path": "./plugins/codex/agentworks-onboarding"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
```

`ON_INSTALL` is the marketplace's required authentication-timing value. The package declares no app
or MCP authentication and performs no authentication itself.

## Direct GitHub installation

Permanent installation docs give these exact commands. They add the Agentworks repository as a
marketplace and then install its onboarding plugin:

```bash
claude plugin marketplace add WayfarerLabs/agentworks
claude plugin install agentworks-onboarding@agentworks
```

```bash
codex plugin marketplace add WayfarerLabs/agentworks
codex plugin add agentworks-onboarding@agentworks
```

Candidate probes use these exact ref forms, followed by the same native install command:

```bash
claude plugin marketplace add WayfarerLabs/agentworks@<branch-or-tag>
claude plugin install agentworks-onboarding@agentworks
```

```bash
codex plugin marketplace add WayfarerLabs/agentworks --ref <branch-or-tag>
codex plugin add agentworks-onboarding@agentworks
```

PR acceptance pins the PR branch, records the marketplace commit each harness resolves, and asserts
that it equals the expected PR head. Release evidence uses the release tag. Operator documentation
uses the repository default branch. Private forks rely on the harness's existing Git credentials;
neither package reads or carries a token.

## Deterministic generation and drift guard

`scripts/generate-onboarding-bootstrap.py` uses only the Python standard library. It is a small
literal renderer, not a general template engine. It parses and validates `metadata.json`, reads the
canonical body as UTF-8, renders the two frontmatter wrappers and four JSON documents with sorted
keys and a final newline, and replaces only the README generated region.

The command has two modes:

```bash
./scripts/generate-onboarding-bootstrap.py
./scripts/generate-onboarding-bootstrap.py --check
```

Normal mode writes each result through a same-directory temporary file and atomic replace. Check
mode renders all outputs into a fresh temporary directory, compares bytes and modes against the
committed files, verifies the exact generated-path inventory, and exits nonzero with every drifted
path. It never modifies the checkout.

Validation occurs before any write. It rejects:

- unknown or missing metadata fields, non-SemVer bootstrap versions, a minimum other than 0.14.0 in
  the first release, or a declared maximum CLI version;
- mismatched marketplace, plugin, and skill identities;
- missing disclosure clauses or security links;
- any command, executable fence, probe instruction, or guide invocation before the `After approval`
  heading;
- any `allowed-tools`, hook, MCP, app, script, command, or executable package resource;
- generated paths outside the exact inventory;
- a README region that is absent, duplicated, outside `## Getting Started`, or not the first fenced
  content under that heading.

CI adds a `bootstrap-packaging-drift` job running `--check` and adds it to `ci-success.needs`. A
separate focused CI test compares the generated package fingerprint and bootstrap version with their
merge-base values. The generator itself remains checkout-local. Focused tests invoke the renderer
rather than copying its validation rules.

## README contract

The generator owns one region immediately after `## Getting Started`:

````markdown
<!-- BEGIN GENERATED AGENTWORKS ONBOARDING BOOTSTRAP -->

```text
<exact bytes from packaging/onboarding/bootstrap.md>
```

<!-- END GENERATED AGENTWORKS ONBOARDING BOOTSTRAP -->
````

The block is the first content beneath the heading, so GitHub exposes a single copy button for the
whole agent-addressed request. The human installation path follows under
`### Install Agentworks yourself`; it is not inside the generated region and does not paraphrase the
security disclosure.

Tests extract the README fence and compare its inner bytes exactly with `bootstrap.md`. The two
generated skill bodies are compared to the same source after frontmatter removal. There is no
normalization, semantic comparison, or harness-specific substitution that could hide drift.

## Failure behavior

Bootstrap failures are safe and actionable:

- No affirmative consent: stop without a tool call and offer manual instructions.
- Required harness posture unavailable or centrally prohibited: keep the stricter policy, explain
  which action it prevents, and let the operator perform that action manually. Never weaken policy.
- uv unavailable: point to <https://docs.astral.sh/uv/getting-started/installation/> and ask before
  installing it or using a different operator-approved installer.
- Python older than 3.12: allow uv to select a managed compatible runtime. Do not use root or
  replace the workstation's system Python.
- `agw` missing, unknown, malformed, or older than 0.14.0: do not invoke the guide. Show installed
  and required versions when known, give the exact uv upgrade command, and retry the version check
  only after approval.
- PyPI, GitHub, or network failure: preserve the harness error, name the failed source, and stop. Do
  not silently install from another index, branch, or archive.
- Marketplace or package validation failure: install nothing from that package. Other marketplace
  entries remain independently usable.
- Guide failure: show the framed `agw` error and stop or follow its explicit remediation. Do not
  invent onboarding actions from bootstrap prose.

No failure path reports success, records synthetic evidence, treats refusal as verification, or
falls back to an older guide contract.

## Test and probe matrix

### Deterministic repository gates

| Probe                      | Claude Code                                                  | Codex                                                                        | README                                |
| -------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------- | ------------------------------------- |
| Canonical body equality    | Strip frontmatter and compare exact bytes                    | Strip frontmatter and compare exact bytes                                    | Extract fence and compare exact bytes |
| Native manifest validation | `claude plugin validate --strict`                            | Install from an isolated local marketplace with the pinned current Codex CLI | Not applicable                        |
| Generated inventory        | Exact Claude package paths, no extras                        | Exact Codex package paths, no extras                                         | Exactly one marked region             |
| Inertness                  | No hook, MCP, command, script, app, or allowed tool          | Same                                                                         | No executable wrapper                 |
| Disclosure order           | Every R12 clause and both posture links precede first action | Same                                                                         | Same                                  |
| Minimum version            | Compatibility and metadata say 0.14.0, no maximum            | Same                                                                         | Body says 0.14.0, no maximum          |
| Drift                      | Generator `--check` is clean                                 | Same invocation                                                              | Same invocation                       |

Harness validators are version-pinned in CI to the latest stable versions verified during
implementation. Updating either pin requires rerunning its local-install and clean-environment probe
before merge. The generated JSON also receives schema-focused unit tests so a validator availability
outage does not obscure an ordinary generator defect.

### Flow fixtures

`cli/tests/bootstrap/test_flows.py` runs every scenario over the Claude Code skill path, the Codex
skill path, and the README body. Each case begins from the same canonical body and drives the real
`agw guide` and JSON surfaces against isolated Agentworks config and database fixtures.

| Scenario               | Required evidence                                                                                                                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fresh guided setup     | Disclosure is transcript event 1; consent is event 2; the first action follows; the resulting guide action IDs and final registry, graph, and stored-row facts match the Phase 1 fixture. |
| Fresh replayable setup | Same action IDs and final facts as guided mode; commands use `agw --non-interactive`; every JSON v1 document is parsed and its version, command, and data shape asserted.                 |
| Refused probe          | No probe runs; both modes retain the same `unverifiable` outcome and manual alternative.                                                                                                  |
| Rerun                  | Replayed verified evidence produces the expected no-op plan without a new Agentworks ledger.                                                                                              |
| Post-upgrade delta     | A newly available fixture capability appears as not yet adopted; already-ready work is not repeated.                                                                                      |
| Old CLI                | Version 0.13.x never reaches the guide; the transcript contains the exact minimum and upgrade command after disclosure.                                                                   |
| Failed upgrade         | No guide or setup action follows; the package-manager error remains visible and no alternative source is selected.                                                                        |

The fixture driver records events at the command-runner boundary rather than inferring order from
final output. It does not implement guide logic or parse bootstrap prose into an action language.
The only bootstrap-specific decisions it makes are the consent response and the installed CLI
version; all later actions come from the real guide records.

### Clean-environment and live probes

Before Phase 3 is handed off, each row runs with a fresh operating-system account, empty harness
plugin state, empty Agentworks config and database, and no `agw` on `PATH`. The harness version,
plugin source ref, CLI artifact, transcript, elapsed time, every approval, and every refusal are
retained as acceptance evidence.

| Path                   | Install source                                                         | Required outcome                                                                                                                                            |
| ---------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code plugin     | Exact Claude candidate-ref command pair above                          | Installed cached skill equals canonical body; a vanilla session emits disclosure before action, reaches the guide, and completes the first working session. |
| Codex plugin           | Exact Codex candidate-ref command pair above                           | Same, using `danger-full-access` and on-request approvals.                                                                                                  |
| README only            | Copy the GitHub-rendered fenced block into a vanilla supported harness | Same flow without any plugin installed.                                                                                                                     |
| Claude refusal         | Installed plugin                                                       | Refuse the first probe; no probe runs and a manual alternative is shown.                                                                                    |
| Codex refusal          | Installed plugin                                                       | Same.                                                                                                                                                       |
| Old CLI upgrade        | Preinstall 0.13.x, then invoke each plugin                             | Disclosure precedes the version repair; 0.14.0 or newer is installed; the guide then runs.                                                                  |
| Non-interactive replay | Each plugin in turn                                                    | Same caller-owned evidence yields the same final facts and JSON v1 parsing as guided setup.                                                                 |
| Upgrade discovery      | Each plugin in turn with an older complete fixture                     | A newly registered capability is reported without redoing ready work.                                                                                       |

Production metadata and prose always require `agentworks-cli >=0.14.0`. Before 0.14.0 is on PyPI,
the clean probes install the built 0.14.0 release-candidate artifact directly at the package-install
boundary. They do not resolve the candidate from PyPI and do not change the production bootstrap
command or metadata. After publication, the same matrix runs once against real PyPI and the tagged
GitHub marketplace. The post-publication smoke is release evidence, not permission to merge a
bootstrap that failed the candidate probes.

Every live transcript is checked for a strict prefix: disclosure, operator decision, then the first
command or probe. A model response that combines disclosure with an already-executed action fails.
No acceptance probe grants bypass mode, no-prompt execution, or root. Codex retains on-request
approvals while using the required workstation-wide `danger-full-access` sandbox mode.

## Documentation, completions, and sample config

The implementation commit ships these permanent artifacts with the generated packages:

- README generated bootstrap block and the retained human installation path.
- A permanent onboarding and security section in `cli/README.md` or a focused
  `docs/guides/onboarding.md`, linked from both README and CLI docs. It owns the direct GitHub
  install commands, minimum CLI behavior, update command, and uninstall command. It points to the
  generated README bootstrap for the full disclosure, harness posture, and official security links.
- Package-local generated metadata and the two marketplace catalogs.

Phase 3 adds no Agentworks CLI command or option. Existing `agw guide` completions are unchanged, so
the shell completion generators and snapshots need no edit. It adds no setting, so
`cli/agentworks/sample-config.toml` is unchanged. The implementation handoff records both audits.

Permanent prose uses the current destination only. Package-layout rationale, candidate-artifact
details, and this test decomposition remain in the SDD.

## External contracts verified

| Contract                                        | Primary source                                                                                 | Design consequence                                                                             |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Claude Code plugin structure                    | <https://code.claude.com/docs/en/plugins>                                                      | Native `.claude-plugin/plugin.json` plus plugin-root `skills/`.                                |
| Claude Code marketplace and GitHub installation | <https://code.claude.com/docs/en/plugin-marketplaces>                                          | Root `.claude-plugin/marketplace.json`, relative source path, two-step GitHub add and install. |
| Claude Code cache boundary                      | <https://code.claude.com/docs/en/plugins-reference>                                            | Generated package is self-contained and never references canonical files outside its root.     |
| Claude Code permissions and sandbox             | <https://code.claude.com/docs/en/permissions> and <https://code.claude.com/docs/en/sandboxing> | Default permission mode with manual approvals and no bypass.                                   |
| Codex package structure                         | <https://developers.openai.com/plugins/build/plugins>                                          | Native `.codex-plugin/plugin.json`, plugin-root `skills/`, no unused component declarations.   |
| Codex sandbox and approvals                     | <https://learn.chatgpt.com/docs/agent-approvals-security>                                      | `danger-full-access` for workstation-wide account access, with on-request approvals.           |
| Agent Skills frontmatter                        | <https://agentskills.io/specification>                                                         | Common name, description, compatibility, and string metadata; no allowed-tool grant.           |

Codex CLI 0.147.0 was also exercised locally on 2026-08-10 to verify
`codex plugin marketplace add owner/repo` and `codex plugin add plugin@marketplace`. These commands
match the current OpenAI plugin builder documentation. Implementation rechecks both harness CLIs
against their latest stable releases before committing generated formats.

## Open questions

None. The production minimum remains 0.14.0, and pre-release gates consume the built
release-candidate artifact directly rather than resolving an unreleased version from PyPI.
