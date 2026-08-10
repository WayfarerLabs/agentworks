# HLA: Agentworks Assistance, Discovery, and Management

- Status: Draft for pre-implementation review
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Prior art: `docs/sdd/2026-08-05-onboarding-and-discovery/prior-art-research.md`
- Early contract message:
  `docs/sdd/2026-07-31-declarative-schema/onboarding-topic-content-contract-message.md`

## Architectural summary

The implementation has four cooperating surfaces:

1. `agw guide [topic ...]` renders authored teaching plus facts from the live finalized system.
2. A universal inert topic contract lets kinds, capability implementations, core concepts, and
   plugins contribute through the same path.
3. Selected operational commands expose versioned JSON from the same service-layer fact records
   their human renderers use.
4. Claude Code and Codex publish thin, equivalent Agentworks skills that remain available for the
   Agentworks lifecycle. They disclose access, install or update the CLI when needed, and hand the
   current request to the top-level `agw guide --agent` intent router.

The first implementation slice uses only contracts on `main`: current registries, finalized graph
facts, resource inspection records, and the completion mechanism. Wave 2 adoption is a later adapter
over its schema and field-documentation sources after those contracts land on `main`. No branch-only
wave 2 interface is a dependency.

## Component topology

```text
kind / capability / plugin / core concept registrations
                          |
                          v
                  topic catalog builder
                          |
                 validate identities and blocks
                          |
          +---------------+----------------+
          |                                |
          v                                v
 authored inert blocks             core dynamic block resolvers
                                           |
                                  guide access mode over
                                  registry + finalized graph
                                           |
          +---------------+----------------+
                          v
                 markdown topic renderer
                          |
                    agw guide CLI

service fact records ------> human renderers
          |
          +----------------> JSON v1 serializers
```

## Universal topic-content contract

### Records

`TopicContribution` is immutable data with:

- `topic`: canonical slug.
- `title`: display title.
- `summary`: one authored paragraph used in topic indexes and reference introductions.
- `anchor`: a typed identity for `concept`, `kind`, `resource`, or `implementation`.
- `blocks`: an ordered tuple of validated block records.
- `related_topics`: exact topic slugs for progressive navigation.

The initial block vocabulary is closed:

- `Overview`: inert markdown prose. The declarative-schema topic prose maps to this same block
  rather than a separate blurb registry.
- `Teaching`: inert markdown prose rendered by guide, not by describe.
- `AgentContract`: inert markdown prose whose heading and placement are foregrounded in agent mode.
- `InstanceList`: core-rendered live resources anchored at a kind or implementation.
- `State`: core-rendered enablement and readiness for `me`.
- `Relationships`: core-rendered inbound and outbound resource relationships for `me`.
- `FieldReference`: a core-rendered declarative-schema field-documentation fragment. It reads the
  landed `SchemaReference` tree, including each union alternative's fields and recurring or
  separately addressable arms. An exact section selector may cross an alternative only when one
  field path matches.
- `Sample`: a core-rendered declarative-schema live sample.
- `ReleaseNotes`: a core-rendered, size-bounded section selected for the exact installed CLI version
  from release-please's changelog packaged in the wheel. Contributions cannot supply its path,
  version, or prose.
- `ActionList`: inert, strictly validated `GuideAction` records with an exact consent boundary,
  command or platform-neutral manual step, expected state, verification, and refusal alternative.
- `TopicLinks`: core-rendered related-topic links.

Contributions supply block records and authored strings only. They cannot supply functions, imports,
expressions, format strings, attribute paths, or renderer names outside the closed enum. Markdown is
emitted as text, never interpreted by Agentworks as executable content. Registration rejects unknown
fields and expression-like placeholder syntax outside the contract's narrow same-line inline-code
form. Inline literals are always inert and are never template-evaluated. A participant with no
content registers no topic. Registration also enforces byte and count bounds on every authored
field, block payload, selector, and related-topic slug. Rendering removes terminal control bytes at
one final boundary, preserving only line feed and tab from the control ranges, so neither authored
nor projected text can reset a terminal, ring a bell, erase output, or forge a control sequence.

### Registration ownership

The topic contract is not a field on the capability-kind descriptor. That descriptor covers only
capability kinds, while R14 also covers ordinary resource kinds and concepts. Instead, a core-owned
topic catalog accepts `TopicContribution` values through small adapters at existing ownership
boundaries:

