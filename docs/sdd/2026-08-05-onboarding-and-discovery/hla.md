# HLA: Onboarding, Discovery, and Management

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
4. Claude Code and Codex publish thin, equivalent bootstrap skills that disclose access, install the
   CLI, and hand control to `agw guide`.

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

- `Overview`: inert markdown prose. The proposed wave 2 alignment maps its planned blurb to this
  same block rather than a separate blurb registry.
- `Teaching`: inert markdown prose rendered by guide, not by describe.
- `AgentContract`: inert markdown prose whose heading and placement are foregrounded in agent mode.
- `InstanceList`: core-rendered live resources anchored at a kind or implementation.
- `State`: core-rendered enablement and readiness for `me`.
- `Relationships`: core-rendered inbound and outbound resource relationships for `me`.
- `FieldReference`: a core-rendered wave 2 field-doc fragment once its contract lands.
- `Sample`: a core-rendered wave 2 live sample once its contract lands.
- `TopicLinks`: core-rendered related-topic links.

Contributions supply block records and authored strings only. They cannot supply functions, imports,
expressions, format strings, attribute paths, or renderer names outside the closed enum. Markdown is
emitted as text, never interpreted by Agentworks as executable content. Registration rejects unknown
fields and any expression-like placeholder syntax. A participant with no content registers no topic.

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

Every canonical slug has exactly one owner. Duplicate registration is a startup error regardless of
load order; there is no precedence or override rule. The catalog validates every related-topic link
after registration.

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

## Guide rendering and agent shaping

`agw guide` always writes markdown to stdout. With no topic it renders a compact overview, security
disclosure, golden-path entry, and live topic index.

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

`concept-onboarding` does not persist a second onboarding ledger. Done and not-yet-done status is a
pure assessment over sanitized facts already available through `GuideView`: resource identity and
description from registry rows, finalized enablement and readiness verdicts from graph nodes,
declared graph relationships, and existing instance rows from kind-owned read-only inventory hooks.
The guide never loads raw config to infer additional state and never runs doctor while rendering. A
fact outside that set is `unverifiable`, not permission to reach around the view.

Agent-only workstation facts are never inferred by `agw`. Doctor, tool checks, SSH tests, and other
verification commands are explicit onboarding actions that the agent runs only after obtaining the
applicable consent. The guide asks the agent to check presence without reading sensitive values and
record refusals in the current interaction or caller-owned replay log. The guide presents an ordered
action plan:

- guided use lets the agent ask before each consent boundary and execute the next action;
- replayable use uses the same actions with `agw --non-interactive` and explicit inputs;
- reruns skip facts already ready and report new, disabled, not-ready, or unverifiable items.

The first slice defines the assessment and plan. It does not add a CLI wizard or hidden state
machine.

Each action is inert guide data with an identifier, sanitized precondition, required operator
inputs, consent boundary, command template, expected observable state, verification command, and
manual alternative when consent is declined. Guided and replayable modes consume the same ordered
action records. Equivalence means both produce the same registry, graph, stored-row, and explicit
verification outcomes for the same inputs. A refusal produces the same `unverifiable` outcome in
both modes.

### Verification surface inventory

| Need                          | Existing surface                                                                                                                                        | Gap and commitment                                                                                                                                                                                                                                                                |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Secret reference availability | `agw secret describe` predicts the ready backend without resolving or exposing a value. Doctor reports the same prediction in its consented full check. | Prediction is not proof. Add a named-secret verification operation that resolves through the normal boundary, returns only success or framed failure, never emits or returns the value to the caller, and cannot fall through to an interactive backend without explicit consent. |
| Required host tools           | `agw doctor` checks `ssh`, `scp`, and `tailscale`; finalized capability rows carry already-computed readiness for their host requirements.              | The LLD inventories every onboarding action's required tool and adds a safe explicit check only where doctor or readiness does not already cover it. Agent-side discovery of other installed tools remains consent-first.                                                         |
| SSH connectivity              | VM lifecycle code verifies connectivity during mutating operations, but there is no dedicated read-only operator surface for an existing VM.            | Add a non-mutating named-VM connection verification operation that uses the standard transport and reports success or framed failure without repairing, rekeying, or changing power state.                                                                                        |

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

