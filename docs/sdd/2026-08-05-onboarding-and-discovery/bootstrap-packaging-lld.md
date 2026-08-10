# Phase 3 Agentworks Assistance Packaging and Guide Companion LLD

- Status: Revised draft for the Phase 3 lifecycle-assistance checkpoint
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- HLA: `docs/sdd/2026-08-05-onboarding-and-discovery/hla.md`
- Plan: `docs/sdd/2026-08-05-onboarding-and-discovery/plan.md`, Phase 3
- Verified against: Claude Code and Codex packaging tools and official documentation on 2026-08-10

## Purpose and limits

Phase 3 makes Agentworks assistance available before and after installation through native Claude
Code and Codex packages and the repository README. One inert body discloses the workstation access
Agentworks work can require, obtains consent, offers review of the exact intended release source,
installs or updates a compatible CLI when needed, and hands all teaching to `agw guide --agent`.

The same phase completes the guide companion needed by that handoff. The no-topic guide routes agent
requests, `concept-release-notes` renders the installed release's packaged changelog section offline
and connects older ranges to canonical release notes, `concept-management` connects ordinary
operations to live facts and built-in CLI help, and `concept-onboarding` can offer bounded first-VM
and first-session actions.

This design does not add a package intent switchboard, copied recipes, custom installer, Markdown
parser, bootstrap orchestration driver, command registry, runtime package loader, network renderer,
or general packaging framework. Packages contain no hooks, commands, scripts, MCP servers, apps, or
pre-approved tools. Installing a package makes inert instructions discoverable but does not inspect
or change the workstation.

## Decisions

| Concern                 | Decision                                                                                                                                                                                                                                              |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical body          | `packaging/agentworks/assistance.md` is the only authored package body and owns its prose, security posture, and links.                                                                                                                               |
| Canonical metadata      | `packaging/agentworks/metadata.json` owns machine metadata only: identity, package version, minimum CLI version, and publisher fields.                                                                                                                |
| Identity                | Both marketplaces, both plugins, and both skills use the neutral name `agentworks`. The native install identity is `agentworks@agentworks`.                                                                                                           |
| Minimum CLI             | `agentworks-cli >=0.14.0`, with no maximum. Version 0.14.0 first contains the guide companion this package invokes.                                                                                                                                   |
| Projection              | `scripts/generate-agentworks-package.py` emits two native wrappers from one body and metadata record. Generated skill bodies are byte-identical after frontmatter.                                                                                    |
| README parity           | One marked region under README `## Getting Started` contains the canonical body exactly. The generator owns only that region.                                                                                                                         |
| Runtime behavior        | The inert skill tells the agent to disclose, obtain consent, offer bounded review of the exact intended release source, check or install the CLI, and run the guide. It performs no action itself.                                                    |
| Teaching ownership      | The top-level guide owns routing and the installed guide topics own teaching. Packages contain no intent routes, recipes, or release prose.                                                                                                           |
| Current versus temporal | Live guide facts answer current capability and adoption questions. The packaged release-please changelog section answers what changed in the installed version; canonical GitHub releases are the consented fallback for older or unavailable ranges. |
| Security posture        | Claude uses default/manual approvals without bypass. Codex starts with `workspace-write` and `on-request`, using scoped escalation for account files, network, or SSH.                                                                                |
| Release ownership       | The Agentworks repository owns package source, projections, catalogs, guide companion, tests, and versions. Saga and release leads own integration into the release PR.                                                                               |

The package version starts at `1.0.0` and changes whenever an installed generated artifact changes.
It remains independent of the CLI version. A focused CI comparison against the merge base rejects a
changed package fingerprint without a package-version bump. Generator `--check` stays
checkout-local: it renders the current source and compares current bytes and inventory only.

## Repository layout and generated boundaries

