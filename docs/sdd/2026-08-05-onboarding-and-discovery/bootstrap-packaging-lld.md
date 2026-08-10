# Phase 3 Agentworks Assistance Packaging and Guide Companion LLD

- Status: Revised draft for the Phase 3 design gate within the full-feature PR
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- HLA: `docs/sdd/2026-08-05-onboarding-and-discovery/hla.md`
- Plan: `docs/sdd/2026-08-05-onboarding-and-discovery/plan.md`, Phase 3
- Verified against: Claude Code and Codex packaging tools and official documentation on 2026-08-10

## Purpose and limits

Phase 3 makes Agentworks assistance available before and after installation through native Claude
Code and Codex packages and the repository README. One inert body discloses the workstation access
Agentworks work can require, establishes a durable authorization envelope for the operator's goal,
offers review of the exact intended release source, installs or updates a compatible CLI when
needed, and asks `agw guide --agent` for current context. The Agentworks assistant agent reads that
context and decides what to propose next.

The same phase completes the guide companion needed by that handoff. The no-topic guide presents an
intent-to-topic map and live index; it does not classify or route the current request.
`concept-release-notes` renders installed and normalized historical changelog sections offline and
connects only locally missing history to canonical release notes, `concept-management` connects
ordinary operations to live facts and built-in CLI help, and `concept-onboarding` can bootstrap
configuration and offer bounded first-VM and first-session actions. The Agentworks assistant agent
chooses among those contexts and actions.

This design does not add a package intent switchboard, copied recipes, custom installer, Markdown
parser, bootstrap orchestration driver, command registry, runtime package loader, network renderer,
or general packaging framework. Packages contain no hooks, commands, scripts, MCP servers, apps, or
pre-approved tools. Installing a package makes inert instructions discoverable but does not inspect
or change the workstation.

Terminology follows the FRD. **Agentworks assistant agent** means any external agent that can accept
the canonical copy/paste prompt, invoke and interpret the Agentworks CLI, and use the
operator-approved workstation access appropriate to the requested task. Claude Code and Codex are
the two native package targets, not limits on the role. **Agentworks-managed agent** means an agent
resource in the system being operated. Literal `--agent`, `AgentContract`, and CLI `agent` names
retain their established spellings; prose uses the full role name whenever ambiguity is possible.

## Decisions

| Concern                 | Decision                                                                                                                                                                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical body          | `packaging/agentworks/assistance.md` is the universal copy/paste prompt and only authored package body. It owns its prose, security posture, and links.                                                                                                                                           |
| Canonical metadata      | `packaging/agentworks/metadata.json` owns machine metadata only: identity, package version, minimum CLI version, publisher fields, display name, `skillDescription`, and interface descriptions. `skillDescription` alone supplies both generated skill-frontmatter descriptions.                 |
| Identity                | Both marketplaces, both plugins, and both skills use the neutral name `agentworks`. The native install identity is `agentworks@agentworks`.                                                                                                                                                       |
| Minimum CLI             | `agentworks-cli >=0.14.0`, with no maximum. Version 0.14.0 first contains the guide companion this package invokes.                                                                                                                                                                               |
| Projection              | `scripts/generate-agentworks-package.py` emits two native wrappers from one body and metadata record. Generated skill bodies are byte-identical after frontmatter.                                                                                                                                |
| README parity           | One marked region under README `## Getting Started` contains the compact, table-free canonical body exactly inside a collision-proof generated fence. The generator owns only that region; detailed tables below describe the design and tests rather than text copied into the prompt.           |
| Runtime behavior        | The inert canonical body, whether pasted directly or loaded as a native skill, tells the Agentworks assistant agent to disclose once, establish a working authorization envelope, offer bounded source review, check or install the CLI, and request guide context. It performs no action itself. |
| Teaching ownership      | The top-level guide owns the intent-to-topic map and the installed guide topics own teaching. Native assistance packages contain no intent map, recipes, or release prose.                                                                                                                        |
| Current versus temporal | Live guide facts answer current capability and adoption questions. The packaged normalized release-please changelog answers exact installed and historical versions offline; canonical GitHub releases are the authorized fallback only for locally missing history.                              |
| Security posture        | Every Agentworks assistant agent uses the strictest practical documented approval, visibility, and sandbox posture that permits the task. Claude and Codex receive the pinned conditional native profiles below.                                                                                  |
| Release ownership       | PR #480 owns the complete feature and merges normally to `main`; release-please then regenerates its separate release PR from that mainline feature. Release leads own candidate gates, release-PR merge, tag, and publish.                                                                       |

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
establish ownership. For the README projection, it scans the canonical body for the longest
contiguous backtick run and uses an outer fence of `max(3, longest + 1)` backticks. The opener and
closer match, and the bytes between their surrounding newlines are the canonical body unchanged.
Ordinary fenced examples therefore remain valid without a delimiter collision. No other README
content is rewritten.

