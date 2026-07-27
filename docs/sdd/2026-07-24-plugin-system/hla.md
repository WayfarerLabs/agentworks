# HLA: system plugins (initial structure)

Implements the [FRD](./frd.md). One new package (`agentworks/plugins/`), one new origin variant, one
new config section, one new step in `build_registry`, and one extension to the Registry's collision
policy. Every other seam already exists; the work is to add the plugin unit and route the existing
publish/gate machinery through it.

> **Design dependency (2026-07-27): parked pending a registry-redesign SDD; see the [FRD](./frd.md)
> note.** The plugin implementation was reset; it will be rebuilt from scratch after the registry
> SDD lands. This HLA's "Current state" snapshot, the `_check_collision` extension (component 6b),
> and the present-but-disabled roster (component 7) all lean on registry mechanics that the redesign
> changes: decoupling graph construction from config validation, splitting resource "can't run"
> readiness from plugin "not enabled" absence, and renaming the `disabled_reason` readiness hook.
> Treat the component details below as the prior design of record, to be revised against the
> registry SDD before the rebuild.

## Current state (verified)

- `resources/origin.py`: `variant` is `operator-declared | built-in | auto-declared`;
  `system-plugin` / `external-plugin` are documented as reserved and not constructible. Factories:
  `operator_declared`, `built_in`, `auto_declared`. Rendering lives in `resources/render.py` and the
  doctor / resource-list / describe surfaces.
- `manifests/builtin.py`: `publish_to(registry)` globs `manifests/builtin/` via
  `importlib.resources`, loads through `manifests.loader.load_manifests`, asserts issue-free, and
  adds each entry with `Origin.built_in(source="agentworks.manifests.builtin/<file>")`.
- Capabilities: each kind package owns an implementation registry keyed by impl `name`, populated
  unconditionally at module import, and a `publish_to(registry)` that builds a per-kind row
  dataclass (`VMPlatformEntry`, `HarnessEntry`, ...) with a `built-in` origin. The registries are
  NOT uniform: `VM_PLATFORM_REGISTRY`, the harness registry, and the git-credential registry hold
  impl _classes_; `SECRET_BACKEND_REGISTRY` holds stateless impl _instances_
  (`secrets/backends.py`). The capability _kinds_ self-register into `KIND_REGISTRY` via
  `resources/kinds/__init__` import side effects.
- `bootstrap.build_registry(config, manifests=None)` is documented as a pure function (no memo, no
  cache): `Registry.empty()`, then `builtin_manifests.publish_to`, then the operator `apt` /
  `install_commands` publishers, then the built-in capability rows (`git_credential` / `harness` /
  `secrets` / `vm_platform`), then `config.publish_to`, then `manifests.publish_to`, then
  `finalize()`, then `secrets.validate_chain` / `vm_sites.validate_sites` (post-finalize). Note the
  operator `apt` / `install` publishers run _early_ (steps 2-3), before the capability rows.
- `resources/registry.py` `_check_collision`: variant-aware but only handles `built-in` (built-in
  over built-in is a no-op; operator over built-in consults `builtin_override`) and
  operator-over-operator (error); every other variant pairing hits a generic "publisher ordering
  conflict" error. No `system-plugin` case.
- `resources/inspect.py`: the generic `disabled_reason_for(registry, kind, resource)` dispatches
  through `KIND_REGISTRY` to a kind's optional `disabled_reason` hook; `resource list` renders
  `(disabled)` and `describe` renders `Disabled: <reason>`. This is resource-kind machinery.
- `config/models.py` `Config` is a frozen dataclass; `config/load.py` + `loaders_*` parse the TOML.
  There is no `[plugins]` section.
- `cli/commands/*` attach subapps at import; `cli/commands/doctor.py` renders the doctor report.

## Target state

