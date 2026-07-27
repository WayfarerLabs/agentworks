# FRD: system plugins (initial structure)

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Start date: 2026-07-24.

> **Design dependency (2026-07-27): this SDD is parked pending a separate registry-redesign SDD.**
> The plugin implementation was reset and will be rebuilt from scratch once that lands. The
> resolution, collision (R7), and present-but-disabled (R9) semantics here, and the "disabled"
> terminology throughout, are provisional against that redesign. The redesign decouples the
> registry's graph construction from config validation and splits "can't run" (resource readiness)
> from "not enabled" (plugin absence). The requirements below capture the intended plugin behavior;
> their registry mechanics get revised on the registry SDD's merge. After the reset, the FRD's
> "empty installed set / first plugin is a follow-on" framing (Summary, R11, Scope) is once again
> accurate.

## Summary

Agentworks has, deliberately, built the runway for a plugin system without a plugin system: the
resource-manifests SDD reserved the `system-plugin` / `external-plugin` origin variants, laid the
built-in-manifest mechanism "future plugins can do the same" ride on, and the capability model was
written throughout for "both the app and future plugins add capabilities." What is missing is the
unit that groups a bundle of app-shipped, opt-in functionality and the gate that keeps it invisible
until an operator turns it on.

This SDD introduces **system plugins**: in-repo, in-release units that bundle (a) capability
implementations conforming to the existing core-fixed capability kinds, and (b) declarable resources
as YAML manifests. A system plugin is **strictly opt-in**: unless an operator enables it in config,
none of its capabilities or resources publish into the Registry, so nothing it _provides_ (no
capability, no resource) is available at any consumption site or shown in any resource listing. The
plugin itself surfaces only as a present-but-disabled roster entry (R9). Enablement gates
**publication**, exactly as host-support gates the publication of a built-in capability today
(installed vs. supported); it does not gate code presence.

This is the _initial structure_ only. It ships the plugin unit, the enablement gate, the
`system-plugin` origin, and the wiring in `build_registry`, proven end to end by a test-fixture
plugin. It deliberately leaves the door open for external plugins and the broader feature/trust
model (see Future direction) without building either. No real system plugin is migrated in this
effort; the first one is a follow-on.

## Motivation

Three reasons drive this first step, in priority order.

1. **Signal, not noise, for each operator.** Agentworks serves operators working in very different
   worlds, and a lot of functionality is world-specific. A Microsoft/Azure-heavy operator carries a
   pile of Azure and MS-specific machinery (platforms, credential providers, install commands,
   templates); an AWS or GCP operator carries an entirely different pile; each is pure noise to the
   other. If every world's functionality lives in the core, every operator pays for all of it, in
   CLI surface, config, docs, `doctor` output, and cognitive load, for capabilities they will never
   touch. System plugins make world-specific functionality **strictly opt-in**: absent, invisible,
   and silent unless the operator enables it, so an operator sees only the surface for the worlds
   they actually work in. This is the primary near-term payoff.

2. **Keep the core sacred, the finishing move.** The core is Agentworks' trust root and its
   security-critical surface, so core development should be rare and deliberate. Well-separated
   extensions are not automatically safe (they can absolutely introduce issues), but separation
   makes them far easier to manage, reason about, and audit, and it **flattens the slippery slope**
   where every tool-specific need lobbies to be pulled into the core. The capability model already
   did most of this work by putting backends, providers, and harnesses behind uniform contracts; the
   plugin unit is the finishing move, giving that separated functionality a home _outside_ the core
   entirely, so "should this go in the core?" stops being a recurring question for anything that is
   not one of the blessed, core-owned concerns.

3. **Lay the table for external plugins.** The obvious next step everyone anticipates: once a system
   plugin exists as a bundling-and-enablement unit, third-party (external) plugins are a natural
   extension under a trust model (origin tiers plus explicit enablement). This SDD builds only the
   in-repo system-plugin structure, but it deliberately shapes the unit and the origin taxonomy so
   external plugins slot in later without re-litigating the foundation (see Future direction).

## Background (verified)