- a resource kind registers its kind topic beside its kind definition;
- a capability implementation registers its implementation topic beside its implementation;
- a system plugin carries its topic tuple in the existing inert plugin descriptor;
- core registers `concept-*` topics from package data beside the owning subsystem.

The future capability descriptor may expose the implementation's topic tuple through its generic
registration record, but it does not own or reshape the contract. This keeps one contract across all
contributors and avoids a capability-only parallel.

Wave 2 should register its rich prose as `Overview` blocks and consume the same summary and overview
in describe. Its field walker, schema emission, and sample renderer remain presentation-free shared
fact sources. `FieldReference` and `Sample` blocks hold stable core references to those sources, not
copied field lists or rendered CLI text.

### Taxonomy and collision policy

- `concept-*` is reserved for core concept topics.
- A bare kind slug is owned by that registered kind.
- `kind/name` identifies either a resource or a capability implementation according to the kind's
  registered category.
- Additional plugin-owned topics, if needed, use `plugin/<plugin>/<topic>`.

Every canonical slug has exactly one owner; there is no precedence or override rule. Strict CI
construction makes trusted in-tree taxonomy, ownership, duplicate, and broken-link contradictions
hard failures. Runtime construction records trusted taxonomy drift as a scoped issue and retains
unaffected topics, so a kind rename cannot make the installed guide unusable. Other trusted
ownership, duplicate, and broken-link contradictions remain hard startup failures because they are
curation bugs with no unambiguous winner. Invalid plugin content is isolated before it can suppress
trusted content. A full index renders all visible isolated issues and exits 1; an explicit retained
topic keys status only to issues visible for that request and can render cleanly with exit 0.

Multiple requested topics are validated as one request before output begins, rendered in the order
requested, and separated by a markdown horizontal rule. Repeated slugs render once at their first
position. An unknown topic fails the whole request with suggestions and produces no partial output.

## Safe `me`-anchored graph access

Guide rendering receives a `GuideView`, not `Registry`, `DependencyGraph`, a capability object, a
database handle, or configuration blobs. `GuideView` is a read-only access mode over the existing
finalized registry and graph, not a persisted mirror. It returns frozen fact records assembled from
already-materialized data:

- identity, category, origin, and description from registry rows;
- enablement and readiness verdicts from finalized graph nodes;
- declared inbound and outbound relationships from graph edges;
- resource instances from existing kind-owned instance inventory hooks.

Descriptions are operator or plugin data, not authored teaching. Guide projects them as labeled
plain text with Markdown and HTML syntax neutralized, so configured facts cannot become agent
instructions, headings, links, images, or executable markup.

The mode is gated by leaving powers unwired. Its public API has no secret resolver, run target,
capability implementation, mutable node, raw config, or arbitrary graph traversal entry point. In
particular, the current graph's `impl_of` power is absent. Named concept roots expose only bounded
catalogs such as kinds and implementations.

Dynamic blocks may make only these traversals:

- kind `me` to its registered resources;
- resource `me` to its kind, inbound references, and outbound references;
- implementation `me` to its capability kind and published resource row;
- concept named root to an explicitly named inventory.

Every query reads materialized state. Rendering never finalizes a graph, resolves a secret, opens a
connection, probes the workstation, invokes a capability, or mutates data. Database-backed instance
inventory is a read-only stored-row query already used by resource inspection; it cannot initiate a
remote operation.

Host-probing capability kinds are declared once in the resource-graph policy used by both ordinary
readiness dispatch and the guide's suppression path. End-to-end guide tests exercise the default
composition path and enforce an import boundary that prevents guide modules from acquiring probe,
resolver, transport, mutation, or low-level filesystem-write powers through direct aliases.

Guide remains usable when operator configuration is broken. It always loads and validates authored
core content independently, then attempts a normal config load and a guide-scoped registry build for
every guide request. The guide build preserves declaration, publication, materialization,
validation, graph finalization, and freezing, but disables host-readiness probes. A readiness fact
that requires such a probe remains explicitly unavailable and is assessed as unverifiable, never as
an observed failure. Normal command registry builds retain their existing readiness probes. If
config load or finalization fails, authored content still renders, each affected dynamic block says
its facts are unavailable, and the original framed config error appears in the markdown. `GuideView`
construction is non-interactive by construction and can never prompt for or resolve a secret. A
malformed plugin contribution is isolated to the guide-scoped catalog build, reported as
unavailable, and cannot break unrelated CLI commands or valid core topics.