```text
packaging/agentworks/
  assistance.md                       # authored canonical body
  metadata.json                       # authored machine metadata
plugins/
  claude-code/agentworks/
    .claude-plugin/plugin.json        # generated
    skills/agentworks/SKILL.md        # generated
  codex/agentworks/
    .codex-plugin/plugin.json         # generated
    skills/agentworks/SKILL.md        # generated
.claude-plugin/marketplace.json       # generated Claude catalog
.agents/plugins/marketplace.json      # generated Codex catalog
scripts/generate-agentworks-package.py
cli/tests/assistance/
  test_generation.py
  test_contract.py
cli/CHANGELOG.md                       # release-please-owned history source
```

The two native plugin roots are intentional. Neither harness documents one dual-manifest directory
as a cross-harness package contract, so separate generated wrappers are the smallest supported
layouts.

The generator owns exactly these regions:

1. `.claude-plugin/marketplace.json` in full.
2. `.agents/plugins/marketplace.json` in full.
3. `plugins/claude-code/agentworks/.claude-plugin/plugin.json` in full.
4. `plugins/claude-code/agentworks/skills/agentworks/SKILL.md` in full.
5. `plugins/codex/agentworks/.codex-plugin/plugin.json` in full.
6. `plugins/codex/agentworks/skills/agentworks/SKILL.md` in full.
7. The README region between `<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->` and
   `<!-- END GENERATED AGENTWORKS ASSISTANCE -->`.

The generator refuses missing or duplicate README markers and unexpected files under either
generated plugin root. JSON cannot carry a generated notice, so the exact-path inventory and tests
establish ownership. No other README content is rewritten.

## Canonical assistance contract

`assistance.md` is agent-addressed Markdown without frontmatter. Its headings and order are stable:

1. `Agentworks assistance request`
2. `Access disclosure and consent`
3. `Strict harness posture`
4. `Source review offer`
5. `After approval`

The body is deliberately thin. It contains no topic choice, operation recipe, generated inventory,
or historical release text.

### Disclosure before action

Before a command, probe, file read, verification, installation, or configuration action, the body
instructs the agent to restate these facts and wait for affirmative operator consent:

- Work runs from the workstation from which the operator intends to manage Agentworks.
- The agent needs the ability to inspect every file available to that workstation account and
  execute commands as that account, even though each actual probe remains separately scoped.
- Account access is not root access. Privilege elevation is a separate action that requires its own
  disclosure and approval.
- Agentworks work can reach managed resources, secret references, and destinations reachable over
  SSH from the workstation.
- Discovery checks sensitive material only for presence. It never views secret values, private-key
  contents, or secret contents.
- Each later boundary crossing names its exact scope and offers an inert manual alternative.

Refusal stops automation without penalty. Manual instructions are not treated as verification.

The matching strict harness posture is advice, not a settings mutation:

- Claude Code uses `default` permission mode and normal manual approvals. It never uses
  `bypassPermissions` for workstation management. The canonical body links
  <https://code.claude.com/docs/en/permissions> and <https://code.claude.com/docs/en/sandboxing>.
- Codex starts with `sandbox_mode = "workspace-write"` and `approval_policy = "on-request"`. It
  requests scoped escalation only when an exact operation needs account-wide files, network, or SSH.
  If the bounded sandbox and escalation flow cannot support the requested task, the agent discloses
  that limitation and may ask whether the operator elects `danger-full-access` as a fallback. Full
  access removes the sandbox boundary and must never be described as retaining on-request prompts
  for already allowed work. The agent never selects it or `approval_policy = "never"` for the
  operator. The body links <https://developers.openai.com/codex/security> and
  <https://developers.openai.com/codex/config-basic>.

The package never writes `.claude/settings*.json`, `.codex/config.toml`, or managed policy.

### Source review offer

