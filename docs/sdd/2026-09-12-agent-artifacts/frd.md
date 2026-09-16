# Agent artifacts: functional requirements

## Status

Approved requirements (2026-09-13). This effort builds on
[harness-scope-framework](../2026-09-06-harness-scope-framework/frd.md) and participates in the
[next-steps saga](../2026-08-04-next-steps/target-state.md). The operator approved the
first-delivery scope and the HLA. The 2026-09-15 operator ruling below supersedes the original
automatic session delivery and final-session launch-error policy. The [plan](plan.md) tracks
implementation and acceptance.

## Problem and intended outcome

Agentworks can configure harnesses at VM, user, workspace and session facets, but it cannot yet
collect and deliver agent artifacts through those facets. An **artifact** is a logical unit of
content supplied to a harness: a hint, rule, skill or agent persona. It may comprise several files.
Some artifacts belong in model context; others must be available for use when needed.

This effort addresses two related needs:

1. Operators need to bring reusable artifacts into Agentworks, combine them across its resource
   scopes, and use them with different harnesses. Integration developers translate the common inputs
   into native representations; operators should not maintain a separate copy per harness.
2. Agentworks functionality needs to tell workloads what it provides. Core setup and extension
   surfaces should be able to emit facts such as "mise is available", "gh is authenticated with a
   GitHub App", or "XYZ holds the API key for foobar", without knowing each harness's files or
   prompt format. These hints describe availability, not the secret values themselves.

Both needs use one artifact pipeline. The first delivery introduces explicitly declared bundles;
automatic emissions from other core functionality and, later, features will enter that same
pipeline. Operators should receive the relevant supplied and generated artifacts in the appropriate
context or native discovery mechanism. Core preserves origin and routes unresolved inputs; each
harness integration owns native placement. Acquisition choices converge before propagation, without
duplicate delivery or implicit integration activation.

## Relationship to the completed implementation

PR 761 merged as `7c744828184ccb0ad9ffd90a8a02226384fb384e`. It supplies explicit integration
activations, facet configuration, owner-bound setup, native settings/plugin reconciliation, session
prerequisites, and instance-state storage. It deliberately supplies no artifact declaration,
normalized bundle, propagation, deferral, or publication mechanism.

This effort will extend that implementation and its facet interfaces. Locking the predecessor SDD
freezes its historical specification, not the code it produced. New behavior and any changed
contracts belong in this SDD and in permanent documentation alongside the implementing change.

