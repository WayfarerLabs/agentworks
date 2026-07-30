# HLA: system plugins (initial structure)

Implements the [FRD](./frd.md). Built on the landed registry readiness refactor, which already owns
the enablement axis, the fold that distributes it, and every consumer that gates on it. The plugin
work is therefore one new package (`agentworks/plugins/`), one new origin variant, one config
section, an unconditional publish step plus an enabled-only manifest step in `build_registry`, one
collision extension, and, the load-bearing new piece, the **first `_node_enablement` producer**.
There is no bespoke publish-gate, no bespoke disabled-roster dispatch, and no reference-time
diagnosis: the registry does that.

## Current state (verified against post-refactor code)

- `resources/origin.py`: `variant` is `operator-declared | built-in | auto-declared`;
  `system-plugin` / `external-plugin` reserved and not constructible. Rendering in
  `resources/render.py` + doctor/list/describe.
- `resources/graph.py`: the frozen `DependencyGraph` carries per-node `enablement`
  (`Enablement.enabled | disabled`), `readiness` (a `Readiness` verdict), and the capability `impl`.
  `enablement_of` / `readiness_of` / `is_ready` / `edges_of` / `dependents_of` / `reachable_from` /
  `impl_of` are the query API. The fold hands each node its dependencies' `DependencyState`
  (enablement; readiness when enabled; impl); a disabled dependency has `readiness=None`, and a
  dependent's own `not_ready` synthesizes the "depends on X, which is disabled; enable its unit"
  hint (`vms/sites.py`).
- `resources/registry.py`: `finalize` runs ordered passes (build, resolve, cycle-detect, fold,
  readiness-gated materialize, attach, validate, freeze). **`_node_enablement()` is the seam**: it
  returns an `enablement` map over present nodes, currently all `enabled`, threaded into the fold,
  materialization gate (`has_ready_referrer`), `build_graph`, and the validate pass.
  `_check_collision` is variant-aware over three variants only; the refactor left it unchanged.
- Capabilities: each kind owns an impl registry populated at import; the graph builder reads it via
  `_impl_for` (fail-fast on a registry-less capability row, a whitelisted builder read). Three
  registries hold impl _classes_; `SECRET_BACKEND_REGISTRY` holds instances.
- `secrets/resolve.py` `active_backends` filters `present ∧ enabled ∧ opted-in` via `enablement_of`;
  the secret's finalize `validate` skips mappings to disabled backends (R9.9). So resolution and
  validation already honor a disabled producer.
- `doctor` reads stored `readiness_of` off the graph; `inspect` renders `resource list` / `describe`
  from the graph; `bootstrap.build_registry` is a pure function with the fixed publisher order.
- `config/models.py` `Config` is frozen; there is no `[plugins]` section. CLI subapps attach at
  import.

## Target state

`agentworks/plugins/` defines the validated `Plugin` descriptor, the installed index with inverted
`register_plugin`, the per-kind registration adapter, and the publish step. `Origin` can express
`system-plugin`. `Config` carries an enabled-plugins list. `build_registry` publishes **every
shipped plugin's** capability rows unconditionally and **enabled plugins'** manifests.
`Registry._node_enablement` becomes a **composition over enablement sources**; the plugin source
marks a not-opted-in plugin's contributions `disabled` with a reason. `_check_collision` learns
`system-plugin`. `resource list` hides disabled contributions; `doctor` shows the plugin roster. A
test-fixture plugin exercises the whole path; the shipped installed set is empty.

## Components

### 1. `system-plugin` origin (R1)

Add `"system-plugin"` to the `variant` `Literal`, a `plugin: str | None` field, and
`Origin.system_plugin(*, plugin, source)` (`file`/`line` `None`, `plugin` set, `source` a code
identifier). Extend the variant-contract docstring; add the `system-plugin <plugin> (<source>)`
render shape. `external-plugin` stays documented-only.

### 2. The plugin package: descriptor, index, atomic registration (R2, R3, R5)

`agentworks/plugins/`:

- `base.py`: the `Plugin` descriptor (frozen dataclass): `name`, `description`, contributed impls
  grouped by capability kind, `manifests` (package/dir or `None`), plus reserved `required_scopes`
  (typed to `ScopeLevel`) and `commands` (a real placeholder frame, not `tuple[Any, ...]`), both
  defaulted empty and unused (R10). The descriptor holds impl **classes** uniformly; the adapter
  (component 3) seats each into its registry, instantiating once for the instance-shaped
  `secret-backend`. `__post_init__` normalizes the capabilities mapping to immutable.
- `registration.py`: `register_plugin(plugin: Plugin)`. **Validates the whole descriptor first**
  (R2: name non-empty and `/`-free; every kind has an adapter; every impl is a class with a
  non-empty `/`-free `name`; no intra-descriptor collisions), **then** seats every impl atomically
  (all-or-nothing; a mid-loop failure seats nothing). Idempotent per impl name; a cross-plugin impl
  name collision is a typed error. A context-manager / snapshot helper is exported so tests can seat
  and unseat a fixture plugin without hand-snapshotting global dicts.
- `__init__.py`: the installed index `SYSTEM_PLUGINS: dict[str, Plugin]`. **The index imports each
  shipped plugin module and calls `register_plugin(module.PLUGIN)` itself** (inverted control, R3),
  so a registration failure is wrapped with plugin attribution and provenance is derived from the
  real module. A duplicate plugin name is a typed error. Starts empty in the shipped build (R11);
  the fixture uses the exported snapshot helper against a test-local index.
- `publish.py`: `publish_plugins(registry, config)`. Publishes a capability row for **every shipped
  plugin's** impl (unconditional, R5) and loads **enabled plugins'** manifests. Resolves
  `config.plugins_enabled` names against `SYSTEM_PLUGINS`; an unresolved name is collected and
  raised as a single typed config error (R4) before any publish, never a `KeyError`. Publication
  only; no registration (that happened at import), so `build_registry` stays pure.

### 3. The generic per-capability-kind adapter (R5, R6)

A small adapter table `CAPABILITY_ADAPTERS: Mapping[str, CapabilityAdapter]`, one per core
capability kind, each knowing (a) how to **seat an impl** into that kind's registry (class vs
instance) and (b) how to **build and add its row** dataclass (`VMPlatformEntry`, `HarnessEntry`,
...) with a supplied origin. Seating runs in `register_plugin`; row-building runs in
`publish_plugins` with `Origin.system_plugin(plugin=name, source="agentworks.plugins.<name>")`.
Publication is tied to seating: a row is built only for an impl actually seated (Fable hardening, so
a descriptor claim can never publish an unseated row). A kind with no adapter is caught at
descriptor validation (R2/R6). A test asserts `CAPABILITY_ADAPTERS.keys()` equals the
capability-category kinds in `KIND_REGISTRY`, so a future kind fails until its adapter exists.

Note the refactor's R13 already made every built-in capability publish unconditionally, so there is
no publish-gate to honor and no `_BUILTIN_*`-vs-full-registry split to introduce: a plugin
capability row is just another unconditional row, distinguished only by its `system-plugin` origin.
(The `_impl_for` builder read already resolves an enabled plugin's impl from its registry to run
it.)

### 4. Plugin manifests through the existing loader (R5, R9)

For an **enabled** plugin whose descriptor carries a manifests location, the publisher resolves it
via `importlib.resources`, calls `load_manifests`, and adds each entry with the `system-plugin`
origin. This is `builtin.publish_to` parameterized by directory and origin; factor the shared body.
The shared body **raises a typed error** on manifest issues rather than
`assert not manifests.issues` (the assert is stripped under `python -O` and is wrong for the
eventual external-plugin path; correct for first-party bundles now regardless). Manifests are
enabled-only (R9): a not-enabled plugin contributes no resources.

### 5. Config `[plugins]` (R4, R8)

`Config` gains `plugins_enabled: tuple[str, ...]` (empty when the section is absent). A loader
parses `[plugins] enabled = [...]`; unknown keys in the section are a config error. Enablement is a
setting consumed in `build_registry`, never a Registry resource (mirroring `secret_config`), present
on both load paths.

### 6. `build_registry` wiring, staying pure (R4, R5, R7)