Expected per-topic projection failures, such as a declared relationship naming a resource absent
from the finalized registry, become scoped unavailable facts. Translation occurs at the exact lookup
boundary. Programming errors from graph methods or kind-owned inventory hooks are not caught as
missing resources.

## Guide rendering and agent shaping

`agw guide` always writes markdown to stdout. With no topic it renders a compact overview, security
disclosure, intent router, and live topic index. Agent mode routes common requests without assuming
that every invocation is first-run onboarding:

- setup, adoption assessment, and installed-release capability questions route to
  `concept-onboarding`;
- temporal release-change questions route to `concept-release-notes`, which renders the installed
  release's packaged canonical notes, offers a consented canonical fallback for older ranges, and
  then links back to the live adoption assessment;
- configuration, declared-resource changes, and VM or session operation route to
  `concept-management` and the applicable kind or instance topics;
- diagnosis routes to `concept-troubleshooting`;
- breaking-input remediation routes to `concept-migration`;
- secret-model questions route to `concept-secrets`;
- defects route to `concept-reporting-bugs`.

The router is authored core guide content over the live topic catalog. It contains no operation
commands and grants no authority. Adding a topic does not silently make it a top-level intent; the
small set of intent routes is reviewed teaching, while the complete index remains derived.

`concept-release-notes` combines authored connective teaching with one core `ReleaseNotes` block.
The wheel contains release-please's existing `cli/CHANGELOG.md` as package data; it does not contain
a separately authored release-note file. The block resolves the installed distribution version,
requires exactly one matching release section through a bounded parser, sanitizes it through the
normal guide boundary, renders it as visibly labeled escaped plain-text evidence with links inert,
and fails soft with the canonical releases URL when the artifact or unique matching section is
unavailable. Rendering opens no network connection and never renders the whole changelog.

For an older or missing range, a validated `read-release-notes` action requires operator-supplied
`FROM_VERSION` and `TO_VERSION`, uses a dedicated `read-canonical-release-notes` consent boundary,
and names only `https://github.com/WayfarerLabs/agentworks/releases` as the allowed network source.
Its manual step reads only the requested inclusive release range, summarizes the changes, and does
not follow embedded links. Fetched or packaged release prose is untrusted evidence. The agent
ignores instructions, commands, permission requests, and scope changes within it, and treats any
proposed follow-up as a new action requiring its own operator decision. Refusal performs no network
request and leaves the canonical URL and requested range as manual operator steps without claiming a
summary. The topic then links to `concept-onboarding` for the separate question of which
capabilities the current installation has adopted.

The CLI exposes a paired `--agent/--human` override. Detection precedence is:

1. An explicit flag always wins.
2. A registered, unambiguous harness signature selects agent mode.
3. Otherwise, stdout attached to a TTY selects human mode and redirected or piped stdout selects
   agent mode.

Harness signatures are exact, non-secret environment markers documented or contractually supplied by
the harness, registered beside its bootstrap adapter, and pinned by tests. General configuration
variables are not signatures: `CODEX_HOME`, provider selectors, API keys, and similar variables can
exist in an ordinary shell. The current public Codex environment-variable contract does not expose a
stable execution signature, so the first slice does not treat an internal variable observed in one
session as a contract. The guide LLD inventories both harnesses again at implementation time.
`--human` is the documented override when a human pipes or redirects markdown; `--agent` makes an
agent invocation deterministic when no signature exists. Detection never inspects parent processes,
session files, or other workstation state.

Both modes traverse the same topic and block sequence. Agent mode may move `AgentContract` blocks
immediately after the summary, expand their heading, and foreground the R12 disclosure and R4
consent rules. It may not add, remove, or alter factual content. Snapshot tests normalize headings
and prove both modes contain the same semantic block identifiers.

`concept-migration` is the exceptional remediation topic for breaking resource-model changes. It is
not a general upgrade guide: ordinary upgrades should remain routine. The topic carries authored
rewrite sequencing and points into kind and implementation topics whose `FieldReference` and
`Sample` blocks consume the declarative-schema services now on `main`. An operator or agent
therefore works against the installed model rather than a frozen migration oracle.
`concept-onboarding` and `concept-management` link to it without duplicating its teaching.

Schema-derived guide topics consume the context-free reference and sample records. Owner-dependent
default filling remains at the manifest decode boundary; the guide does not recreate that step, pass
Pydantic validation context, or construct capability implementations.