## Canonical assistance contract

`assistance.md` is Markdown addressed to the Agentworks assistant agent, without frontmatter. Its
headings and order are stable:

1. `Agentworks assistance request`
2. `Startup disclosure and authorization`
3. `Strict harness posture`
4. `Source review offer`
5. `Working within the authorized scope`

The body is deliberately thin and table-free. Each stable section is concise; the detailed tables
and enumerations in this LLD specify behavior and acceptance rather than literal operator-facing
copy. The body contains no topic choice, operation recipe, generated inventory, or historical
release text.

`metadata.json.skillDescription` contains the one generated skill description:

```text
Help with any Agentworks setup, discovery, adoption, configuration, troubleshooting, VM operation,
or session operation request. Use whenever the operator asks to install, understand, configure,
troubleshoot, or operate Agentworks.
```

The generator copies that field byte-for-byte into both `SKILL.md` frontmatter documents. Codex
`interface.shortDescription` and `interface.longDescription` remain separate marketplace display
fields; neither is allowed to become a third skill-description spelling.

The opening calls its addressee the **Agentworks assistant agent** and states that this is the
external helper using Agentworks with the operator, not an Agentworks-managed agent resource. That
distinction appears before any use of the literal `agw agent` resource group or `--agent` guide
flag.

### Startup disclosure and authorization envelope

Before the first command, probe, file read, verification, installation, or configuration action in
an assistance session, the body gives one concise disclosure and states its interpretation of the
working authorization envelope. An explicit operator instruction authorizes reasonably necessary
work inside that disclosed envelope; the Agentworks assistant agent does not ask for a redundant
"yes." If the initial request is exploratory or materially ambiguous, it asks once to establish the
envelope. It does not restate the disclosure or request approval before every later step. The
operator-facing form is a compact paragraph, not a recital of every possible risk. It covers these
facts:

- Work runs from the workstation from which the operator intends to manage Agentworks.
- The Agentworks assistant agent can inspect files available to that workstation account and execute
  commands as that account when reasonably needed for the requested goal.
- Account access is not root access. Privilege elevation is a distinct authorization boundary that
  must be explicitly covered, either in the operator's instruction or by a later decision.
- Agentworks work can reach managed resources, secret references, and destinations reachable over
  SSH from the workstation.
- Discovery checks sensitive material only for presence. It never views secret values, private-key
  contents, or secret contents.
- The operator's instruction and this disclosure authorize ordinary necessary work inside the stated
  goal, targets, access classes, and impact. A refusal or narrower instruction limits that envelope.

After the operator establishes that envelope, the Agentworks assistant agent proceeds naturally
through in-scope reads, commands, probes, verification, installation, and mutations. Progress
updates may say what it is doing, but they are not disguised approval prompts. Missing inputs are
ordinary questions, not new consent requests. The Agentworks assistant agent asks again only before
a material expansion that the operator has not explicitly instructed. A clear instruction covering
the expansion is already the operator decision; the assistant briefly states any newly relevant
impact and proceeds without a redundant confirmation. Material expansions include:

- a different workstation, account, environment, or remote target;
- a new access class, including sensitive contents rather than presence-only checks;
- work not reasonably necessary for the stated goal or a materially different mutation;
- privilege elevation, destructive or irreversible work, an unanticipated material cost, or an
  external side effect not already stated;
- ambiguity whose resolution materially changes target, access, impact, or risk; or
- any action when the operator requested confirmation every time.

For example, "set up Agentworks through a working first session" may cover the disclosed CLI
installation, configuration, VM creation, and session creation sequence after the operator selects
the required targets and provider inputs. It does not cover deleting an existing VM, changing
workstation, reading secret values, or privilege elevation. Refusal stops only the refused or
out-of-scope work without penalty. Manual instructions are not treated as verification. The envelope
exists only in the current Agentworks assistant agent interaction; the package and CLI do not
persist it or infer authorization from an earlier session.

The matching strict harness posture is advice, not a settings mutation. For any harness, the body
requires the strictest practical approval, visibility, and sandbox posture that still permits the
operator-approved workstation task. It gives these conditional profiles for the two native package
targets. The startup message summarizes the applicable posture and links its controls; it does not
walk through every setting unless the operator asks or the current posture blocks the requested
work. Harness-enforced tool approvals, escalation prompts, and CLI safety confirmations still apply;
the Agentworks assistant agent does not add a duplicate conversational approval prompt for the same
in-scope operation:

- Claude Code uses `default` permission mode and normal manual approvals. It never uses
  `bypassPermissions` for workstation management. The canonical body links
  <https://code.claude.com/docs/en/permissions> and <https://code.claude.com/docs/en/sandboxing>.
