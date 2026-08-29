# 23. Declared Schemas and the Capability-Kind Descriptor (the Core Reads, It Does Not Invoke)

Date: 2026-08-06

## Status

Accepted. Builds on [ADR 0016](0016-yaml-resource-manifests.md) (the config/resource split and the
capability kinds), [ADR 0021](0021-system-plugins.md) (plugins contribute implementations of
existing kinds), and [ADR 0022](0022-single-resource-declaration-frontend.md) (one declaration
frontend, which is what makes modeling each kind once possible).

Supersedes the invoked capability config-validation contract: the `validate` / `dependencies`
classmethods and `SecretBackend.validate_mapping` are gone. The author-facing contract lives in
`cli/agentworks/capabilities/README.md` and `cli/agentworks/plugins/README.md`; the operator-facing
surfaces are in `docs/guides/resources.md`.

## Context

Every fact about a piece of configuration used to be written down more than once.

A resource kind's spec had a hand-rolled decoder that knew its fields, a hand-authored sample YAML
file that described them to operators, and prose in the guides that described them again. A
capability's config had a `validate(owner, config)` classmethod the core INVOKED to check a blob and
a `dependencies(owner, config)` classmethod it invoked to learn what that blob referenced, plus the
same sample and prose duplication. Defaults were written a third time, on the consumer side, as an
`or 4` or an `if x is not None else 50` beside each read.

Four consequences, all of which had arrived by the time this was decided:

1. **The copies drifted, and nothing could catch it.** A capability-kind description reading
   "(shell, claude-code)" had been wrong since `codex` landed, and the resources guide listed the VM
   platforms without `aws-ec2` in five places while using `aws-ec2` as a worked example in a sixth.
   A sample file could describe a field the validator did not accept, and only an operator would
   find out.
2. **The core ran capability code during finalize.** Building the resource graph meant calling
   `dependencies` on every capability's blob, so a plugin that raised took the registry build down
   with it, and a plugin that under-reported quietly produced a graph missing an edge. Neither is a
   failure the framework should be able to have.
3. **Unknown keys warned and loaded on.** A misspelled key in a kind's spec accumulated a warning
   and did nothing, so config that looked applied was inert. Silent no-op config is a footgun, not a
   kindness.
4. **Consumers re-defaulted what the model should have resolved.** Each VM platform carried its own
   `cpus=4` / `memory=8` / `disk=50` fallbacks, so the "default" was whatever the last consumer to
   be edited said it was.