Insert `plugins.publish_plugins(registry, config)` between the built-in capability rows and
`config.publish_to`. It publishes only, so `build_registry` mutates no module-level state and its
purity holds. The unknown-name typed error is raised inside `publish_plugins`, before any publish,
not in the post-finalize block. Precedence is not a function of the insertion point:
`_check_collision` decides by variant pair (component 7), so the result is the same wherever the
rows land.

### 7. `_check_collision` precedence extension (R7)

**Two collision layers, reconciled (the LLD pins the split).** A capability's row name IS its impl's
registry key, so a **capability** name-clash is caught earlier, at **seating** in `register_plugin`
(component 2): seating a plugin `vm-platform` named `lima` finds the built-in `lima` already in
`VM_PLATFORM_REGISTRY` and raises a typed error at registration, before any row reaches
`_check_collision`. So the seating guard, not `_check_collision`, is the enforcement point for
capability built-in/plugin and plugin/plugin clashes; its message must name the occupant's
**actual** origin (a core built-in vs another plugin), not assume "cross-plugin".
`_check_collision`'s system-plugin work is therefore reached for **declarable (manifest) rows** and
for the operator-override case.

Extend `resources/registry.py` `_check_collision` to decide by the unordered
`{existing.variant, incoming.variant}` pair, applying the unordered normalization **only to
system-plugin-involving pairs** (the existing built-in/operator directional asymmetry is preserved
verbatim): `operator-declared` overrides `built-in` or `system-plugin` where `builtin_override`
permits, else a typed reserved-name error; `system-plugin` and `built-in` are peers (typed error);
two `system-plugin` rows collide (typed error). Note that in practice every declarable kind a plugin
would ship (`vm-site`, `vm-template`, `session-template`, `secret`) is
`builtin_override = reserved`, so an operator cannot generally override a plugin's shipped resource;
the "allow" path is only the deprecated TOML-only kinds. Because all shipped plugins publish (R5), a
curated-set name clash between two shipped plugins (enabled or not) is a legitimate build error, the
correct outcome for a curation bug; namespacing (for independent external plugins) is deferred.
Acceptance tests target the layer that actually fires (capability clash, the seating guard;
declarable/operator clash, `_check_collision`).

### 8. The `_node_enablement` producer (R9, R13), the load-bearing piece

The refactor left `_node_enablement()` returning all-enabled. This SDD makes enablement a
**composition over enablement sources** and adds the first source. Shape:

- An enablement **source** is a callable `(registry rows) -> Mapping[(kind, name), DisabledMark]`
  where `DisabledMark` carries the disabling **remediation reason** (the clause a dependent's hint
  renders, e.g. `enable plugin <name>`, NOT the state phrasing) and its **source identity** (so a
  future surface can say _which source_ disabled it). Composition folds a **list** of sources: a
  node is `disabled` if any source disables it, retaining that source's reason; otherwise `enabled`.
  Multiple sources disabling one node compose deterministically (first-source-wins the reason,
  ordered as `build_registry` supplies them, the LLD pins it), so the axis is genuinely multi-source
  (R13), not plugin-only.
- **Layering (the seam shape the LLD pins).** The `Registry` is deliberately config-agnostic, so it
  cannot itself read `config.plugins_enabled`. `build_registry` (which holds config) constructs the
  sources already **bound to config** and injects them at finalize; the `Registry` folds opaque
  source callables and never imports `Config` or `plugins`. So the shipped `_node_enablement()`
  no-arg method is replaced by injected sources (finalize gains a defaulted `enablement_sources`
  input); the refactor's four `_node_enablement` monkeypatch tests migrate to injecting a stub
  source (mechanism churn, not behavior). The binary `enablement` map stays a pure projection of the
  marks (no drift).
- The **plugin source** (the only one built): a row whose origin is `system-plugin` with
  `plugin=<name>` is disabled with remediation reason `enable plugin <name>` iff `<name>` is not in
  `config.plugins_enabled`. Reads the frozen rows' origins and the (bound) enabled set; no new
  probe.
- The refactor's consumers already honor the result: the fold distributes the disabled
  `DependencyState` (so a `vm-site` on a disabled plugin platform is not-ready with the enable
  hint), `has_ready_referrer` withholds a disabled node's materialized deps, and `active_backends` /
  secret-mapping validation exclude disabled backends. A **carried reason** is the one extension the
  refactor's binary `Enablement` needs: the disabled state gains an optional reason so the
  dependent's hint reads "enable plugin `<name>`" rather than a generic "enable its unit". (The
  refactor's `DependencyState` already flows enablement; this threads the reason alongside it.)