- **The origin taxonomy already reserves the variants.** `resources/origin.py` documents
  `system-plugin` ("distributed with the app but separable, possibly requiring explicit enable") and
  `external-plugin` ("installed from outside sources") as reserved and "not constructible until [the
  plugin system] lands"; `Origin.built_in`'s docstring states plugin-shipped resources will NOT use
  the `built-in` variant. The `variant` `Literal` today is
  `operator-declared | built-in | auto-declared`.
- **Declarable resources are already app-shippable as data.** `manifests/builtin.py` globs
  `manifests/builtin/*.yaml` and publishes each document through the ordinary manifest loader with
  `Origin.built_in(source="agentworks.manifests.builtin/<file>")`. A plugin's manifests are the same
  loader, a different directory, and a different origin.
- **Capabilities already separate installed code from published rows.** Each capability kind keeps a
  build-wide implementation registry (e.g. `VM_PLATFORM_REGISTRY`, holding every platform this build
  ships) and a `publish_to(registry)` that adds read-only capability rows only for the ones usable
  here (`VMPlatform.unsupported_reason` gates the row). The docstrings say "future plugins register
  here." The registries are populated unconditionally at module import, then publication is gated;
  the four are not uniform in shape (three hold impl _classes_, `SECRET_BACKEND_REGISTRY` holds
  stateless impl _instances_). Capability _kinds_ (`vm-platform`, `harness`, `secret-backend`,
  `git-credential-provider`) are fixed by the core; the README is emphatic that neither the app nor
  a plugin adds a kind.
- **Capability config lives on the consuming resource, not the capability.** A `vm-site` supplies a
  `vm-platform`'s config; a `session-template` supplies a `harness`'s. A capability instance is
  constructed from the consuming resource's spec. So a plugin capability is configured at its
  consumption site like any built-in capability; a plugin needs no per-plugin config beyond
  enablement.
- **`build_registry` is the one assembly point, and it is documented as a pure function** (no memo,
  no cache). `bootstrap.build_registry` wires publishers in a fixed order: the bundled built-in
  manifests first, then the deprecated operator-TOML `apt` / `install_commands` publishers (which
  emit `operator-declared` rows), then the built-in capability rows (`git_credential` / `harness` /
  `secrets` / `vm_platform`), then `Config.publish_to`, then the operator `ManifestSet`, then
  `finalize()`. Two boundary validations that name resources (`secrets.validate_chain`,
  `vm_sites.validate_sites`) run _after_ finalize.