- Codex starts with `sandbox_mode = "workspace-write"` and `approval_policy = "on-request"`. It
  requests scoped escalation only when an exact operation needs account-wide files, network, or SSH.
  If the bounded sandbox and escalation flow cannot support the requested task, the Agentworks
  assistant agent discloses that limitation and may ask whether the operator elects
  `danger-full-access` as a fallback. Full access removes the sandbox boundary and must never be
  described as retaining on-request prompts for already allowed work. The Agentworks assistant agent
  never selects it or `approval_policy = "never"` for the operator. The body links
  <https://developers.openai.com/codex/security> and
  <https://developers.openai.com/codex/config-basic>.

The package never writes `.claude/settings*.json`, `.codex/config.toml`, or managed policy. Another
Agentworks assistant agent applies the same general posture using its own documented controls; the
body does not invent product-specific setting names.

### Source review offer

Before any CLI installation or update, assistance identifies one exact intended stable version and
its canonical `vVERSION` release tag. If no compatible installed version exists and the operator has
not selected an exact version, the Agentworks assistant agent may consult PyPI's release metadata at
<https://pypi.org/pypi/agentworks-cli/json> when ordinary network reads are in the established
envelope; otherwise this is a material access expansion and needs one operator decision. It treats
that metadata as untrusted evidence and selects the latest non-prerelease version satisfying the
minimum. The resulting installation is pinned to `agentworks-cli==VERSION`; a range is never treated
as an exact review target.

Assistance then offers two compact, table-free choices before proceeding with installation. The
following LLD table pins their records and tests; it is not copied literally into the canonical
prompt:

