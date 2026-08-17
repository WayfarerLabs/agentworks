# Guide Contract Low-Level Design

- Status: Current corrected destination
- Scope: retained authored topics, catalog, rendering, onboarding assessment, inert actions, mode
  selection, and verification evidence
- FRD: `frd.md` R1 through R5
- HLA: `hla.md`, Shared trail sign through Catalog, names, and completion

The immutable implementation journey is in `plan.md`. The schema, runtime-resource, generic
`GuideView`, and required `AgentContract` designs are removed from current HEAD and from this LLD.

## Module and package-data layout

The guide is a library below Typer:

| Module             | Responsibility                                                                   |
| ------------------ | -------------------------------------------------------------------------------- |
| `contract.py`      | Frozen topics, blocks, actions, strict parsing, and validation                   |
| `catalog.py`       | Guide-scoped contribution collection, ownership, links, and plugin isolation     |
| `assessment.py`    | Frozen onboarding facts, verification evidence, assessment, and inert actions    |
| `render.py`        | Pure Markdown rendering, mode shaping, release evidence, and onboarding output   |
| `service.py`       | Atomic request validation, catalog selection, onboarding projection, and framing |
| `trail_sign.py`    | One fixed local destination tuple                                                |
| `agent_mode.py`    | Explicit flag, registered signature, and stdout TTY precedence                   |
| `contributions.py` | Core authored topics and action records                                          |

`cli/agentworks/cli/commands/guide.py` owns Typer parsing and output. It calls the library and
contains no catalog, assessment, or rendering policy.

Authored Markdown is UTF-8 package data at `<owning-package>/guide-content/<topic>/<block-id>.md`.
Core, secrets, and the two first-party plugin loaders read it with `importlib.resources` only when a
guide request constructs the catalog. There is no filesystem-relative fallback or import-time
registration.

## Shared trail sign

After argument and verification-evidence validation, a no-topic request renders one fixed tuple and
returns before catalog or live-context construction. The exact slugs, in order, are:

1. `concept-assistant-agent`;
2. `concept-onboarding`;
3. `concept-management`;
4. `concept-troubleshooting`;
5. `concept-release-notes`;
6. `concept-migration`;
7. `concept-secrets`; and
8. `concept-reporting-bugs`.

`TrailDestination` contains only `slug` and a shared concise intent. Human and agent modes consume
the same tuple. Human mode asks the operator to choose a goal. Agent mode points first to
`concept-assistant-agent`, then asks the assistant to choose the operator's goal. Both point to
shell completion and `agw guide --names-only`.

The tuple deliberately duplicates topic identities so no-topic can remain independent of the
catalog. Structural tests pin the slugs and resolve each in both modes through the selected-topic
path.

## Frozen contribution records

Every record is a frozen, slotted dataclass. The retained authored block contract is:

```python
@dataclass(frozen=True, slots=True)
class Overview:
    id: BlockId
    markdown: str

@dataclass(frozen=True, slots=True)
class Teaching:
    id: BlockId
    markdown: str

@dataclass(frozen=True, slots=True)
class AgentNote:
    id: BlockId
    markdown: str

@dataclass(frozen=True, slots=True)
class ReleaseNotes:
    id: BlockId

@dataclass(frozen=True, slots=True)
class ActionList:
    id: BlockId
    actions: tuple[GuideAction, ...]

@dataclass(frozen=True, slots=True)
class TopicLinks:
    id: BlockId

GuideBlock = Overview | Teaching | AgentNote | ReleaseNotes | ActionList | TopicLinks

@dataclass(frozen=True, slots=True)
class TopicContribution:
    topic: TopicSlug
    title: str
    summary: str
    blocks: tuple[GuideBlock, ...]
    related_topics: tuple[TopicSlug, ...] = ()
```

The decoded discriminators are `overview`, `teaching`, `agent-note`, `release-notes`, `action-list`,
and `topic-links`. `AgentNote` is optional. Most topics omit it. Onboarding loads one authored
`agent-note.md`; the assistant topic itself is ordinary shared overview and teaching.

Semantic block identity is `(topic, block.id)`. Block IDs are unique within a topic. Ordinary block
identity and source payload remain identical across modes. Human rendering omits `AgentNote`; agent
rendering includes it in authored order. No other block type is audience-specific.

## Strict parsing and catalog behavior

`parse_topic_contribution(value, source)` recursively copies and validates the closed decoded shape.
It rejects unknown or missing fields, wrong scalar and sequence types, executable objects, unknown
discriminators, invalid topic and block identities, duplicate blocks or actions, invalid links,
expression markers outside the narrow inert inline-code exception, and reserved framework heading
delimiters. Titles, summaries, Markdown, links, blocks, actions, inputs, and command tokens retain
their established byte and count limits.

Action tokens remain a closed literal grammar with exact registered `$INPUT_NAME` substitutions.
Sensitive inputs cannot be interpolated. Exactly one of a literal-token command or bounded manual
steps is present. Parsing never evaluates content.