A `agentworks/plugins/` package defines the `Plugin` descriptor, the installed index, the
import-time impl registration, and the publication step. `Origin` can express `system-plugin`.
`Config` carries an enabled-plugins list. `build_registry` publishes each enabled plugin's
capabilities and manifests (publication only, no global mutation). `_check_collision` learns the
`system-plugin` variant. `doctor` shows the plugin roster present-but-disabled. A test-fixture
plugin exercises the whole path; the shipped installed set is empty.

## Components

### 1. `system-plugin` origin (R1)

Add `"system-plugin"` to the `variant` `Literal`, a `plugin: str | None` field, and an
`Origin.system_plugin(*, plugin: str, source: str)` classmethod (`file`/`line` `None`, `plugin` set,
`source` a code identifier). Extend the variant-contract docstring. `resources/render.py` and the
doctor / list / describe renderers gain the `system-plugin <plugin> (<source>)` shape.
`external-plugin` stays documented-only (not added to the `Literal`; it lands with external
plugins).

### 2. The plugin package (R2, R3, R5)

`agentworks/plugins/`:

- `base.py`: the `Plugin` descriptor (frozen dataclass): `name`, `description`, the contributed
  capability impls grouped by capability kind, `manifests` (the package/dir holding the plugin's
  bundled YAML, or `None`), plus the reserved `required_scopes` and `commands` fields (typed,
  defaulted empty, unused in v1, R10). The descriptor carries impl _classes_ uniformly; the per-kind
  adapter (component 3) knows how to seat each into its registry, including instantiating once for
  the instance-shaped `secret-backend` registry.
- `__init__.py`: the installed index `SYSTEM_PLUGINS: dict[str, Plugin]`, populated by importing
  each shipped plugin package (import-index pattern, mirroring `resources/kinds/__init__`).
  Importing a plugin package runs its impl registration (component 3), so the installed set's impls
  populate the build-wide registries at import, unconditionally, exactly as core impls do. Starts
  empty in the shipped build (R11); the fixture plugin is added to a test-local index, not this one.
- `publish.py`: `publish_enabled(registry, config)`. For each name in `config.plugins_enabled`,
  resolve the descriptor from `SYSTEM_PLUGINS`; an unresolved name is collected and raised as a
  single typed config error (R4) _before_ any publish is attempted, so an unknown name never becomes
  a `KeyError`. For each resolved (enabled) plugin, publish its capability rows and manifests. This
  function does not register impls (that already happened at import); it only publishes.

### 3. The generic per-capability-kind adapter (R5, R6)

Publishing a capability row is per-kind today (each kind builds its own `*Entry`, and the registries
differ in shape). Introduce a small adapter table in the plugin package:
`CAPABILITY_ADAPTERS: Mapping[str, CapabilityAdapter]`, one entry per core capability kind. Each
adapter knows two things for its kind: how to **seat an impl** into that kind's implementation
registry (keyed by the impl's `name`), reconciling class-vs-instance (three registries take the impl
class; `secret-backend` takes a constructed instance of the stateless impl), and how to **build and
add its row** dataclass (`VMPlatformEntry`, `HarnessEntry`, ...) with a supplied origin. Seating
runs at import (component 2); row-building runs in `publish_enabled` with
`Origin.system_plugin(plugin=name, source="agentworks.plugins.<name>")`. The adapters wrap the
existing per-kind registries and row types. A plugin naming a capability kind with no adapter (a
non-capability or unknown kind) is a typed error, R6's "existing kinds only" enforced mechanically.