## Bootstrap packaging

One canonical bootstrap body is the source for both harness packages. It contains only:

1. supported Python and `agentworks-cli` installation guidance;
2. the complete R12 access disclosure and concrete strict-security posture links;
3. the instruction to run `agw guide concept-onboarding --agent` and follow it.

A small generator wraps that body in the Claude Code and Codex package layouts. Generated files are
committed so GitHub installation works without a build step. CI regenerates into a temporary
directory and requires a clean diff, proving substantive parity. Bootstrap metadata declares the
minimum CLI version that first supplies the referenced guide contract and no maximum. The guide
itself owns all evolving teaching.

The repository README leads with the same canonical agent-addressed bootstrap text in a fenced
copyable block. The plugins remain an additional discovery channel, not a prerequisite.

The bootstrap's instruction to follow `concept-onboarding` also exercises the machine-readable
contract. The guide tells the agent which list, describe, and doctor JSON documents to request and
inspect at each applicable action. End-to-end bootstrap tests follow that instruction and parse the
returned v1 documents rather than merely checking that the flags exist.

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

## Wave 2 coordination boundary

As of 2026-08-06, wave 2's branch-only `FieldDoc`, `ModelDoc`, schema, and blurb ideas are
provisional. Main has no schema package or renderer contract. PR #420 proposes the following
coordination contract before wave 2 implements plan section 2.8; it is not coordinated fact until
the recipient accepts it:

- keep `FieldDoc` and schema sources presentation-free;
- replace the proposed standalone blurb registration with `TopicContribution.summary` plus
  `Overview` blocks;
- expose service APIs for field reference and samples so guide blocks call sources, never CLI text;
- settle distinct names for instance describe and schema field reference without changing guide
  topic identities;
- let disabled implementations render from registered models without constructing implementations.

Each adapter is gated only on its required surface merging to `main`, so schema-derived depth can
adopt field docs, samples, or descriptor inventory incrementally as they land. If wave 2 lands a
different authoritative shape, this HLA and plan are updated before the affected adapter is built.
The agreed record shape is the coordination contract, not an implementation dependency in either
direction. Whichever effort reaches the seam first implements the record where it naturally lives;
the other consumes that landed shape. Wave 2 never waits for onboarding phase 1. If it reaches plan
section 2.8 first, it proceeds with the agreed shape, or with its own blurb surface if agreement has
not completed, and onboarding adapts after re-verifying the authoritative `main` contract.

## Documentation and compatibility

Permanent CLI docs define topic taxonomy, agent shaping, JSON v1, bootstrap installation, and the
safe contribution contract in the same commits as their code. Package-level contributor docs explain
how to colocate inert topic data. The sample config changes only if onboarding introduces a new
setting; no setting is currently planned.

The contributor contract also becomes durable agent guidance through Rulesync's canonical sources.
An always-on rule tells developers that code adding or changing a resource kind, capability
implementation, plugin, or documented workflow must add or update its colocated topic contribution.
The `agentworks-dev` role treats that contribution as part of implementation completeness, and the
`agentworks-reviewer` role checks the code and contribution together for missing or stale teaching.
The implementation audits other agent roles for a real need rather than copying the rule blindly,
then regenerates and commits the Claude Code, Codex, and Copilot projections with the existing
Rulesync drift check.

`agw guide` and JSON v1 are additive. Bootstrap packages state their minimum compatible CLI.
Machine-contract breaking changes follow the repository's normal warn-and-migrate then reject
policy.

## Key risks

- Wave 2 may land a blurb or renderer shape inconsistent with the universal topic contract. Route
  this HLA's proposed contract to the operator before its section 2.8 implementation.
- Entity commands do not all currently return structured fact records. Each conversion must keep
  human output byte-compatible and avoid remote work solely for JSON.
- The current graph exposes capability implementations. The guide view must be tested as a
  deny-by-construction boundary, not trusted renderer discipline.
- Cross-harness package formats evolve independently. Generated committed wrappers and real install
  probes reduce drift risk.
- Registry inventory will move when wave 2 lands. Sequence that work late or rebase once as
  directed.