Before any CLI installation or update, assistance identifies one exact intended stable version and
its canonical `vVERSION` release tag. If no compatible installed version exists and the operator has
not selected an exact version, the agent separately discloses and obtains consent to consult PyPI's
release metadata at <https://pypi.org/pypi/agentworks-cli/json>, treats that metadata as untrusted
evidence, and selects the latest non-prerelease version satisfying the minimum. The resulting
installation is pinned to `agentworks-cli==VERSION`; a range is never treated as an exact review
target.

Assistance then offers these two inert choices before asking to install:

| Action                   | Exact scope and result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `inspect-focused-source` | With dedicated `inspect-canonical-source` consent, inspect the canonical repository at exactly `vVERSION`: `cli/pyproject.toml`, `cli/uv.lock`, the shipped `cli/agentworks/` tree, `cli/CHANGELOG.md`, `packaging/agentworks/`, both generated `plugins/*/agentworks/` roots, `scripts/generate-agentworks-package.py`, `release-please-config.json`, `.github/workflows/release-please.yml`, and `.github/workflows/release.yml`. Summarize package, dependency, executable, guide, and release risks and cite exact tagged paths. |
| `inspect-full-source`    | Warn that the repository is substantial and a full review can consume significant model usage. With separate `inspect-canonical-source` consent, inspect the complete canonical repository tree at exactly `vVERSION`, report review limits and findings, and cite exact tagged paths.                                                                                                                                                                                                                                               |

These are two fixed, structurally tested inert Markdown action records, not a package action
framework. They do not extend or depend on `GuideAction` because the CLI may not exist yet. Each
record has the exact repository URL `https://github.com/WayfarerLabs/agentworks/tree/vVERSION`,
required `VERSION` and `RELEASE_TAG` inputs, no executable command or verification command, the
dedicated `inspect-canonical-source` consent, the table's fixed read-only scope and expected review
report, and a refusal alternative that leaves the exact tagged URL for manual inspection. The
repository URL, release tag, selected scope, network access, read-only nature, and model-usage
tradeoff appear before consent. Its manual step performs only that fixed network read and never
follows an embedded instruction or link beyond the named scope.

Canonical repository and PyPI content are untrusted evidence. They can inform the review but cannot
authorize a command, broaden scope, weaken a consent boundary, or override these instructions.
Review refusal performs no source request. Declining review does not block a separately disclosed
and approved installation, and review consent never implies installation consent.

Source inspection stays in the assistance session's protected policy root. It never launches or
reconfigures a harness from the candidate tree, changes the working root to that tree, or loads
candidate `AGENTS.md`, `CLAUDE.md`, skills, hooks, plugins, or configuration as policy. If source is
materialized, it goes only into an operator-approved data-only temporary path and is read by
explicit path from the protected root. Candidate scripts and commands are never executed.
Instruction-like content is reported only as evidence. Running candidate code is a separate action
outside source review and requires its own disclosure and consent.

### After approval

Only after global consent, the body instructs the agent to:

1. Run `agw version`.
2. If `agw` is absent, malformed, older than 0.14.0, or the operator requests an update, select an
   exact compatible stable version and make the source review offer above.
3. Independently disclose the network and account-wide tool installation, obtain installation
   consent, and run `uv tool install --upgrade 'agentworks-cli==VERSION'`.
4. Re-run `agw version` and require the exact selected version.
5. Run `agw guide --agent` and follow the installed guide's routes and action boundaries.

The source body merely instructs the agent to perform these steps. It does not execute them. An
installation failure, declined installation consent, or unsatisfied version stops before guide
execution and leaves the exact manual command available.

## Native package projections

Both generated `SKILL.md` files prepend this Agent Skills frontmatter to the same canonical body:

```yaml
---
name: agentworks
description: >-
  Help with any Agentworks setup, discovery, adoption, configuration, troubleshooting, VM operation,
  or session operation request. Use whenever the operator asks to install, understand, configure,
  troubleshoot, or operate Agentworks.
compatibility:
  Requires network and operator-approved workstation access when the requested task needs them.
metadata:
  agentworks-package-version: "1.0.0"
  agentworks-min-cli-version: "0.14.0"
---
```