- **The collision policy is variant-aware but only knows three variants.**
  `Registry._check_collision` special-cases `built-in` (built-in-over-built-in is a no-op;
  operator-over-built-in consults the kind's `builtin_override`) and errors on
  operator-over-operator; every other variant pairing falls through to a generic "publisher ordering
  conflict" error. There is no `system-plugin` case today.
- **CLI subapps attach at import time.** `cli/commands/__init__.py` imports each command module,
  each of which calls `app.add_typer(...)` at module load, before any config is read. There is no
  config-aware command registration path today.
- **Config already splits settings from resources.** Machine/identity settings live in
  `config.toml`; named referenceable entities live in manifests (ADR 0016). Plugin enablement is a
  setting (it configures the operator's install), not a resource.

## Terminology

- **System plugin**: an in-repo, in-release unit under `agentworks/plugins/<name>/` bundling
  capability implementations and/or declarable YAML manifests, published only when enabled. "Plugin"
  alone means system plugin throughout this SDD.
- **External plugin** (reserved, out of scope): a plugin installed from outside the app
  distribution. Named only to keep the origin vocabulary and descriptor shape stable.
- **Plugin descriptor**: the in-code object a plugin package exposes (a `Plugin`) declaring its
  name, description, the capability implementations it contributes, its bundled-manifest location,
  and reserved forward-looking fields (declared execution scopes, owned commands).
- **Installed set**: every system plugin this build ships (all descriptors importable through the
  plugin index). Analogous to a capability's implementation registry.
- **Enabled set**: the subset of the installed set an operator has turned on in `[plugins]`. Only
  enabled plugins publish.

## Functional requirements

- **R1 The `system-plugin` origin variant becomes constructible.** `Origin` gains `system-plugin` in
  its `variant` `Literal` and an `Origin.system_plugin(plugin=..., source=...)` factory carrying the
  plugin name and a shipped-file/code-source identifier
  (`agentworks.plugins.<name>/manifests/<file>` for manifests, `agentworks.plugins.<name>` for
  capability rows). Origin rendering (`resources/render.py`, `doctor`, `resource list`/`describe`)
  gains the display shape `system-plugin <name> (<source>)`. `external-plugin` stays reserved and
  not constructible, documented as before.

- **R2 A plugin is a package with a descriptor.** Each system plugin lives at
  `agentworks/plugins/<name>/` and exposes a `Plugin` descriptor. The descriptor declares: `name`,
  `description`, the capability implementations it contributes (grouped by capability kind), and the
  package/directory holding its bundled YAML manifests. All fields are optional except identity: a
  plugin may contribute only capabilities, only manifests, or both.

- **R3 The installed set is a code-side index.** `agentworks/plugins/` exposes the installed set as
  a name-keyed mapping of descriptors, populated by importing each shipped plugin package (the
  import-index pattern used by `resources/kinds`). Shipping a plugin means adding it to this index;
  there is no filesystem discovery and no external loading.

- **R4 Enablement is operator config, strictly opt-in.** `config.toml` gains a `[plugins]` section
  whose `enabled` list names the plugins to activate. Absent section or absent name = disabled. A
  disabled plugin publishes no capability rows and no manifests; nothing it provides is available at
  any consumption site or visible in any resource-listing command. Enabling a plugin whose name is
  not in the installed set is a typed config error, raised by the plugin publisher itself when it
  cannot resolve the name (see R5); a typo or an uninstalled plugin is caught loudly rather than
  silently ignored.

- **R5 Impls register at import; enabled plugins publish in `build_registry`, which stays pure.**
  Two phases, mirroring how core capabilities already work:
  - **Registration (import time).** A plugin package's capability implementation classes/instances
    register into the relevant build-wide implementation registries when the package is imported by
    the plugin index (R3), unconditionally for every _shipped_ plugin, exactly as core impls
    populate those registries. This is the installed set. A shipped-but-disabled plugin's impl sits
    dormant and unreachable (no capability row references it). Registration is idempotent per impl
    name; a name collision with an existing impl is a typed error at import.
  - **Publication (build time).** For each _enabled_ plugin, `build_registry` publishes one
    read-only capability row per contributed implementation with a `system-plugin` origin, and loads
    the plugin's bundled manifests through the ordinary manifest loader, publishing each document
    with a `system-plugin` origin. `build_registry` mutates no global state; it gates publication
    only, preserving its documented purity. Published plugin resources are ordinary Registry
    resources: they list, describe, resolve, and are referenced exactly like built-in and operator
    resources.

- **R6 Plugins contribute implementations of existing kinds only.** A v1 plugin may contribute
  capability implementations conforming to the core-fixed capability kinds (a new `vm-platform`,
  `harness`, `secret-backend`, or `git-credential-provider`) and declarable YAML resources of
  existing declarable kinds. A plugin does **not** introduce a new capability kind or a new
  declarable kind; kind registration stays an in-tree concern of the core. A plugin naming a
  capability kind with no registration adapter (R5) is a typed error, enforcing this mechanically.

- **R7 Precedence and collisions are defined by an explicit, order-independent matrix.** Because a
  single `build_registry` insertion point cannot sit "after all built-ins, before all operator rows"
  (the deprecated `apt`/`install` operator publishers run early, before the plugin slot; the
  operator TOML and YAML publishers run late, after it), precedence is decided by the _variant
  pair_, not publish order. `Registry._check_collision` is extended to implement it:
  - `operator-declared` overrides `built-in` or `system-plugin` where the kind's `builtin_override`
    permits, else a typed reserved-name error (same rule that governs operator-over-built-in today).
  - `system-plugin` and `built-in` are **peers**: a collision between them is a typed error (a
    plugin may not override a core built-in, nor a built-in a plugin).
  - `system-plugin` vs `system-plugin` (two enabled plugins on the same `(kind, name)`) is a typed
    error.
  - existing operator-vs-operator and built-in-vs-built-in behavior is unchanged.

  Deciding by variant pair (not arrival order) makes the result identical whether the plugin row or
  the operator row lands first, which is what the interleaved publisher order requires. Resource
  names stay flat and global as today; namespacing is deferred (it matters for external plugins, out
  of scope).

- **R8 Plugin capabilities are configured at their consumption sites.** A plugin capability draws
  its config from the consuming resource that references it (a `vm-site`, a `session-template`),
  identically to a built-in capability. `[plugins]` carries only enablement; there is no per-plugin
  runtime config surface in v1.

- **R9 A disabled plugin is present-but-disabled, not absent, a two-layer model.** The gate applies
  in two layers, mirroring the two disable models already in the codebase:
  - **Plugin contents** (contributed capabilities and manifests) are **publish-gated**: a disabled
    plugin publishes none, so its functionality is absent at every consumption site and invisible in
    every resource listing (per R4). Unchanged.
  - **The plugin itself** is shown **present-but-disabled with a reason**, borrowing the UX a
    `vm-site` gets when its platform is missing (the vm-site still registers and renders
    `(disabled)` with a reason). A disabled plugin lists in the roster as
    `plugin <name>: <description> [disabled: not enabled in [plugins]]`; an enabled one as enabled.
    The roster shows existence, description, and enable-state only; it never enumerates a disabled
    plugin's contributed capabilities or resources.

  Note the vm-site UX is driven by the generic `disabled_reason` hook _because a vm-site is a
  resource kind_. A plugin is not (R12), so the roster reimplements the same present-but-disabled
  presentation in `doctor` rather than dispatching through that hook. This dissolves the apparent
  tension in "nothing from the plugin shows up": the plugin surfaces as _explicitly disabled and
  contributing nothing_, exactly how an operator already reads a disabled vm-site, while its
  functionality stays fully gated. Because every contributed resource carries the `system-plugin`
  origin naming its plugin, resource listings can annotate provenance ("from plugin `<name>`")
  without the plugin being a resource itself (see R12).

- **R10 The trust and command doors are left open, not built.** The descriptor reserves (a) an
  optional declaration of the identity/level a plugin's capabilities act at (the least-privilege
  _convention_: a vm-level capability defaults to the admin user, an agent-level one to the agent
  user), drawing on the existing `ScopeLevel` vocabulary and the agent-user isolation boundary,
  recorded and displayable but unenforced in v1; and (b) a place for plugin-owned CLI commands,
  unpopulated in v1. Neither is wired to behavior. The future trust model is distribution trust plus
  explicit enablement, expressed through the origin tiers (`system-plugin` trusted like the app,
  `external-plugin` where provenance and pinning matter), not runtime sandboxing or capability-grant
  confinement; the reserved field is a convention hint, not an enforcement point. The fields exist
  so descriptors authored now need no re-authoring when external plugins and plugin commands land.

- **R11 The mechanism is proven, the product ships no demo.** The end-to-end path (descriptor to
  index to enablement to publication to consumption) is proven by a test-fixture plugin in the
  suite. The shipped `agentworks/plugins/` starts with the framework and an empty installed set; no
  inert example plugin ships in a release. The first real system plugin is a follow-on effort.

- **R12 A system plugin is an origin, not a resource.** A plugin is a _source_ of resources, not a
  referenced node in the resource graph; the framework already models "where a resource came from"
  as `Origin`, and the plugin taxonomy lives there (the `system-plugin` origin variant), not as a
  `plugin` resource kind. The roster (R9) is therefore a `doctor` surface reusing the self-disable
  _pattern_, not a registry kind. Making a plugin a first-class declarable resource (an
  operator-declared, trusted external source) is coherent only for _external_ plugins under the
  future trust model, where an operator declares and trusts one; that is out of scope here and left
  open.

## Scope

In scope: the `system-plugin` origin variant; the `Plugin` descriptor and the code-side installed
index; import-time impl registration; the `[plugins]` config section and its loader; the
enabled-plugin publish step in `build_registry` (publication only, no global mutation); the generic
per-capability-kind registration adapter the plugin publisher uses (reconciling the class- and
instance-shaped registries); the `Registry._check_collision` precedence extension for
`system-plugin` (R7); the unknown-enabled-name typed error; `doctor` plugin visibility; the reserved
(unenforced) least-privilege-scope and command-declaration descriptor fields; a test-fixture plugin
proving the path; author docs (`agentworks/plugins/README.md`) and an ADR recording the decision.

Out of scope: external plugins and any external loading/installation/discovery; the trust model and
its enforcement; plugin-owned CLI commands (registration path deferred; the import-time attach
problem is not solved here); new capability kinds or new declarable kinds from plugins; per-plugin
runtime config beyond enablement; resource-name namespacing; plugin versioning and compatibility;
migrating any existing built-in into a plugin. The larger design these defer to is sketched under
Future direction.

## Future direction (the broader plugin vision)

This initial structure is the deliberate first increment of a larger plugin system that has already
been sketched (a broader draft superseded on this branch). Capturing that target state here keeps
the reserved doors correctly shaped and gives the follow-on efforts a spine. In rough dependency
order, the increments beyond this one:

- **Plugin namespacing as the attribution and trust unit.** A plugin declares a namespace under
  which its resource names, capability names, config keys, and state all live, so every contribution
  is attributable to its plugin and two plugins cannot silently collide. This SDD keeps flat global
  names plus collision errors (R7); namespacing is the upgrade that makes plugins a real
  distribution/trust unit rather than a curated in-repo set.
- **Feature capabilities: lifecycle participation.** A new capability kind bound to exactly one
  level (vm, workspace, or agent) that participates in that level's create/delete lifecycle,
  activated by explicit opt-in on that level's template (`spec.features`), and handed a context (its
  config, the core resource and its lineage, an SSH runner as the admin or agent user, its
  namespaced state). Failure is loud but isolated. This is what lets a plugin _do_ something at init
  (an `az-cli` login, a passport issuance, a privileged-broker daemon), not merely ship resources.
  It is the single biggest piece this SDD does not build.