This is where "a not-enabled plugin's capabilities are present-but-disabled" actually happens: the
rows publish (component 3), and this producer marks the not-opted-in ones disabled. No parallel
gate.

### 8b. Closing the consumer-gating gap for `harness` and `git-credential-provider` (R14)

The producer (component 8) only makes strictly-opt-in real for a kind whose **consumer** honors a
disabled dependency. The refactor wired only two: the `vm-site` propagates (its `not_ready` reads
the platform's disabled state) and the `secret` consults backend enablement in
resolution/validation. The other two kinds' consumers currently opt out of readiness entirely, so a
not-enabled plugin's harness or git-credential-provider would be silently usable. Per
self-determined readiness, each consumer chooses its own model (not a blanket propagation):

- **`git-credential` propagates (the vm-site model).** A `git-credential` has a single provider, so
  it adds a `not_ready(deps)` hook that reads the provider dependency's `DependencyState` and
  returns not-ready with the carried "enable plugin `<name>`" reason when the provider is disabled
  (mirroring `VMSiteDecl.not_ready`). The use-time resolution path
  (`vms/initializer/credentials.py`'s provider resolution, `git_credentials/__init__.py`'s
  advisories) gains an `enablement_of`/`is_ready` refusal before the raw
  `GIT_CREDENTIAL_PROVIDER_REGISTRY` lookup, so a disabled provider cannot be constructed even if a
  resource names it. Its edge already exists (`git-credential -> provider`), so the fold hands it
  the provider's state for free.
- **`session-template` stays ready, gates the harness at use (the secret model).** A
  `session-template` does NOT propagate (it lists ready); instead the harness is gated where it is
  constructed. `harness_for` / `_harness_for_template` (`sessions/nodes.py`) thread no registry, so
  the gate cannot sit literally inside them; it goes one level up at the two session-build call
  sites that DO hold the registry + resolved template (`_create_build.py` create, `_lifecycle.py`
  restart/reattach, both funneling through `_resolve_template`), via a shared
  `ensure_harness_enabled(registry, name)` that reads `enablement_of("harness", name)` and raises a
  typed "enable plugin `<name>`" error when disabled (like `active_backends` skipping a disabled
  backend). Both call sites route every real construction, so this covers use; the read-only
  `_display_harness` path is deliberately left ungated. A disabled harness fails loudly at
  session-create, not silently succeeds.

Both are additive consumer wiring against the already-produced enablement; neither changes the fold
or the producer. The fixture plugin's tests exercise a disabled plugin of each of the four kinds
through its actual consumer (site not-ready; secret backend excluded; git-credential not-ready;
session harness use-error), so R9's guarantee is proven kind-by-kind, not only for vm-platform.

### 9. Presentation: disabled hides, not-ready shows; the doctor roster (R9)

- **`resource list`** hides `disabled` (enablement-axis) rows by default (the plugin work is the
  first producer, so this is where the rule is set), while continuing to show `not-ready` (present,
  enabled, blocked) rows such as a host-unsupported built-in. `inspect` reads `enablement_of` and
  filters the list; an explicit `--include-disabled` flag (or a future `agw plugins` view) reveals
  them. **`describe KIND/NAME` is an explicit by-name lookup, so it always renders the named row**
  (annotating its disabled state); the hide rule is list-only, hiding a resource the operator asked
  for by name would be user-hostile. Provenance ("from plugin `<name>`") comes from the
  `system-plugin` origin already on each row.
- **The doctor plugin roster** iterates `SYSTEM_PLUGINS` against `config.plugins_enabled` and
  renders `plugin <name>: <description>` tagged enabled or `disabled (not enabled in [plugins])`.
  Roster only, existence/description/enable-state, never a disabled plugin's contributed
  capabilities or resources. A plugin is not a resource kind (R12), so this is a bespoke `doctor`
  surface, not a `KIND_REGISTRY`-dispatched hook. Reserved `required_scopes`, when populated, render
  as an informational line (R10, unenforced).

### 10. Test-fixture plugin (R11)

A fixture plugin under `tests/` (its own descriptor + one trivial capability impl of an existing
kind

- one YAML manifest), seated via the exported snapshot helper into a test-local index. Tests assert,
  against the landed registry: enabled means its capability row and manifest resource are present,
  enabled, and consumable with `system-plugin` origin; not-enabled means its capability row is
  **present-but-disabled** (an operator `vm-site` referencing it is not-ready with "enable plugin
  `<name>`", NOT an unknown-name error), its manifest resources are absent, and both are hidden from
  default `resource list`; an unknown enabled name is a typed config error (not `KeyError`); two
  fixtures colliding give a `_check_collision` error; operator override of a plugin resource wins
  where `builtin_override` permits; a plugin cannot override a built-in (peer error); descriptor
  validation rejects a missing-name / instance-not-class / unknown-kind / colliding-impl descriptor
  with a typed, attributed error; enable-then-disable in one process leaves the seated impl present
  but its row disabled (the enablement axis, not re-registration). A composition test injects a
  second stub source disabling a node and asserts `_node_enablement` composes it (R13 seam).

## Interfaces (summary)

- `Origin.system_plugin(*, plugin, source)`; `variant` gains `system-plugin`.
- `Plugin` descriptor; `register_plugin(plugin)`; `SYSTEM_PLUGINS`; a seat/unseat snapshot helper.
- `CapabilityAdapter` (seat + build-row) and `CAPABILITY_ADAPTERS` keyed by capability kind.
- `plugins.publish_plugins(registry, config)`.
- `Config.plugins_enabled: tuple[str, ...]`.
- Enablement source: `(rows) -> Mapping[(kind, name), DisabledMark]`; `_node_enablement` composes a
  list of sources; `Enablement`/`DependencyState` carry an optional disabled reason.

## Sequencing rationale

Origin first (pure vocabulary, mergeable alone); then the plugin package + adapter + atomic
`register_plugin` + the `_check_collision` extension, provable against the fixture in isolation;
then config; then the `_node_enablement` composition + the plugin source (the enablement producer);
then the `build_registry` wiring that joins publication to enablement; then presentation
(disabled-hides + the doctor roster) and docs. Each step ends green. The producer step is where a
not-enabled plugin first becomes present-but-disabled; the wiring step is where a real config first
changes behavior.

## Risks and mitigations

- **Registration is process-global; enablement is per-config.** Impls seat at import for every
  shipped plugin, unconditionally (mirroring core, keeping `build_registry` pure). Reachability is
  governed by the published row and its enablement, not import timing. Mitigation: atomic idempotent
  registration with a typed cross-plugin collision error; the enable-then-disable-in-one-process
  test asserts the seated impl's row goes disabled, not absent.
- **The reason-carrying enablement is a change to the refactor's binary `Enablement`.** Mitigation:
  keep it additive, an optional reason on the disabled state, so the refactor's existing
  fold/gate/consumer code is untouched except where it reads the hint; a test pins that a
  plugin-caused not-ready reason reads "enable plugin `<name>`".
- **Disabled-hides could hide a genuinely referenced-but-disabled row an operator needs to debug.**
  Mitigation: the reference itself is loud (the dependent is not-ready with the enable hint); the
  doctor roster shows the plugin's state; `--include-disabled` reveals the rows.
- **Adapter drift as capability kinds evolve.** Mitigation: the `CAPABILITY_ADAPTERS.keys()` ==
  capability-kinds test.
- **Collision-matrix coverage.** Mitigation: tests for each variant pairing with its specific
  message.
- **Scope creep into external plugins / commands / trust / operator-explicit disable.** Mitigation:
  reserved fields are typed and inert; the enablement source composition is built and tested with a
  stub second source but ships only the plugin source; tests assert the reserved fields and the
  unbuilt operator source are untouched by behavior.

## What does not change

- The capability kinds and `KIND_REGISTRY`; the manifest loader and envelope; the registry's
  finalize passes, fold, materialization, and freeze (the plugin work produces enablement _into_ the
  existing seam, it does not change the passes); operator TOML/YAML surfaces; the CLI entry flow and
  command registration (no plugin owns a command in v1). The Registry's collision policy DOES change
  (component 7). Plugin capabilities configure through their consuming resources exactly as built-in
  capabilities do.