`concept-onboarding` remains the specialized first-run and adoption-assessment topic; it is not the
identity of the installed skill. It does not persist a second onboarding ledger. Done and
not-yet-done status is a pure assessment over sanitized facts already available through `GuideView`:
resource identity and description from registry rows, finalized enablement and readiness verdicts
from graph nodes, declared graph relationships, and existing instance rows from kind-owned read-only
inventory hooks. The guide never loads raw config to infer additional state and never runs doctor
while rendering. A fact outside that set is `unverifiable`, not permission to reach around the view.
Its golden path continues through a usable VM and a started first session. Those operations are
inert action records with explicit names and selected templates or sites, declared impact,
`mutate-agentworks` consent, ordinary JSON verification, and a refusal alternative. The action plan
does not attach, delete, elevate privileges, or infer operator choices.

`concept-management` is the ongoing configuration and operation entry. It routes to live kind and
instance facts, then names the applicable built-in CLI help entry point for exact current command
syntax. Group and command help from the existing Typer command tree is the command authority; the
guide does not project a command registry or copy complete operational recipes. The small stable set
of group-level help links is authored connective teaching and changes in the same commit as a CLI
rename. There is no package-level wall between configuring Agentworks and operating managed VMs or
sessions. The boundary is the action: read-only discovery may remain read-only; creating or changing
declared state uses `mutate-agentworks`; connection uses the named-target boundary; and destructive
work or privilege elevation requires a fresh, explicitly described operator decision. Existing
consent boundaries remain sufficient for configuration and operation; the release-note lookup adds
only the narrow `read-canonical-release-notes` boundary and does not create a second risk model.

Agent-only workstation facts are never inferred by `agw`. Doctor, tool checks, SSH tests, and other
verification commands are explicit assistance actions that the agent runs only after obtaining the
applicable consent. The guide asks the agent to check presence without reading sensitive values and
record refusals in the current interaction or caller-owned replay log. The guide presents an ordered
action plan:

- guided use lets the agent ask before each consent boundary and execute the next action;
- replayable use uses the same actions with `agw --non-interactive`, explicit inputs, and repeatable
  target-scoped `--evidence ACTION_ID:KIND/NAME=OUTCOME` values from the caller-owned replay log;
- reruns skip facts already ready and report currently not-yet-adopted, disabled, not-ready, or
  unverifiable items.

The first slice defines the assessment and plan. It does not add a CLI wizard or hidden state
machine.

Each action is inert guide data with an identifier, sanitized precondition, required operator
inputs, consent boundary, command template, expected observable state, verification command, and
manual alternative when consent is declined. Guided and replayable modes consume the same ordered
action records. Equivalence means both produce the same registry, graph, stored-row, and explicit
verification outcomes for the same inputs. A refusal produces the same `unverifiable` outcome in
both modes.

Evidence outcomes are `verified`, `failed`, or `refused`. The CLI validates every evidence item
atomically and persists none of them. A verified rerun can therefore become a no-op without adding
an Agentworks onboarding ledger; the caller remains responsible for retaining and replaying proof.

### Verification surface inventory

| Need                          | Existing surface                                                                                                                                        | Gap and commitment                                                                                                                                                                                                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Secret reference availability | `agw secret describe` predicts the ready backend without resolving or exposing a value. Doctor reports the same prediction in its consented full check. | Prediction is not proof. Add a named-secret verification operation that resolves through the normal boundary, returns only success or framed failure, never emits or returns the value to the caller, and cannot fall through to an interactive backend without explicit consent. |
| Required host tools           | `agw doctor` checks `ssh`, `scp`, and `tailscale`; finalized capability rows carry already-computed readiness for their host requirements.              | The LLD inventories every assistance action's required tool and adds a safe explicit check only where doctor or readiness does not already cover it. Agent-side discovery of other installed tools remains consent-first.                                                         |
| SSH connectivity              | VM lifecycle code verifies connectivity during mutating operations, but there is no dedicated read-only operator surface for an existing VM.            | Add a bounded, non-mutating named-VM connection verification operation that uses the standard transport and reports success or framed failure without repairing, rekeying, or changing power state.                                                                               |

Doctor and the new proof operations run only as explicit, consented action records. Guide rendering
never calls them.

## Machine-readable output contract

Structured output is an explicit per-command `--output human|json` option, defaulting to `human`. A
global mode is rejected because most mutation commands emit event streams or prompts rather than one
stable result object. The reusable option and serializer make the selected contract consistent
without implying that every command is serializable.