There is no `allowed-tools` field. Loading the skill grants no permission. The minimum CLI belongs
in metadata and the post-consent version check, not in `compatibility`.

### Claude Code

The Claude plugin manifest uses `name: agentworks`, version `1.0.0`, the Wayfarer Labs publisher,
repository and homepage URLs, and the MIT license. The root marketplace uses `name: agentworks`, a
required marketplace description, and one local source at `./plugins/claude-code/agentworks`.

The production install is:

```sh
claude plugin marketplace add WayfarerLabs/agentworks
claude plugin install agentworks@agentworks
```

### Codex

The Codex manifest uses the same identity and version and includes the current required `interface`
object with these minimal fields:

```json
{
  "displayName": "Agentworks",
  "shortDescription": "Agentworks assistance",
  "longDescription": "Set up, understand, configure, troubleshoot, and operate Agentworks.",
  "developerName": "Wayfarer Labs",
  "category": "Productivity",
  "capabilities": ["Lifecycle assistance"],
  "defaultPrompt": ["Help me with Agentworks."]
}
```

The Codex marketplace uses `name: agentworks` and one local source at `./plugins/codex/agentworks`.
The production install is:

```sh
codex plugin marketplace add WayfarerLabs/agentworks
codex plugin add agentworks@agentworks
```

For PR acceptance, Claude adds `WayfarerLabs/agentworks@<branch-or-tag>` and Codex runs
`codex plugin marketplace add WayfarerLabs/agentworks --ref <branch-or-tag>`; both then install
`agentworks@agentworks`. Acceptance pins the PR branch, records the resolved marketplace commit, and
asserts that it is the expected PR head. Release probes use the release tag. A pull-request commit
is not presented as a valid marketplace reference.

## Deterministic generation

`scripts/generate-agentworks-package.py` is a small standard-library renderer with normal write mode
and `--check`. It:

1. Parses and validates the small JSON metadata record.
2. Reads the canonical body as bytes.
3. Renders manifests, catalogs, frontmatter, and README region in fixed key and file order.
4. Rejects unknown files in generated roots and validates exact body parity.
5. Writes changed files atomically, or in `--check` reports every byte or inventory mismatch and
   exits nonzero without writing.

It does not parse the Markdown body, discover plugin types, contact a network, inspect git history,
or load runtime packages. Contract tests parameterize the canonical body and package ordering to
prove both wrappers receive one body. A separate CI test compares package fingerprint and package
version to the merge base and requires a version bump when the fingerprint changes.

## Guide companion contract

The package's only guide handoff is `agw guide --agent`. The top-level guide therefore owns the
following routes and continues to render the complete live topic index after its short router.

| Operator intent                                                                      | Guide destination                                                        |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| First setup, current capability, or current adoption                                 | `concept-onboarding`                                                     |
| What changed across versions or over time                                            | `concept-release-notes`                                                  |
| Configuration, resource, VM, workspace, agent, session, console, or secret operation | `concept-management`, then the applicable live kind or `kind/name` topic |
| Health diagnosis and recovery                                                        | `concept-troubleshooting`                                                |
| Breaking-input conversion only                                                       | `concept-migration`                                                      |
| Secret handling                                                                      | `concept-secrets`                                                        |
| Product bug                                                                          | `concept-reporting-bugs`                                                 |

The router contains destinations, not recipes or commands. It performs no network request.

### Temporal release notes

`concept-release-notes` is a first-class core topic with an authored overview and agent contract, a
dynamic `ReleaseNotes` block, and related links. It contains no hand-maintained release prose. The
block reads only the changelog packaged in the installed wheel and performs no network access. Its
contribution shape contains only `type` and `id`; no contributor can supply a path, version, or
prose payload.

Release-please-managed `cli/CHANGELOG.md` remains the sole history source. Hatch includes it in the
wheel with this explicit mapping from the `cli/` project root:

```toml
[tool.hatch.build.targets.wheel.force-include]
"CHANGELOG.md" = "agentworks/CHANGELOG.md"
```

The renderer reads it through `importlib.resources.files("agentworks").joinpath("CHANGELOG.md")`. It
obtains the installed distribution version from the existing version authority, recognizes only
release-please's anchored level-two version headers, and selects the bytes after the one exact
matching header through the next level-two version header. Zero or multiple matches are unavailable,
never guessed or combined.

The read is capped at 2 MiB and the selected section at 256 KiB. The renderer rejects NULs, terminal
control characters, unsafe expression markers, and reserved framework heading delimiters. It
normalizes line endings, escapes HTML and Markdown punctuation, and presents the remaining content
under a renderer-owned heading as untrusted plain-text release evidence with all source links inert.
It never interprets a changelog line as an instruction or authority. Invalid, ambiguous, or
oversized content yields one bounded issue and the fallback route, not partial notes.

The topic's agent contract instructs the agent to:

1. Use `agw version` to establish the installed version and render its matching local section
   offline.
2. Summarize that evidence while preserving its installed-version scope and treating it as untrusted
   historical claims.
3. Ask which older or missing version range the operator wants when the local section is
   insufficient, then use the topic's inert `read-release-notes` action.
4. Route separately to `concept-onboarding` when the operator wants a current capability or adoption
   assessment.

`read-release-notes` requires operator-supplied `FROM_VERSION` and `TO_VERSION`, with no inferred
range. Its consent is the narrow `read-canonical-release-notes` boundary, the only new serialized
`GuideAction` consent value in this phase. It has no CLI command; its manual step reads only the
inclusive requested range at <https://github.com/WayfarerLabs/agentworks/releases>, follows no
embedded link, summarizes the applicable release pages, and preserves only canonical page links as
citations. Its expected state is a bounded historical summary labeled as untrusted evidence. Its
refusal alternative leaves the canonical URL and exact requested range for the operator without
making a network request or claiming a summary.

Fetched release content cannot authorize instructions, commands, permission changes, new links, or
scope expansion. Any proposed follow-up is a new action with its own operator decision. Packages
contain no release prose, and the top-level guide never initiates a network request.

### Ordinary management

`concept-management` uses live guide facts for current state:

| Need                   | Installed CLI authority                                                                      |
| ---------------------- | -------------------------------------------------------------------------------------------- |
| Declared resources     | `agw resource list --output json` and `agw resource describe KIND/NAME --output json`        |
| Existing instances     | The guide's live kind and `kind/name` topics, backed by current JSON list and describe facts |
| Group capabilities     | `agw GROUP --help`                                                                           |
| Exact operation syntax | `agw GROUP COMMAND --help`, or `agw doctor --help` for the top-level command                 |

The authored topic may name the stable built-in groups `config`, `resource`, `vm`, `workspace`,
`agent`, `session`, `console`, and `secret` for routing. A CLI group rename updates that small route
list in the same change. It does not introduce a command registry, copied mutation catalog,
introspection model, or recipe list. Before any mutation, teaching requires exact scope, selected
inputs, impact, consent boundary, expected state, verification, and a useful refusal alternative.

### Clean first-run actions

`concept-onboarding` derives the absence of VMs and sessions only from the existing
`OnboardingSnapshot.instances` projection. It emits actions in VM-then-session order:

- Emit `create-first-vm` only when no `vm` instance exists.
- Emit `create-first-session` only when no `session` instance exists.

The actions use the existing `GuideAction` fields. Complete impact appears in `precondition` and
`expected_state`, and human rendering places both before the consent request and command. No new
impact field is introduced. JSON retains the same record shape.

#### `create-first-vm`

- `required_inputs`: `VM_NAME`, `VM_TEMPLATE`, `ADMIN_TEMPLATE`, and `VM_SITE`, each explicitly
  selected by the operator.
