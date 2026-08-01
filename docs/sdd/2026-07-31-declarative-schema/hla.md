# HLA: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-01

Status: DRAFT. Companion to [frd.md](frd.md); plan and LLDs follow.

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

### Component 1: the schema foundation

A new `resources/schema/` package owning the framework-wide model vocabulary:

- **Base model.** A shared Pydantic v2 base (strict mode, frozen, `extra="forbid"`) that all spec
  and capability config models extend. Closed-world is universal per FR12: unknown keys are hard
  errors on every modeled surface, kind specs included, retiring today's warn-and-load-anyway
  handling (an operator decision, 2026-08-01). Strict mypy stays authoritative; the pydantic mypy
  plugin is enabled.
- **Agentworks field metadata.** `Annotated` markers carrying the semantics JSON Schema does not
  have natively:
  - `SecretRef(default_template=...)`: the field names a secret; the template (e.g.
    `git-token-{owner}`) derives the default from the owner at decode time.
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
    required/default, description, ref semantics, union arms) consumed by the sample renderer, the
    describe surface, and schema emission. One walker, three presentations, so the surfaces cannot
    disagree.

### Component 2: capability schema registration

The capability contract changes from invoked validation to declared schema:

- Each capability class declares `config_model` (a model class) at registration; capabilities with
  no config declare the shared empty model. Secret backends declare `mapping_model` the same way,
  dissolving `validate_mapping` and the classmethod-vs-instance inconsistency.
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
  from the registry (plugins included); the bundled sample files are deleted. Blurbs live as
  registered text (package data or string constants; LLD detail), never containing field lists.
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
