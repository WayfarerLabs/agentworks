# FRD: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-07-31

Status: DRAFT (phased review: FRD first, HLA and plan to follow)

## Problem

Agentworks has no way to tell an operator what shape a piece of config must be, other than prose
someone wrote by hand. Every schema fact (which fields exist, which are required, what types they
take, what they mean) lives in at least three hand-maintained places: the imperative validator code,
the bundled sample manifests, and the guides. Nothing ties them together, so they drift, and the
burden falls hardest on exactly the surface that changes most: capability-specific config embedded
in consuming resources (`platform_config` in a vm-site, `provider_config` in a git credential,
`harness_config` in a session template, backend mappings in a secret), where the shape depends on
which capability the resource names and the only authoritative answer is reading the capability's
`validate` source.

Two structural facts compound the cost:

- **Dual declaration paths.** Resources can be declared in legacy config.toml sections and in YAML
  manifests, and the decode layer exists largely to keep the two in lockstep. Every schema-touching
  change pays for both frontends. The TOML path is already deprecated (aggregated load warning
  pointing at `agw resource migrate`, shipped ahead of this SDD).
- **A plugin surface that will grow.** Plugins register capabilities today and will register more.
  Each new capability author currently hand-rolls validation and hand-writes sample/doc text, with
  nothing keeping them honest. The capability contract itself carries a standing note that the
  invoked-validation API "may be deprecated in favor of capabilities pushing a declarative config
  schema definition at registration time"; this SDD is that note coming due.

## Personas

- **The operator** declares resources and edits config. They need to discover what fields exist
  without reading source: good samples, field-level docs, a describe/explain surface, editor
  completions, and errors that name the file, location, and fix.
- **The capability author** (app or plugin) implements a capability. They should declare their
  config's shape once and get validation, reference extraction, docs, and samples for free, with no
  way for those surfaces to disagree.
- **The maintainer** evolves the resource model. They need one validation regime instead of the
  current several (capability `validate` classmethods, secret-backend `validate_mapping`, kind
  loaders, manifest decoders) and schema surfaces that cannot silently drift.

## Scope and phasing

Two phases, one feature branch and PR. Phase 1 removes the deprecated TOML resource path so phase 2
builds against a single declaration frontend.

### Phase 1: remove TOML resource declarations

- **FR1.** Resources are no longer loaded from config.toml. The presence of any resource-declaring
  TOML section is a hard, actionable error at config load (not a silent ignore, not a warning)
  naming the sections found and pointing at `agw resource migrate` and `agw resource sample`.
- **FR2.** `agw resource migrate` keeps working after removal; it is the stranded operator's escape
  hatch and reads the TOML file directly rather than requiring the sections to load into the
  registry. Sunsetting the migrator itself is explicitly out of scope.
- **FR3.** Settings sections (`[operator]`, `[paths]`, `[plugins]`, `[defaults]`, `[secret_config]`,
  `[session.config]`) are unaffected. config.toml remains the settings file.
- **FR4.** The permanent record moves with the change: a superseding ADR replaces ADR 0016's
  dual-path stance, and guides, samples, and the sample config describe only the manifest path.
  Machinery that exists solely to keep the two frontends in lockstep is removed rather than
  preserved (the decode layer stops routing through the TOML loaders).

### Phase 2: the declarative schema model

Declaration:

- **FR5.** Every capability declares its config schema as a model at registration time. The core
  derives, without invoking capability code: shape validation (types, required fields, closed-world
  unknown-key rejection), best-effort reference extraction (the `dependencies` contract: total,
  never raising, omitting only refs whose identity a malformed field destroys), and schema emission.
  The invoked `validate`/`dependencies` classmethods are retired.
- **FR6.** The schema vocabulary covers agentworks' semantic needs beyond plain types: fields that
  are references to resources of a named kind, fields that are secret references with
  owner-templated default names (e.g. `git-token-<owner>`), and per-field operator-facing
  descriptions. These semantics survive into every derived surface (validation, extraction, docs,
  samples, emitted schema).
- **FR7.** Resource kinds' own manifest specs are declared the same way, replacing the per-kind
  hand-rolled decoders, so capability config and resource fields form one regime. Secret backends'
  `validate_mapping` unifies onto the same mechanism, dissolving the classmethod-vs-instance
  inconsistency.
