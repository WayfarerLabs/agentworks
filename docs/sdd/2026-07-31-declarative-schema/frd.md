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
- **FR15.** Defaulting is entirely a model-layer concern. Every defaulted field's value, static or
  derived (the owner-templated secret names of FR6 are the derived case), is declared exactly once
  in the model and applied when the declaration is decoded and validated, so everything downstream
  of the model (platform ops, capability code, the service layer) receives fully-resolved values and
  never supplies a fallback of its own. Where a consumer can still observe an unset field that the
  model should have resolved, that is an error, not a silent local default. Today's scattered
  consumer-side fallbacks (hard-coded literals in platform code re-inventing system-wide defaults)
  are enumerated and removed by the HLA and plan, not here.
- **FR8.** Capability-embedded config is a true tagged union: the naming field and its config table
  merge into one table carrying an internal `name` discriminator, `name` being the resource model's
  standard term for the second half of a `kind/name` address (the hosting surface fixes the kind,
  the tag supplies the name), so `spec.platform: {name: lima, ...}` replaces `platform` plus
  `platform_config`, and likewise for `provider`. The harness surface already reached this shape on
  main: the harness-integration rename (PR #383, 2026-08-04) made
  `spec.harness_integration: {name: ...}` canonical, with the legacy `harness`/`harness_config`
  spellings warned and their removal owned by that effort's own 0.14.0 phase, so this effort hardens
  the vm-site and git-credential surfaces and coordinates with (never duplicates) the harness
  selector removal. Secret `backend_mappings` keeps its map-keyed-by-backend shape (the map key is
  already the discriminator). Unions are assembled from the capability registry so plugin-registered
  capabilities join automatically. This is a deliberate breaking manifest schema change, made now
  because the schema model makes it cheapest now. Pre-support shipped ahead of this SDD (PR #349):
  decode accepts both shapes with an aggregated deprecation warning on the old one, and
  `agw resource migrate` already emits the tagged form. This effort's remaining job is hardening:
  the old shape becomes a hard error naming the exact rewrite, and `agw resource migrate` gains a
  manifest-upgrade mode that rewrites YAML files in place under its existing backup-first
  discipline.
- **FR17.** Inheritance is not a dependency. An inheritance edge (a session template's `inherits`,
  and any future inheriting surface) is source composition: it participates in existence checking,
  cycle detection, and merge ordering, and is EXCLUDED from runtime-need traversal (the secret
  resolve union, preflight's resolvability prediction, dependency listings). A resource's runtime
  dependencies derive from its EFFECTIVE config only (FR12's merged blob), so a child overriding a
  parent's secret name depends on the override alone; the parent's own edge to its default secret
  describes the parent's standalone use and never attributes to the child. Without this rule, a
  transitive walk would prompt the operator for a secret the child does not use.

Derived surfaces:

- **FR9.** The framework can emit a machine-readable schema (JSON Schema) per resource kind,
  including the capability unions, via a CLI surface; generated manifests and samples carry an
  editor association (yaml-language-server modeline) so operators get completions, hover docs, and
  diagnostics in schema-aware editors.
- **FR10.** Samples are rendered live by the CLI from the registered schema, uniformly for bundled
  kinds and plugin capabilities alike; no generated sample files are checked in. A rendered sample
  is the schema-derived field reference (every field, type, required/optional, default, description;
  one union arm rendered with alternatives indicated) merged with a hand-authored prose blurb per
  kind and per capability. A capability registered without sample prose still yields a complete
  generated skeleton. The bundled hand-authored sample files are retired, and `agw resource sample`
  (including `--write`) keeps its interface over the new renderer.
- **FR11.** An operator can ask the CLI to explain schema: `agw resource describe` (or a sibling
  surface; naming is HLA's call) renders the field reference for a kind or capability, including
  plugin-registered ones, from the same schema.
- **FR16.** The canonical schema surfaces are discoverable from where operators already are: a
  one-time sweep adds pointers to the rendered sample and describe surfaces wherever guides, command
  help, and remediation text discuss a config shape, and any hand-stated field list the sweep passes
  is either deleted in favor of the pointer or left only where narrative genuinely needs it.
  Pointers, not generated content, are what guides carry (see non-goals).
- **FR12.** Invalid schema on any surface that can run here is a hard, helpful error. Every modeled
  surface is closed-world: unknown keys, wrong types, and missing required fields are load errors
  for kind specs and capability config alike (today's warn-and-load-anyway handling of unknown kind
  fields is retired; silent no-op config is a footgun, not a kindness). Hard validation follows the
  registry's finalize order, which this effort preserves: dependencies are extracted first, totally
  and never raising (a blob the extractor cannot make sense of just contributes no edges);
  enablement and readiness are then computed without validating (the fold is non-constructing); and
  the throwing validate pass runs on the resources that emerge READY and ENABLED. A resource that
  emerges disabled or not-ready skips hard validation at load, so a broken blob on a disabled-plugin
  resource can never sink the whole config; its problems become hard errors the moment it is enabled
  or used, when finalize validates it like any other. Validation operates on the EFFECTIVE config:
  where a surface supports inheritance (session templates), declared blobs merge along the graph's
  declared chain first and the merged blob is what validates, because a declared blob may be
  legitimately partial (completed by a parent or child) and has no completeness of its own to check.
  Every other surface is a chain of length one, so effective-config validation is one uniform rule,
  not a special case; it also moves the merged-blob completeness check from first use (today's
  session-resolve timing) to load, where the rest of hard validation lives. A capability name with
  no registration on this host stays what it is today: a hard finalize error (the registry-readiness
  refactor's R9.2/R9.11 rulings, preserved). Every host registers every shipped plugin's
  capabilities, enablement being a separate axis, so an unregistered name can only be a typo, and
  the cross-host sharing story rides the enablement axis above, not name tolerance. Revisit only if
  out-of-tree plugins ever make unregistered-but-real names possible. Error quality does not
  regress: errors keep owner-scoped framing (`<owner>.<field>: ...`) and file/position context at
  least as good as today's.
- **FR13.** Drift is structurally impossible or test-caught: schema facts appear in exactly one
  authored place (the model), samples and describe output are rendered from it (so they cannot drift
  by construction), the renderer is pinned by tests over fixture schemas plus every bundled kind
  (rendered samples load cleanly and build a registry), and any remaining checked-in derived docs
  are pinned by tests that fail when regeneration would change them.

Wave 2 additions (folded in from the roadmap seed, 2026-08-05; the roadmap is
`docs/sdd/2026-08-04-next-steps/`):

- **FR18.** Structural secret-name reference extraction (rolls in issue #311). Secret references are
  derived structurally from the model's reference-annotated fields (the `SecretRef` marker and its
  owner-templated default), not by string-scraping the blob. This is FR6's reference typing carried
  to secret-name derivation specifically: adding or renaming a secret-bearing field changes the
  extracted references with no other edit, and the capabilities' current ad hoc secret-name logic is
  deleted.
- **FR19.** Contributed-sample uniform validation (rolls in issue #214). Operator-authored and
  plugin-contributed manifests validate through the single model regime exactly as first-party ones
  do, and unknown keys are hard errors there. FR12's strict closed-world direction is the resolution
  of #214's open warn-versus-error tradeoff; there is not a second, looser validation path for
  contributed content.
- **FR20.** Envelope `metadata.expires` rider (rolls in issue #170). An optional `expires` field is
  modeled once on the shared envelope `metadata` (beside `name` and `description`), so every kind
  inherits it uniformly rather than each kind re-modeling it. This effort models and validates the
  field only (a datetime); any behavior that acts on expiry is a separate effort and out of scope.
- **FR21.** Forward-compatibility with the living-graph roadmap. This effort keeps open the four
  doors the roadmap's `target-state.md` requires, and closes none: (a) reference extraction stays
  source-agnostic (a pure function of model, blob, and owner, indifferent to whether the blob is
  declared config or a future persisted instance spec); (b) the effective-config merge is a general
  layer-stack operation, not a template-inheritance-only chain, so a future runtime instance-spec
  layer composes on top; (c) the graph's post-finalize immutability stays a registry/fold property,
  not a model-layer assumption, so a future living-graph effort can relax it without touching the
  models; (d) nothing precludes one shared instance-state store (instance specs, facet
  applied-state, artifact-ownership records). FR15 and FR17 already realize parts of (a) and (b);
  FR21 records the whole set as an explicit non-regression constraint the design honors. Door (c)
  gained its first external consumer (roadmap ruling, 2026-08-05): the guide surface's gated
  read-side graph access modes expose only already-materialized data, which assumes exactly that
  immutability stays a registry/fold property.

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
- Generating doc content inside the prose guides (embedded generated field tables, a guide
  generation pipeline, drift tests over prose). Guides defer to the rendered samples and describe
  surface as the canonical field reference and carry pointers to them (the FR16 sweep); their
  remaining hand-stated field lists shed opportunistically as they are touched.

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
- No consumer-side default fallbacks remain for modeled fields (FR15): the instances the plan
  enumerates are gone, and the values consumers receive are fully resolved by the model layer.

## Dependencies and constraints

- The deprecation warning pre-work (PR #315) ships in a release before phase 1 merges, so operators
  get at least one released version that warns before the version that errors. The tagged-shape
  pre-support (PR #349: dual-shape decode with an aggregated warning, migrator emitting the tagged
  form) plays the same role for FR8's hardening: it ships in a release before the old shape becomes
  an error.
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

Decided with the operator on 2026-08-01:

- **Strictness.** Hard, helpful errors across the board, including unknown keys on every modeled
  surface; FR12 states the full posture. Refined later the same day: hard validation keys on the
  finalize fold's verdict, matching the registry's shipped pass order (dependencies totally
  extracted first, enablement computed without validating, then the throwing pass over READY and
  ENABLED resources), so a resource that emerges disabled or not-ready skips hard validation at load
  and its config problems bite at enable/use instead. Corrected once more after plan review:
  unregistered capability names stay hard finalize errors (R9.2/R9.11 preserved). The earlier
  self-disable answer was taken against a false baseline (a stale decode.py comment described
  tolerate-and-self-disable; the shipped, locked behavior is the hard error), and since every host
  registers every shipped plugin's capabilities, such a name can only be a typo; cross-host sharing
  rides the enablement axis.
- **Tagged-union shape break.** The naming-field-plus-blob pair collapses into one
  `name`-discriminated table (FR8), accepted as a breaking manifest change now, shipped with hard
  actionable errors on the old shape plus a manifest-upgrade mode in `agw resource migrate`.

Decided with the operator on 2026-08-02:

- **Effective-config validation.** There is no per-blob completeness validation: an inheriting
  surface's declared blob may be legitimately partial, so validation universally runs on the merged
  (effective) blob, resolved along the graph's chain at finalize; chain length is one everywhere but
  session templates, making the rule uniform. This moves the merged completeness check from
  session-resolve time to load, and FR12 states it.
- **Inheritance is not a dependency (FR17).** Raised by the operator against the overload risk: a
  child overriding a parent's default secret name must not inherit a transitive dependency on the
  default secret (a double prompt, at worst for a secret the child never uses). The inheritance edge
  is typed as source composition and excluded from runtime-need traversal; runtime needs derive from
  effective config only.