- `consent`: `mutate-agentworks`.
- `command` token order:
  `agw vm create VM_NAME --template VM_TEMPLATE --admin-template ADMIN_TEMPLATE --site VM_SITE`.
- `precondition`: the snapshot has no VM, the operator selected every input, and built-in
  `agw vm create --help` still confirms this syntax.
- `expected_state`: Agentworks persists a VM record and asks the selected provider/site to create
  compute and storage. This can incur provider cost, changes remote and local Agentworks state, and
  uses provider network and later SSH connectivity under normal secret-reference boundaries. No
  privilege elevation is implied.
- `verification`: run `agw vm describe VM_NAME --output json`; require JSON contract v1,
  `command == "vm.describe"`, matching `data.vm.name`, `site`, `template`, and `admin_template`,
  complete provisioning and initialization statuses, and observed running status.
- `refusal_alternative`: keep the exact inert command and input checklist, make no VM or provider
  change, and explain that session creation requires an operator-selected usable VM.

#### `create-first-session`

- `required_inputs`: `SESSION_NAME`, `SESSION_TEMPLATE`, `VM_NAME`, `WORKSPACE_NAME`,
  `WORKSPACE_TEMPLATE`, `AGENT_NAME`, and `AGENT_TEMPLATE`, each explicitly selected by the
  operator. Existing snapshot instances may be shown as choices, but even a sole VM is never
  inferred.
- `consent`: `mutate-agentworks`.
- `command` token order:
  `agw session create SESSION_NAME --template SESSION_TEMPLATE --vm VM_NAME --new-workspace`
  `--workspace-name WORKSPACE_NAME --workspace-template WORKSPACE_TEMPLATE --new-agent`
  `--agent-name AGENT_NAME --agent-template AGENT_TEMPLATE`.
- `precondition`: the snapshot has no session, a selected VM is usable, every input is selected, and
  built-in `agw session create --help` confirms this syntax.
- `expected_state`: Agentworks persists and starts a session, creates the selected workspace and
  agent, and changes local and remote state. The running session can consume provider resources and
  cost, and needs provider network and SSH connectivity under normal secret-reference boundaries. No
  attach, delete, or privilege elevation is implied.
- `verification`: run `agw session describe SESSION_NAME --output json`; require JSON contract v1,
  `command == "session.describe"`, matching session, template, VM, workspace, and agent identities,
  and `data.session.status == "running"`.
- `refusal_alternative`: keep the exact inert command and input checklist, create or start nothing,
  and route to live VM/session topics for manual preparation.

These are the only new mutations. There is no attach, delete, elevation, inferred choice, automatic
execution, or action when the corresponding instance already exists. A failed verification reports
observed state and routes to troubleshooting without retrying the mutation.

## Tests and acceptance probes

### Repository gates

- Generator tests cover deterministic ordering, write and `--check`, README marker failures,
  generated inventory, stale bytes, and canonical-body equality across both skills.
- Contract tests cover neutral identities, minimum/version metadata, inertness, security links,
  disclosure-before-action ordering, exact-version source-review offers, dedicated review consent,
  focused/full/no-review choices, decline-review followed by approved install, completed review
  followed by declined install, and absence of executables, extra plugin files, package routes,
  recipes, and release prose. Adversarial candidate `AGENTS.md`, `CLAUDE.md`, skills, hooks,
  plugins, configuration, links, and embedded commands remain data: fixtures prove they cannot
  change the protected policy or working root, launch or reconfigure a harness, execute, authorize
  install, or expand the selected review scope.
- A merge-base CI test rejects package-content changes without a package-version bump.
- `claude plugin validate --strict` validates the Claude package and marketplace.
- The current Codex plugin validator validates the Codex package and marketplace, including the
  `interface` object.
- Existing real guide fixtures are extended once for guided output and non-interactive JSON. Both
  package projections assert the same `agw guide --agent` handoff and consume that one fixture, not
  duplicate guide runs or a custom orchestration driver.