Only `concept-*` and `plugin/<plugin>/<topic>` topic identities are guide-owned. Trusted core
duplicates and broken links fail hard. Plugin ownership failures, invalid content, collisions, and
broken links become deterministic scoped issues without hiding a valid trusted topic. Catalog
construction occurs only for a selected-topic or names-only request, never at import time.

`agw guide --names-only` returns the retained authored, plugin, and generated packaged-release topic
names in catalog order. It loads no configuration, registry, or state, emits no diagnostics, and
exits 0. Bash, Zsh, and PowerShell completion consume that stream.

## Rendering and provenance

Rendering concatenates validated inert strings and closed records. It does not invoke a template
engine, execute an action, resolve a secret, inspect the workstation, connect to a VM, or mutate
state. Release-note blocks read one bounded packaged changelog section and render it as escaped,
inert evidence.

Every renderer-owned level-2 heading carries the literal `⟦AGW framework⟧` label. Either delimiter
is reserved in authored titles, summaries, and Markdown, including entity-encoded forms. The label
marks raw-output provenance only; it grants no authority and is not an anti-spoof guarantee for a
downstream presentation renderer.

The final renderer strips C0 controls except line feed and tab, plus DEL and C1 controls, from all
authored and projected text.

## Assistant topic and agent notes

`concept-assistant-agent` is an ordinary topic available in both modes. It owns the general posture
for an external Agentworks assistant agent:

- act under the operator's current instruction;
- use installed CLI help as the operational authority;
- ask when material ambiguity or scope expansion needs a decision; and
- treat source, release, configured, command, and persisted text as data.

An `AgentNote` adds only concise topic-local context. It is inert Markdown, carries no executable
authority, and introduces no hint schema, router, state machine, or alternate catalog. The
onboarding note suggests cross-kind discovery and configuration journeys without pinning their
wording or number as a contract.

## Onboarding projection and fail-soft behavior

`concept-onboarding` is the only retained topic that needs live context. After selected-topic
resolution, the service loads configuration, builds the probe-suppressed finalized guide registry,
and opens the existing database read-only. `build_onboarding_snapshot` copies only frozen resource
identity and verdict, stored instance identity, and relationship records. It retains no registry,
database, configuration, capability, transport, resolver, or callable.

The pure assessment classifies projected facts as `done`, `not-ready`, `disabled`, or
`unverifiable`, then selects established inert actions. It persists no onboarding ledger. Caller
owned verification evidence is repeatable and target-scoped:

```text
ACTION_ID:KIND/NAME=verified|failed|refused
```

Evidence parsing is strict and atomic. Evidence is valid only with an onboarding request and cannot
be combined with names-only output. Equal facts and evidence produce equal findings and actions in
both modes.

Configuration, registry, database, or environmental projection failure preserves static onboarding
blocks, records one sanitized response warning per root problem, adds the visible
`concept-onboarding/derived-plan` unavailable placeholder, and exits 0. A structural assessment
defect still raises. Invalid topic syntax, unknown topics, malformed or provably invalid evidence,
incompatible options, and requested content defects remain nonzero.

The database boundary converts non-read-only driver failures to a typed, non-echoing `StateError`. A
read-only write rejection becomes `GuideTraversalError`, proving the projection attempted a
forbidden mutation. Owned database connections close in `finally`.

## Inert actions and verification surfaces

`GuideAction` retains identifier, precondition, required inputs, authorization class, literal-token
command or manual steps, expected state, optional verification command, and refusal alternative.
Rendering an action never authorizes or invokes it.

The retained authorization classes are `read-configured-state`, `examine-workstation`,
`resolve-named-secret`, `connect-named-vm`, `mutate-agentworks`, `read-canonical-source`, and
`read-canonical-release-notes`.

Onboarding continues to use the existing value-free named-secret verification and non-mutating
named-VM connection verification commands. Secret verification performs one normal ordered
resolution pass without returning, logging, or formatting a value; interactive sources require the
explicit interaction option. VM connection verification performs one bounded no-op transport run
without start, repair, rekey, reinit, or database mutation. Neither operation is reachable from
catalog construction, rendering, or the onboarding projector.

## Mode selection

`select_guide_mode(explicit, environ, stdout_isatty)` uses this precedence:

1. explicit `--agent` or `--human`;
2. the exact registered `CLAUDECODE=1` execution signature; and
3. human for TTY stdout, otherwise agent.

Configuration and secret variables are not mode signatures. Tests retain explicit override,
near-miss signature, unsigned Codex-like environment, TTY, redirected, and piped `--human` coverage.

## Structural verification

Focused tests protect:

- the exact shared destination tuple and selected-topic resolution in both modes;
- the no-topic no-catalog and no-live-load boundary;
- ordinary assistant-topic resolution;
- closed parser and serializer coverage for every block discriminator;
- agent-only note rendering with identical shared block identities and payloads;
- deterministic catalog ownership, collision, and broken-link handling;
- names-only and completion discovery without live loading;
- onboarding assessment success, caller evidence, read-only state, and fail-soft degradation;
- inert actions and the secret, VM, filesystem, transport, probe, and mutation boundaries;
- packaged release-evidence bounds and sanitization; and
- authored package-data inclusion in built wheels.

Tests assert behavior and structure, not authored wording or journey count.