**Publish source vs. lookup source (the seated-but-unreachable invariant).** Seating a plugin impl
into a shared capability registry is only inert with respect to publication if the core capability
publisher does NOT publish the whole registry. It did (each `publish_to` iterated its full
`*_REGISTRY`), so a seated plugin impl was published as a `built-in` row unconditionally: reachable
while disabled, and colliding with its own `system-plugin` row when enabled. So each of the four
core publishers is changed to publish only its explicit built-in set (`_BUILTIN_HARNESSES`,
`_BUILTIN_GIT_CREDENTIAL_PROVIDERS`, `_BUILTIN_VM_PLATFORMS`, `_BUILTIN_SECRET_BACKENDS`), with the
mutable registry _derived_ from that constant so the two cannot drift. The registry stays the
execution-lookup source (resolvers read it, so an enabled plugin's impl runs once its row
publishes); only the built-in constant is the publish source. This is what makes a seated-but-
disabled plugin genuinely absent (no row from any publisher) and an enabled one land exactly one
`system-plugin` row.

### 4. Plugin manifests through the existing loader (R5)

If the descriptor carries a manifests location, the publisher resolves it via `importlib.resources`
(as `builtin.publish_to` does), calls `load_manifests`, asserts issue-free (plugin manifests are app
data, a dirty bundle is a bug), and adds each entry with the `system-plugin` origin (source
`agentworks.plugins.<name>/manifests/<file>`). This is `builtin.publish_to` parameterized by
directory and origin; factor the shared body so both call it.

### 5. Config `[plugins]` (R4, R8)

`Config` gains `plugins_enabled: tuple[str, ...]` (empty when the section is absent). A loader in
`config/loaders_*` parses `[plugins] enabled = [...]` (a string list; unknown keys in the section
are a config error, keeping the door for future per-plugin settings explicit rather than silently
tolerant). No resource publishing: enablement is a setting, consumed in `build_registry`, never a
Registry resource (mirroring `secret_config`). It is present on both load paths (settings-only and
full), since it is a setting.

### 6. `build_registry` wiring, staying pure (R4, R5, R7)

Insert a single `plugins.publish_enabled(registry, config)` call between the built-in capability
rows and `config.publish_to`. It publishes only (impls were seated at import, component 2), so
`build_registry` mutates no module-level state and its documented purity holds. The unknown-name
typed error is raised _inside_ `publish_enabled`, before any publish; it is NOT appended to the
post-finalize `secrets.validate_chain` / `vm_sites.validate_sites` block (those run after finalize,
too late, and a `KeyError` would already have crashed the publish). Precedence is not a function of
this insertion point: because the operator `apt` / `install` publishers run before it and the
operator TOML/YAML publishers run after it, a single slot cannot be "after all built-ins, before all
operator rows." Instead, `_check_collision` decides by variant pair (component 6b), so the result is
the same wherever the rows land.

### 6b. `_check_collision` precedence extension (R7)

Extend `resources/registry.py` `_check_collision` to implement the R7 matrix, decided by the
unordered `{existing.variant, incoming.variant}` pair so arrival order does not matter:
`operator-declared` overrides `built-in` or `system-plugin` where the kind's `builtin_override`
permits (reusing the existing reserved-name path), else a typed reserved-name error; `system-plugin`
and `built-in` are peers (typed error); two `system-plugin` rows collide (typed error). Existing
operator-vs-operator and built-in-vs-built-in behavior is untouched. Each new pairing gets its own
clear message, not the generic "publisher ordering conflict".

### 7. `doctor` roster: present-but-disabled (R9, R12)

The two-layer gate: contents are publish-gated (a disabled plugin published nothing, so it is absent
from the rest of the report for free), while the plugin _itself_ is shown present-but-disabled.
`doctor` gains a Plugins section that iterates `SYSTEM_PLUGINS` and renders
`plugin <name>: <description>` against `config.plugins_enabled`, tagging a disabled one
`[disabled: not enabled in [plugins]]` and an enabled one as enabled, the same present-but-disabled
shape a platform-less vm-site gets. Roster only: existence, description, enable-state; never a
disabled plugin's contributed capabilities or resources. The reserved `required_scopes`, when
populated, render as an informational line, unenforced (R10).

