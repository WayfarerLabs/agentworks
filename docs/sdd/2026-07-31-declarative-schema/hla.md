# HLA: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-01 (phase 2 framed by the descriptor contract, 2026-08-05)

Status: DRAFT. Companion to [frd.md](frd.md); plan and LLDs follow. Phase 1 is on `main`; phase 2
executes through the capability-kind descriptor (see the phase-2 callout and Component 0).

## Architecture overview

The target state puts a single declarative model layer between every config frontend and every
consumer:

```text
YAML manifests ----\                                    /---> validation errors (owner + file:line)
                    +--> decode through MODELS --> registry --> reference extraction (graph edges)
settings TOML -----/         (one regime)               \---> JSON Schema emission --> editor tooling
 (stretch, FR14)                                         \--> sample / describe renderer --> operator
```

Every schema fact (field, type, required, default, description, reference semantics) is authored
exactly once, in a model. The registry carries the models; validation, reference extraction,
defaulting, schema emission, and rendering are all derived views. Capability config participates
through discriminated unions assembled from the capability registry, so plugin capabilities join
every derived surface automatically.

Phase 1 clears the ground: the YAML manifest path becomes the only resource frontend, so phase 2
replaces one decode surface instead of two kept in lockstep.

## Phase 1: remove TOML resource declarations

Small architecture, mostly deletion. Components:

- **Hard error at load.** The settings loader keeps a single check over `KIND_SECTIONS` (the
  existing shared table): any resource-declaring section present in config.toml is an aggregated
  `ConfigError` naming the sections and pointing at `agw resource migrate` / `agw resource sample`.
  The existing warning (and its `--no-deprecations` silence) is replaced by this error. The
  settings-only load (`resources=False`) that `resource sample --write` and `resource edit`'s
  fallback use today becomes the escape-hatch mechanism; `agw resource migrate`, which today loads
  the full config (it is exempted only from the deprecation nudge and reads registry rows for
  verification), MOVES to the settings-only load. That move is what forces the verification rework
  below.
- **Loader deletion.** The TOML resource loaders (`loaders_resources.py`, `loaders_secrets.py`'s
  resource half, `loaders_sessions.py`) and the decode layer's route-through-TOML-loaders shims are
  deleted. Decode logic that survives moves to be owned by the manifest decoders outright; this is
  an interim, phase-1-only state that phase 2 replaces with models, so no effort goes into
  beautifying it.
- **Migrator verification rework.** Planning becomes pure over the config file text (today it also
  takes the built registry for its pre-migration rows, a source that disappears when TOML rows stop
  loading). The reworked verification decodes the emitted YAML through the manifest decoder, proving
  the output actually loads, and compares it against a pre-side derived from the TOML text. The
  pre-side derivation must be INDEPENDENT of the section-to-spec mapping that produced the emitted
  YAML, or the comparison is tautological for exactly the mapping under test; defining that
  independent derivation is the core of the phase 1 LLD. The check gains "the output loads" and must
  not lose "the migration preserved meaning".
- **Record keeping.** A superseding ADR replaces ADR 0016's dual-path stance; guides and the
  capabilities README drop dual-path language; the resource-manifests SDD lockfile gains a dated
  entry (standing instruction: every PR advancing this effort appends lockfile entries to the locked
  SDDs whose stance it revises).

## Phase 2: the declarative schema model

> **Executed through the capability-kind descriptor (wave 2, 2026-08-05).** Phase 2 is a child of
> the next-steps roadmap SDD, and its architecture is now framed by the roadmap's
> `capability-descriptor-contract.md` (the authority; this HLA does not restate it, it adopts it).
> The descriptor is the execution vehicle: Component 0 below adopts it before any schema modeling,
> and the per-implementation config models of Components 2 and 4 register as `config_model`
> attributes offered to consumers. (Two intermediate designs, schema slots and consuming-kind
> keying, were specified and then rescinded by the operator on 2026-08-05; the contract on `main`
> and Component 0 below record the settled shape.) The reconciliation with this HLA's original
> framing is recorded in the contract: keep the plan's 2.4-before-2.5 ordering (the HLA had folded
> hardening into the kind-model swap; the split stands so the old-shape error survives the decoder
> swap), union assembly stays at the existing post-registration boundary, and the lazier-plugin-load
> risk transfers to the roadmap's wave 8 unchanged.