Version 1 covers:

- entity list and describe commands for resources, VMs, workspaces, agents, sessions, consoles, and
  secrets;
- `resource kinds`;
- `doctor`.

JSON is UTF-8 with deterministic key and collection ordering and this envelope:

```json
{
  "schema_version": 1,
  "command": "resource.list",
  "data": {}
}
```

Each command owns a documented `data` schema derived from its service-layer fact record. JSON mode
never contains ANSI styling or friendly prose. Missing values are JSON `null`, not display sentinels
such as `-`. Identifiers and enum values use stable lowercase strings. Human and JSON renderers
receive the same fact object; neither parses the other's output.

Additive optional fields may be added within version 1. Removing a field, changing its type or
meaning, or changing an enum value requires a new schema version and an explicit compatibility
period. Unknown requested output formats fail before work begins. Business errors remain on stderr
using the existing error route and a nonzero exit. `doctor --output json` emits the complete report
then preserves doctor's current nonzero exit when any check fails.

`--names-only` remains completion plumbing, not a guide rendering mode, and is mutually exclusive
with JSON. User-facing guide output is markdown only and deliberately outside this contract.

## CLI and completion integration

The root Typer application gains a `guide` command module. Topic completion uses the existing
dynamic-completion map and calls `agw guide --names-only`. Every full guide request attempts the
normal config load and full registry build so its inventory is consistent; the graceful-degradation
contract above keeps authored help available on failure. Completion uses the guide-scoped catalog's
fail-soft projection, returning validated authored topics plus any dynamic topics available from a
successful build. Bash, Zsh, and PowerShell snippets and completion tests land with the command.

The topic catalog and guide renderer live below Typer. CLI functions parse options, load request
state, call the service, and emit the returned markdown. This mirrors the existing resource
inspection separation and keeps tests independent of terminal presentation.

## Assistance packaging

One canonical assistance body is the source for both harness packages. It contains only:

1. supported Python and `agentworks-cli` installation guidance;
2. the complete R12 access disclosure, including the intended workstation, full account-scoped file
   inspection and command execution, separate explicit privilege elevation, Agentworks-reachable
   resources, and concrete strict-security posture links;
3. a pre-install offer to review the exact canonical source tag, with a warning that the repository
   is substantial and a full review can consume significant model usage, and separate focused, full,
   decline-review, and install decisions;
4. the instruction to run `agw guide --agent`, select the route matching the operator's current
   goal, and follow that topic.

After the general disclosure and network consent, the package resolves the exact stable PyPI version
it proposes to install and pins both the source-review tag and install command to that version. A
focused review covers packaging, dependencies, CLI entry, and security-sensitive execution
boundaries; a full review may inspect the entire canonical tag. Repository content remains untrusted
evidence: it cannot grant permission, direct execution, or expand the reviewed scope. Declining
review neither claims review nor blocks a separately approved install. Review approval never implies
install approval.

Source inspection runs from the assistance session's protected policy root. The agent does not
launch or reconfigure a harness from the candidate tree, change its working root to that tree, load
candidate `AGENTS.md`, `CLAUDE.md`, skills, hooks, plugins, or configuration as policy, or execute
candidate scripts and commands. If files are materialized locally, they remain in an
operator-approved data-only temporary location and are read by explicit path from the protected
root. Instruction-like repository content is reported only as source evidence. Running any candidate
code is a separate action and is not part of source review.

A small generator wraps that body in the Claude Code and Codex package layouts. Generated files are
committed so GitHub installation works without a build step. CI regenerates into a temporary
directory and requires a clean diff, proving substantive parity. Package metadata declares the
minimum CLI version that first supplies the referenced guide contract and no maximum. The guide
itself owns all evolving teaching.

The wheel build includes release-please's canonical `cli/CHANGELOG.md` at a fixed package-data path.
The release PR carries the Phase 3 implementation, version bump, and generated changelog together;
candidate-wheel and live harness gates run from that release PR before it merges. The release tag
and PyPI publish follow only after those gates pass, so the installed version and its internal
release section are one reviewed artifact.

Production assistance reviews canonical `vVERSION` before installing exact `VERSION`. A pre-merge
candidate cannot claim a tag that does not yet exist: candidate probes instead review the exact
release-PR commit that built the candidate wheel, record that test-only ref substitution, and
install that exact artifact. The post-tag PyPI smoke exercises the production `vVERSION` review and
exact install path.