- **Feature dependencies.** Lineage-scoped, one-directional declared requirements
  (`agent-feature-X requires vm-feature-Y`), validated at create/plan time (refused loudly if
  unsatisfiable so a half-activated cross-level facility is unrepresentable), with same-level
  activation ordered by topological sort and cross-level ordering implicit in the containment
  hierarchy. Validation, not resolution: no auto-enable, no inference.
- **Namespaced per-feature state store.** Plugins read core state freely but write only to a store
  keyed by (namespace, resource kind, resource id), so disabling a plugin can drop its state
  wholesale without touching core tables. ADR 0020's per-session `harness_state` blob is the
  precedent.
- **Namespaced plugin config schema and doctor contributions.** A plugin declares its own namespaced
  config (validated by plugin code, not the core) and its own `doctor` checks, both surfacing only
  when the plugin is enabled.
- **External plugins and the trust model.** The `external-plugin` origin becomes constructible; the
  trust boundary is distribution trust plus explicit enablement, expressed through the origin tiers,
  explicitly NOT runtime sandboxing or confinement (an enabled plugin runs with full CLI privilege
  by design, the same privilege the CLI already holds). A least-privilege _convention_ (vm-level
  capabilities default to the admin user, agent-level to the agent user) nudges the safer identity
  without enforcing it. R10's reserved descriptor field is the seat for that convention. One
  concrete hardening this requires: the shared bundled-manifest publisher asserts its manifests are
  issue-free (`assert not manifests.issues`), which is correct for first-party built-ins and system
  plugins but must become a typed error before external plugins load, since a malformed third-party
  manifest must not crash on an `AssertionError` (and `assert` is stripped under `python -O`).

Already landed independently, so not future work: the **harness capability** (a session-level
capability owning start/restart/probe and asset placement) shipped as ADR 0020, which is why this
SDD treats `harness` as an existing core-fixed capability kind rather than a thing to build.