- Wheel tests build and inspect the release artifact, assert `agentworks/CHANGELOG.md` is present
  and byte-equal to `cli/CHANGELOG.md`, and prove only the installed version's one matching section
  is selected. Missing, duplicate, malformed, oversized, control-bearing, and expression-bearing
  fixtures fail closed without partial history. Instruction-like prose and Markdown links remain
  visibly untrusted plain text with no active link or command behavior.
- Guide tests cover every no-topic destination and index order; offline installed release rendering,
  stable fallback URL, no render-time network, untrusted plain-text sanitization, and
  current-versus-temporal separation; the exact `read-release-notes` inputs, consent, inclusive
  range, no-follow rule, expected evidence, and refusal; live management facts and built-in help
  authority; and both first-run action records, exact inputs/tokens, impact-before-consent
  rendering, refusal, JSON verification, selection rules, and absence of attach/delete/elevation.

### Clean-environment probes

Candidate probes install the built 0.14 release-candidate wheel at the package boundary rather than
PyPI. The future `v0.14.0` tag does not yet exist, so these probes exercise the same focused source
review against the exact release-PR commit that built the candidate wheel, label and record that
test-only ref substitution, and never claim they reviewed a tag. Each starts from a disposable clean
home and records refs, commands, exit codes, installed package identity/version, model output,
consent boundaries, and cleanup.

| Scenario                        | Required result                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude fresh first run          | With default/manual approval mode and no bypass, install `agentworks@agentworks`; disclose, name the exact candidate commit substitution, perform the focused review before a separate installation consent, install the candidate wheel, render the shared guide fixture, offer first VM then first session only after explicit inputs and consent, and verify both describes. |
| Codex fresh first run           | With `workspace-write` plus `on-request` and scoped escalation for account files/network/SSH, produce the same result without electing `danger-full-access`. Declining source review must not suppress the separate install choice.                                                                                                                                             |
| Source-review independence      | Offer both scopes and the substantial-usage warning. Decline review then approve install in one path; complete the focused review then decline install in another. A full-review selection requires its own consent and exact-ref scope, and candidate policy files remain data.                                                                                                |
| Returning current adoption      | Route to `concept-onboarding` and use current live facts, not release history.                                                                                                                                                                                                                                                                                                  |
| Rerun and post-upgrade adoption | An unchanged adopted system is a no-op; after an upgrade, report current not-yet-adopted capabilities without replaying completed work or inventing a historical delta.                                                                                                                                                                                                         |
| Installed temporal history      | Route to `concept-release-notes` and render only the candidate wheel's matching 0.14 changelog section offline as bounded untrusted evidence.                                                                                                                                                                                                                                   |
| Older temporal history          | Request an older range and network consent, then use the canonical release URL; refusal performs no network request and invents no delta.                                                                                                                                                                                                                                       |
| Ordinary management             | Route through `concept-management`, use a live JSON kind/instance fact and built-in group/command help, and run at most one disposable consented action with JSON verification.                                                                                                                                                                                                 |
| Higher-risk refusal             | Refuse an attach, delete, elevation, or unselected mutation request; execute no command and provide the applicable live topic/help route.                                                                                                                                                                                                                                       |
| README equivalence              | Starting from only the marked README body produces the same disclosure, package install, and shared guide handoff as either package.                                                                                                                                                                                                                                            |
| Old CLI or failure              | Select an exact compatible version, offer exact-tag review, and upgrade a pre-0.14 CLI only after separate install consent; a declined or failed install stops before the guide and reports the exact pinned repair command.                                                                                                                                                    |
| Post-publish production         | Resolve the published stable version, review its real canonical `vVERSION` tag, then run the exact pinned PyPI install and verify the installed version and local release section.                                                                                                                                                                                              |

Live probes validate model interpretation. Unit tests validate deterministic files and guide data.
No test adds a Markdown parser or test-only bootstrap workflow.