The repository README leads with the same canonical agent-addressed assistance text in a fenced
copyable block. It derives from or is checked against the canonical source rather than maintaining a
second security paraphrase. The plugins remain an additional discovery channel, not a prerequisite.

The package is named for Agentworks rather than onboarding, and its description activates for setup,
discovery, adoption, configuration, troubleshooting, and operation. It contains no intent-to-topic
switchboard of its own. The top-level guide owns that routing, and selected topics tell the agent
which list, describe, doctor, and operation records to request at each applicable action. End-to-end
assistance tests cover focused and full source-review choices, decline-review followed by separately
approved installation, completed review followed by declined installation, an initial VM and
session, a returning current-capability and adoption assessment, offline installed release notes
plus the consented range fallback, an ongoing management operation, and a refusal at a higher-risk
action boundary.

## Feedback decision

General feedback collection is deferred. The first release adds no telemetry, product-feedback
prompt, or request for the operator to relay non-bug comments manually. Acceptance runs record their
own timing and intervention evidence as test artifacts. A real product feedback channel requires a
later operator decision.

`agw guide concept-reporting-bugs` covers the narrower case where the operator or agent encounters a
defect. It teaches how to reproduce the problem, remove secrets and identifying data from evidence,
check existing issues, and use the repository's bug-report template. The topic may direct the agent
to prepare or submit an issue only with the operator's explicit authorization. It never files an
issue automatically and is not presented as a channel for general feedback.

## Declarative-schema coordination boundary

Declarative-schema Phase 2 merged on 2026-08-07 and accepted the early topic-content direction.
`agentworks.topics.TopicProse` keeps one title and overview beside each kind or implementation, and
`summary_of` plus `prose_of` expose those authored facts without a second blurb registry.
`agentworks.manifests.reference.SchemaReference` is the config-free field contract for declarable
kinds, capability kinds, and disabled implementations. `describable_targets` enumerates those
targets, and `agentworks.manifests.samples.sample_text` renders declarable samples from the same
model stream.

The release-gate adapter resolves these services from typed topic anchors. Bare kind and capability
implementation topics render schema facts even when operator config fails, while resource state and
graph facts continue to degrade through the existing framed `GuideView` boundary. Capability
references are never passed to the manifest sample renderer. The exact record mapping, target rules,
failure behavior, and tests are pinned in `wave2-guide-adapter-lld.md`.

The landed contract is compatible with the HLA's safety boundary and requires no provisional
dependency. Broader Phase 4 registry inventory and specific-resource projection remain separately
gated work.

## Documentation and compatibility

Permanent CLI docs define topic taxonomy, agent shaping, JSON v1, assistance-package installation,
and the safe contribution contract in the same commits as their code. Package-level contributor docs
explain how to colocate inert topic data. The sample config changes only if assistance introduces a
new setting; no setting is currently planned.

The contributor contract also becomes durable agent guidance through Rulesync's canonical sources.
An always-on rule tells developers that code adding or changing a resource kind, capability
implementation, plugin, or documented workflow must add or update its colocated topic contribution.
The `agentworks-dev` role treats that contribution as part of implementation completeness, and the
`agentworks-reviewer` role checks the code and contribution together for missing or stale teaching.
The implementation audits other agent roles for a real need rather than copying the rule blindly,
then regenerates and commits the Claude Code, Codex, and Copilot projections with the existing
Rulesync drift check.

`agw guide` and JSON v1 are additive. Bootstrap packages state their minimum compatible CLI.
Breaking changes follow the repository's warn-then-reject runway where one exists. Remediation is
precise errors plus `concept-migration`, not an automated migrator. Machine-contract changes keep
their own explicit versioning and compatibility rules.

## Key risks

- Declarative-schema prose and schema facts must cross the guide contribution validator exactly
  once. Escaping authored Markdown or bypassing contribution validation would each violate one side
  of the shared contract.
- Entity commands do not all currently return structured fact records. Each conversion must keep
  human output byte-compatible and avoid remote work solely for JSON.
- The current graph exposes capability implementations. The guide view must be tested as a
  deny-by-construction boundary, not trusted renderer discipline.
- Cross-harness package formats evolve independently. Generated committed wrappers and real install
  probes reduce drift risk.
- Specific-resource projection still depends on the runtime registry and remains fail-soft when
  configuration cannot build it; config-free schema discovery must not invent resource instances.