Separately, and for the same underlying reason, the framework's knowledge of a capability KIND was
scattered. Seven sites independently enumerated the four kinds (the plugin adapter table, the
graph's kind set, its readiness dispatch, the per-kind registry loaders, bootstrap publication, the
plugin snapshot/restore tuple, and manifest decode's capability-field map). Adding a kind meant
finding all seven, and an implementation reached its registry through an `isinstance(impl, type)`
gate and a `cast`, so a class that merely looked plausible seated cleanly and failed later, far from
the mistake.

## Decision

**The framework READS declared data; it does not invoke code to learn about configuration.** That
one rule is applied at both levels: to a capability's config, and to a capability kind itself.

### 1. Config shape is declared as a model, and every view of it is derived

A capability declares `config_model`, and a resource kind's spec IS a model. Both extend `AgwModel`
(or `AgwRootModel` where the config's root is not a mapping): strict, frozen, closed-world.

From that one declaration the core derives shape validation, reference extraction, defaulting, JSON
Schema emission, the generated sample manifest, and the field reference. **No capability code is
invoked for any of them.** The invoked `validate` / `dependencies` classmethods and
`SecretBackend.validate_mapping` are retired.

Two semantics beyond plain types survive into every derived surface, because they are declared in
one place rather than reimplemented per surface:

- **A field that names another resource carries a `SecretRef` / `ResourceRef` marker**, optionally
  with an owner-templated default (`git-token-{owner_name}`). Extraction reads the marker to build
  the dependency graph, validation fills the default from it, and emitted JSON Schema carries it as
  `x-agw-ref`.
- **A field's attribute docstring IS its operator-facing description**, rendered by
  `agw resource describe-kind`, by the generated sample, and as hover text in a schema-aware editor.

The two derivations keep the contracts the split classmethods used to carry, and the split between
them is what makes the graph buildable without validating: **extraction is total and never raises**
(a blob it cannot make sense of just contributes no edges), while **validation throws** and runs
only in the finalize pass over the READY and ENABLED set, plus at construct.

### 2. Closed-world, on the effective blob, at finalize

An unknown key, a wrong type, and a missing required field are load errors, for kind specs and
capability config alike. The warn-and-load-anyway handling of unknown kind fields is retired.

Where a surface inherits (session templates), validation runs on the EFFECTIVE blob: declared blobs
merge along the chain first, and the merged result is what validates, because a declared blob may be
legitimately partial and has no completeness of its own to check. Every other host is a chain of
one, so this is a uniform rule rather than a special case.

A resource that emerges disabled or not-ready skips hard validation at load, so a broken blob on a
disabled plugin's resource can never sink the whole config. Its problems become hard errors the
moment it is enabled or used.

### 3. Config is offered per FACET

The core does not ask a capability for "its schema". It asks for the config the capability offers at
a FACET: the level it is driven at (`vm`, `user`, `workspace`, `session`), pairing that level's
methods with that level's config. Consumers choose the facet they drive, so a producer never has to
know who is asking.

The ordinary case stays invisible: every capability shipped today offers one config shared by all of
its operations, so it declares `config_model`, names no facet, and the base answers with it at every
facet. Offering a config at a facet is **not** a claim to support that level, and offering none is
not a claim to lack it: support is carried by the implementation.

### 4. A capability kind is one core-owned frozen record

`CapabilityKindDescriptor` is the single enumeration of the four kinds, contributed per capability
package. The seven switchboard sites derive from it. The record carries what the framework needs to
wire a kind in (its registry and how that registry stores things, how its rows publish, which
declarable kind's spec selects it) and, load-bearing here, two contract facts:

- **`config_schema`, the kind's model contract**: what a config model offered for this kind must BE.
  The kind states the contract; implementations declare the models.
- **`contract_version`**, matched EXACTLY at registration. Every implementation declares its own and
  nothing defaults it, so a contract change is a hard cutover: bumping the number refuses every
  implementation still on the old contract until each is migrated.

**Registration-time conformance replaces the type-and-cast seam.** Because the descriptor states the
contract, the contract is checkable, and `register_plugin` checks every implementation against it
before any registry is mutated: base, metadata, required attributes, constructibility, required
operations, config model, contract version. The check is structural and never constructs the
implementation. A non-conforming implementation is a typed error naming the plugin, and seating
stays all-or-nothing.

### 5. Merge policy is declared by the model

Template inheritance and final instance layers read merge policy from the same core model or
capability-offered model that declares the value's shape. A capability offers that model through
`config_for()` (normally from `config_model`). When its core-owned `ConfigContract` says the config
participates in layered merging, registration checks that exact offered model. No capability merge
callback decides how raw config combines, and kinds without a layered config surface do not acquire
an unrelated merge constraint.

The framework keeps the offered-model selection stable while an implementation's declared
`config_model` identity is unchanged. Registration, merging, validation, reference extraction, and
schema assembly therefore consume one model rather than observing different answers from a stateful
hook.

The closed `MergeStrategy` vocabulary has three values. Objects and mappings default to recursive
`merge`, lists default to stable `append-dedupe`, and scalars default to `replace`. A field can
override its node with `Annotated` metadata. A mapping-shaped model can declare its root policy as a
class variable. The containing field wins over the selected model, and the shape default applies
last.

Whole-object and whole-list replacement discards the complete prior subtree, including when the
incoming value is empty. Mapping value annotations participate because the map key identifies a
conflict. Mapping keys and individual list elements have no independent policy position.

A containing replacement wins before union-arm selection. Otherwise values recurse only when both
select the same discriminated or structural arm. Different or unreadable arms replace the complete
union, preventing a hybrid. Unknown conflicting object keys replace at that raw child rather than
acquiring a second runtime-shape merge language.

For an opted-in capability config, registration checks the complete reachable annotation contract.
It rejects duplicate or shape-incompatible strategies, merged mappings without exact-string keys,
append-deduplicated lists whose element schemas exceed the closed comparison carrier, and validation
aliases anywhere in a participating model, including below replacement boundaries. Serialization
aliases remain valid. Replacement is the escape for a list or mapping whose declared Python domain
is intentionally broader than recursive merging can interpret safely.

The raw merger neither validates nor invokes model validators. Wrong-shaped, unknown, cyclic, and
otherwise malformed input remains available to final typed validation instead of being filtered,
coerced, or repaired by merging.

### 6. List items are atomic; model-owned identity remains a future direction

Schema-directed append-deduplication treats each list item as one atomic value. No item-identity
declaration, callback, or protocol exists, and this ADR does not design one.

If a future model needs identity-aware list merging, the approved semantic direction is:

- equal values deduplicate;
- different identities append in stable order; and
- matching identity with unequal values recursively merges through the item model's existing field-,
  model-, and shape-directed strategies.

Without a readable identity, atomic equality and append remain the fallback. The representation of
identity and the rules for duplicate identities, identity mutation, union arms, and provenance stay
deliberately unresolved until a concrete model requires the feature. This records the intended
outcomes, not an implementation-ready design.

## Consequences

- **A capability author adding a field touches one file.** Validation, reference extraction,
  defaulting, merge policy, `agw resource sample`, `agw resource describe-kind`, and emitted JSON
  Schema all reflect it with no further edits. A test proves this end to end for a fixture
  capability.
- **Hand-maintained duplication of schema facts is gone for modeled kinds.** The bundled sample YAML
  files are deleted and rendered live instead; prose blurbs carry no field lists; the guides carry
  pointers to the rendered surfaces plus what is genuinely not a fact about a field. The rot this
  ADR's Context describes is now structurally impossible for those facts.
- **A misbehaving plugin cannot break the finalize pass.** Graph construction reads models and raw
  blobs and invokes no user code at all.
- **Model changes can be merge-contract changes.** Registration rejects a model whose recursive
  merge or append-deduplication policy cannot be honored over its declared structural input domain.
  Capability authors use explicit replacement when atomic behavior is the honest contract.
- **Harness integrations make a versioned hard cutover.** Removing the imperative config-merge
  callback moves that capability contract from version 1 to version 2. Third-party authors migrate
  through `docs/guides/upgrading-to-0.17.md`; shipped integrations already declare version 2.
- **Breaking, and broadly.** Closed-world validation, strict types, and model-layer defaulting each
  reject configuration that used to load. The operator upgrade note is in
  `docs/guides/upgrading-to-0.14.md`; the commits carry `!` markers with `BREAKING CHANGE` footers
  so release-please surfaces them.
- **Editor support falls out, as a deliberate UNDER-approximation.** Emitted JSON Schema rejects
  only what the loader also rejects; the loader rejects more (cross-field validators, name character
  rules, whether a capability is registered on this host). A permissive schema costs an operator a
  completion; a strict one would red-underline valid configuration, which is worse than shipping no
  schema.
- **A new capability KIND is still a core change**, deliberately. The descriptor table is core-owned
  and frozen: a plugin contributes implementations of existing kinds. A kind has to be integrated
  into core logic to be worth anything, so making the table extensible would offer a promise the
  rest of the system could not keep.
- **A pydantic dependency, at the framework's core.** The model layer is a leaf package importing
  only Agentworks' top-level `errors`, `path_rendering`, `source_location`, and `value_provenance`
  leaves, and nothing under `resources`. A capability module can therefore declare its model at
  import time without dragging in the kind registry. That constraint is real and load-bearing; see
  `cli/agentworks/schema/__init__.py`.
- **The former secret-backend asymmetry is resolved.** Its registry and graph now retain the exact
  implementation class, like the other capability kinds. Configured `secret-source` resources own
  per-instance config and bounded client construction, so the descriptor needs no constructed
  singleton policy or consuming-code special case.