### Component 0: capability-kind descriptor adoption

One frozen, typed, core-owned `CapabilityKindDescriptor` per kind, registered in a single table that
becomes the only capability-kind enumeration in the codebase. The switchboard collapses onto it:
today's independently-enumerating sites (the adapter table, the graph's kind set and readiness
dispatch, the per-kind registry loaders, bootstrap publication, the plugin snapshot/restore tuple,
and manifest decode's per-kind capability branches) derive from the descriptor instead.
`KIND_REGISTRY` and manifest decode's `KIND_SECTIONS` legitimately enumerate all resource kinds and
stay; the descriptor's `kind_strategy` field feeds `KIND_REGISTRY` rather than replacing it. The
migrator's kind-participation flags stay hand-maintained (the migrator is a deliberately independent
frozen oracle; the deferred `migration_participation` field exists only for the future in which it
derives).

The descriptor also makes trust-but-verify enforceable at registration, replacing the current
type-and-cast seam: implementation-contract conformance, required metadata, a side-effect-free
constructibility check, required operations, config model conformance, and `contract_version`
compatibility (declared from day one so the discipline predates the first incompatible change).
Atomic seating is preserved. This strengthens the internal extension framework; it makes no public
plugin promise (that stays gated on the roadmap's wave 8). Secret-backend's constructed-singleton
registry policy is a descriptor-carried interim exception that wave 3 removes; `_VMPlatformKind`
moves in from `vms/kinds.py` for symmetry during adoption.

**Config is offered per FACET** (operator ruling, 2026-08-06, roadmap note 4; settles the contract
after two superseded designs, the seed's schema slots and `config_model_for(consuming_kind)`).

A **facet** is the level a capability is driven at: `vm`, `user`, `workspace`, `session`. It pairs
that level's methods with that level's config. A capability offers a fixed set of facet configs the
same way it offers a fixed set of API methods, and consumers choose which facet they drive, so
producers never know their consumers. Core asks `config_for(facet)`.

**Facets are deliberately NOT scopes, and core owns the mapping between them.** The roadmap's scopes
are `vm`, `admin`, `agent`, `workspace`, `session`; the facets are the four levels above. Admin and
agent both resolve to the `user` facet, and session start and resume share the `session` facet, so a
vm-template's admin attachment and an agent-template get the same answer BY CONSTRUCTION rather than
by each capability encoding that they mean the same thing. Core performs that mapping; a capability
only ever sees a facet.

- **The common case spells nothing extra.** Every capability today has one config shared by all of
  its operations, so it writes `config_model = X` and nothing else. That covers vm-platform,
  git-credential-provider, secret-backend, and harness-integration as they stand.
- **Wave 4's multi-level case falls out without new machinery.** A harness integration's methods run
  at different levels (`vm_init`, `user_init`, `workspace_init`, session `start`/`resume`), so its
  configs differ per method for the same reason its parameters do. A vm-template's admin attachment
  and an agent-template both carry the config for `user_init`, exactly as both call it, so neither
  the capability nor the framework has to encode that those two kinds mean the same thing.
- **Nothing needs naming.** The methods are already named; the configs inherit those names rather
  than getting a parallel vocabulary. This is also why no collective noun is introduced: the API
  side never needed one for "the set of methods", and the config side is the same set viewed
  differently.
- Asking for a config a capability does not have is a hard error naming what it does have, which
  replaces the wrong-kind rejection the consumer-keyed design needed.

Why not `config_model_for(consuming_kind)`: it made every PRODUCER enumerate its CONSUMERS, so
adding a hosting surface meant editing every capability that should serve it; it was strictly more
dynamic than the API it parallels, whose methods and levels are settled; and it forced each harness
integration to encode that vm-template-hosting-admin and agent-template mean the same thing. All
three dissolve once the config is simply part of the method contract.

**Readable at finalize.** Core reads the facet-config association before any method runs, so it must
be DECLARED DATA, not merely a signature annotation the framework never reads.

**Config presence is NOT the support claim, and must not become one.** That is the line between this
and the rescinded slot mechanism. The roadmap's scope-participation contract is explicit: scope
support is carried by the integration's implementation (the base class provides no-op defaults and
an integration implements what it supports), and "accepting no config at a surface means emitting no
schema for it". So a capability may support a scope while declaring no config there, and a method
that takes no config is ordinary. Wiring the two together would reinvent slots under a new name.

Union assembly is per `(kind, facet)`, which reduces to today's per-kind union while every
capability declares one config and no facet. The descriptor carries no config field until step 2.3
registers the first model.

### Component 1: the schema foundation

A new `agentworks/schema/` package owning the framework-wide model vocabulary (**corrected
2026-08-06**: this said `resources/schema/`, and it shipped top-level because a package under
`resources/` cannot be the import leaf the design requires; see the schema-foundation LLD section
8):

- **Base model.** A shared Pydantic v2 base (strict mode, frozen, `extra="forbid"`) that all spec
  and capability config models extend. Closed-world is universal per FR12: unknown keys are hard
  errors on every modeled surface, kind specs included, retiring today's warn-and-load-anyway
  handling (an operator decision, 2026-08-01). Strict mypy stays authoritative; the pydantic mypy
  plugin is enabled.
- **Agentworks field metadata.** `Annotated` markers carrying the semantics JSON Schema does not
  have natively:
  - `SecretRef(default_template=...)`: the field names a secret; the template (e.g.
    `git-token-{owner_name}`) derives the default from the owner at VALIDATION time, and
    independently during reference extraction, which runs before and regardless of validation.
    (Corrected 2026-08-06: this said "at decode time", which is false for capability blobs.
    `manifests/decode.py:18-23` states outright that capability-owned blobs are not validated at
    decode; their check is the finalize pass, matching this HLA's own timing table below.)
  - `ResourceRef(kind=...)`: the field names a resource of a fixed kind.
  - Both flow into emitted JSON Schema via `json_schema_extra` (an `x-agw-*` vocabulary) so docs and
    editor hovers can show them without confusing validators.
- **Walkers.** Two functions over `model_fields` that power everything downstream:
  - `extract_references(model_cls, blob, owner)`: the total, never-raising reference extractor. It
    walks the declared fields, reads ref-marked values straight from the raw blob (never through
    validation), applies default templates for absent optional refs, and skips any field whose value
    is malformed. This preserves the `dependencies` contract exactly: graph construction never
    depends on a blob being valid.
  - `iter_field_docs(model_cls)`: the ordered field-reference stream (name, type rendering,
    required/default, description, ref semantics, union arms) consumed by the sample renderer and
    the describe surface, plus the roadmap's teaching surface (`agw guide`, owned by the onboarding
    child SDD) as an external third presenter; Component 7 records that coordination. One walker,
    three presentations, so those surfaces cannot disagree. **Correction (step 2.1 LLD,
    2026-08-06):** schema emission is NOT a consumer of this stream. Component 6 derives it from
    `model_json_schema` over the same models, because deriving JSON Schema from `FieldDoc` would
    mean writing a second schema generator. Emission and the stream are SIBLING derivations from one
    authority (the models), not a chain; the marker's schema hook plus a round-trip test is what
    keeps them from drifting.

### Component 2: capability schema registration

The capability contract changes from invoked validation to declared schema:

- Each capability class declares `config_model` (a model class) at registration; capabilities with
  no config declare the shared empty model. Secret backends declare `mapping_model` the same way,
  dissolving `validate_mapping` and the classmethod-vs-instance inconsistency.
- The core reaches a model by asking the capability `config_for(facet)` (Component 0), never by
  assuming a capability has exactly one. The declaration above is all a simple capability author
  writes, since today every capability has one config and names no facet; the indirection exists so
  wave 4's harness integrations add per-facet configs without any framework change, and so consumers
  pick a facet the way they pick an API method. Registration-time conformance checks every model an
  implementation offers against the kind's model contract.
- The base `Capability.validate` / `Capability.dependencies` classmethods are retired. The core
  performs both: validation is `model_validate` on the registered model (owner-framed by the error
  bridge), extraction is `extract_references`. Capability code is never invoked for either, which is
  what the base class docstrings promised and what keeps plugin misbehavior out of the finalize
  pass.
- Construction still re-validates (unchanged contract: an instance is config-valid by construction),
  now by validating into the model and binding the validated, fully-defaulted model instance instead
  of the raw mapping. Capability ops read typed fields off their model rather than
  `config.get(...)`.

### Component 3: capability config dispatch, and when it validates

Capability-embedded config is a real tagged union (FR8, operator decision 2026-08-01): the naming
field and its blob collapse into one table with an internal `name` discriminator
(`spec.platform: {name: lima, vm_host: ...}`; `name` is the resource model's standard term for the
second half of a `kind/name` address, and the hosting surface already fixes the kind), so Pydantic's
native discriminated-union machinery serves both runtime validation and the emitted schema, with no
sibling-field indirection. Each capability's model carries its registered name as a `Literal` tag;
after plugin registration (the boundary `build_registry` already provides; plugin impls seat at
import), the framework assembles one union per capability kind from the registered models and caches
it on the kind's registry entry. Secret `backend_mappings` keeps its map-keyed-by-backend shape; the
map key dispatches to the backend's `mapping_model` and the emitted schema expresses it as per-key
properties.

The harness surface is already fully tagged on main: the harness-integration rename (PR #383) made
`spec.harness_integration: {name: ...}` canonical, built a comment-preserving YAML-rewrite mechanism
in the migrator (ruamel round-trip plus document-marker text patching, digest/CAS guards,
backup-first rollback, YAML-native migration units), and scheduled the legacy selector's hard cut
for its own 0.14.0 phase.

**Correction (step 2.4 implementation, 2026-08-06): that machinery is NOT at HEAD.** Wave 1's
`6d44a12c feat(cli)!: remove session compatibility surfaces` deleted `YamlRewrite` and dropped the
ruamel dependency along with the compatibility surfaces it served, so this paragraph's present tense
was stale by the time step 2.4 reached it. The mechanism was recovered from that commit's parent and
generalized, so the OUTCOME matches this paragraph's intent, but the premise that it was shipped
machinery to build on was wrong: it was archaeology. Also learned in the rebuild, and worth
carrying: quote preservation is load-bearing rather than cosmetic. Without `preserve_quotes` ruamel
re-emits `subscription_id: "0000"` bare and the next load reads the integer `0`, so verification
catches it only after the operator's file has already been rewritten wrong.

This effort's manifest-upgrade mode GENERALIZES that (recovered) machinery to the platform/provider
sibling fold rather than building anew, and its hardening step leaves the harness selector's removal
to the owning SDD.

Pre-support already shipped ahead of this SDD (PR #349): manifest decode accepts both shapes and
warns once, aggregated, on the old one, and `agw resource migrate` emits the tagged form (so the
phase 1 migrator work inherits tagged emission; there is no shape flip to schedule). What lands here
is the hardening: the old sibling shape (`platform` plus `platform_config` and kin) becomes a hard
error naming the exact rewrite, and `agw resource migrate` gains a manifest-upgrade mode that
rewrites YAML files in place under its existing backup-first discipline.

A `name` tag naming a capability with no registration on this host is a hard finalize error, exactly
as shipped today (the registry-readiness refactor's R9.2/R9.11 rulings, preserved by operator
decision after plan review): the union has no arm to select, and the bridge renders the error naming
the registered options. Every host registers every shipped plugin's capabilities, so such a name can
only be a typo; the cross-host sharing story is carried by the enablement axis below, not by name
tolerance. For everything registered, hard validation keys on the finalize fold's verdict, exactly
as the registry's shipped pass order already works: dependencies are extracted first (the total
walker, enablement-blind by construction), the fold computes enablement and readiness without
validating (non-constructing, total over unvalidated config), and the throwing validate pass then
runs over the resources that emerged READY and ENABLED. A disabled or not-ready resource skips hard
validation at load; its blob is validated the moment it is enabled or used, and doctor/describe
already mark such rows with their reasons, so nothing is silent. This preserves the secrets
contract's disabled-backend seam as a consequence of the general rule rather than a special case.
Samples and describe render for disabled capabilities too: rendering reads the model, not the
operator's blob.

Validation operates on effective config (FR12): the finalize pass resolves each inheritance chain
through the graph (session templates' `inherits` edges; every other surface is a chain of length
one) and validates the merged blob, never a partial declared blob, since a Pydantic model's required
fields would wrongly reject a child blob a parent completes. Today's session-resolve-time merged
check moves to finalize as a consequence; resolve still merges to build the instance, and
construction re-validates as always. Two LLD-owned details: the merge tracks per-key provenance so
the bridge can name the template that declared a bad key, and reference extraction stages
(structural refs per declared blob feed the graph the merge walks; secret refs read the effective
blob so an overridden parent secret is not over-declared).

The graph types the inheritance edge (FR17). It is source composition, not a runtime dependency: it
drives existence checks, cycle detection, and merge ordering, and is excluded from runtime-need
traversal, so the secret union, the preflight resolvability sweep, and dependency listings never
cross it. A parent's own secret edge describes the parent's standalone use; a child's runtime needs
come from its effective blob alone. (Concrete failure this prevents: a child overriding the parent's
default secret name would otherwise inherit a transitive edge to the default secret and
double-prompt the operator.) The reference model already labels the parent edge with a usage string;
this effort promotes that to a traversal-relevant kind. Whether readiness or enablement propagates
across an inheritance edge is a policy question the capability-contract LLD settles; today session
templates opt out of the fold entirely, so nothing is decided by accident.

Timing preserves today's deliberate two-pass shape (capability blobs validate at finalize, never at
decode, so graph construction never depends on a blob being valid):

- **Decode** validates only kind-owned fields; the capability blob passes through as a raw mapping,
  exactly as now.
- **Finalize** validates each tagged table through the assembled union, raising through the error
  bridge with owner framing.
- **Reference extraction** runs the total walker over the raw table at graph-construction time,
  independent of validity, preserving the `dependencies` totality contract.

### Component 4: kind spec models and decode replacement

Each resource kind declares a spec model replacing its hand-rolled `_decode_*` function. The
manifest envelope (apiVersion / kind / metadata / spec, duplicate detection, origin capture) stays
as is; what changes is that `spec` decoding becomes `model_validate` into the kind's model,
producing the declared-resource object. Capability blobs are NOT part of the kind spec model's
validated surface: the spec model types them as raw mappings and Component 3's finalize pass owns
their validation. The frozen-dataclass decl classes become frozen models (or thin wrappers over a
validated model where behavior-rich classes warrant it; LLD's call per kind). Kind-specific semantic
checks that models cannot express field-locally (name/length rules with derived caps, cross-field
constraints) become model validators, so they stay inside the one regime.

### Component 5: the error bridge

One module translating `pydantic.ValidationError` into the operator-facing error discipline:

- Each error's `loc` path renders as `<owner>.<field.path>`, message text is normalized to today's
  tone, and the manifest document's `SourceLocation` (file, line) frames the whole batch. The loader
  already composes YAML with per-document marks; the bridge reuses them. Field-level line numbers
  within a document are not promised (parity with today), only document-level file:line plus the
  full field path.
- The bridge is the single choke point for FR12: decode issues, finalize validation, and
  construct-time re-validation all raise through it.

### Component 6: schema emission

`model_json_schema` over the kind spec models (unions included) produces one JSON Schema document
per kind, plus an envelope schema. Surfaces:

- A CLI command (working name `agw resource schema <kind>`, naming settled in the plan; the
  completions tree gains it) prints or writes the schema set.
- `agw resource sample --write` stamps the yaml-language-server modeline referencing the written
  schema files, so operators get completions and hover docs with zero editor setup beyond the
  extension. FR9's "generated manifests" resolves to this surface plus the migrator's emitted YAML,
  which gains the same modeline stamp in phase 2 once schemas exist (the migrator itself lands in
  phase 1, before there is a schema to reference). Emission targets JSON Schema 2020-12; a draft-4
  down-level exists only if FR14's taplo integration lands and demands it.

### Component 7: the sample and describe renderer

One renderer over `iter_field_docs`:

- **Sample rendering** (FR10): fully-commented YAML skeleton per kind (and per capability arm),
  every field with its type, required/default, and description, one union arm rendered and the
  alternatives listed. Merged with an optional hand-authored prose blurb registered alongside the
  model (kind-level and capability-level). `agw resource sample` keeps its interface, rendering live
  from the registry (plugins included); the bundled sample files are deleted.
- **Blurbs are structured data, not presentation** (roadmap guide-surface coordination, 2026-08-05):
  a blurb registers as structured markdown (identity, level, title, body), colocated beside the kind
  or capability it documents (plugin blurbs ride plugin registration), never pre-rendered CLI text
  and never containing field lists. The onboarding child's `agw guide` composes the same sources
  (schema fragments, field references, sample skeletons, blurbs) into topic pages, so the authored
  layer is shared rather than forked; the blurb shape must not preclude a topic-content contract,
  but committing to the guide's actual contract waits for that effort's LLD. Blurbs are inert prose
  with no templating; if they ever grow dynamic placeholders, they adopt the guide's locked-down
  template vocabulary rather than inventing a second dialect. This SDD's renderers are
  registry-anchored and side-effect-free (schema facts only, no instance state), which is what lets
  the guide reuse them as dynamic blocks without new data-access paths.
- **Describe rendering** (FR11): the same stream rendered for the terminal under
  `agw resource describe <kind>` / capability, replacing the "read the source" answer.
- FR16's pointer sweep repoints guides, command help, and remediation text at these two surfaces.

### Component 8: model-layer defaulting (FR15)

Defaults are declared on model fields (static values or `SecretRef` templates) and applied by
validation, so decoded objects are fully resolved. Downstream request/consumer types make that
structural: fields with a model default are non-optional after decode (mypy enforces the boundary),
and the plan enumerates and deletes every consumer-side fallback (the platform-code literals). Where
a legacy row can still surface an unset value (pre-model DB rows), the consumer raises instead of
defaulting locally.

## Tech stack

- **Pydantic v2** (latest stable at implementation time), with the mypy plugin, strict mode, and
  frozen models. New runtime dependency; the only one this effort adds.
- Everything else stays: pyyaml composition for marks, tomlkit for the migrator and settings, typer
  for the CLI surfaces.

## Sequencing (within one branch and PR)

1. Phase 1 in full (removal, hard error, migrator rework, ADR).
2. Schema foundation + error bridge, proven on capability config models (all shipped capabilities
   and plugins), retiring `validate` / `dependencies` / `validate_mapping`.
3. Kind spec models, kind by kind, behind the stable decode entry points. The tagged-union hardening
   lands here: the old sibling shape (accepted-with-warning since PR #349) becomes a hard error, and
   the migrator's manifest-upgrade mode arrives in the same change.
4. Emission, renderer, describe; delete bundled samples; completions update.
5. FR15 defaulting sweep, then FR16 pointer sweep, then permanent-doc promotion (capabilities
   README, resources guide, ADR cross-references) and lockfile entries.

Steps 2 and 3 keep the suite green at every commit; there is no flag-day cutover.

## Risks and open architectural notes

- **Union assembly vs registration timing.** The unions must build after all plugin registration.
  Assembly at `build_registry`'s existing boundary handles it; the risk is a future lazier plugin
  load, noted for the plugin SDD's owners.
- **Migrator verification rework** is the one place phase 1 writes new logic rather than deleting;
  it gets its own LLD attention and a fixture-config end-to-end test.
- **Error-message parity** is a review gate, not a hope: the plan carries the FRD's
  representative-mistakes corpus as a checked test comparing bridge output against the curated
  framing.
- **FR13's test regime** rides with the renderer: the renderer is pinned over fixture schemas plus
  every bundled kind, and a test renders each kind's sample, loads it through the manifest path, and
  builds a registry from the result. The plan carries these beside the FR12 corpus.
- **Verification normalization vs step 3.** The phase-1-reworked migrator verification normalizes
  rows with a dataclass-only helper (`strip_source_fields`); when sequencing step 3 turns decl
  classes into models, that helper silently stops normalizing unless taught the model shape. The
  step 3 work carries an explicit checkbox for it.
- **Model performance** is a non-concern at this scale (hundreds of small documents), but union
  rebuild per process is kept O(registered capabilities) and cached.
- **DB-sourced rows** (`db/models.py`) are outside the declaration regime; FR15's "error, not local
  default" rule is the boundary contract there until a future effort models them.
