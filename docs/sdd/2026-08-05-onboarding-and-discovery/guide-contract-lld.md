# Guide Contract Low-Level Design

- Status: Approved for Phase 1 implementation
- Scope: `agw guide`, its inert contribution contract, safe read projection, onboarding actions,
  agent-mode selection, and the two missing verification surfaces
- Inputs: this effort's FRD, HLA, and plan; the roadmap target state; current CLI, registry, graph,
  secret, VM, transport, and declarative-schema contracts at HEAD

## Module and package-data layout

The guide core is a library below Typer. Phase 1 adds these focused modules under
`cli/agentworks/guide/`:

| Module             | Responsibility                                                                                 |
| ------------------ | ---------------------------------------------------------------------------------------------- |
| `contract.py`      | Frozen anchors, blocks, topics, actions, fact records, and strict constructors                 |
| `catalog.py`       | Guide-scoped contribution collection, batch validation, and fail-soft issue records            |
| `view.py`          | The deny-by-construction projection from a finalized registry and read-only instance inventory |
| `render.py`        | Pure markdown rendering and semantic block ordering                                            |
| `assessment.py`    | Pure onboarding status and action selection over `GuideView` facts                             |
| `agent_mode.py`    | Explicit flag, registered signature, and stdout TTY precedence                                 |
| `contributions.py` | Core contribution enumeration only, with no import-time registration side effect               |

`cli/agentworks/cli/commands/guide.py` owns Typer parsing and output. It calls the library and never
contains catalog, projection, or rendering policy. `cli/agentworks/guide/__init__.py` exports only
the public records and service entry points.

Authored markdown is UTF-8 package data, one file per authored block. The path is
`<owning-package>/guide-content/<topic>/<block-id>.md`. Examples are
`agentworks/guide/guide-content/concept-onboarding/security-disclosure.md`,
`agentworks/secrets/guide-content/concept-secrets/overview.md`, and
`agentworks/vms/guide-content/vm-template/teaching.md`. The owning Python package exposes a
`guide_contributions() -> tuple[TopicContribution, ...]` adapter which reads those resources with
`importlib.resources`. These adapters are trusted core registration code invoked once during
guide-scoped catalog construction. They return inert records and are never retained or called back
during rendering. Executable adapters supplied by external plugins are out of scope. Dynamic blocks
remain Python records with no companion file. The wheel and source distribution package all
`guide-content/**/*.md`; a packaging test reads every registered resource from an installed wheel.
There is no filesystem-relative lookup and no fallback embedded copy.

System plugin descriptors gain an inert `guide_topics: tuple[TopicContribution, ...] = ()` field.
Catalog construction collects topics from every installed system plugin regardless of operator
enablement. After registry finalization, `GuideView` projects each implementation's enabled or
disabled verdict, so disabled implementations stay discoverable and truthful. Loading the ordinary
CLI may import descriptors but does not build or validate a guide catalog. External plugin content
remains out of scope, while using exactly the same data type later.

## Frozen Python records

All records below are `@dataclass(frozen=True, slots=True)`. Tuple fields are normalized once by
their strict construction functions. No record accepts `Any`, mappings retained from decoded input,
callables, classes, modules, renderer names, or arbitrary attribute paths.

```python
TopicSlug = NewType("TopicSlug", str)
BlockId = NewType("BlockId", str)
ActionId = NewType("ActionId", str)

@dataclass(frozen=True, slots=True)
class ConceptAnchor:
    name: str

@dataclass(frozen=True, slots=True)
class KindAnchor:
    kind: str

@dataclass(frozen=True, slots=True)
class ResourceAnchor:
    kind: str
    name: str

@dataclass(frozen=True, slots=True)
class ImplementationAnchor:
    kind: str
    name: str

TopicAnchor = ConceptAnchor | KindAnchor | ResourceAnchor | ImplementationAnchor
```