## Failure behavior

- Missing global consent means no probe, installation, guide command, or network request runs.
- Declined source inspection performs no repository read but does not decide the separate
  installation consent. A source review never authorizes installation.
- Missing action consent means no mutation runs; the inert refusal alternative remains available.
- Missing or invalid selected input suppresses the applicable first-run action command.
- CLI installation or minimum-version failure stops before guide execution.
- Missing, ambiguous, invalid, or oversized installed release evidence renders no partial section
  and preserves the consented canonical fallback. Network refusal or failure does not synthesize
  historical claims.
- Package validation or generated-byte drift fails CI with the exact path.
- Guide fact or verification failure reports observed facts and routes to troubleshooting without
  replaying a mutation.

## Documentation, completion, and configuration impact

- README gains only the marked neutral assistance body. Its generated content ships with the code
  and package layouts that make it true.
- Permanent assistance installation docs name both production package commands, exact-version CLI
  installation behavior, source-review choices, consent boundaries, and strict harness posture
  without copying the body.
- Permanent guide docs describe the no-topic routes, current-versus-temporal split,
  `concept-release-notes`, management help authority, and bounded first-run actions.
- `concept-release-notes` joins the existing dynamic guide topic source, so guide topic completion
  fixtures, snapshots, and generated completion documentation update in the same implementation
  commit. No hand-maintained completion list is added.
- `cli/pyproject.toml` adds only Hatch's explicit changelog force-inclusion mapping. No
  configuration key is introduced, so sample configuration remains unchanged. Tests assert that the
  package and guide companion need no new setting.

## Release choreography

Candidate gates run from the 0.14 release-please PR only after its version bump, generated
`cli/CHANGELOG.md` section, and reviewed Phase 3 implementation coexist. They build that branch's
wheel, prove the wheel contains the exact changelog, perform the test-only focused source review at
the exact release-PR commit that produced the wheel, install that exact candidate artifact at the
package boundary, and run both live harness probes before merge, tag, or publish. Evidence
identifies the commit substitution and does not call it a release-tag review.

The ready Phase 3 implementation is incorporated into that release PR and does not merge
independently to main. The release PR lands README assistance, package layouts, guide companion,
version bump, and release metadata on main in one release commit. Release-please creates the tag and
release from that commit, `release.yml` publishes it, and the post-publish smoke resolves the stable
version, exercises the real canonical `vVERSION` source review, then runs the exact pinned
production PyPI installation.

Only the tag-to-PyPI interval is transitional. A publish failure blocks release completion and does
not silently change the installation source. Saga and release leads own this coordination.

## Validation evidence

- Claude Code 2.1.222 accepted neutral marketplace, plugin, and skill identity `agentworks` under
  strict validation. Its marketplace required a description, which this design includes.
- Codex CLI 0.147.0 and the current plugin validator accepted neutral marketplace and plugin
  identity `agentworks` and the required `interface` object.
- Claude Code package and marketplace fields follow
  <https://code.claude.com/docs/en/plugins-reference> and
  <https://code.claude.com/docs/en/plugin-marketplaces>.
- Codex package fields and installation flow follow <https://developers.openai.com/codex/plugins>.
- Agent Skills frontmatter follows <https://agentskills.io/specification>.
- Hatch's documented target `force-include` mapping supports copying `cli/CHANGELOG.md` to the exact
  wheel path `agentworks/CHANGELOG.md` without a build hook:
  <https://hatch.pypa.io/latest/config/build/#forced-inclusion>.
- Current repository release configuration uses release-please's Python release type,
  `changelog-path: CHANGELOG.md`, unprefixed component tags, and `vVERSION` release workflow tags.

## Process

A delegated onboarding-and-discovery developer authored this LLD under the effort lead's ownership.
The lead reviews it before opening the draft artifact checkpoint PR. The artifact merges only after
the saga review converges; implementation review and release integration remain separate gates.