The predecessor remains open for its retained live acceptance and cleanup obligations; see its
[acceptance plan](../2026-09-06-harness-scope-framework/plan.md#acceptance-and-closeout). This
effort neither waives those obligations nor transfers them silently. Artifact design can proceed
against the merged foundation while predecessor acceptance is completed independently. Future
artifact work is not itself a reason to keep the predecessor unlocked.

## Users

- Operators who want their workloads to receive all relevant artifacts, both those they supply and
  those their Agentworks setup emits, within the intended user and session boundaries.
- Harness integration developers who translate common artifacts into native harness representations.
- Agentworks developers who expose core or extension functionality to workloads through artifacts
  without writing harness-specific delivery logic.

## Design constraints

These constraints build on the predecessor's
[successor context](../2026-09-06-harness-scope-framework/frd.md#future-artifact-context-not-this-efforts-contract).

### Artifact types and acquisition boundary

Harnesses use several forms of supplied content: guidance loaded into context, reusable procedures
with supporting files, and definitions for specialized agents. Portable formats and generation tools
reduce the need to maintain these separately for every harness. The
[Agent Skills standard](https://agentskills.io/home), originally developed by Anthropic and released
as an open standard, packages reusable instructions and resources for on-demand use.
[Rulesync](https://github.com/dyoshikawa/rulesync) manages common source files and generates
tool-specific configuration, including rules, skills and subagents.

Agentworks builds on these ideas with delivery governed by its resource scopes and integration
facets. Its artifact types distinguish contextual facts from persistent guidance and reusable
capabilities. The same types serve explicitly supplied content and future automatic emissions:

Hints are small contextual facts, such as the availability of an environment variable or configured
GitHub authentication. They remain distinct from rules, skills and a session's initial prompt. A
producer needing stronger behavior can emit a rule or skill. A rule is guidance always loaded into
context wherever that rule applies. A rule accepts plain Markdown or optional YAML frontmatter
containing only a nonempty `description`; the artifact map key is its name. The description is
inspection metadata, separate from the always-loaded body. A skill is standard
[Agent Skills](https://agentskills.io/specification) content: a directory with a `SKILL.md` entry
document and any supporting files, preserving the standard's metadata and progressive disclosure
model. Loading a rule does not guarantee obedience; publishing files for shell retains the delivery
limitation described below.

**Agents** is shorthand for **agent personas**: reusable definitions containing behavioral
instructions and supported execution settings. They can serve primary or delegated agents where the
harness supports it; see the [terminology research](prior-art-research.md). In artifact discussions,
an agent means this definition. Where resource ownership could be ambiguous, say **agent persona**
for the artifact and **agent resource** for the Agentworks resource. Native support for primary and
delegated use must be stated per integration; the name does not promise both in every harness. The
model also leaves room for limited hooks and MCP configuration later.

Rule and agent frontmatter must reject unexpected fields, invalid field types, duplicate keys and
non-string keys rather than silently ignoring them. Agent frontmatter accepts only `name`,
`description` and optional `native_options` keyed by integration; each integration validates its own
supported options. Rules do not accept conditional activation, target selection or placement fields.
Captured descriptions appear in artifact inspection without fetching sources or disclosing bodies.
Existing captures retain their recorded meaning until an explicit owning refresh; fresh capture uses
the current authoring contract.

An **artifact bundle** is a reusable collection with separate `hints`, `rules`, `skills` and
`agents` maps. These are top-level fields of the bundle's `spec`; the containing map supplies the
artifact type. Each key is the artifact's canonical name. Skill and persona definitions must agree
with that name, and a skill's selected package directory must also satisfy the Agent Skills naming
contract.

Workstation files, Git references and potentially packaged distributions are acquisition concerns.
Once acquired, all sources enter one normalized representation; integrations do not carry separate
filesystem, Git and archive ingestion paths. Normalize text line endings to Unix LF. Preserve
complete skill packages, including their supporting files, rather than reducing a skill to its entry
document.

A declarative `artifact-bundle` resource owns ingestion and gives consumers an ID to reference. The
HLA selects workstation and Git sources with capture during owning setup operations; detailed field
validation and codec definitions belong in the LLD. Evaluate Rulesync's canonical model and
generation machinery for reuse; reuse is not an approved runtime dependency. Agentworks retains
ownership of resource scopes, activation, routing and provisioned-resource lifecycle.

### Acquisition safety carried forward

The predecessor designed concrete acquisition safeguards before artifacts were deferred. They remain
design inputs for this successor, recovered in
[prior-art research](prior-art-research.md#recovered-acquisition-safety-design). Preserve these
safeguards when designing bundle ingestion; the historical declaration syntax and refresh schedule
are not selected by carrying them forward.

- Capture consistent content before native writes. Resolve each Git repository/reference to an
  immutable commit for a capture operation, and read its selected members at that revision. Keep
  credential-free source identity, requested reference, selected path and resolved commit as
  provenance. Integrations receive captured content, not source references to fetch themselves.
- Acquire on the workstation with its existing authentication. Do not execute repository hooks,
  checkout filters or bundled scripts during acquisition, apply export substitutions, or omit
  export-ignored members. Credentials must not enter declarations, persisted provenance or
  diagnostics. Guest authentication is not an acquisition input.
- Preserve the complete selected package, including relative paths and executable-file intent from
  file modes. Reject selected submodule entries and unresolved Git LFS pointers instead of claiming
  a complete package. Git acquisition does not export `.git`; a local package containing Git
  metadata is rejected rather than silently trimmed.
- Select skill roots explicitly and preserve their standard metadata and supporting tree. Refuse
  links, special files, absolute or escaping member paths, and portable-path collisions. Validate
  package content without recursively discovering and installing unrelated skills.
- Normalize designated text to UTF-8 and Unix LF, including CRLF and lone CR, before validation,
  content identity and delivery. Never rewrite sources. Supporting members use a closed text
  classification; successful UTF-8 decoding alone is insufficient. Unknown formats, including an
  ASCII-only PDF, remain opaque bytes. Preserve byte-sensitive supporting fixtures even when their
  suffix is recognized as text; `SKILL.md` itself must retain its required text normalization.
- Content identity includes normalized bytes, member paths and executable intent, not timestamps or
  acquisition provenance. Equivalent local and Git content must not cause a native rewrite merely
  because its source changed. Persisted content must round-trip text and opaque bytes.
- Bound capture time, storage, member count, individual and total size, and traversal depth. Reject
  observed local mutation during capture, clean temporary acquisition storage on success and
  failure, and never pass stale content off as a successful new capture.

The design must make the byte-preservation control concrete. The predecessor called it
`preserve_bytes`; its spelling and location in bundle configuration remain open. The historical
classifier and the failure cases it protects are retained in the research so they are not lost while
the new ingestion interface is designed.

### Scope, facet and placement

The predecessor established this vocabulary. The permanent
[capability contract](../../../cli/agentworks/capabilities/README.md#stage-1-declare) defines a
facet generally; the
[harness integration contract](../../../cli/agentworks/capabilities/harness_integration/README.md#a-note-on-scope)
defines the specific harness facets and their owners.

A scope identifies an owning resource and its context. A facet is a scoped part of a capability.
Harness integrations use VM, user, workspace and session facets; admin and agent scopes use the same
user facet for their respective actual users. The admin user is not an ancestor of an agent. User
and workspace are separate ancestors of a session, not a nested pair.

Native placement at the defining scope is the ordinary case. The integration decides whether an
inner invocation needs anything further. Core attaches immutable origin: owning scope, resource
identity and producer. Core derives the origin facet from its fixed scope-to-facet mapping;
declarations cannot supply another facet of origin. Bundle identity and consumer origin are separate
facts.

### Names and composition

Each actual owning scope has its own set of artifact type maps. An artifact name is shared by all
producers of that type at that scope. Bundles and producing resources do not introduce additional
namespaces. A rule and a skill can share a name, and different scopes can independently contribute
the same type and name. Producers should choose meaningful names to avoid unintended replacement.
Producer and bundle information remains provenance rather than part of the logical namespace.

Within one scope, later selected bundles replace earlier definitions with the same type and key.
Replacement applies to the complete definition, not individual fields or skill package members.
Bundle inheritance likewise combines type-map keys and replaces an overridden entry as a whole
through ordinary resource resolution. Definitions discarded during inheritance are not acquired.
When composing selected, resolved bundles, inspection must identify the winning source and the
sources it replaced. Changed replacements produce a concise warning; identical content does not
warn. The HLA defines deterministic ordering.

Deferral preserves this separation. A receiving facet gets its local type maps and distinct deferred
groups identified by their original owning scopes. It must not merge incoming groups into its local
maps or relabel an artifact with the scope through which it passed. A VM artifact deferred through
the user facet remains VM-originated at the session. Different scopes have no implicit override
precedence, including the user and workspace branches. Core preserves their contributions; the
integration selects faithful native placement or aggregation and reports an unsupported combination
when native mechanisms cannot represent it without loss or unintended effects on other scopes.

### Operations and outcomes

- **Produce** means introduce artifact inputs through explicit declarations and, later, emissions
  that are a side effect of other functionality. Bundle declarations explicitly acquire artifacts;
  core functionality and future features can emit them while doing their own work. A facet does not
  produce artifacts; it handles, applies or defers inputs. Translating inputs into native files is
  application and preserves their origin.
- **Apply** is the one idempotent operation that brings owned native effects into agreement with
  current inputs, including creation, updates and cleanup where possible. Reconciliation describes
  that behavior; it is not a separate operation or lifecycle stage.
- **Handle** means fulfill an input's delivery obligation for the applicable owner so it needs no
  further propagation along that path. It is an outcome, not another mutation operation.
- **Defer** means leave an input unhandled for a later facet, with its reason and route. A route
  selects the next facet; placement is the native destination. Neither changes the input's origin.

Applied state is the existing instance-state record of provisioned configuration and owned effects.
It supports idempotent application; successful setup alone does not prove artifact handling.

### Routing and inactive integrations

An activated integration facet can apply inputs and return what it defers, with reasons and intended
next facets. A VM facet can route to user, workspace or directly to session. It chooses solely from
its own inputs and responsibilities, without discovering downstream instances or checking whether
those descendants activate the integration.

Each owner calculates its result independently for its current inputs. Later consumers reuse it;
creating another user, workspace or session does not rerun the ancestor facet or consume its result
globally. A user facet processes applicable VM inputs without consulting workspaces or sessions. The
session combines direct VM input and applicable user/workspace results. Handling for one user or
workspace cannot discharge another's obligations.

**Without a VM integration activation, core routes its defined inputs to the user facet.** This
default selects one branch; it does not broadcast inputs to the workspace. An inactive user or
workspace facet passes its applicable routed and local inputs to the session. Resolution can be lazy
for the session's selected integration and actual ancestors; agent initialization need not enumerate
inactive integrations or store empty passthrough results for them. Origin survives every skipped
facet.

Applying an artifact updates the handling facet's native locations during its setup. Live reload by
an already running workload is harness-specific and is not guaranteed. Deferral records inputs for
later application; it does not refresh existing descendants. Owning setup must warn when it defers
inputs, including inactive-owner passthrough of locally captured inputs, without enumerating or
mutating descendants. User-routed updates require that actual user's setup; session-routed updates
require a subsequent managed start or restart after stale ancestor setup is resolved. Workspace
setup runs only on creation: there is no workspace reinit, and neither repair nor an upstream reinit
can refresh an existing workspace's applied artifacts. Diagnostics must describe that limitation
without suggesting unsupported workspace operations. Removal follows the same descendant timing.

An explicitly activated but unimplemented facet is an error. That is distinct from an inactive facet
and from an implemented facet that needs no native changes. Successful setup alone never means an
artifact was handled. The approved result contract lists what remains deferred; a successful
application reports other inputs as handled. The integration must fulfill that delivery obligation
through its supported native mechanism, which can include launch arguments without a published file.
Core validates the result and warns for every final-session deferral with its source and reason. An
integration silently dropping an input violates the handling contract; omission from the deferral
list does not independently prove native consumption. No per-item acknowledgment ledger or
file-or-deferral coverage requirement is added.

### Pipeline and lifecycle

The required order at each facet is **core, then harness integrations**. Core prepares env and
artifacts before an integration runs; both flow downstream from where they are declared. Each
integration receives the completed applicable env and its local artifacts plus applicable ancestor
artifacts still needing handling. Artifact routing respects the separate user/workspace branches and
the inactive-facet rules above; artifacts do not acquire env's override semantics.

The planned extension is **core, then features, then harness integrations**. Features will consume
the env available to them and may add env and artifacts before the integrations run. Core emissions
will use the same artifact input boundary as explicit declarations. This effort establishes that
boundary; implementing automatic hints from other core functionality and executing features are
follow-on work. The future user feature capability is named `user-features` for both admin and agent
users.

Integrations must be explicitly activated, including defaults-only configuration. Applying artifacts
must respect the selected owner's actual identity and native placement. Session handling must not
silently provision shared user or workspace state. Existing instance state supports idempotent
application and cleanup of previously provisioned effects wherever ownership and native tooling make
that possible. Parent deletion retains the existing core lifecycle; artifact bookkeeping must not
become a prerequisite for deleting a VM and everything within it.

A session runs as an existing Linux user in a workspace. Both the user and the workspace may be
shared with other sessions; a session has no separate filesystem or private home of its own.
Sessions of the same agent resource already share access through that Linux user. A directory named
after one session cannot create a security boundary between those sessions.

Session-specific artifacts therefore belong beneath the actual user's home in an
`.agentworks-artifacts` area with a session component. This adds no new access for the same user's
other sessions. Placing those files in a shared workspace would instead expose them to other agent
resources with access to that workspace, even when the path includes a session name. Native delivery
must still ensure that session-specific content takes effect only for the intended workload. The
exact layout and identity/reuse semantics belong in the design.

The shell integration publishes artifacts as files with a documented discovery contract. File
publication must not be described as proof that a shell workload interpreted or obeyed the content.

For shell, successful publication and discoverability fulfill delivery. For a model harness, the
native delivery mechanism must provide the artifact's promised loading or availability semantics.
The support matrix must state these handling criteria per integration and artifact type, so the
shared outcome **handled** does not imply identical consumption behavior. Neither outcome proves
that a workload obeyed the content.

## First delivery scope

The following requirements are approved. The HLA and LLDs define their implementation.

**R1. Declare and consume bundles through ordinary resources.** Introduce `artifact-bundle` and an
`artifacts` block on the appropriate owning resources, using ordinary resource references,
inheritance, validation and discovery. Multiple consumers can reference a bundle without sharing
completion or placement state. Defining a bundle alone does not install it or activate an
integration. The bundle has top-level per-type maps and supports whole-entry replacement within the
consuming scope, with no namespace for the supplying bundle or producer.

**R2. Acquire workstation and Git sources.** Support workstation files/directories and Git sources
in the first delivery. Build on existing dotfiles source conventions and shared workstation source
handling where they meet artifact capture requirements; evaluate and extend those common parts
before introducing another acquisition path. The
[reuse analysis](prior-art-research.md#existing-source-and-inspection-boundaries) identifies why
dotfiles' guest checkout/install behavior cannot itself serve as artifact capture. Define how a Git
reference selects a revision and package root, when source changes are captured, and how an operator
deliberately updates captured content. A setup operation uses consistent captured inputs. Errors
identify the source and consumer without revealing credentials or content. A packaged distribution
source remains a later extension unless research shows an existing format makes it a small,
justified addition.

**R3. Normalize hints, rules, skills and agents.** Carry these four artifact types through one
internal representation using per-type maps grouped by actual owning scope, with source and
replacement provenance and complete supporting files. Preserve binary assets while normalizing text
line endings. Acquisition must reject package paths that escape the declared boundary; the design
must state link and file-type handling. Hooks and MCP configuration are recognized extension
directions, not first-delivery functionality.

**R4. Apply and defer through explicit facets.** Implement the carried-forward owner-independent
routing model, including lazy inactive-facet passthrough, routing to user when VM activation is
absent, and convergence of user/workspace paths without duplicate delivery. Preserve origin and
destination applicability separately. Keep local maps separate from deferred groups throughout
propagation. Apply the within-scope replacement contract without losing same-named contributions
from distinct scopes or creating precedence between sibling scopes.

**R5. Publish honestly through the shipped integrations.** Specify an artifact type and facet
support matrix for shell, Claude Code, Codex and Grok Build. Implement native delivery where the
harness has a suitable mechanism, and explicit unsupported outcomes where it does not. Do not infer
capability merely from a similarly named harness setting or from files being present. Include worked
examples for native placement, deferral, inactive ancestors and two concurrent sessions. The matrix
must identify any first-delivery limitation before the HLA is approved. State the evidence that
counts as handled in each case, including shell's file publication and discoverability and the
appropriate native loading or availability contract for rules, skills and agents.

Native precedence must not silently discard a contribution from another scope. In particular, a
workspace skill must not hide a same-named user skill that the integration promises to deliver.
Integrations must account for the native identities and discovery locations of local, deferred and
already applied ancestor contributions, including existing native entries that can shadow managed
artifacts. Use faithful aggregation, non-conflicting placement or a supported native namespace;
otherwise refuse the combination with the competing origins and native identity. A successful file
write or same-scope replacement rule does not authorize cross-scope native shadowing. Perform checks
using the actual owners available at that lifecycle stage; the session validates the joined user and
workspace context without making outer scopes inspect their descendants.

**R6. Give hints an appropriate representation.** Integrations may aggregate small hints into a
native rule or include them in launch context when supported. Prefer one clearly named
`agentworks-hints` rule-like file per native destination where a file is appropriate, rather than a
separate rule for every hint. The representation must preserve hints from all applicable scopes
without native filename or identity precedence dropping a group's contribution. Keep provenance and
lifecycle clear without manufacturing one rule per fact. Hints are supplied through the artifact
mechanism; automatic emission by install commands, env declarations or features is separate future
work.

**R7. Keep sessions and owners isolated.** Session publication under a user's home must respect that
user's access boundaries and must not unintentionally affect another session in the same workspace.
Define cleanup and replacement behavior for session restart, deletion and name reuse. A session
consumes applicable ancestor results without repairing ancestor setup implicitly. Use the saga's
[session identity contract](../2026-08-04-next-steps/scope-participation-contract.md#session-and-run-identity)
when designing durable session ownership. It distinguishes `session_uuid` from per-incarnation
`run_id`; neither the reusable session name nor VM `boot_id` is a substitute. Verify implementation
availability and coordinate missing ownership with the saga lead before the HLA depends on it,
rather than inventing an artifact-specific identity. This effort implements the permitted early
identity slice before artifact publication depends on it.

**R8. Apply idempotently.** Repeated setup converges. Removing a reference, changing a bundle,
removing an activation or deleting an owning resource has an explicit outcome for previously
provisioned artifacts. Remove owned effects where safe; retain and diagnose ambiguous or unowned
content. Preserve recoverable progress after failure through the existing instance-state facility.
Avoid a new universal reconciliation engine or restoring the predecessor's rejected per-key settings
ownership ledger.

**R9. Explain unresolved delivery.** Diagnostics identify the selected integration, original owner,
artifact and the reason it remains unhandled. Final-session inputs are optional: warn when an input
cannot be handled, including the applicable enabled-workaround choice when one exists, and continue
launch without claiming delivery. Preserve the terminal unhandled result for inspection. Malformed
input, unsafe publication and ambiguous ownership remain errors.

Provide **`agw artifact show`**, following the scope-selection conventions of `agw env show`, to
explain artifacts for a VM, an admin or agent user, a workspace, or a session. Include local inputs
and all applicable parent scopes by default. Resolve the selected owner's actual ancestors: a
session includes its VM, actual user and workspace; a workspace does not acquire a user ancestor,
and an agent user does not inherit from the admin user. Reject selectors that describe conflicting
lineages rather than presenting a hypothetical composition as the selected owner's state.

Show artifact type and identity, bundle/source provenance, original scope and resource, integration
activation, and recorded handling, deferral reasons/routes and native placement where available.
Include artifacts already handled upstream, so operators can explain the whole path even though
those payloads no longer reach the session. Distinguish current declarations from captured content
and recorded application evidence. Uncaptured bundle contents and missing or stale evidence must be
shown as unknown, unavailable or not applied as appropriate, never as an empty successful result.

Inspection reads declarations and existing instance state. It must not acquire artifacts, fetch
sources, contact remotes, run integration setup or mutate state to manufacture an answer. Recorded
application does not prove current native files or model consumption. Reuse existing resource
resolution and instance-state evidence rather than introducing a separate acknowledgment ledger.
Exact selectors and output details belong in the HLA and CLI design.

**R10. Validate without unrelated external services.** Use simple local artifacts, local Git
repositories and dedicated test integrations to prove ingestion, routing and cleanup. Exercise real
harness tooling and a scoped live VM where placement and ownership are the claim. Avoid model calls
and external marketplace dependencies. Publish the native capability matrix, source/update rules,
worked manifests and migration guidance with the behavior they explain.

## Explicit exclusions

- Feature execution, inter-feature dependencies and automatic core hint emission.
- General environment management or replacing the existing env precedence rules.
- Hook execution, MCP server management and artifact generation/distillation from runtime sessions.
- Workspace plugin installation, a harness plugin marketplace, or a new package distribution
  service. Artifact bundles and native harness plugins are distinct concepts.
- A permissions/grants engine for first-party integrations, or a second instance-state system.
- A mandatory Rulesync runtime dependency or a new archive format chosen without research.
- Compressed storage, lazy content loading and content deduplication. These are expected follow-up
  storage work for large skill packages; the operator explicitly deferred them from this delivery.

## Approved design decisions

1. First-delivery sources are workstation and Git, with packaged distributions deferred. All source
   readers converge on the same normalized representation.
2. Session artifact delivery is disabled by default across first-party integrations. Explicit
   `enabled_workarounds` in the session-facet configuration permits specific documented methods.
   Unhandled inputs warn with integration-supplied reasons and available workaround names; there is
   no silent omission or implied successful delivery.
3. The HLA's native support matrix governs first delivery, including its explicit unsupported cases.
4. Capture occurs in the owning setup operation. The HLA's source refresh, content identity,
   ordering, collision and independent-consumer rules govern implementation.

## Definition of done

The operator can acquire a reusable bundle, reference it from actual owning resources, and launch a
session whose selected integration receives exactly the applicable inputs still needing handling.
Inactive ancestors and the VM/user/workspace diamond cannot lose or duplicate an input. Same-type
keys replace whole definitions within one scope, with inspectable provenance; names at different
scopes remain distinct through deferral and native handling. Supported native representations and
shell files have the promised placement and lifecycle; unsupported cases receive the agreed explicit
disposition. Repetition, update, removal, failure and session reuse are demonstrated with observed
evidence. Operators can use `agw artifact show` to explain local and ancestor declarations,
provenance and recorded delivery without changing the system. Permanent documentation stands alone,
and the predecessor's remaining acceptance is not misrepresented as artifact completion.

## Operator ruling: conservative workarounds, 2026-09-15

The operator directed: "let's call it enabled_workarounds" and "the right posture is to warn on
unhandled session artifacts. They are by definition optional." The direction applies "across all the
integrations, again with a highly conservative bias."

This supersedes the original automatic session delivery and terminal-error decisions. Native outer
placement remains the ordinary path. Workarounds are explicit, separately named opt-ins, never
implicitly enabled by an artifact reference or an ancestor activation. Do not require a tested
native version as a blanket artifact minimum or build a release-by-release compatibility framework.

## Operator ruling: native scope placement and progress, 2026-09-15

The operator directed: "We want to apply at the producing scope wherever possible."

"We should create an Agentworks generated section of AGENTS.md. The only issue is potentially
conflicting with other harnesses that use AGENTS.md, but I think that's the best we can do for now."

"When re-applying, both an empty file and a correctly-delineated Agentworks section are fine, with
any existing Agentworks content completely replaced. A partial Agentworks section (start delimiter
with and end one or vice versa) should warn and skip application."

"And then we do need to output more in general. My thinking is that during application, each applied
type gets an output line (\"Applying N <types> (<list>)\"). With a small number of artifacts of that
type (say <= 3), they are all listed. Otherwise we should list the first two then \"...\"."

"And we should similarly communicate deferrals."

"And yeah, VM artifacts should apply in that scope if possible, otherwise defer. And since shell is
completely made up, we should just make up scope locations but there should be no deferrals. For vm,
maybe /opt/agentworks/artifacts/?"