Each block carries an explicit contributor-chosen `id: BlockId`. Authored blocks carry `markdown`;
dynamic blocks carry only closed, typed selectors:

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
class AgentContract:
    id: BlockId
    markdown: str

@dataclass(frozen=True, slots=True)
class InstanceList:
    id: BlockId

@dataclass(frozen=True, slots=True)
class State:
    id: BlockId

@dataclass(frozen=True, slots=True)
class Relationships:
    id: BlockId

@dataclass(frozen=True, slots=True)
class FieldReference:
    id: BlockId
    section: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class Sample:
    id: BlockId

@dataclass(frozen=True, slots=True)
class ActionList:
    id: BlockId
    actions: tuple["GuideAction", ...]

@dataclass(frozen=True, slots=True)
class TopicLinks:
    id: BlockId

GuideBlock = Overview | Teaching | AgentContract | InstanceList | State | Relationships |
    FieldReference | Sample | ActionList | TopicLinks

@dataclass(frozen=True, slots=True)
class TopicContribution:
    topic: TopicSlug
    title: str
    summary: str
    anchor: TopicAnchor
    blocks: tuple[GuideBlock, ...]
    related_topics: tuple[TopicSlug, ...] = ()
```

`FieldReference` and `Sample` were initially inert Phase 1 placeholders. The release-gate adapter in
`wave2-guide-adapter-lld.md` binds their now-authoritative declarative-schema services. They never
scrape another CLI renderer.

`ActionList` contains the same validated, inert `GuideAction` records used by onboarding assessment.
It is presentation data, not an executor. Every action states its precondition, named inputs,
consent boundary, expected state, optional verification command, and useful refusal alternative. A
command action supplies literal tokens; a manual action supplies bounded platform-neutral steps.
Rendering an action never authorizes or invokes it.

Semantic identity is `(topic, block.id)`, rendered in snapshots as an internal
`GuideBlockKey(topic, block_id)` and not emitted as a visible HTML comment. A block ID matches
`^[a-z][a-z0-9-]{0,62}$` and is unique within its topic. Reordering an unchanged block preserves
identity. Agent shaping may reposition `AgentContract` records, but the set of block keys and each
block's source payload must remain identical between modes. Renderer-added headings are not semantic
content and are normalized in parity snapshots. Every renderer-owned level-2 section begins with the
visible `⟦AGW framework⟧` provenance marker. The two marker delimiters are reserved across authored
titles, summaries, and static markdown after Unicode compatibility normalization and HTML entity
decoding, so authored content cannot emit the exact literal marker in raw CLI Markdown. This is a
source-provenance convention for raw output, not an anti-spoof guarantee for presentation by an
arbitrary downstream Markdown, HTML, CSS, image, or styling renderer. It grants no authority or
trust to the body that follows. Ordinary authored ATX and setext headings remain valid, retain their
source text, and never receive the framework marker.

## Strict construction and catalog errors

Decoded plugin or package data enters through `parse_topic_contribution(value, source)`. Parsing is
closed and recursive. It rejects:

- unknown fields at every record level;
- missing required fields, wrong scalar types, lists where tuples are required after normalization,
  blank titles or summaries, and non-string markdown;
- unknown anchor or block discriminators;
- functions, callable objects, import references, renderer names, and object instances;
- `{{`, `}}`, `${`, `<%`, `%>`, `{%`, or `%}` placeholder delimiters in authored markdown;
- either reserved `⟦AGW framework⟧` marker delimiter in an authored title, summary, or static
  markdown block, including an HTML-entity encoding of a delimiter;
- invalid slugs, IDs, anchor/topic mismatches, duplicate block IDs, and repeated related links;
- a dynamic block on an anchor that cannot support it;
- plugin ownership of `concept-*`, a bare kind, another plugin's namespace, or a resource not
  contributed through its registered owner adapter.

Markdown code fences and ordinary braces are allowed. The ban is deliberately syntactic and does not
attempt to evaluate or sanitize markdown. Renderers concatenate inert strings and never call
`format`, `format_map`, a template engine, markdown extensions, `eval`, or `exec`.

Errors use a guide-specific hierarchy under `ValidationError`:

```python
class GuideContributionError(ValidationError): ...
class InvalidTopicSlugError(GuideContributionError): ...
class InvalidAnchorError(GuideContributionError): ...
class InvalidBlockError(GuideContributionError): ...
class DuplicateTopicError(GuideContributionError): ...
class BrokenTopicLinkError(GuideContributionError): ...
class UnknownGuideTopicError(NotFoundError): ...
```

Every contribution error carries `source`, optional `topic`, and a field path such as
`blocks[2].markdown`. Messages never include authored markdown or configuration values. Unknown
request topics carry the requested slug and deterministic close matches from valid catalog names.

Catalog construction occurs only inside a guide or guide-completion request, after command parsing.
It has two stages:

1. Collect and strictly parse core, kind, implementation, and all installed system-plugin
   contributions without loading operator config or building a registry.
2. Batch-check ownership, canonical slug uniqueness, block uniqueness, and related links after all
   candidates are known.

The batch check makes collision handling independent of load order and never selects a winner.
Strict CI construction raises the typed contribution error for every trusted core, kind, or
implementation taxonomy, ownership, duplicate, or broken-link contradiction. Runtime construction
records trusted taxonomy drift as a deterministic scoped issue and retains unaffected topics;
trusted ownership, duplicate, and broken-link contradictions remain hard startup failures because
there is no safe winner. A malformed system-plugin contribution follows the fail-soft runtime path:
ownership-invalid candidates are rejected before collision grouping; a plugin candidate colliding
with a reserved or trusted topic is rejected while the trusted topic is retained; and every plugin
candidate in a plugin-to-plugin collision is rejected. Broken links invalidate only their source
topics. Issues are sorted by source, topic, and field path.

Catalog errors never occur at import time, so unrelated CLI commands still work. Visible issues
render in a "Guide content unavailable" section. A full index reports every rejected contribution
and exits 1 after producing complete markdown. An explicit retained topic reports only issues scoped
to that request and can exit 0 when unrelated content is invalid. `--names-only` returns only
retained names, suppresses issue prose, and exits 0 so completion remains useful. Catalog
construction has no global cache because installed package inventory is a request fact; enablement
is a later finalized-view fact and never controls collection.

Validation applies limits before catalog retention: title 256 bytes, summary 2 KiB, each static
block 64 KiB, total topic content 256 KiB, and at most 64 blocks and 64 related links. Selectors and
every related-topic value are bounded separately from that markdown total: a
`FieldReference.section` has at most 32 components of 256 bytes each, and every related topic is at
most 317 bytes.

An `ActionList` contains at most 32 actions and 128 KiB of cumulative action data, which also counts
toward the topic's 256 KiB limit. Action IDs are unique across every action block in one topic. One
action has at most 32 inputs, 64 command tokens, and 64 verification tokens. An input name is at
most 64 bytes; an action token is at most 1 KiB; an input description is at most 2 KiB; and each
precondition, expected state, refusal alternative, or manual instruction is at most 8 KiB. Every
string is copied into a new exact frozen `GuideAction` or `ActionInput`, and duplicate input names
are rejected. Raw mappings and contributor-owned object instances are never retained.

The `action-list` block parser accepts only `type`, `id`, and `actions`. Each action mapping accepts
only the `GuideAction` fields above; each input mapping accepts only the `ActionInput` fields.
`command` and `verification` are token sequences, while `manual_steps` is bounded inert prose, and
exactly one of `command` or `manual_steps` must be non-null. Programmatic records are first copied
into this closed decoded shape and then recursively parsed through the same path as plugin data.

Bare concept and kind slugs use one strict 63-character lower-kebab segment. `kind/name` uses that
strict kind segment plus the canonical resource-name grammar and its 253-character maximum.
Plugin-owned slugs use `plugin/<plugin>/<topic>` with two strict 63-character segments. The final
renderer strips every C0 control except line feed and tab, plus DEL and the entire C1 range, from
both authored and projected text.

## `GuideView`: denied powers and allowed facts

`GuideView` is not a wrapper exposing a registry. Its constructor is private and copies permitted
values into frozen records. Neither it nor any returned record retains a `Registry`,
`DependencyGraph`, database, resource declaration, `Origin` object containing a path, capability
implementation, transport, resolver, config, or callable.

```python
@dataclass(frozen=True, slots=True)
class GuideIdentity:
    kind: str
    name: str

@dataclass(frozen=True, slots=True)
class GuideOrigin:
    variant: Literal["operator-declared", "built-in", "auto-declared", "system-plugin"]
    plugin: str | None

@dataclass(frozen=True, slots=True)
class GuideVerdict:
    enabled: bool
    ready: bool
    reason: str | None
    is_available: bool = True

@dataclass(frozen=True, slots=True)
class GuideResourceFact:
    identity: GuideIdentity
    category: Literal["declarable", "capability"]
    description: str | None
    origin: GuideOrigin
    verdict: GuideVerdict

@dataclass(frozen=True, slots=True)
class GuideRelationship:
    source: GuideIdentity
    target: GuideIdentity
    usage: str

@dataclass(frozen=True, slots=True)
class GuideInstanceFact:
    kind: str
    name: str
```

`description` is untrusted operator or plugin data. Before Markdown rendering, guide normalizes it
to labeled plain text and neutralizes Markdown links, images, headings, HTML, and other block
syntax. It never shares the authored teaching style or placement.

`GuideOrigin` intentionally drops operator file paths, source identifiers, and auto-declaration
source tuples. `usage` is the already-materialized sanitized relationship label. Descriptions and
readiness reasons are operator-facing strings already used by inspection surfaces, never raw
configuration values.

The public API is exactly:

```python
class GuideView:
    def me(self) -> GuideResourceFact: ...
    def kind(self) -> GuideResourceFact: ...
    def instances(self) -> tuple[GuideInstanceFact, ...]: ...
    def inbound(self) -> tuple[GuideRelationship, ...]: ...
    def outbound(self) -> tuple[GuideRelationship, ...]: ...
    def inventory(self, root: GuideRoot) -> tuple[GuideResourceFact, ...]: ...
```

`GuideRoot` is a closed enum initially containing `KINDS` and `IMPLEMENTATIONS`. Concept topic
construction supplies one permitted root set. Resource, kind, and implementation views supply an
anchor-specific implementation object internally, but no method accepts arbitrary kind/name pairs.
Unsupported traversal raises `GuideTraversalError`. This is the `me` anchor enforcement, not a
convention in block resolvers.

`build_guide_view(anchor, registry, db)` is the sole composition boundary. It requires an already
finalized registry, reads `KIND_REGISTRY` metadata, registry rows, `graph.enablement_of`,
`graph.readiness_of`, `graph.edges_of`, and `graph.dependents_of`, and copies only the records
above. For instance inventory it calls the same optional kind-owned
`instances(db, registry, resource)` hook used by resource inspection, exhausts it during
construction, sorts facts by `(kind, name)`, and retains no hook or database reference. It never
calls `Registry.finalize()` or `graph.impl_of()`.

The builder accepts no resolver or run context. A structural API test enumerates public attributes
and recursively inspects returned dataclass fields, proving the forbidden objects cannot be reached.
Sentinel tests replace finalize, `impl_of`, resolver calls, transport construction, database writes,
and capability methods with raising spies. An end-to-end default-composition test starts before the
guide registry is built, and an architectural import-boundary test rejects direct or aliased imports
of probe, resolver, transport, mutation, and low-level filesystem-write powers into guide modules.
Rendering and view construction must not trigger them.

Expected missing-resource failures are translated at the exact registry lookup inside view
construction into `GuideTraversalError`, which makes only that topic unavailable. `KeyError` from a
graph method, an inventory hook, or another programming mistake is not caught by the service layer.

## Broken configuration and unavailable facts

Every full guide request follows this order:

1. Build the authored catalog independently of operator configuration.
2. Validate only request syntax and repeated-slug normalization. Invalid syntax produces no output.
3. Attempt `load_config()` and one guide-scoped registry build. This build uses the normal
   declaration, publication, materialization, validation, finalization, and freezing path with host
   readiness checks disabled. It preserves probe-dependent readiness as unavailable rather than
   converting it to not-ready. Ordinary command registry builds remain unchanged.
4. On success, derive dynamic bare-kind and `kind/name` topics from the finalized registry, combine
   them with authored topics, and remove any dynamic identity already owned by a retained authored
   topic. Then validate existence for the entire request atomically. Any truly unknown topic
   produces no output. A requested topic rejected into a scoped runtime catalog issue retains its
   position as an unavailable topic rather than becoming unknown. Only after that check does
   rendering begin.
5. On `ConfigError` or another framed `AgentworksError` from load or finalize, classify requested
   slugs without pretending the failed registry exists. Authored topics can still render. A
   syntactically valid bare registered-kind slug or `registered-kind/name` slug is a dynamic request
   and receives an unavailable topic record. A slug whose kind is not in the code-owned kind
   registry remains unknown. Validate this degraded request atomically, then render authored and
   unavailable dynamic topics in requested order, or emit nothing if any slug is unknown.

The markdown includes the same framed error message and hint the CLI would show, without traceback
or raw configuration. It exits 1 because live facts were requested but unavailable. No output begins
before atomic existence validation, and no partial dynamic facts from a failed build are retained.
Catalog issues and system failure are independent sections. Completion uses the same attempt but
degrades to retained authored names plus code-owned bare kind names and exits 0.

`GuideVerdict` preserves whether readiness was available. An unavailable verdict renders as
unavailable and assessment classifies it as `unverifiable`; it must not trigger a doctor action as
if a host check had actually failed.

Missing configuration is broken configuration, not a cue to create it or prompt. Guide code never
changes interactivity, catches a prompt, substitutes a prompt backend, or retries with a reduced
registry. A test installs a prompt handler that raises and proves both successful and broken view
construction leave it untouched.

## Inert onboarding actions

Actions are content records, not an execution API:

```python
class ConsentBoundary(Enum):
    NONE = "none"
    READ_CONFIGURED_STATE = "read-configured-state"
    EXAMINE_WORKSTATION = "examine-workstation"
    RESOLVE_NAMED_SECRET = "resolve-named-secret"
    CONNECT_NAMED_VM = "connect-named-vm"
    MUTATE_AGENTWORKS = "mutate-agentworks"

@dataclass(frozen=True, slots=True)
class ActionInput:
    name: str
    description: str
    required: bool
    sensitive: bool = False

@dataclass(frozen=True, slots=True)
class GuideAction:
    id: ActionId
    precondition: str
    required_inputs: tuple[ActionInput, ...]
    consent: ConsentBoundary
    command: tuple[str, ...] | None
    expected_state: str
    verification: tuple[str, ...] | None
    refusal_alternative: str
    manual_steps: str | None = None
```

Exactly one of `command` and `manual_steps` is present. Command actions retain the closed token
grammar below. Manual actions describe a bounded, platform-neutral file or configuration operation
whose exact scope comes from registered inputs. They do not smuggle a shell fragment into prose and
the guide never chooses an editor, file-copy tool, or platform-specific command for the operator.

Command tokens are literals or exact `$INPUT_NAME` substitutions validated against
`required_inputs`. Literal tokens use a closed grammar: an alphanumeric first character followed by
only alphanumeric, dot, underscore, colon, slash, or hyphen, with a separate lower-kebab option
grammar. Shell operators, substitutions, redirects, expansions, glob syntax, comments, grouping,
environment assignments, whitespace, and unregistered placeholders are rejected. Sensitive input
values are never interpolated into rendered commands. Such actions instruct the operator to use the
verification surface's ordinary secure input boundary instead.

Assessment returns `done`, `not-ready`, `disabled`, or `unverifiable` plus an ordered tuple of
action IDs. It consumes only `GuideView` facts and explicit caller inputs. There is no onboarding
table, marker file, raw-config inspection, doctor invocation, or action executor. Guided and
replayable rendering select the same records. Refusal maps to `unverifiable` and the same manual
alternative in both paths.

The CLI accepts repeatable `--evidence ACTION_ID:KIND/NAME=OUTCOME` values, where `OUTCOME` is
`verified`, `failed`, or `refused`. Parsing is strict and atomic. Parsed target-scoped evidence is
the public replay boundary into rendering; Agentworks does not store it. The same evidence tuple
must produce the same findings and ordered action records in human and agent modes.

## Agent-mode signature inventory and precedence

`select_guide_mode(explicit, environ, stdout_isatty)` uses this exact precedence:

1. Explicit `--agent` or `--human` wins. Typer exposes them as one paired option and rejects both.
2. Any registered exact harness execution signature selects agent mode.
3. Otherwise TTY stdout selects human mode; piped or redirected stdout selects agent mode.

The Phase 1 signature registry contains only `HarnessSignature("claude-code", "CLAUDECODE", "1")`.
Claude Code uses `CLAUDECODE=1` in tool subprocesses as its nested-session execution marker. Match
the exact value, not mere presence. Codex's public contract still supplies no stable subprocess
execution marker, so Codex registers no signature in Phase 1 and its bootstrap invokes
`agw guide concept-onboarding --agent` explicitly.

Rejected signatures include `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_SESSION_ID`, `CODEX_HOME`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, provider selectors, model selectors, and any secret or
configuration variable. They identify configuration, credentials, or sometimes a session, not the
unambiguous fact that the current process was launched as an agent tool call. Detection does not
inspect parent processes, executable names, session files, terminals, or the filesystem.

Tests cover both explicit overrides against every other signal, exact and near-miss Claude values,
Codex-like configuration environments remaining unsigned, TTY fallback, redirected fallback, and
especially `--human` with piped stdout.

## Verification surfaces

HEAD has prediction but not proof. `agw secret describe` builds a non-resolving backend preview;
doctor repeats prediction and checks `ssh`, `scp`, and `tailscale`; registry finalization stores
cheap host-readiness verdicts; VM lifecycle and ordinary execution reach SSH only while performing
another operation. There is no named-secret proof command and no dedicated non-mutating named-VM
connection command.

Phase 1 adds service functions below Typer and thin commands:

```python
@dataclass(frozen=True, slots=True)
class SecretVerification:
    name: str

def verify_named_secret(
    config: Config,
    registry: Registry,
    name: str,
    *,
    interaction_policy: SecretInteractionPolicy = SecretInteractionPolicy.NON_INTERACTIVE,
) -> SecretVerification: ...

@dataclass(frozen=True, slots=True)
class VMConnectionVerification:
    name: str
    transport: str

def verify_vm_connection(
    db: Database,
    config: Config,
    registry: Registry,
    name: str,
) -> VMConnectionVerification: ...
```

`agw secret verify NAME` verifies one registered secret through one normal resolution pass and
backend precedence. The explicit boundary first calls `active_backends(config, registry)`, then
wraps each provider behind a verification-only adapter identified by the caller-known registered
backend key. Ordered resolution reads and snapshots a provider's interaction classification only
when it reaches that backend, behind the sanitizing boundary. Default non-interactive filtering uses
only that snapshot, so a raising or stateful provider property cannot leak or change policy between
filtering and execution. A later backend is never inspected after an earlier one succeeds.
`ActiveBackend` carries that registered key from the config-chain lookup alongside the capability
and stored readiness. Neither the verification adapter nor diagnostics recover identity by reading a
provider-owned `name` property. Verification accepts readiness only when its exact type is the
core-owned `Readiness` record and copies its safe fields; a provider-authored subclass or substitute
is rejected without property access.

HEAD's `resolve_secrets()` combines the ordered backend algorithm with `output.warn` readiness
messages and `output.info("Resolved ...")` progress. Verification must not call that emitting
wrapper. Extract its loop without semantic changes into the private lower-level seam
`_resolve_secrets_ordered(secrets, backends, *, errors, registry, reporter, interactive_available)`.
`resolve_secrets()` calls it with an `_OutputResolutionReporter` and the current
`output.is_interactive()` value, preserving every existing caller's output and early-doom behavior.
The new verification-only `resolve_secrets_quiet()` calls the same seam with a
`_QuietResolutionReporter`, which discards readiness-skip and resolved-backend events, and passes
the explicit consent policy as `interactive_available`. `verify_named_secret()` invokes
`resolve_secrets_quiet([decl], backends, registry=registry, interactive_available=allow_interactive)`
exactly once. Neither reporter receives resolved values.

This path does not redirect, suppress, or mutate global output interactivity, config, the backend
registry, or the resolver. The service copies no resolved value into its success-only result and
never returns, logs, or formats a value. Backend exceptions and every provider-authored field
crossing this boundary are replaced by caller-known identity plus fixed safe prose, without
retaining the provider exception as a chained cause. First-party resolution diagnostics outside the
provider boundary keep their typed category and actionable explanation. `--allow-interactive` is the
only opt-in. The CLI translates it to the closed `SecretInteractionPolicy` enum. The service owns
the invariant and rejects `ALLOW_INTERACTIVE` while global output policy is non-interactive, so
non-Typer callers cannot bypass it. The consent is named explicitly in the action's boundary. On
success the command emits exactly one line, `Secret '<name>' verified.`. Normal framed secret,
mapping, connectivity, and configuration categories pass through with sanitized text.

`agw vm verify-connection NAME` loads the named stored VM row, resolves its declared site and the
canonical non-activating admin transport with `agentworks.transports.transport(vm, config)`, then
runs the transport's smallest no-op command (`true`) with an explicit bounded verification timeout
and without a TTY, sudo, environment injection, or workspace. It does not retry or call activation,
observe/start, readiness repair, rekey, reinit, or any database write. A stopped target therefore
fails as connectivity, rather than being started. Success reports only the VM name and transport
kind; failure uses the existing `NotFoundError`, `StateError`, or `ConnectivityError` framing.

Neither verification operation is callable from `GuideView`, a dynamic block resolver, assessment,
catalog construction, or rendering. Tests use spy backends and transports to prove secret values do
not cross the service boundary and the VM check performs exactly one no-op run with no power or
mutation call.

## Verification matrix

Phase 1 is complete only with these focused proofs:

- contract parsing rejects every unknown field, executable object, expression delimiter, invalid
  taxonomy claim, duplicate block ID, duplicate topic, broken link, overlong authored field,
  over-count block/link/selector collection, overlong selector component or related slug, and
  malformed related slug;
- trusted taxonomy, ownership, duplicate, and broken-link contradictions fail strict CI
  construction; runtime construction isolates taxonomy drift as a visible issue and retains
  unaffected topics while the other trusted contradictions remain hard startup failures; a full
  index with isolated issues exits 1 and an unrelated explicit retained topic renders cleanly with
  exit 0; plugin collision and link invalidation stay deterministic under reversed contribution
  order;
- installed-wheel package-data tests load every authored block under the normal CI marker selection;
- human and agent snapshots have equal semantic block keys and payloads despite allowed heading and
  placement differences;
- every renderer-owned level-2 section carries the reserved visible provenance marker, ordinary
  authored headings remain unmarked, and adversarial contributions cannot reproduce the marker;
- atomic multi-topic lookup emits nothing on any unknown slug and deduplicates repeated valid slugs
  at first position;
- the public `GuideView` surface and returned-record graph contain no denied power; a default-path
  composition test begins before registry construction; an import-boundary proof rejects direct and
  aliased denied powers; and raising spies prove no finalize, prompt, resolve, capability call,
  probe, transport, or low-level write occurs;
- missing registry lookups become scoped topic failures while unrelated `KeyError` from graph or
  inventory code escapes as a programming error;
- every forbidden C0/C1/DEL byte is removed from authored and projected output while line feed and
  tab are preserved;
- broken-load and broken-finalize fixtures render authored content, one framed error, and an
  unavailable marker for every dynamic block, with no partial live facts;
- onboarding scenarios prove guided and replayable paths choose the same action IDs and yield equal
  ready, disabled, not-ready, and refusal-driven unverifiable outcomes;
- mode-selection tests pin explicit, signature, then TTY precedence and piped `--human` behavior;
- named-secret tests cover success, miss, mapped miss, sanitized backend failure, default
  interactive filtering, explicit interactive consent, one shared ordered resolution pass, unchanged
  global interactivity, raising and stateful provider properties, a secret-bearing provider name,
  one snapshotted interaction classification, caller-known registered identity, and absence of
  secret bytes from outputs, records, logs, and errors; successful verification captures stdout and
  stderr and proves the sole emitted line is the verification success line, with no readiness or
  resolved-backend progress;
- ordered verification tests prove a later backend's policy and readiness remain untouched after an
  earlier success, an excluded interactive backend's readiness remains untouched, and a
  provider-authored `Readiness` subclass is rejected without reading its properties;
- VM connection tests cover success plus missing, stopped, unreachable, and bad-transport cases, and
  assert no start, repair, rekey, reinit, secret prompt, or database mutation.

## Contract risks discovered at HEAD

VM verification must construct the existing canonical, non-activating admin transport directly with
`agentworks.transports.transport(vm, config)`. That factory only builds the Tailscale SSH transport
and never observes, starts, or repairs the VM. The verification service must not reuse an ordinary
`vm exec` body or activation gate.

The SSH logger's redaction set is immutable and complete before its first write. Lima provisioning
therefore registers the Tailscale key at logger construction. Local provisioning streams the
secret-bearing template to `limactl create` on standard input and never writes a local template
file. Remote provisioning atomically creates and validates a high-entropy private directory on the
VM host, streams the template into a mode-0600 file inside it through the bare SSH standard-input
boundary, and suppresses remote output that could reflect that input.

After every remote create attempt, provisioning recursively removes the private directory and
verifies its absence, retrying only expected transport, operating-system, and interrupt failures. A
typed cleanup error names only the validated residue path and safe manual-removal action when
removal cannot be confirmed. That credential-residue risk takes precedence over the provisioning
failure without chaining potentially secret-bearing provider or transport text. Otherwise the
original failure or `KeyboardInterrupt` survives logger-close and cleanup failures. The separate
partial-instance rollback kills detached work, removes its `.out`, `.sh`, `.pid`, and `.status`
artifacts, then deletes the failed instance. Tests pin both cleanup layers for ordinary exceptions,
adversarial filesystem entries, and `KeyboardInterrupt`.

The current kind-owned `instances` hook receives the full registry and resource object. It is safe
only while called inside `build_guide_view` and eagerly reduced to `GuideInstanceFact`. Passing the
hook, registry, or resource onward would turn the projection into ambient authority.

`CLAUDECODE=1` is observable and used as Claude Code's nested-session marker, but it is less formal
than a dedicated documented subprocess-signature guarantee. Keep the signature in a small tested
registry so it can be removed without changing rendering contracts if Anthropic changes it. Codex
has no acceptable signature at this design point, so explicit `--agent` remains part of its
bootstrap contract.