- **FR8.** Capability-embedded config is modeled as a discriminated union keyed on the naming field
  (`platform`, `provider`, `harness`, backend name), assembled from the capability registry so
  plugin-registered capabilities join the union automatically.

Derived surfaces:

- **FR9.** The framework can emit a machine-readable schema (JSON Schema) per resource kind,
  including the capability unions, via a CLI surface; generated manifests and samples carry an
  editor association (yaml-language-server modeline) so operators get completions, hover docs, and
  diagnostics in schema-aware editors.
- **FR10.** Samples are rendered live by the CLI from the registered schema, uniformly for bundled
  kinds and plugin capabilities alike; no generated sample files are checked in. A rendered sample
  is the schema-derived field reference (every field, type, required/optional, default,
  description; one union arm rendered with alternatives indicated) merged with a hand-authored
  prose blurb per kind and per capability. A capability registered without sample prose still
  yields a complete generated skeleton. The bundled hand-authored sample files are retired, and
  `agw resource sample` (including `--write`) keeps its interface over the new renderer.
- **FR11.** An operator can ask the CLI to explain schema: `agw resource describe` (or a sibling
  surface; naming is HLA's call) renders the field reference for a kind or capability, including
  plugin-registered ones, from the same schema.
- **FR12.** Error quality does not regress: validation errors keep owner-scoped framing
  (`<owner>.<field>: ...`) and file/position context at least as good as today's, and unknown keys
  remain errors for capability config.
- **FR13.** Drift is structurally impossible or test-caught: schema facts appear in exactly one
  authored place (the model), samples and describe output are rendered from it (so they cannot
  drift by construction), the renderer is pinned by tests over fixture schemas plus every bundled
  kind (rendered samples load cleanly and build a registry), and any remaining checked-in derived
  docs are pinned by tests that fail when regeneration would change them.

Stretch (in scope only if phase 2 lands cleanly; may be descoped to a follow-up without
renegotiating this FRD):

- **FR14.** config.toml's settings sections are declared as models under the same regime, and a
  schema for config.toml is emitted for TOML editor tooling (taplo).

## Non-goals

- Adopting pydantic-settings or changing config file discovery, layering, or precedence.
- New capability kinds, changes to the capability lifecycle (preflight/runup/ops), or plugin
  protocol changes beyond how config schema is declared.
- Removing `agw resource migrate` or `agw config` compatibility surfaces beyond FR1.
- Publishing schemas externally (SchemaStore or hosted URLs); emitted schemas are local artifacts.
- Backfilling generated docs into every existing guide; guides adopt generated field references
  opportunistically, with the samples and describe surface as the canonical reference.

## Success criteria

- A capability author adding a field touches exactly one file (the model) and `agw resource sample`,
  `agw resource describe`, emitted schema, validation, and reference extraction all reflect it with
  no further edits; a test proves this end to end for a fixture capability.
- Zero hand-maintained duplication of schema facts remains for migrated kinds: the hand-authored
  sample files are gone, prose blurbs carry no field lists, and the FR13 renderer tests are in
  place.
- The TOML resource loaders and the decoder shim layer are deleted (phase 1), with the full test
  suite green and the migrator verified working against a fixture config.
- Operator-facing error output for the reworked validation passes review against a corpus of
  representative mistakes (unknown key, wrong type, missing required field, bad capability name)
  with file/position framing preserved.

## Dependencies and constraints

- The deprecation warning pre-work (PR #315) ships in a release before phase 1 merges, so operators
  get at least one released version that warns before the version that errors.
- Pydantic v2 becomes a runtime dependency (latest stable at implementation time), and models must
  hold up under the repo's strict mypy configuration.
- Frozen/immutable declaration objects remain the norm for the registry, matching the current
  frozen-dataclass discipline.

## Resolved questions

Decided with the operator at FRD review (2026-07-31):

- **Release sequencing.** One released warning version is enough runway: PR #315 ships in the next
  release, and phase 1's hard error may land in the release after.
- **FR14 commitment.** Stretch, as drafted: phase 2's definition of done excludes it, and it
  descopes to a follow-up without renegotiating this FRD.
- **Generated samples in-repo vs at-runtime.** All live-rendered: the CLI renders every sample from
  the registered schema on demand, bundled kinds and plugin capabilities uniformly, with no
  checked-in generated sample files. FR10 and FR13 state this.
