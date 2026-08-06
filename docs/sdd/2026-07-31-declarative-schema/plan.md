# Plan: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-01

Status: DRAFT (authored alongside the FRD and HLA; implementation gated, see prerequisites)

## How to work this plan

- **Split delivery.** Phase 1 (the TOML sunset) merged to main on its own via PR #316 (2026-08-05),
  as a precondition that carries independent standalone value. Phase 2 was then HELD at the phase
  gate pending the next-steps roadmap SDD (`docs/sdd/2026-08-04-next-steps/`), which owned the
  capability-kind descriptor and the 0.14 compatibility-removal ordering. **The hold is released as
  of 2026-08-05** (descriptor contract PR #405, removals PR #406; see the phase-2 callout below);
  phase 2 runs on the `feat/declarative-schema-phase2` branch as a child of that roadmap. The SDD
  stays unlocked (no `locked.md`) until phase 2 completes, then locks summarizing both phases. This
  is the SDD skill's multi-branch model.
- The suite stays green at every step, with ONE deliberate, operator-approved exception: step 1.2's
  config-load hard error breaks ~58 test fixtures that declare resources in TOML, and the operator
  chose (2026-08-04) a bounded red window over the additive-first alternative. So the core 1.2
  production change lands first (suite red on a tracked, enumerated set of fixture files), and step
  1.2f converts those fixtures back to green by area. The window is CLOSED (suite fully green)
  before step 1.2 is checked off; phase 1 does not merge red.
- Each numbered step is delegated to an `agentworks-dev` subagent (LLD first where one is called
  for, then implementation), then reviewed by `agentworks-reviewer` before its boxes are checked.
- Every step's definition of done includes the standing gates: `ruff check`, `ruff format --check`,
  `mypy .` (strict), `pytest -q` all green, `./scripts/lint-files.sh` clean, plus the step's own
  criteria. Steps that touch the CLI surface include the completions check; steps that change
  operator-visible behavior update docs in the same commit.
- Checked boxes are immutable history; plan changes add new boxes rather than editing old ones.

## Prerequisites (hard gates, in order)

- [x] The codex harness effort has landed on `main` (operator direction, 2026-08-01: wait for it,
      then start; landed via PR #360 on 2026-08-02, alongside the EC2 vm-platform, PR #359). Its
      capability joins the phase 2 model inventory like any other.
- [x] PR #315 (TOML deprecation warning) and PR #349 (tagged-shape pre-support) are both in a
      shipped release (v0.13.0, cut 2026-08-04). Phase 1's hard error and phase 2's shape hardening
      each require one released warning version of runway (FRD dependencies).
- [x] Branch rebased onto `main` after the above (2026-08-04); capability inventory re-enumerated:
      the harness kind is now `harness-integration` (PR #383 rename; canonical field
      `spec.harness_integration: {name: ...}`, legacy selector warned until that SDD's 0.14.0
      removal phase), integrations are shell / claude-code / codex, and shell's config vocabulary is
      `command` / `resume_command` / `required_commands` (`restart_command` deprecated by the
      concurrent session-resume SDD, riding the same migrator rewrite pass). Steps 2.3, 2.4, and 2.5
      updated below.

## Phase 1: remove TOML resource declarations

### 1.1 Phase 1 LLD (the migrator verification rework is the core)

- [x] LLD `toml-sunset-lld.md` written and reviewed (2026-08-04; reviewer approve-with-changes, all
      nine findings folded in). It must settle:
  - The independent pre-side derivation for migrate verification (FRD FR2, HLA phase 1). Candidate
    to evaluate first: RELOCATE the TOML resource loaders into `migrate/` as the migrator's private
    TOML reader instead of deleting them outright. That satisfies "reads the TOML file directly",
    keeps the pre-side independent of the emission mapping (old loader semantics vs section-to-spec
    sweep), and still removes the loaders from the app's config-load path. If the LLD rejects this,
    it must name the alternative independent derivation.
  - The hard-error mechanics: where the `KIND_SECTIONS` check raises, the aggregated error text, and
    which commands keep the settings-only escape hatch (`resource migrate` moves to it;
    `resource sample --write` and `resource edit` fallback already use it).
  - What happens to the `--no-deprecations` channel contents (the TOML nudge becomes an error and
    leaves the channel; the #349 tagged-shape warning remains on it).
  - The fate of every consumer of the deleted loaders (decode shims, `resource edit`'s TOML pointer
    text, DB-migration snippet printers, doctor rows naming TOML sections, and the
    `tests/resources/test_graph_guard.py` allowlist that hardcodes `config/loaders_secrets.py`).

### 1.2 Hard error and loader removal (core production change)

Lands the whole production change in one reviewable body; the suite goes RED on the fixture files
enumerated by 1.2f (operator-approved bounded window, see the preamble). The step's own new tests
(below) land green; the red set is only pre-existing fixtures that declare TOML resources.

- [x] TOML resource loaders relocated into `migrate/` per the LLD; decode layer stops routing
      through loader shims, each decoder owning its per-kind logic (interim fork; phase 2 replaces
      it). The two vestigial `publish_to` functions are deleted, not emptied, and the dead
      `restart_command` config-channel block is removed (LLD discrepancies 1 and 2, folded in).
- [x] Config load hard-errors (aggregated, actionable, naming sections, pointing at
      `agw resource migrate` and `agw resource sample`) when any resource-declaring TOML section is
      present; settings sections load exactly as before. `Config`'s now-empty resource fields, the
      publish resource loop, and the `resources_loaded` guard are removed together (LLD section 3).
- [x] `agw resource migrate` works end to end on a fixture config (proxmox or azure section, git
      credentials, secrets, session template with a legacy harness selector) with the reworked
      verification: `pre_rows` scoped to selected units, emitted-key-set guard, rollback on
      mismatch. `test_full_migration_golden` and the new verification-independence test green.
- [x] Deprecation-warning tests for TOML sections replaced by hard-error tests (fires with sections
      present, not without; exempted commands still run); doctor renders a fail row and continues
      via the `resources=False` retry (not a truncated report).
- [x] The now-red fixture set is enumerated (file list grouped by area) and recorded in 1.2f, so the
      window is explicitly bounded, not open.

### 1.2f Close the red window (fixture conversion to green)

- [x] The ~58 TOML-resource fixtures are converted to YAML manifests (or hand-built registry rows
      where a test asserts registry/graph outcomes), by area: config, resources, sessions,
      git-credentials, vms, plugins, orchestrated. A shared resources-dir fixture helper is added
      first (none exists today; 28 files already author manifests inline as the pattern). Tests that
      assert on removed `Config` fields or pin TOML-only behavior (`declared_at` line capture,
      decode-through-loader parity) are redesigned, not just relocated.
- [x] Suite fully green (`pytest -q`, `mypy .` strict, ruff, lint-files) with the window closed;
      only then is step 1.2 checked off.

### 1.3 Phase 1 records

- [x] Superseding ADR replaces ADR 0016's dual-path stance (the ADR the status note promised); ADR
      0016 gains its superseded-by pointer.
- [x] Guides, cli README, root README, and sample-config.toml describe only the manifest path
      (sample-config's pointer comments already do; sweep for stragglers).
- [x] Dated lockfile entries: resource-manifests (TOML path removed; which shipped machinery from
      that SDD is retired) and any other locked SDD whose stance this revises.
- [x] The removal commit carries the breaking-change marker (`!` / BREAKING CHANGE footer) so
      release-please surfaces it, and an operator upgrade note (run `agw resource migrate` before
      upgrading, or after via the escape hatch) is written for the release notes.
- [x] Reviewer pass on the whole phase; findings fixed.

## Phase 2: the declarative schema model

> **HOLD RELEASED (2026-08-05).** The phase gate is lifted: the next-steps roadmap SDD
> (`docs/sdd/2026-08-04-next-steps/`) settled both prerequisites. The capability-kind descriptor
> contract merged via PR #405 (`capability-descriptor-contract.md`, the authority for step 2.0
> below), and the 0.14 compatibility removals landed and locked via PR #406
> (`docs/sdd/2026-08-05-deprecation-removal/`). Phase 2 (wave 2 in the roadmap) now executes on the
> `feat/declarative-schema-phase2` branch. This SDD is a child of that roadmap: the roadmap lead
> reviews these PRs before merge and tracks status from merged PRs (this SDD does not edit the
> roadmap ledger); inconsistencies found in roadmap or sibling SDDs are flagged, not edited.
>
> Two structural changes the roadmap seed folds into this plan, integrated below. (1) Phase 2
> executes THROUGH the descriptor: a new **step 2.0** adopts it before the schema foundation. (2) A
> capability declares the config it **OFFERS** as a fixed set, exactly as it declares its fixed set
> of API methods, and consumers choose which config they use; registration carries
> `contract_version` and registration-time conformance from day one. (Two superseded designs, both
> rescinded before any model registered through them: the seed's schema slots on 2026-08-05, and
> consuming-kind keying on 2026-08-06. 2.1/2.3/2.5 proceed as originally planned.) The removals
> having landed first discharges the harness-selector-shim and `restart_command` coordination notes
> in 2.5 (confirmed in code at seeding); the 2.4-before-2.5 hardening order stands (the generic
> sibling-shape deprecation is still live on main, so 2.4 is real work). The four open doors from
> the roadmap's `target-state.md` (source-agnostic extraction, layer-stack merge, graph immutability
> as a registry/fold property, one instance-state store) are honored, not closed; see FR21.

### 2.0 Descriptor adoption (step zero)

Adopt the capability-kind descriptor per
`docs/sdd/2026-08-04-next-steps/capability-descriptor-contract.md` before any schema modeling.
Always-green and mechanical: stand up the descriptor table from the existing wiring, then derive one
switchboard site at a time with the full gate passing after each. `KIND_REGISTRY` and manifest
decode's `KIND_SECTIONS` legitimately enumerate all resource kinds and STAY; only the duplicative
per-kind capability branches are derivation targets. Secret-backend's constructed-singleton registry
policy is recorded as an explicit descriptor-carried interim exception (wave 3 removes it);
`_VMPlatformKind` moves in from `vms/kinds.py` for symmetry.

- [x] `descriptor-adoption-lld.md` written and reviewed: the frozen `CapabilityKindDescriptor`
      record shape (day-one fields per the contract, deferred fields recorded with their triggers),
      the single descriptor table as the only capability-kind enumeration, the generic adapter
      parameterized by descriptor (replacing the four hand-written five-method adapters), and the
      registration-time conformance checks (`implementation_contract`, required metadata,
      side-effect-free constructibility, required ops, per-slot model conformance,
      `contract_version`) that replace the current type-and-cast seam. Settles the contract's open
      questions carried to the seed: the constructibility check shape, slot-vocabulary naming (and
      whether the default slot is spelled in single-slot kinds), and whether the four entry
      dataclasses unify behind a generic entry or stay per-kind behind `entry_factory`.
- [x] `_VMPlatformKind` relocated into `capabilities/vm_platform/kinds.py` (LLD section 9); pure
      relocation, full gate green. (Commit `2f4d8c77`.)
- [x] Descriptor table introduced, populated from existing wiring; full gate green (no site derives
      yet, table is additive). (Commit `7b8d69c4`; reached via the `capability_descriptors()`
      accessor, LLD section 2's implementation note.)
- [x] Registration-time conformance wired into `register_plugin`'s pass 1 (LLD section 4: contract
      shape, required metadata and attributes, side-effect-free constructibility, required
      operations, `contract_version`), replacing the type-and-cast seam. Rejection happens before
      any registry mutation, so atomic seating is preserved; negative tests cover every defect
      class. Behavior-additive: all shipped built-ins and the onepassword plugin conform. (Commits
      `2d366643`, `98e80831`.)
- [x] Each switchboard site derived from the descriptor, one commit per site, full gate green after
      each: the adapter table (`plugins/adapters.py` `CAPABILITY_ADAPTERS`), the graph kind set and
      readiness dispatch (`resources/graph.py` `_CAPABILITY_KINDS`, `_capability_node_readiness`),
      the per-kind registry loaders (`resources/graph.py` `_CAPABILITY_REGISTRY_LOADERS`), bootstrap
      publication (`bootstrap.py`), and the plugin snapshot/restore tuple (`plugins/registration.py`
      `_capability_registries`). Manifest decode's per-kind capability branches derive too, except
      the phase-1 interim decode fork the LLD scopes to 2.5. The migrator's kind-participation flags
      stay hand-maintained (deferred `migration_participation` field; the migrator is a deliberately
      independent frozen oracle). (Commits `314f5402` snapshot tuple, `de6ee365` bootstrap
      publication, `c296b74e` registry loaders, `a59ff186` kind set and readiness, `a1892685`
      adapters, `459ab313` decode host surfaces.)
- [x] The guard test (`tests/plugins/test_plugin_framework.py`
      `test_capability_adapters_keys_match_the_capability_category_kinds`) flips from "detect an
      omitted site" to "assert every site derives from the descriptor"; the sibling drift guards
      (`test_recipe_gate_drift.py`, `test_harness_integration_gate_drift.py`) are reconciled.
      (Commit `1a4a9ece`; renamed to
      `test_every_capability_switchboard_site_derives_from_the_descriptor` since the old name no
      longer described it, and proven non-vacuous by mutation. Sibling guards got docstring-only
      changes, no assertion touched.)
- [x] Reviewer pass on the whole step; findings fixed. (Roadmap lead reviews the PR before merge.)
      Three independent reviewer passes, one per batch, all findings fixed. The substantive ones:
      `interactive` escaped the conformance check entirely and would have surfaced as a late
      `AttributeError` in the resolve loop (fixed via `required_attributes`); the graph guard's
      pattern-2 coverage had silently shrunk to "reads spelled with the constant's name" once
      modules moved to `descriptor.registry()` (fixed with a second detector); session-template's
      wave-1 hardening had become one token in a data record with NO behavioral test, so flipping it
      un-hardened the manifest shape while only two table-shape tests noticed (fixed with two
      negative tests in `test_capability_shape.py`); and the flipped guard proved agreement but not
      derivation, since set-equality is satisfied exactly by a re-hardcoded enumeration (fixed with
      a source-level AST pin over all six derived sites). The last three were each verified by
      re-running the mutation that motivated them, by the lead independently of the implementer.

**Progress note, 2026-08-05 (paused here for a design revision).** The first batch (the three boxes
above, LLD section 10 steps 1-3) is landed, reviewed, and green at 3443 tests; the reviewer's two
substantive findings are fixed (`interactive` escaped the conformance check entirely and would have
surfaced as a late `AttributeError` in the resolve loop; the four new graph-guard exemptions are now
function-scoped rather than whole-file, commit `32d3b88f`). Two review calls were taken beyond the
LLD: `contract_version` no longer defaults on `Capability` (each impl states its own, so a future
base bump cannot silently re-certify unmigrated impls), and the conformance branch keys on
protocol-ness rather than `Capability`-ness (so a future plain-ABC contract keeps its nominal
check). All review minors are now swept in `8b415982`: a deterministic stub pins BOTH vm-platform
readiness branches and the exact blocked sentence on every host (previously the blocked branch ran
only because `wsl2` happens to be unsupported on Linux), non-vacuity guards cover the three
descriptor tests that iterate registry contents (the implementer found a third instance beyond the
two the review named), and `descriptor.py`'s lazy-collection docstring now claims cycle safety only.

**Design revision, 2026-08-05 (operator, roadmap note 3).** Schema slots were rescinded before any
model registered through them, and `config_slots` was deleted from the descriptor rather than
simplified (it had no reader, could not be typed until 2.1, and its only reason to exist early died
with the mechanism). Config schemas are keyed by CONSUMING RESOURCE KIND from day one; the settled
shape is recorded in the step 2.0 LLD section 7, HLA Component 0, and step 2.3 below. Steps 4-10 of
the derivation sequence are not started.

### 2.1 Schema foundation

- [x] `schema-foundation-lld.md` written and reviewed (2026-08-06; reviewer found four BLOCKING
      defects, all fixed: `extra="forbid"` on a `RootModel` raises at class definition, so the
      configs split; `RefRelationship` moved to `resources/reference.py` to kill an import cycle the
      design itself created; the extraction visited-set is path-scoped, not accumulating, so sibling
      fields of one nested model type cannot silently drop edges; and the error bridge owns
      framed-batch rendering, since multi-line aggregation would otherwise leave lines unlocated and
      regress FR12. `FieldDoc` gained `choices`/`constraints` and a `ModelDoc`, because it is a
      cross-SDD coordination point and widening it later means renegotiating with the onboarding
      child. The two load-bearing pydantic claims were verified by execution against 2.13.4 by the
      lead, not read from docs): base model config (strict, frozen, `extra="forbid"`), the
      `SecretRef` / `ResourceRef` `Annotated` markers and their `json_schema_extra` (`x-agw-*`)
      encoding, `extract_references` (total, never raising, reads raw blobs, applies owner
      templates) and `iter_field_docs` signatures, and the pydantic pin policy (latest stable v2 at
      implementation time, checked then, not from memory).
- [x] `agentworks/schema/` package implemented with unit tests (relocated from the
      originally-specified `resources/schema/`; a package under `resources/` cannot be the leaf),
      including totality tests for `extract_references` over malformed blobs (property-style: no
      input raises) and marker round-trip into emitted JSON Schema.
- [x] pydantic dependency added; mypy plugin enabled; strict mypy green across the repo.
- [x] `pydantic` and related vocabulary promoted from the SDD cspell dictionary to the root
      dictionary (it now appears in permanent code).
- [x] **Collections of models walk in both walkers** (`list[Model]`, `tuple[Model, ...]`,
      `dict[str, Model]`), with the element index or mapping key in the `FieldDoc` path. Surfaced by
      the 2.1 implementation: the LLD's walk enumerated a marked list of SCALARS and a single nested
      model, but not a collection whose elements are models, so such a field contributes no
      references and does not expand. Extraction is latent today (nothing shipped puts a marked
      field inside a model collection) but FIELD DOCS ARE LIVE: aws-ec2 `instance_types`
      (`plugins/aws/platform.py:144`) and azure-vm `vm_sizes` (`plugins/azure/platform.py:309`) are
      operator-overridable catalogs of models whose entry fields would render opaque, which FR10
      forbids. Fixed here rather than at 2.8, because discovering it there means reworking the
      renderer's input contract after the onboarding child's guide has started consuming it.
- [x] Structural secret-name reference extraction (issue #311): the `SecretRef` marker carries the
      owner-templated default name, and `extract_references` derives secret references structurally
      from the annotated model fields rather than by string-scraping the blob. This is the
      model-layer replacement for the ad hoc secret-name derivation the capabilities do today;
      pinned by a test that a renamed/added secret field changes the extracted references with no
      other edit.

### 2.2 Error bridge

- [x] `error-bridge-lld.md` (may fold into 2.1's LLD if small): `ValidationError.loc` to
      owner-framed message mapping, message normalization rules, `SourceLocation` framing, and the
      severity plumbing for fold-gated validation (FR12: the bridge raises for READY+ENABLED
      resources; the same rendering is reusable as diagnostic text elsewhere). **FOLDED into
      `schema-foundation-lld.md` (2026-08-06)**, which settles all of the above. The fold is
      justified there: the bridge's only input is the `ValidationError` that LLD's base model
      raises, and shipping a strict base model with no rendering would put raw pydantic text on main
      as a regression against every message it replaces. Only the LLD folds; the implementation box
      below stays its own step. **Correction to this box's wording:** "severity plumbing" describes
      machinery that ALREADY exists. `Registry.finalize` pass 7 (`resources/registry.py:466`)
      already scopes the throwing validate to the READY + ENABLED set, with the R3/R9.4 reasoning in
      its docstring. Step 2.2 builds no severity mechanism; the real requirement behind the phrase
      is a PURE rendering entry point (`render_validation_error`) beside the throwing one, so the
      same text is reusable as diagnostic output.
- [x] Bridge implemented with the FRD's representative-mistakes corpus as a pinned test: unknown
      key, wrong type, missing required field, bad capability name, each asserting owner framing and
      file/position context at least as good as today's. (The old-sibling-shape corpus entry lands
      in 2.4, where its bespoke error exists to pin.)

### 2.3 Capability config models (the contract flip)

- [ ] `capability-contract-lld.md` written and reviewed: the registration surface, the interim
      tagged-table synthesis while decode still routes through the phase-1 decoders, the typed-ops
      migration per capability (the hidden bulk), retirement of the stale tolerate-and-self-disable
      comment in `manifests/decode.py` (~298-301), which misstates the shipped R9.2 hard-error
      behavior, and effective-config validation (operator decision 2026-08-02): validation runs on
      the MERGED blob only, resolved along the graph's `inherits` chain at finalize (chain length
      one everywhere but session templates), never on a partial declared blob. The LLD settles the
      per-key provenance the merge tracks for error attribution, the two-stage reference extraction
      (structural refs per declared blob feed the graph; secret refs read the effective blob), the
      inheritance edge as a typed, non-dependency edge (FR17: excluded from the secret union,
      resolvability prediction, and dependency listings; readiness/enablement propagation across it
      is this LLD's policy call), and the retirement of the session resolver's use-time completeness
      call (`sessions/templates.py::_validate_merged`) in favor of the finalize pass.
  - **FR17 starting point, surveyed 2026-08-06.** Half the machinery already exists and the LLD must
    not rebuild it: `ResourceReference` (`resources/reference.py:59`) has typed subclasses
    `SecretReference` and `TemplateReference`, and all four template kinds emit `TemplateReference`
    from their `for parent in self.inherits` loop (`agents/template.py:59`, plus the vms,
    workspaces, and sessions equivalents) with usage "a parent template". The subclass survives into
    `edges_of`, so the type is available to consumers at runtime. What is missing is only the
    consuming half: NO traversal distinguishes today, so an inherits edge and a uses edge are
    treated identically, which is exactly the overload FR17 was raised against.
  - **Type the RELATIONSHIP, not the target kind (LLD requirement).** `TemplateReference` is
    documented as "targeting a template-kind Resource", so it types the target, and it coincides
    with inheritance today only because `inherits` is currently the sole reason to point at a
    template. FR17 must not be implemented as `isinstance(ref, TemplateReference)`: that filter
    really means "points at a template" and would silently misclassify any future uses-a-template
    edge as inheritance, reintroducing the same conflation one level down. The LLD names the
    explicit relationship marker instead. - [x] Per-capability config offered per FACET (operator
    ruling, 2026-08-06, roadmap note 4; settles the contract after two superseded designs, schema
    slots and `config_model_for(consuming_kind)`). A facet is the level a capability is driven at
    (`vm`, `user`, `workspace`, `session`), pairing that level's methods with its config. A
    capability offers a fixed set of facet configs the way it offers a fixed set of API methods, and
    consumers choose which facet they drive, so producers never know their consumers; core asks
    `config_for(facet)` (names indicative). **The ordinary case stays invisible:** every capability
    today has one config shared by all its operations, so it writes `config_model = X` and names no
    facet. Only harness-integration declares per-facet configs, in wave 4. **Facets are NOT scopes
    and core owns the mapping:** admin and agent both resolve to `user`, session start and resume
    share `session`, so a vm-template admin attachment and an agent-template agree by construction
    rather than by each capability encoding it. **Readable at finalize:** core reads the
    facet-config association before any method runs, so it must be declared data, not merely a
    signature annotation. Asking for a facet a capability does not offer is a hard error naming what
    it does offer; pin it with a test. **Config presence is NOT the support claim** and must not
    become one, or this is the rescinded slot mechanism under a new name: support is carried by the
    implementation, and accepting no config at a facet just means emitting no schema there. "Facet"
    is a plain noun here; the machinery meaning retired on 2026-08-05 (declaration contracts,
    support-by-presence, grants) stays dead. The descriptor's deferred `config_schema` field (the
    kind's model contract) is created here, and union assembly is per `(kind, facet)`, reducing to
    today's per-kind union while every capability declares one config and no facet. Registration
    carries `contract_version` (day-one, operator ruling) and passes the registration-time
    conformance checks from 2.0 (implementation-contract, metadata, constructibility, required ops,
    plus config model conformance added here) that replace the retired type-and-cast seam. The
    secret-backend `mapping_model` registers as that kind's config model; its constructed-singleton
    instance policy stays the descriptor-carried interim exception (wave 3 re-homes it, not this
    effort). Empty-config capabilities register the shared empty model. Inventory re-enumerated
    2026-08-02, still re-check at implementation: vm-platform lima, wsl2, azure-vm (including the
    nested `service_principal` model), proxmox, aws-ec2 (new, renamed from ec2 by PR #363; nested
    `credentials` model with `access_key_secret` as a `SecretRef` defaulting to the well-known name,
    plus the `instance_types` catalog); git-credential-provider github (scope union: repos/owner
    mutual exclusion as a model validator; `token` as `SecretRef` with the `git-token-{owner_name}`
    template), azdo; harness-integration (the kind renamed by PR #383) shell (config: command,
    resume_command, required_commands; the `restart_command` alias was removed by the session-resume
    SDD before wave 2, so shell's config is just those three fields), claude-code, codex
    (`extra_args` list plus flag fields); secret backends env-var, prompt (no mapping), onepassword
    (mapping is itself a union: `op://` string or account/reference table). **Modeling consequence
    (2.1 LLD):** a mapping whose root is a bare string or a string-or-table union cannot be a
    `BaseModel` at all, so backend mapping models extend the root-model base, not the mapping base.
    Do not model the generic `False` opt-out into a backend's model: it is filtered by the loop
    before any backend sees it (`secrets/base.py:133`).
- [ ] **Strict-mode tightening is a BREAKING change and ships with an operator note.** The 2.1 base
      model is `strict=True`, and while today's hand-rolled validators are almost all
      `isinstance`-strict already, proxmox is not: `plugins/proxmox/platform.py:93` does
      `int(str(config["template_vmid"]))`, so a quoted `template_vmid: "9000"` loads today and
      becomes an error. Its `api_url`, `node`, `token_id`, `storage`, `bridge`, `pool`, and
      `verify_ssl` get no type check at all today, and `verify_ssl` is consumed as
      `bool(self._cfg("verify_ssl", True))`, so `verify_ssl: "no"` currently means `True` and
      becomes an error too. Taking the break rather than carving out `Field(strict=False)` matches
      the operator's standing direction (hard, helpful errors everywhere; break the schema now if
      ever), but it needs a breaking-change marker and an upgrade note naming proxmox specifically,
      and the migrator should be checked for whether it emits quoted scalars. **Second break for the
      same note (surfaced by the 2.1 implementation):** an explicit `secret: null` currently OMITS
      the edge for azure, aws, and proxmox, because they read `config.get(key, DEFAULT)`. The model
      rule is that absent OR `None` emits the owner template, matching git-credential's
      `token_dependency` today, so `null` flips from "no dependency" to "the default-named
      dependency" for those three. Deliberate and tested, and it makes the four capabilities
      consistent, but it is operator-visible and belongs in the same note. **It flips VALIDATION on
      ALL THREE cloud platforms, which is the more visible half** (corrected 2026-08-06; an earlier
      note here said azure alone, and the 2.3 implementer caught it). Azure's
      `_parse_service_principal`, aws's `access_key_secret` check, and proxmox's `token_secret`
      check each raise a `ConfigError` today on an explicit `null` whose message tells the operator
      to OMIT the key instead, and under the model that same input silently resolves to the
      default-named secret. Verified against the pre-flip sources. An operator who followed the old
      error's advice will not otherwise connect the two, so the 2.9 note must name all three.
      **Fourth break for the same note (found by the 2.5 LLD, 2026-08-06):** `agent-template`
      accepts and silently DROPS `username` and `git_force_safe_directory` today (both are in
      `_AGENT_TEMPLATE_KEYS` but neither is a field), so modeling the kind turns two
      silently-ignored keys into hard errors. An operator who has been setting either has never had
      it take effect, which makes the error a fix, but their config still stops loading.
- [x] Core-driven validation and extraction wired: registry name-to-model maps per capability kind;
      `Capability.validate` / `Capability.dependencies` classmethods and
      `SecretBackend.validate_mapping` retired; per-capability hand-rolled validate code deleted;
      `capabilities/git_credential/base.py`'s token helpers absorbed into the model layer.
- [x] Construction binds the validated model instance; ops read typed fields (this is a real
      per-capability migration: azure and proxmox ops currently read `self.platform_config[...]`
      dict keys; each capability's op code moves to model attributes with mypy enforcing it).
- [x] Fold-gated severity proven by tests: broken blob on a disabled plugin's resource loads with
      the row marked, errors on enable/use; broken blob on an enabled resource is a load error; an
      unregistered capability name remains a hard finalize error (R9.2/R9.11 preserved, operator
      decision 2026-08-01; the cross-host story rides the enablement axis, not name tolerance).
- [x] `test_capability_config_contract.py` and `test_capability_base.py` reworked to pin the new
      contract (declare-and-receive: models in, typed instances out).

### 2.3b Effective-config validation at finalize (deferred out of 2.3)

Designed in `capability-contract-lld.md` sections 12 and 14 but deliberately not built there: it
shares no code with the contract flip and would have doubled that step's review surface. Kept as its
own step rather than folded into 2.4 or 2.5 so it cannot quietly evaporate, and sequenced next
because FR17 is an operator-raised requirement and the longer it waits the more consumers assume the
current traversal.

- [x] Validation runs on the EFFECTIVE (merged) config, resolved along the graph's inherits chain at
      finalize, never on a partial declared blob (a child template's blob is legitimately partial,
      so a model's required fields would wrongly reject it).
- [x] Per-key merge provenance tracked for error attribution, so a message names the layer the bad
      key actually came from.
- [x] FR17's traversal split: the inherits edge stays a typed, non-dependency edge, excluded from
      the secret union, resolvability prediction, and dependency listings. Per the FR17 survey
      already in this plan, mark the RELATIONSHIP explicitly; do NOT filter on
      `isinstance(ref, TemplateReference)`, which means "points at a template" and would silently
      misclassify a future uses-a-template edge.
- [x] FR17 pinned by a regression test over a fixture inheriting surface: a child overriding the
      parent's default secret name declares only the override in its refs, the parent keeps its own
      default-secret edge, and no runtime-need traversal (secret union, resolvability prediction,
      dependency listing) attributes the parent's default secret to the child.
- [x] `sessions/templates.py::_validate_merged` retires in favor of the finalize pass. Step 2.3
      repointed it at the core entry point (no capability code runs) but left its resolve-time
      timing, so the timing change lands here.

**Closed 2026-08-06 at 4441 tests.** Two recorded deviations, both accepted by the lead. (1) INBOUND
dependency listings (`dependents_of`) deliberately still cross the inheritance edge, because a
parent template genuinely IS referenced by its children; FR17 is amended to say OUTBOUND. (2)
Inherited harness references are attributed at BLOCK granularity, to the layer that selected the
integration, because a `ConfigReference` carries no field path. Exact for every shape a template can
currently write; the inexact case needs a child that restates the selector while inheriting a
ref-bearing key it does not touch, and closing it means threading field paths through the schema
package's hot walker for a path no shipped integration exercises. Documented rather than hidden.

Also landed here beyond the boxes: `reachable_from` deleted (no caller survived the gate narrowing,
and its only property was crossing everything, which is what a future consumer reaches for instead
of deciding), and the closures now name the relationships they CROSS rather than the ones they skip,
so a third `RefRelationship` joins neither closure and `test_every_relationship_has_a_closure` fails
until someone decides.

### 2.4 Tagged-shape hardening

- [ ] `tagged-hardening-lld.md` written and reviewed: the old-shape detection and error text, how
      the old-shape error survives 2.5's decoder-to-model swap, and the manifest-upgrade mode as a
      GENERALIZATION of the migrator's YAML-rewrite machinery (**corrected 2026-08-06: NOT
      shipped.** Wave 1's `6d44a12c` deleted `YamlRewrite` and dropped the ruamel dependency along
      with the compatibility surfaces it served, so step 2.4 recovered it from that commit's parent
      and generalized it. The outcome matches this box's intent; the premise that it was standing
      machinery was stale. The recovery also improved on the original, which never set
      `preserve_quotes`) (PR #383's `YamlRewrite`: ruamel round-trip with document-marker text
      patching, digest/CAS guards, backup-first rollback, YAML-native units), extended from the
      bespoke session-template selector fold to the platform/provider sibling fold. (The
      harness-selector and `restart_command` removals already landed in wave 1, so the earlier
      cross-SDD coordination is discharged; this step is scoped to the still-live
      `platform`/`platform_config` and `provider`/`provider_config` sibling shapes.)
- [ ] Old sibling shape (`platform` + `platform_config`, `provider` + `provider_config`) becomes a
      hard error naming the exact rewrite; #349's dual-shape normalization, its aggregated warning
      channel (`ManifestSet.deprecation_issues` for shape), and the bundle-gate special case are
      removed (the hard error makes the gate redundant).
- [ ] The old-sibling-shape entry joins the representative-mistakes corpus here, pinned end to end
      so 2.5's swap cannot degrade it to a generic unknown-key error (in the model regime the old
      shape would otherwise surface as exactly that on `platform` plus `platform_config`).
- [ ] `agw resource migrate` gains the manifest-upgrade mode (backup-first discipline reused;
      completions updated for any new flag/subcommand).
- [ ] Upgrade mode proven on a fixture resources dir authored in the old shape (comments preserved
      per the LLD's decided policy; result loads clean; idempotent re-run is a no-op).
- [ ] The hardening commit carries the breaking-change marker and an operator upgrade note (run the
      manifest-upgrade mode) for release-please.
- [ ] Decide `agw resource migrate`'s future (the roadmap hands this decision to wave 2). Options:
      it stays as the frozen TOML-to-YAML oracle plus the new manifest-upgrade mode, or it retires
      once the last legacy shape is gone. Record the decision and its rationale here; if it retires,
      that is its own commit with an operator note. Note the descriptor's deferred
      `migration_participation` field exists precisely for the "migrate survives and derives from
      the live descriptor" branch, against the counterargument that the migrator is a deliberately
      independent frozen oracle.

**Step 2.4 records, landed 2026-08-06.**

- **The LLD box is ruled WAIVED, not skipped.** No `tagged-hardening-lld.md` was written: the
  concurrent agent authored the 2.5 LLD instead, and 2.4 turned out to be a flip of a mechanism step
  2.0 had already designed (`descriptor-adoption-lld.md` section 6's `legacy_string_shape`) rather
  than new design. The three calls that did need deciding were made by the implementer and reviewed
  explicitly: deleting `legacy_string_shape` once every surface rejects, upgrading whole-tree rather
  than selector-scoped, and folding parsed values for the verification pre-side. The reviewer signed
  off on all three and independently verified the whole-tree premise. Recording the waiver here so
  the absence is a decision rather than an omission.
- **`agw resource migrate`'s future, DECIDED:** it survives, and stays a deliberately independent
  frozen oracle with a hand-maintained kind-participation table. The descriptor's
  `migration_participation` field stays deferred and uncreated. Deriving the migrator from live
  wiring would defeat the independence phase 1 built deliberately, which is the whole reason the
  TOML loaders were relocated into `migrate/` rather than deleted. Rationale also carried in
  `manifest_upgrade.py:25-30` so it survives this SDD's deletion.
- **Selector asymmetry, deliberate:** selectors scope TOML units only; the manifest-upgrade half is
  always whole-tree, because a leftover legacy document makes the post-registry verification load
  raise, so a scoped run cannot complete. Documented in `cli/README.md` and
  `docs/guides/resources.md` and previewed before confirmation.

### 2.5 Kind spec models replace the decoders

- [ ] `kind-spec-models-lld.md` written and reviewed: the per-kind model-vs-thin-wrapper calls,
      semantic-validator placement (name/length caps, cross-field rules), and the decode entry point
      contract the swap preserves. The session-template model is simpler than earlier drafts
      assumed: the legacy `harness`/`harness_config` selector shim and the `restart_command` alias
      were both removed in wave 1 (confirmed in code at seeding), so the model has no compatibility
      shim to absorb. It covers the canonical `harness_integration` tagged surface only. The phase-1
      interim decode fork this step also resolves (how much the 2.0 descriptor adoption already
      absorbed is settled in the 2.0 LLD; the remainder lands here).
- [ ] Kind-by-kind migration behind the stable decode entry points, smallest first to bed in the
      pattern: apt-package, apt-source, system/user-install-command, workspace-template,
      named-console-template, admin-template, agent-template, vm-template, secret, git-credential,
      vm-site, session-template. Each kind's box covers: model (with semantic validators for
      name/length caps and cross-field rules), decode swap, error parity via the bridge, tests
      updated.
- [ ] Unknown-key handling flips from warn to hard error for kind specs (FR12): the
      `_warn_unexpected_keys` machinery retires with the last kind; tests updated accordingly.
- [ ] `migrate/verify.py` normalization taught the model shape (the dataclass-only
      `strip_source_fields` stops silently no-oping when decl classes become models).
- [ ] Decl classes are frozen models (or thin wrappers where behavior-rich; per-kind LLD calls),
      with `DeclaredResource`'s hooks preserved for the registry.
- [ ] `metadata.expires` rider (issue #170): model the optional `expires` field once on the shared
      envelope `metadata` (alongside `name` / `description`), not per kind, so every kind inherits
      it uniformly. Scope is the modeling and validation of the field (a datetime, TOML/YAML native
      or RFC 3339 string); any behavior that acts on expiry is out of scope and left to its own
      effort. Pinned by a test that the field validates on any kind and rejects a malformed value.

- [ ] **Row-shape change forced by `tagged_config`'s deletion** (no box existed for this; added
      2026-08-06). Retiring the synthesis means the rows themselves carry the tagged capability
      table, which touches roughly twenty read sites plus three signatures. Scope it explicitly
      rather than discovering it mid-swap.
- [ ] **`EnvEntry` becomes a model and loses `key`** (forced by the frozen-model box, which does not
      imply it; added 2026-08-06). Fourteen test modules reference it.
- [ ] **Two error-bridge defects that step 2.5 is the first consumer to hit**, both verified by
      execution in the 2.5 LLD: a constrained dict key renders as `env.1BAD.[key]`, and an
      undiscriminated union produces three lines carrying pydantic's member labels where today's
      message is one line. Leaving either ships a worse error than the code being replaced.
- [ ] **Two PERMANENT files still cite the pre-relocation schema path**: `cli/pyproject.toml`'s
      pydantic comment and `cli/agentworks/schema/reference.py`'s docstring. Permanent docs must
      match HEAD, so these are corrected here rather than at 2.9.

### 2.6 Model-layer defaulting (FR15)

- [ ] Sweep and enumerate every consumer-side fallback for modeled fields (the
      is-not-None-else-literal and or-literal idioms on request/config reads). Known at authoring
      time: azure cpus=4 / memory=8 / disk=50 / swap=0, lima cpus=4 / memory=8 / disk=50 / swap=0
      (`capabilities/vm_platform/lima.py` ~220), wsl2 swap=0 (`wsl2.py` ~545), proxmox swap=0; the
      sweep is the authority and the in-tree platforms carry the same literals as the plugins.
- [ ] Defaults declared on the models; post-decode types non-optional where defaulted; every
      enumerated fallback deleted; consumers observing an unexpectedly-unset modeled field raise
      (DB-sourced legacy rows included: raise, never locally default).
- [ ] The end-to-end fixture-capability test (FRD success criterion): add a field with a default and
      description to a fixture capability model and prove validation, extraction, sample, describe,
      and emitted schema all reflect it with no other edits.

### 2.7 Schema emission and editor association

- [ ] JSON Schema (2020-12) emitted per kind plus the envelope schema, unions expressed as `oneOf` +
      discriminator over `name`; CLI surface (working name `agw resource schema`) prints/writes the
      set; completions tree updated for the new command.
- [ ] `agw resource sample --write` and migrator-emitted YAML stamp the yaml-language-server
      modeline referencing written schema files; end-to-end check that a schema-aware editor setup
      validates a sample manifest (documented manual check plus an automated schema-validates-the-
      sample test using a JSON Schema validator in tests only, if the LLD approves the dev-only
      dependency).

### 2.8 Live-rendered samples and describe

> **Coordination SETTLED, 2026-08-06.** The onboarding effort's topic-content-contract message
> arrived on `main` (`9db60238`), so the gate that stood here is discharged. Wave 2 confirms all
> five requested alignments, with two scope clarifications, and 2.8 adopts the contract's prose
> portion as its blurb source instead of inventing a blurb registry.
>
> **Confirmed:** (1) `summary` plus `Overview` are the sole authored prose source for BOTH describe
> and guide, replacing this plan's earlier separate-blurb framing; (2) `FieldDoc`, emitted schemas,
> and sample inputs stay presentation-free, which they already are, since `render_type` was
> deliberately split out of `FieldDoc` in 2.1 for exactly this; (3) 2.8 exposes reusable SERVICE
> functions for field reference and samples, not just CLI commands, because guide must call APIs and
> never scrape rendered CLI output; (4) disabled implementations render from registered models
> without being constructed, which this plan already required and 2.0's side-effect-free
> constructibility discipline already enforces; (5) no standalone blurb registry and no
> rendered-output adapter.
>
> **Scope clarification 1 (matches their own wording, "use the prose portion").** Wave 2 authors
> ONLY the prose fields (`title`, `summary`, `Overview`) and the colocation convention. The envelope
> is onboarding's: topic slugs, `anchor`, the block vocabulary, `related_topics`, the core topic
> catalog, and duplicate-slug validation. Wave 2 building the catalog would balloon 2.8 and make it
> depend on an HLA that has not settled its own root vocabulary yet.
>
> **Scope clarification 2.** Topic data does NOT go on the capability-kind descriptor in wave 2.
> Their contract says the descriptor "may transport" it but does not own it, and wave 2 has no
> reader for it, so per the descriptor's minimal-by-rule discipline it stays a deferred field with a
> trigger. Colocation beside the implementation is enough for onboarding's catalog to collect.
>
> **Still open, and NOT settled by their message:** the describe surface's NAME. This plan flagged
> it as needing coordination with the onboarding child, and the contract does not name it. 2.8
> cannot close without it; raised to the operator.

- [ ] `emission-and-renderer-lld.md` (shared with 2.7 if the seams overlap) written and reviewed:
      renderer output contract, blurb registration surface, and the describe surface's naming. Per
      the roadmap's guide-surface note (2026-08-05), the LLD must (a) shape blurbs as structured
      markdown data (identity, level, title, body; never pre-rendered CLI text) so the onboarding
      child's `agw guide` can compose them into topic pages without forking the authored layer, (b)
      name the colocation convention explicitly (built-in blurbs live beside the kind they document;
      plugin blurbs ride plugin registration) since the guide effort inherits that convention rather
      than inventing a second home, and (c) record the templating guardrail: blurbs are inert prose
      today, and any future dynamic placeholders adopt the guide's locked-down template vocabulary,
      never a second dialect.
- [ ] Renderer over `iter_field_docs`: commented-YAML skeleton per kind and capability arm (one
      union arm rendered, alternatives listed), merged with registered prose blurbs (kind-level and
      capability-level; blurbs carry no field lists). Disabled capabilities render too (rendering
      reads the model, not the operator's blob), pinned by a test.
- [ ] `agw resource sample` (and `--write`) rendering live from the registry, plugins included;
      bundled sample YAML files deleted; `samples.py`'s bundled-file machinery retired;
      sample-pinning tests replaced by renderer tests (every kind renders, loads through the
      manifest path, and builds a registry; fixture-schema renderer unit tests).
- [ ] `agw resource describe` (or the sibling surface the LLD names) renders the field reference for
      kinds and capabilities from the same stream; completions updated for any new command or
      argument surface. The `agw resource schema` / describe surface NAMES are settled here (open in
      the phase-2 plan and left open by the descriptor contract), coordinated with the roadmap's
      onboarding-and-discovery child SDD, since those surfaces are its raw material.
- [ ] Prose blurbs authored for every bundled kind and capability (content lifted from today's
      samples' narrative lines, field lists dropped).
- [ ] Contributed-sample uniform validation (issue #214): operator-authored and plugin-contributed
      manifests validate through the one model regime, and unknown keys are hard errors there
      (FR12's strict direction resolves #214's open warn-vs-error tradeoff). Pinned by a test that a
      contributed sample with an unknown key fails validation the same way a first-party one does.

### 2.9 Pointer sweep and docs promotion (FR16, FR4 tail)

- [ ] One-time sweep: guides, command help, and remediation text discussing a config shape point at
      the rendered sample / describe surfaces; redundant hand-stated field lists deleted
      (narrative-necessary ones may stay).
- [ ] Permanent-doc promotion: `capabilities/README.md` rewritten for the declare-schema contract
      AND the capability-kind descriptor (the single kind-enumeration table, config schemas keyed by
      producer-side config declarations, registration-time conformance, `contract_version`) so the
      contract has a permanent home once the roadmap SDD is gone; the invoked-validation sections
      and their standing deprecation notes retire; `capabilities/harness_integration/README.md` (the
      harness developer guide, added 2026-08-02 and renamed with the kind, whose
      `validate`/`dependencies` sections document the retired contract) updated the same way;
      `cli/agentworks/plugins/README.md` documents `config_model` registration and the config
      declaration for plugin capability authors; `docs/guides/resources.md` updated; the superseding
      ADR extended or a sibling ADR added for the schema model and the descriptor if the phase 1 ADR
      (0022) did not already cover it.
- [ ] Dated lockfile entries: resource-manifests (Phase 5.7 invoked-validation contract retired;
      sample machinery replaced) and vm-sites (its "schema-registration is future work" deferral
      resolved).
- [ ] SDD cspell words promoted to root wherever they now appear in permanent code or docs.

### 2.10 Stretch: settings schema (FR14; descope without renegotiating)

- [ ] Settings sections (`[operator]`, `[paths]`, `[plugins]`, `[defaults]`, `[secret_config]`,
      `[session.config]`) declared as models under the same regime, validated through the bridge
      with TOML line framing.
- [ ] config.toml JSON Schema emitted; taplo association documented (draft-4 subset decision made
      here, not before).

## Closeout

- [ ] Full-suite gates green; end-to-end live verification (fresh config init, sample-driven
      resource authoring with editor schema association, vm-site declare, migrate fixture, doctor).
- [ ] Final `agentworks-reviewer` pass over the whole phase-2 branch; findings fixed. (The roadmap
      lead reviews the phase-2 PRs before merge; this SDD does not edit the roadmap ledger, the
      roadmap lead checks off wave-2 status from the merged PRs.)
- [ ] `locked.md` written summarizing final state across BOTH phases, decisions, and deviations; the
      descriptor-contract concepts, the schema-model contract, and the four honored doors promoted
      to permanent homes (see 2.9) so the locked SDD is deletable; phase-2 PR ready; Copilot /
      roadmap-lead review triaged. The roadmap SDD locks separately once every child does.

## Pressure-test notes (what writing this plan surfaced)

- The migrator pre-side "relocate the loaders into migrate/" candidate resolves FR2, verification
  independence, and loader removal with one move; the phase 1 LLD must confirm it or beat it.
- The ops-read-typed-fields migration (2.3) is the hidden bulk of the capability flip: azure and
  proxmox op code reads config dicts today, and the HLA's one line about it is a multi-file change
  with real review surface.
- #349's dual-shape normalization, warning channel, and bundle gate are deliberately temporary; 2.4
  removes them. Building then removing is the cost of the runway, accepted.
- The onepassword mapping model is itself a union (string or table), a good early test of union
  ergonomics inside a backend mapping model.
- The github scope rules (repos/owner mutually exclusive, list shapes) exercise model validators
  beyond field types on day one of 2.3.
- Deleting bundled samples (2.8) also retires the strip-one-`#` sample test convention; the renderer
  tests replace that coverage. `agw resource sample`'s CLI contract (kind selection, `--all`,
  `--write` append-only behavior) is already pinned by `tests/manifests/test_samples.py`; the 2.8
  work is carrying those pins through the renderer swap intact, not writing them.
- The unregistered-capability-name question was re-decided with the operator (2026-08-01) after
  review showed the shipped behavior is R9.2's hard finalize error, not the stale
  tolerate-and-self-disable a decode.py comment described: the hard error stays, and cross-host
  sharing rides the enablement axis.