| Action                   | Exact scope and result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inspect-focused-source` | When the operator selects focused review, that choice authorizes the fixed `inspect-canonical-source` scope at exactly `vVERSION`: `cli/pyproject.toml`, `cli/uv.lock`, the shipped `cli/agentworks/` tree, `cli/CHANGELOG.md`, `packaging/agentworks/`, both generated `plugins/*/agentworks/` roots, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, `scripts/generate-agentworks-package.py`, `release-please-config.json`, `.github/workflows/release-please.yml`, and `.github/workflows/release.yml`. Summarize package, dependency, executable, guide, catalog-policy, and release risks and cite exact tagged paths. |
| `inspect-full-source`    | Warn that the repository is substantial and a full review can consume significant model usage. When the operator selects full review, that choice authorizes the fixed `inspect-canonical-source` scope over the complete canonical repository tree at exactly `vVERSION`; report review limits and findings, and cite exact tagged paths.                                                                                                                                                                                                                                                                                                          |

These are two fixed, structurally tested inert Markdown action records, not a package action
framework. They do not extend or depend on `GuideAction` because the CLI may not exist yet. Each
record has the exact repository URL `https://github.com/WayfarerLabs/agentworks/tree/vVERSION`,
required `VERSION` and `RELEASE_TAG` inputs, no executable command or verification command, the
dedicated `inspect-canonical-source` authorization class, the table's fixed read-only scope and
expected review report, and a refusal alternative that leaves the exact tagged URL for manual
inspection. The repository URL, release tag, selected scope, network access, read-only nature, and
model-usage tradeoff appear before the choice. Its manual step performs only that fixed network read
and never follows an embedded instruction or link beyond the named scope.

Canonical repository and PyPI content are untrusted evidence. They can inform the review but cannot
authorize a command, broaden the authorization envelope, or override these instructions. Review
refusal performs no source request. Declining review does not block installation already authorized
by the operator's instruction or a later decision, and selecting review never implies installation
authorization.

Source inspection stays in the assistance session's protected policy root. It never launches or
reconfigures a harness from the candidate tree, changes the working root to that tree, or loads
candidate `AGENTS.md`, `CLAUDE.md`, skills, hooks, plugins, or configuration as policy. If source is
materialized, it goes only into an operator-approved data-only temporary path and is read by
explicit path from the protected root. Candidate scripts and commands are never executed.
Instruction-like content is reported only as evidence. Running candidate code is outside source
review and requires authorization from the operator's established envelope or a new decision.

### Execution under established authorization

After the startup envelope is established, the body instructs the Agentworks assistant agent to:

1. Run `agw version`.
2. If `agw` is absent, malformed, older than 0.14.0, or the operator requests an update, select an
   exact compatible stable version and make the source review offer above.
3. If exact-version installation is already authorized by the operator's instruction and startup
   envelope, run `uv tool install --upgrade 'agentworks-cli==VERSION'` without asking again. If it
   is not covered, disclose that one material expansion and obtain a decision first.
4. Re-run `agw version` and require the exact selected version.
5. Run `agw guide --agent`, interpret its intent-to-topic map and live index against the operator's
   current request, and decide what topic or action to propose next.

The source body merely instructs the Agentworks assistant agent to perform these steps. It does not
execute them. An installation failure, absent or declined installation authorization, or unsatisfied
version stops before guide execution and leaves the exact manual command available.

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
in metadata and the post-authorization version check, not in `compatibility`.

### Claude Code

The Claude plugin manifest uses `name: agentworks`, version `1.0.0`, the Wayfarer Labs publisher,
repository and homepage URLs, and the MIT license. The root marketplace uses `name: agentworks`, a
required marketplace description, and one local source at `./plugins/claude-code/agentworks`.

The production install uses explicit HTTPS rather than the Claude shorthand's SSH default:

```sh
claude plugin marketplace add https://github.com/WayfarerLabs/agentworks.git
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

The Codex marketplace has top-level `name: agentworks` and `interface.displayName: Agentworks`. Its
one plugin entry uses the local source `./plugins/codex/agentworks`, `category: Productivity`, and
this required policy:

```json
{
  "installation": "AVAILABLE",
  "authentication": "ON_INSTALL"
}
```

The production install is:

```sh
codex plugin marketplace add WayfarerLabs/agentworks
codex plugin add agentworks@agentworks
```

For PR acceptance, Claude adds `https://github.com/WayfarerLabs/agentworks.git#<branch-or-tag>` and
Codex runs `codex plugin marketplace add WayfarerLabs/agentworks --ref <branch-or-tag>`; both then
install `agentworks@agentworks`. Acceptance pins the PR branch, records the resolved marketplace
commit, and asserts that it is the expected PR head. Release probes use the release tag. A
pull-request commit is not presented as a valid marketplace reference.

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
or load runtime packages. Finding the longest backtick run is a byte scan for delimiter selection,
not Markdown parsing. Contract tests parameterize the canonical body, embedded backtick-run lengths,
and package ordering to prove both wrappers receive one body and the README remains byte-exact. A
separate CI test compares package fingerprint and package version to the merge base and requires a
version bump when the fingerprint changes.

## Guide companion contract

The package's only guide handoff is `agw guide --agent`. The top-level guide therefore supplies the
following intent-to-topic map and continues to render the complete live topic index after that short
map. It does not inspect or classify the operator's current request. The Agentworks assistant agent
uses this context to decide what topic, proposal, or inert action to present next.

| Operator intent                                                                      | Guide destination                                                        |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| First setup, current capability, or current adoption                                 | `concept-onboarding`                                                     |
| What changed across versions or over time                                            | `concept-release-notes`                                                  |
| Configuration, resource, VM, workspace, agent, session, console, or secret operation | `concept-management`, then the applicable live kind or `kind/name` topic |
| Health diagnosis and recovery                                                        | `concept-troubleshooting`                                                |
| Breaking-input conversion only                                                       | `concept-migration`                                                      |
| Secret handling                                                                      | `concept-secrets`                                                        |
| Product bug                                                                          | `concept-reporting-bugs`                                                 |

The map contains destinations, not recipes or commands. It performs no network request and grants no
authority.

### Temporal release notes

`concept-release-notes` is a first-class core topic with an authored overview and `AgentContract`, a
dynamic `ReleaseNotes` block, and related links. It contains no hand-maintained release prose. The
block reads only the changelog packaged in the installed wheel and performs no network access. Its
contribution shape contains only `type` and `id`; no contributor can supply a path or prose payload.
The base topic selects the installed version. The core guide catalog owns and derives strict dynamic
`concept-release-notes/vMAJOR-MINOR-PATCH` topics from the packaged changelog's validated stable
headers, so one exact historical section can be selected without adding flags to the generic guide
command. For example, changelog version `0.13.0` maps one-to-one to `concept-release-notes/v0-13-0`;
contributors cannot mint that namespace, and the slug contains no dots and satisfies the existing
guide identity grammar.

Release-please-managed `cli/CHANGELOG.md` remains the sole history source. Before Phase 3 packaging,
one reviewed normalization consolidates the duplicate 0.13 sections, preserving the curated manual
material inside the canonical release-please section, and proves exactly one section for each
repository tag from 0.2.0 through 0.13.0. The repository has no v0.1 tag or release, so the
normalization invents no 0.1 section. Release-please remains the only writer after this one-time
cleanup. Hatch includes the complete normalized file in the wheel with this explicit mapping from
the `cli/` project root:

```toml
[tool.hatch.build.targets.wheel.force-include]
"CHANGELOG.md" = "agentworks/CHANGELOG.md"
```

The renderer reads it through `importlib.resources.files("agentworks").joinpath("CHANGELOG.md")`. It
obtains the installed distribution version from the existing version authority for the base topic.
For a dynamic topic, it resolves the slug through the catalog's one-to-one mapping back to its
already validated stable changelog version; it never treats arbitrary hyphenated text as a version.
It recognizes only release-please's anchored level-two version headers and selects the bytes after
the one exact matching header through the next level-two version header. Zero or multiple matches
are unavailable, never guessed or combined. The validated header inventory supplies dynamic topic
names and completion; arbitrary unvalidated strings never become topics.

The read is capped at 2 MiB and the selected section at 256 KiB. The renderer rejects NULs, terminal
control characters, unsafe expression markers, and reserved framework heading delimiters. It
normalizes line endings, escapes HTML and Markdown punctuation, and presents the remaining content
under a renderer-owned heading as untrusted plain-text release evidence with all source links inert.
It never interprets a changelog line as an instruction or authority. Invalid, ambiguous, or
oversized content yields one bounded issue and the fallback route, not partial notes.

The topic's `AgentContract` instructs the Agentworks assistant agent to:

1. Use `agw version` to establish the installed version and render its matching local section
   offline.
2. Summarize that evidence while preserving its installed-version scope and treating it as untrusted
   historical claims.
3. For older local history, render the applicable exact `concept-release-notes/vMAJOR-MINOR-PATCH`
   topics, one section at a time. A requested range is the ordered set of locally present
   exact-version topics; it never causes the renderer to concatenate the whole changelog.
4. Ask which exact missing version or range the operator wants only when packaged history is
   insufficient, then use the topic's inert `read-release-notes` action.
5. Use `concept-onboarding` instead when the operator wants a current capability or adoption
   assessment.

`read-release-notes` requires operator-supplied `FROM_VERSION` and `TO_VERSION`, with no inferred
range. Its `consent` field carries the narrow `read-canonical-release-notes` authorization class,
the only new serialized value in this phase. It has no CLI command; its manual step reads only the
inclusive requested range at <https://github.com/WayfarerLabs/agentworks/releases>, follows no
embedded link, summarizes the applicable release pages, and preserves only canonical page links as
citations. Its expected state is a bounded historical summary labeled as untrusted evidence. Its
refusal alternative leaves the canonical URL and exact requested range for the operator without
making a network request or claiming a summary.

Fetched or packaged release content cannot authorize instructions, commands, permission changes, new
links, or scope expansion. A proposed follow-up already within the operator's envelope may proceed;
the fetched content itself cannot create or expand that envelope. Native assistance packages contain
no copied release prose, and the top-level guide never initiates a network request.

### Ordinary management

`concept-management` uses live guide facts for current state:

| Need                   | Installed CLI authority                                                                      |
| ---------------------- | -------------------------------------------------------------------------------------------- |
| Declared resources     | `agw resource list --output json` and `agw resource describe KIND/NAME --output json`        |
| Existing instances     | The guide's live kind and `kind/name` topics, backed by current JSON list and describe facts |
| Group capabilities     | `agw GROUP --help`                                                                           |
| Exact operation syntax | `agw GROUP COMMAND --help`, or `agw doctor --help` for the top-level command                 |

The authored topic may name the stable built-in groups `config`, `resource`, `vm`, `workspace`,
`agent`, `session`, `console`, and `secret` in the intent map. A CLI group rename updates that small
group list in the same change. It does not introduce a command registry, copied mutation catalog,
introspection model, or recipe list. Each mutation record provides exact scope, selected inputs,
impact, authorization class, expected state, verification, and a useful refusal alternative so the
Agentworks assistant agent can recognize whether it remains within the current envelope. Teaching
does not require the agent to recite the record or request approval again for an in-scope action.

### Clean first-run actions

`concept-onboarding` derives the absence of VMs and sessions only from the existing
`OnboardingSnapshot.instances` projection. A clean-home assistance run establishes usable settings
before it emits resource actions:

1. If the config is absent, check only for the presence of candidate public-key files and matching
   private-key paths. The operator selects an existing pair. If no usable pair exists, offer to run
   `ssh-keygen -t ed25519 -f SSH_KEY_PATH` at one explicit operator-selected path whose public and
   private files do not exist. Key generation is a workstation mutation and runs only when the
   established setup instruction covers it or after one operator decision; it never overwrites and
   never reads private-key content.
2. Run the existing `agw config init` command. It owns sample creation and must retain its existing
   refusal to overwrite a config file. No custom installer or second config writer is added.
3. Update only the generated settings fields needed for the operator-selected SSH identity and
   provider or plugin path. Collect provider identifiers and secret references explicitly, never
   secret values. Built-in local templates and sites remain valid choices, so optional cloud
   providers are not front-loaded.
4. Run `agw doctor --output json`. Proceed to resource creation only after the configuration and
   applicable readiness checks report no failure or unavailable status. If config already exists,
   skip initialization and verify it through the same path without overwriting it.

These steps are authored AgentContract teaching over existing CLI and ordinary file-edit surfaces,
not a bootstrap state machine. Replayable use records the chosen paths and settings explicitly. The
guide persists no setup ledger and reads no workstation state while rendering.

After readiness, it emits actions in VM-then-session order:

- Emit `create-first-vm` only when no `vm` instance exists.
- Emit `create-first-session` only when no `session` instance exists.

The actions use the existing `GuideAction` fields. Complete impact appears in `precondition` and
`expected_state`, and human rendering places both before the authorization class and command. No new
impact field is introduced. JSON retains the same record shape. The `consent` value classifies the
boundary; it does not require a new conversational prompt when the startup setup envelope already
covers the selected target and impact.

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
  `command == "vm.describe"`, matching `data.vm.name`, `site`, and `template`, complete provisioning
  and initialization statuses, and observed running status. The canonical admin-template comparison
  requires `data.vm.admin_template` to be null when `ADMIN_TEMPLATE` is the reserved `default`;
  every non-default selection must equal `ADMIN_TEMPLATE` exactly.
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
  Agentworks-managed agent, and changes local and remote state. The running session can consume
  provider resources and cost, and needs provider network and SSH connectivity under normal
  secret-reference boundaries. No attach, delete, or privilege elevation is implied.
- `verification`: run `agw session describe SESSION_NAME --output json`; require JSON contract v1,
  `command == "session.describe"`, matching session, template, VM, workspace, and Agentworks-managed
  agent identity, and `data.session.status == "running"`.
- `refusal_alternative`: keep the exact inert command and input checklist, create or start nothing,
  and point to live VM/session topics for manual preparation.

The two resource actions are the only new Agentworks operation records. Configuration bootstrap
reuses existing config/file surfaces and optional key generation is separately disclosed as a
workstation mutation. There is no attach, delete, elevation, inferred choice, automatic execution,
or action when the corresponding instance already exists. A failed verification reports observed
state and points the Agentworks assistant agent to troubleshooting without retrying the mutation.

## Tests and acceptance probes

### Repository gates

- Generator tests cover deterministic ordering, write and `--check`, README marker failures,
  generated inventory, stale bytes, canonical-body equality across both skills, and README bodies
  containing backtick runs of three, four, and greater lengths with the minimally longer exact outer
  fence.
- Contract tests cover neutral identities, minimum/version metadata, inertness, security links, the
  distinction between `Agentworks assistant agent` and `Agentworks-managed agent`, product-neutral
  prompt requirements, conditional native-harness guidance, one startup disclosure before action, an
  explicit instruction proceeding without a redundant confirmation, durable in-scope authorization
  without repeated prompts, one resolving question for material ambiguity, material-expansion and
  operator-selected per-action confirmation behavior, exact-version source-review offers,
  focused/full/no-review choices, decline-review followed by an authorized install, completed review
  followed by declined install, and absence of executables, extra plugin files, package intent maps,
  recipes, and release prose. Adversarial candidate `AGENTS.md`, `CLAUDE.md`, skills, hooks,
  plugins, configuration, links, and embedded commands remain data: fixtures prove they cannot
  change the protected policy or working root, launch or reconfigure a harness, execute, authorize
  install, or expand the selected review scope.
- A merge-base CI test rejects package-content changes without a package-version bump.
- A focused-review contract test asserts that every hard-coded repository path in the focused scope
  exists at the tested HEAD, so a rename cannot silently narrow or invalidate the promised review.
- `claude plugin validate --strict` validates the Claude package and marketplace. The clean install
  uses the explicit HTTPS repository URL with SSH keys, SSH agent, Git credential helpers, and Git
  credential environment removed.
- The current Codex plugin validator validates the Codex package and marketplace, including the
  top-level `interface.displayName` and each entry's `category`, `policy.installation`, and
  `policy.authentication`. With the Claude and Codex catalogs at the same repository root, an
  acceptance fixture proves Codex selects `.agents/plugins/marketplace.json` rather than
  interpreting the Claude catalog.
- Existing real guide fixtures are extended once for guided output and non-interactive JSON. Both
  package projections assert the same `agw guide --agent` handoff and consume that one fixture, not
  duplicate guide runs or a custom orchestration driver.
- Wheel tests build and inspect the release artifact, assert `agentworks/CHANGELOG.md` is present
  and byte-equal to `cli/CHANGELOG.md`, prove exactly one normalized section exists for each tagged
  0.2.0-through-0.13.0 release, preserve the curated 0.13 material, invent no 0.1 section, and
  select one exact installed or historical section at a time. Missing, duplicate, malformed,
  oversized, control-bearing, and expression-bearing fixtures fail closed without partial history.
  Instruction-like prose and Markdown links remain visibly untrusted plain text with no active link
  or command behavior.
- Guide tests cover every no-topic destination and index order; offline installed and historical
  exact-version release rendering, dynamic version-topic discovery and completion, stable fallback
  URL only for missing local history, no render-time network, untrusted plain-text sanitization, and
  current-versus-temporal separation; the exact `read-release-notes` inputs, authorization class,
  inclusive range, no-follow rule, expected evidence, and refusal; live management facts and
  built-in help authority; clean config creation, existing-config preservation, presence-only SSH
  identity selection, non-overwriting optional key generation, provider/plugin input collection,
  doctor readiness; and both resource action records, exact inputs/tokens, impact and
  authorization-class rendering, refusal, JSON verification, default and non-default admin-template
  normalization, selection rules, and absence of attach/delete/elevation.

### Clean-environment probes

Candidate probes install the built 0.14 release-candidate wheel at the package boundary rather than
PyPI. The future `v0.14.0` tag does not yet exist, so these probes exercise the same focused source
review against the exact release-PR commit that built the candidate wheel, label and record that
test-only ref substitution, and never claim they reviewed a tag. Each starts from a disposable clean
home with no SSH key, SSH agent, Git credential helper, or Git credential environment. The Claude
marketplace install must succeed through the explicit HTTPS URL. First-run setup may later create a
distinct operator-selected SSH identity only through the authorized bootstrap path; that does not
become a Git installation credential. Each probe records refs, commands, exit codes, installed
package identity/version, model output, authorization decisions, and cleanup.

| Scenario                        | Required result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude fresh first run          | With default/manual approval mode and no bypass, add the explicit HTTPS marketplace and install `agentworks@agentworks`; give one startup disclosure, establish a setup-through-first-session envelope, name the exact candidate commit substitution, perform the selected focused review, install the candidate wheel, initialize absent config without overwrite, select or generate the explicit SSH identity, collect required settings, pass doctor, create the first VM and session, and verify both describes without repeated approval prompts. |
| Codex fresh first run           | With `workspace-write` plus `on-request` and scoped escalation for account files/network/SSH, produce the same result without electing `danger-full-access`. Declining source review must not suppress the separate install choice.                                                                                                                                                                                                                                                                                                                     |
| Generic prompt-only first run   | Give the marked canonical body to an agent meeting the Agentworks assistant agent capability definition, with no Agentworks plugin. It identifies that role, discloses its actual workstation access once, establishes the requested setup envelope, installs the candidate wheel within it, obtains guide context, and completes the same config-through-session path without assuming Claude Code or Codex controls or asking before every step.                                                                                                      |
| Source-review independence      | Offer both scopes and the substantial-usage warning. Decline review while retaining an already authorized install in one path; complete the focused review then decline install in another. Selecting full review authorizes only its exact-ref scope, and candidate policy files remain data.                                                                                                                                                                                                                                                          |
| Returning current adoption      | The Agentworks assistant agent selects `concept-onboarding` from the map and uses current live facts, not release history.                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Rerun and post-upgrade adoption | An unchanged adopted system is a no-op; after an upgrade, report current not-yet-adopted capabilities without replaying completed work or inventing a historical delta.                                                                                                                                                                                                                                                                                                                                                                                 |
| Installed temporal history      | The Agentworks assistant agent selects `concept-release-notes` from the map and renders only the candidate wheel's matching 0.14 changelog section offline as bounded untrusted evidence.                                                                                                                                                                                                                                                                                                                                                               |
| Older temporal history          | Request an older packaged range and render its ordered exact-version topics locally, one bounded section at a time. For a deliberately missing version, use the canonical release URL when that network read is inside the current envelope; otherwise ask once for that expansion. Refusal performs no network request and invents no delta.                                                                                                                                                                                                           |
| Ordinary management             | The Agentworks assistant agent selects `concept-management`, establishes the requested target and authorization envelope, uses live JSON facts and built-in help, and completes a short multi-command disposable task with verification without repeated approval prompts.                                                                                                                                                                                                                                                                              |
| Material ambiguity              | Give an exploratory request whose interpretation changes the target, access class, or impact. The Agentworks assistant agent asks one resolving scope question, then completes the selected interpretation without another confirmation.                                                                                                                                                                                                                                                                                                                |
| Material expansion              | During ordinary management, make the task unexpectedly require a different target, destructive operation, privilege elevation, or sensitive-content read not covered by the operator's instruction or current envelope. The Agentworks assistant agent pauses once, explains the expansion, and proceeds only if authorized.                                                                                                                                                                                                                            |
| Operator-selected confirmations | Ask the Agentworks assistant agent to confirm every action. It honors that preference for the session even when the actions would otherwise share an authorization envelope.                                                                                                                                                                                                                                                                                                                                                                            |
| README equivalence              | Starting from only the marked README body in an agent meeting the capability definition produces the same disclosure, package install, and shared guide handoff as either native package.                                                                                                                                                                                                                                                                                                                                                               |
| Old CLI or failure              | Select an exact compatible version, offer exact-tag review, and upgrade a pre-0.14 CLI when installation is covered by the startup envelope or a later decision; a declined or failed install stops before the guide and reports the exact pinned repair command.                                                                                                                                                                                                                                                                                       |
| Post-publish production         | Resolve the published stable version, review its real canonical `vVERSION` tag, then run the exact pinned PyPI install and verify the installed version and local release section.                                                                                                                                                                                                                                                                                                                                                                      |

Live probes validate model interpretation. Their full matrix runs for Phase 3 acceptance and release
candidates, not for a later metadata-only package-version pin. Unit tests validate deterministic
files and guide data. Structural tests enforce heading order, non-empty disclosure, links, and the
no-command-before-authorization boundary; a reviewer judges semantic disclosure quality in live
transcripts rather than comparing model prose to literal clauses. No test adds a Markdown parser or
test-only bootstrap workflow.

## Failure behavior

- Missing startup authorization means no probe, installation, guide command, or network request
  runs. A narrower envelope permits only the covered work.
- Declined source inspection performs no repository read but does not revoke installation authority
  established by the operator's instruction or a later decision. A source review never authorizes
  installation.
- An action outside the current envelope does not run until the operator authorizes that material
  expansion; the inert refusal alternative remains available. An in-scope action does not require a
  repeated approval prompt.
- Missing or invalid selected input suppresses the applicable first-run action command.
- CLI installation or minimum-version failure stops before guide execution.
- Missing, ambiguous, invalid, or oversized local release evidence renders no partial section and
  preserves the authorized canonical fallback for only the missing version or range. Network refusal
  or failure does not synthesize historical claims.
- Package validation or generated-byte drift fails CI with the exact path.
- Guide fact or verification failure reports observed facts and points the Agentworks assistant
  agent to troubleshooting without replaying a mutation.

## Documentation, completion, and configuration impact

- README gains only the marked neutral assistance body. Its generated content ships with the code
  and package layouts that make it true.
- The standalone website effort consumes that canonical body for its copy/paste prompt, verifies
  byte parity, and deletes its temporary security-disclosure message input after integration so no
  second authored source survives.
- Permanent assistance installation docs name both production package commands, exact-version CLI
  installation behavior, source-review choices, authorization behavior, and strict harness posture
  without copying the body.
- Permanent guide docs describe the no-topic intent map, current-versus-temporal split,
  `concept-release-notes`, management help authority, and bounded first-run actions.
- `concept-release-notes` joins the existing dynamic guide topic source, so guide topic completion
  fixtures, snapshots, and generated completion documentation update in the same implementation
  commit. No hand-maintained completion list is added.
- `cli/pyproject.toml` adds only Hatch's explicit changelog force-inclusion mapping. No
  configuration key is introduced, so sample configuration remains unchanged. Tests assert that the
  package and guide companion need no new setting.

## Release choreography

PR #480 remains the sole full-feature PR. After design approval it continues through implementation,
repo tests, source review, and live feature validation; it does not merge at this design gate. Once
the complete feature is ready, it merges normally to `main` with a conventional `feat:` title. That
merge is the release-please input and triggers regeneration of the separate 0.14 release PR from the
new mainline state. Phase 3 is inherited from `main`; it is never injected into or carried as a
foreign commit on the release branch.

The regenerated release PR adds only release-owned deltas: version, generated changelog, manifest,
and lockfile. Candidate gates run there after the generated 0.14 section and packaged Phase 3 code
coexist. They build that branch's wheel, prove the wheel contains the exact normalized changelog,
perform the test-only focused source review at the exact release-PR commit that produced the wheel,
install that exact candidate artifact at the package boundary, and run both live harness probes
before release-PR merge, tag, or publish. Evidence identifies the commit substitution and does not
call it a release-tag review.

The release PR merges the release metadata onto main. Release-please creates the tag and release
from that release commit, `release.yml` publishes it, and the post-publish smoke resolves the stable
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
- Codex package fields and installation flow follow
  <https://developers.openai.com/plugins/build/plugins>.
- Agent Skills frontmatter follows <https://agentskills.io/specification>.
- Hatch's documented target `force-include` mapping supports copying `cli/CHANGELOG.md` to the exact
  wheel path `agentworks/CHANGELOG.md` without a build hook:
  <https://hatch.pypa.io/latest/config/build/#forced-inclusion>.
- Current repository release configuration uses release-please's Python release type,
  `changelog-path: CHANGELOG.md`, unprefixed component tags, and `vVERSION` release workflow tags.
- `.github/workflows/release-please.yml` runs on pushes to `main` and regenerates its release branch
  from mainline conventional commits. Release PR #402, inspected on 2026-08-10, contains only
  release-owned manifest, changelog, version, and lockfile deltas, which is why Phase 3 must merge
  normally before candidate testing that regenerated PR.

## Process

A delegated onboarding-and-discovery developer authored this LLD under the effort lead's ownership.
PR #480 carries the entire Phase 3 feature. This artifact checkpoint is a design gate inside that
same draft PR, not an artifact-only merge gate: the PR remains draft and does not merge when design
review converges. Implementation continues on the same branch and PR, followed by implementation
review, live validation, release preparation, and the remaining plan gates. The PR becomes ready for
normal feature merge only when the complete feature and every Phase 3 definition-of-done requirement
are present and green. Its `feat:` merge then causes release-please to regenerate the separate
release PR; #480 is never transplanted into that release branch.
