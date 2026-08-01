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
                                                         \--> sample / describe renderer --> operator
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
  The existing warning (and its `--no-deprecations` silence) is replaced by this error. The existing
  command exemption mechanism (migrate and `resource sample --write` load settings-only today)
  carries over so the escape hatch can run.
- **Loader deletion.** The TOML resource loaders (`loaders_resources.py`, `loaders_secrets.py`'s
  resource half, `loaders_sessions.py`) and the decode layer's route-through-TOML-loaders shims are
  deleted. Decode logic that survives moves to be owned by the manifest decoders outright; this is
  an interim, phase-1-only state that phase 2 replaces with models, so no effort goes into
  beautifying it.
- **Migrator verification rework.** `plan_migration` stays pure over the config file text, but its
  registry-equivalence verification loses its "pre" side once TOML rows no longer load. The
  verification flips to a decode-the-output check: the emitted YAML documents are decoded through
  the manifest decoder and compared against rows the planner derives directly from the TOML text.
  Both sides of the comparison then run through the one surviving decode path, which is a strictly
  stronger check than today's (it proves the emitted manifests actually load).
- **Record keeping.** A superseding ADR replaces ADR 0016's dual-path stance; guides and the
  capabilities README drop dual-path language; the resource-manifests SDD lockfile gains a dated
  entry (standing instruction: every PR advancing this effort appends lockfile entries to the locked
  SDDs whose stance it revises).

## Phase 2: the declarative schema model

### Component 1: the schema foundation

A new `resources/schema/` package owning the framework-wide model vocabulary:

- **Base model.** A shared Pydantic v2 base (strict mode, frozen, `extra="forbid"` for closed-world
  unknown-key rejection) that all spec and capability config models extend. Strict mypy stays
  authoritative; the pydantic mypy plugin is enabled.
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

### Component 3: union assembly at finalize

The capability-hosting surfaces (`platform_config` keyed by `platform`, `provider_config` by
`provider`, `harness_config` by `harness`, secret backend mappings by backend name) are
discriminated unions. Assembly is registration-time: after plugins register (the same boundary
`build_registry` already runs), the framework builds one union per capability kind from the
registered capabilities' models and caches it on the kind's registry entry. Pydantic's callable
discriminator dispatches on the naming field, so an unknown capability name fails with a typed error
listing the registered names. Not-enabled plugins follow the existing enablement model: their
capabilities are registered (so their config still validates and their samples still render) and
use-gating stays where it is today.

### Component 4: kind spec models and decode replacement

Each resource kind declares a spec model replacing its hand-rolled `_decode_*` function. The
manifest envelope (apiVersion / kind / metadata / spec, duplicate detection, origin capture) stays
as is; what changes is that `spec` decoding becomes `model_validate` into the kind's model,
producing the declared-resource object. The frozen-dataclass decl classes become frozen models (or
thin wrappers over a validated model where behavior-rich classes warrant it; LLD's call per kind).
Kind-specific semantic checks that models cannot express field-locally (name/length rules with
derived caps, cross-field constraints) become model validators, so they stay inside the one regime.

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
  extension. Emission targets JSON Schema 2020-12; a draft-4 down-level exists only if FR14's taplo
  integration lands and demands it.

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
3. Kind spec models, kind by kind, behind the stable decode entry points.
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
- **Model performance** is a non-concern at this scale (hundreds of small documents), but union
  rebuild per process is kept O(registered capabilities) and cached.
- **DB-sourced rows** (`db/models.py`) are outside the declaration regime; FR15's "error, not local
  default" rule is the boundary contract there until a future effort models them.