A plugin is NOT a resource kind (R12): it is an origin, not a referenced graph node, so this roster
is a bespoke `doctor` surface, NOT the generic `disabled_reason_for` path (which dispatches through
`KIND_REGISTRY` and therefore only serves resource kinds). It reimplements the same present-but-
disabled presentation. The `system-plugin` origin on every contributed resource already carries the
provenance link, so resource listings can annotate "from plugin `<name>`" without a plugin row. (An
`agw plugins` command could later host the same roster; not needed in v1.)

### 8. Test-fixture plugin (R11)

A fixture plugin under `tests/` (its own descriptor + one trivial capability impl of an existing
kind + one YAML manifest) registered into a test-local installed index. Tests assert: enabled means
its capability row and manifest resource appear in the finalized Registry with `system-plugin`
origin and are consumable at their site; disabled means both absent everywhere; unknown enabled name
gives a typed config error (not `KeyError`); two fixtures colliding give a `_check_collision` error;
operator override of a plugin resource wins where `builtin_override` permits; a plugin cannot
override a built-in (peer error); enable-then-disable within one process leaves the seated impl
present but unreachable at its consumption site (the process-scoped-registration guard).

## Sequencing rationale

Origin first (pure vocabulary, mergeable alone), then the plugin package + adapter +
`_check_collision` extension against the fixture (framework provable in isolation), then config,
then the `build_registry` wiring that joins them, then doctor and docs. Each step ends green. The
wiring step is where behavior first changes for a real config; everything before it is inert
additions.

## Risks and mitigations

- **Impl registration is process-global, but enablement is per-config.** Impls seat into
  module-level registries at import, for every shipped plugin, unconditionally. This is deliberate:
  it mirrors core exactly (core impls are always present too), keeps `build_registry` pure, and
  means a plugin's reachability is governed solely by whether its row published, not by import
  timing. In a long-lived process serving multiple configs (the anticipated web client), a shipped
  plugin's impl is present regardless of any one config, which is correct: with no published row it
  is unreachable. Mitigation: seating is idempotent per impl `name`; a genuine name collision
  between two shipped impls is a typed error at import; a test enables-then-disables within one
  process and asserts the seated impl stays unreachable at its consumption site.
- **Enablement not honored somewhere resources are read.** The gate is "did the row publish"; every
  consumption path reads the finalized Registry, so a disabled plugin is absent uniformly.
  Mitigation: the disabled-fixture test asserts absence across `resource list`, a consumption site,
  and doctor's non-roster sections.
- **Unknown-name error placement.** The check must live inside `publish_enabled` (pre-publish), not
  in the post-finalize boundary block, or a `KeyError` crashes first. Mitigation: the publisher
  resolves all names up front and raises the typed error before publishing; a test pins the message
  and that it precedes any publish.
- **Collision-matrix coverage.** The R7 matrix must be exercised for each variant pairing.
  Mitigation: tests cover operator-over-plugin (both `builtin_override` outcomes), plugin-over-
  built-in, built-in-over-plugin, and plugin-over-plugin, each asserting the specific message.
- **Adapter drift as capability kinds evolve.** The adapter table must cover every capability kind.
  Mitigation: a test asserts `CAPABILITY_ADAPTERS.keys()` equals the set of capability-category
  kinds in `KIND_REGISTRY`, so a new capability kind added later fails the test until its adapter
  exists.
- **Scope creep into external plugins / commands / trust.** The reserved fields are typed and
  defaulted but touched by nothing. Mitigation: no code reads `required_scopes` or `commands` beyond
  doctor's informational render; tests assert they are inert.

## What does not change

- The capability model and its kinds; the manifest loader and envelope; the Registry's finalize and
  freeze; operator TOML and YAML surfaces; the CLI entry flow and command registration (no plugin
  owns a command in v1). The Registry's collision policy DOES change (component 6b); everything else
  in the Registry is untouched. Plugin capabilities configure through their consuming resources
  exactly as built-in capabilities do.
