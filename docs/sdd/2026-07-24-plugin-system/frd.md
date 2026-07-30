# FRD: system plugins (initial structure)

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Start date: 2026-07-24. Rebuilt 2026-07-30 against the landed registry readiness refactor.

## Summary

Agentworks has, deliberately, built the runway for a plugin system without a plugin system: the
resource-manifests SDD reserved the `system-plugin` / `external-plugin` origin variants, laid the
built-in-manifest mechanism "future plugins can do the same" ride on, and the capability model was
written throughout for "both the app and future plugins add capabilities." The
registry-readiness-refactor then landed the missing registry model: a first-class **enablement
axis** (a present resource can be `enabled` or `disabled`), separate from readiness, with
"enabled/disabled" reserved in the vocabulary for exactly this opt-in concept, and a single producer
seam (`Registry._node_enablement()`) that the refactor modeled and fixture-tested but ships with no
real producer. What is still missing is the unit that groups a bundle of app-shipped, opt-in
functionality, and the **first producer** of disabled state.

This SDD introduces **system plugins**: in-repo, in-release units that bundle (a) capability
implementations conforming to the existing core-fixed capability kinds, and (b) declarable resources
as YAML manifests. A system plugin is **strictly opt-in**: unless an operator enables it in config,
nothing it offers is available at a consumption site or shown in a default resource listing. It
becomes the first producer of the registry's enablement axis: a not-enabled plugin's capabilities
are **present-but-disabled nodes**, so a resource that references one is cleanly not-ready ("depends
on `azure-vm`, which is disabled; enable plugin `azure`") rather than hitting an unknown-name hard
error.

Because the registry already owns the enablement axis, the fold that distributes it, and every
consumer that gates on it, this plugin unit is small: it seats impls, publishes capability rows
unconditionally (as the refactor's R13 already does for built-ins), and **produces enablement** by
marking a not-opted-in plugin's contributions disabled. It builds no bespoke publish-gate, no
bespoke disabled-roster dispatch, and no reference-time diagnosis; the registry does that work.

This is the _initial structure_ only. It ships the plugin unit, the enablement producer, the
`system-plugin` origin, and the `build_registry` wiring, proven end to end by a test-fixture plugin.
It deliberately leaves the door open for external plugins, the broader feature/trust model, and, per
operator direction, **operator-explicit disable of individual units** (R13) without building any of
them. No real system plugin is migrated in this effort; the first one is a follow-on.

## Motivation

Three reasons drive this first step, in priority order.

1. **Signal, not noise, for each operator.** Agentworks serves operators working in very different
   worlds, and a lot of functionality is world-specific. A Microsoft/Azure-heavy operator carries a
   pile of Azure and MS-specific machinery (platforms, credential providers, install commands,
   templates); an AWS or GCP operator carries an entirely different pile; each is pure noise to the
   other. If every world's functionality lives in the core, every operator pays for all of it, in
   CLI surface, config, docs, `doctor` output, and cognitive load, for capabilities they will never
   touch. System plugins make world-specific functionality **strictly opt-in**: silent and out of
   the default surface unless the operator enables it, so an operator sees only the surface for the
   worlds they actually work in. This is the primary near-term payoff.

2. **Keep the core sacred, the finishing move.** The core is Agentworks' trust root and its
   security-critical surface, so core development should be rare and deliberate. Well-separated
   extensions are not automatically safe (they can absolutely introduce issues), but separation
   makes them far easier to manage, reason about, and audit, and it **flattens the slippery slope**
   where every tool-specific need lobbies to be pulled into the core. The capability model already
   did most of this work by putting backends, providers, and harnesses behind uniform contracts; the
   plugin unit is the finishing move, giving that separated functionality a home _outside_ the core
   entirely, so "should this go in the core?" stops being a recurring question for anything that is
   not one of the blessed, core-owned concerns.

3. **Lay the table for external plugins and operator-controlled enablement.** Once a system plugin
   exists as a bundling-and-enablement unit, two natural extensions open up: third-party (external)
   plugins under a trust model (origin tiers plus explicit enablement), and an operator's ability to
   explicitly disable individual units (including specific parts of a third-party plugin they
   otherwise trust). This SDD builds only the in-repo system-plugin producer, but it shapes the
   enablement axis, the descriptor, and the origin taxonomy so both slot in later without
   re-litigating the foundation (R13, Future direction).

## Background (verified against the post-refactor code)

- **The enablement axis is landed but has no producer.** The registry graph node carries
  `enablement` (`enabled | disabled`); `Registry._node_enablement()` computes it and currently
  returns `enabled` for every node. The readiness fold distributes a dependency's enablement (a
  disabled dependency's state carries `readiness=None`, so a dependent's own `not_ready` synthesizes
  the "depends on X, which is disabled; enable its unit" hint); `has_ready_referrer`
  (materialization) and secret resolution/validation already exclude disabled units via
  `enablement_of`. A disabled node's own readiness is a `Readiness.ready()` placeholder, so
  **consumers gate on `enablement_of`, not `is_ready`** (recorded in the refactor's `locked.md`).
  This SDD is the first `_node_enablement` producer.
- **Capability rows publish unconditionally.** The refactor's R13 made every installed capability
  publish a row regardless of host support; host-unsupported is a not-ready verdict on a present
  node, not an absent one. Plugin capabilities extend this: a shipped plugin's capability publishes
  a present node whether or not the plugin is enabled; enablement is the overlay `_node_enablement`
  produces.
- **"enabled/disabled" is reserved vocabulary.** The refactor's R6 retired "disabled" for host
  readiness (renamed to `not_ready`) and reserved "enabled/disabled" for exactly this opt-in axis.
  So the plugin work's "disabled" is now the correct, un-overloaded word.
- **The origin taxonomy reserves the variants.** `resources/origin.py` documents `system-plugin`
  ("distributed with the app but separable, possibly requiring explicit enable") and
  `external-plugin` as reserved and not constructible; the `variant` `Literal` is
  `operator-declared | built-in | auto-declared`. `Origin.built_in`'s docstring states
  plugin-shipped resources will not use `built-in`.
- **Declarable resources are already app-shippable as data.** `manifests/builtin.py` globs
  `manifests/builtin/*.yaml` and publishes each document through the ordinary manifest loader with a
  `built_in` origin. A plugin's manifests are the same loader, a different directory, a different
  origin.
- **Capabilities separate installed code from published rows.** Each kind keeps a build-wide impl
  registry (`VM_PLATFORM_REGISTRY` etc.), populated unconditionally at import, plus a `publish_to`.
  Post-refactor, the graph builder reads the registry to stamp each capability node's impl (the
  whitelisted builder path; `_impl_for` fails fast on a registry-less row). The four registries are
  not uniform (three hold impl _classes_; `SECRET_BACKEND_REGISTRY` holds stateless impl
  _instances_). Capability _kinds_ are fixed by the core; neither the app nor a plugin adds a kind.
- **Capability config lives on the consuming resource.** A `vm-site` supplies a `vm-platform`'s
  config; a `session-template` supplies a `harness`'s. So a plugin capability is configured at its
  consumption site like any built-in capability; a plugin needs no per-plugin config beyond
  enablement.
- **`build_registry` is the one assembly point and is a pure function** (no memo, no cache). It
  wires publishers in a fixed order (bundled built-in manifests, the deprecated operator-TOML
  apt/install publishers, the built-in capability rows, `Config.publish_to`, the operator
  `ManifestSet`, then `finalize`), with the post-finalize secret/site reachability checks after.
- **The collision policy is variant-aware but knows only three variants.**
  `Registry._check_collision` special-cases `built-in` and errors on operator-over-operator; every
  other pairing falls through to a generic "publisher ordering conflict". There is no
  `system-plugin` case. The refactor left the collision policy unchanged.
- **CLI subapps attach at import time**, before any config is read; there is no config-aware command
  registration path today.
- **Two Fable-tier reviews of the pre-reset plugin code found the shape sound but nearly every
  advertised invariant enforced by convention, not construction.** Their hardening (atomic,
  validating registration; typed errors for duplicate/malformed descriptors; inverted registration
  control; typed manifest errors over `assert`) is folded into the requirements below rather than
  rediscovered.

## Terminology

- **System plugin**: an in-repo, in-release unit under `agentworks/plugins/<name>/` bundling
  capability implementations and/or declarable YAML manifests. "Plugin" alone means system plugin.
- **External plugin** (reserved, out of scope): a plugin installed from outside the app
  distribution.
- **Plugin descriptor**: the in-code object a plugin exposes (a `Plugin`) declaring its name,
  description, contributed capability implementations, bundled-manifest location, and reserved
  forward-looking fields (declared execution scopes, owned commands).
- **Installed set**: every system plugin this build ships (all descriptors in the plugin index).
- **Enabled set**: the subset an operator has turned on in `[plugins]`.
- **Enablement axis / present-but-disabled**: the registry's `enabled | disabled` node state. A
  shipped-but-not-opted-in plugin's contributions are present nodes marked `disabled`, distinct from
  absent (a typo or an uninstalled unit) and from present-and-enabled-but-not-ready.
- **Enablement producer / source**: a contributor to `_node_enablement`. This SDD builds one source
  (plugin opt-in). R13 designs the axis to compose multiple sources (a future operator-explicit
  disable), each carrying its own disabled reason.

## Functional requirements

- **R1 The `system-plugin` origin variant becomes constructible.** `Origin` gains `system-plugin` in
  its `variant` `Literal` and an `Origin.system_plugin(*, plugin, source)` factory carrying the
  plugin name and a code-source identifier (`agentworks.plugins.<name>/manifests/<file>` for
  manifests, `agentworks.plugins.<name>` for capability rows). Origin rendering gains the display
  shape `system-plugin <plugin> (<source>)`. `external-plugin` stays reserved and not constructible.

- **R2 A plugin is a package with a validated descriptor.** Each system plugin lives at
  `agentworks/plugins/<name>/` and exposes a `Plugin` descriptor declaring: `name`, `description`,
  the capability implementations it contributes (grouped by capability kind), the package/directory
  holding its bundled YAML manifests (optional), and the reserved forward-looking fields (R10). All
  fields are optional except identity. The descriptor is **validated as a whole before it is used**
  (Fable hardening): the plugin `name` is non-empty and `/`-free (it is the identity the origin
  taxonomy and future trust model hang off); every contributed capability kind has a registration
  adapter (R6); every contributed impl is a class carrying a non-empty, `/`-free `name`; there are
  no intra-descriptor name collisions. A malformed descriptor is a typed error naming the plugin,
  not a raw `AttributeError` or a silently-seated instance.

- **R3 The installed set is a code-side index with inverted registration control.**
  `agentworks/plugins/` exposes the installed set as a name-keyed mapping of descriptors. The index
  **imports each shipped plugin module and calls `register_plugin(module.PLUGIN)` itself** (Fable
  hardening: registration is not an import side effect). This wraps a plugin's registration failure
  with plugin attribution (not an opaque traceback that kills the whole CLI), derives provenance
  from the real module (no self-declared-name spoofing), and makes external-plugin loading later
  "another way to obtain a descriptor" rather than a new authoring contract. A duplicate plugin name
  in the index is a typed error (not last-writer-wins). Shipping a plugin means adding it to this
  index; there is no filesystem discovery and no external loading. The shipped index starts empty
  (R11).

- **R4 Enablement is operator config, strictly opt-in.** `config.toml` gains a `[plugins]` section
  whose `enabled` list names the plugins to activate; absent section or absent name = not enabled.
  Enabling a plugin whose name is not in the installed set is a typed config error (a typo or an
  uninstalled plugin is caught loudly), raised where the name is resolved, before any publish, never
  a `KeyError`. `[plugins]` carries only enablement; unknown keys in the section are a config error
  (keeping the door for future per-plugin settings explicit). Enablement is a setting (it configures
  the operator's install), not a resource, and is present on both config load paths.

- **R5 Impls register atomically at import; all shipped plugins publish; enablement is an overlay.**
  Two phases, both leaning on the landed registry model:
  - **Registration (import time), atomic and validating.** `register_plugin` validates the whole
    descriptor (R2) **first**, then seats every contributed impl into its kind's implementation
    registry, keyed by the impl's `name`, reconciling class-vs-instance (three registries take the
    impl class; `secret-backend` takes a constructed instance). Seating is all-or-nothing: a
    mid-descriptor failure leaves no impl seated for that plugin. Registration is idempotent per
    impl name; a collision with an existing impl is a typed error at registration. This runs for
    every _shipped_ plugin, unconditionally, exactly as core impls populate their registries.
  - **Publication (build time), unconditional, with enablement as the overlay.** `build_registry`
    publishes a read-only capability row for **every shipped plugin's** contributed impl, with a
    `system-plugin` origin, whether or not the plugin is enabled, mirroring R13's unconditional
    built-in publication. Enablement does not gate this publication; instead `_node_enablement` (R9)
    marks a not-opted-in plugin's rows `disabled`. A plugin's **bundled manifests** publish only for
    **enabled** plugins (see R9 for why capabilities and manifests differ). `build_registry` mutates
    no module-level state (impls were seated at import); it only publishes, preserving its
    documented purity.

- **R6 Plugins contribute implementations of existing kinds only.** A v1 plugin may contribute
  capability implementations of the core-fixed capability kinds (`vm-platform`, `harness`,
  `secret-backend`, `git-credential-provider`) and declarable YAML resources of existing declarable
  kinds. A plugin does not introduce a new capability kind or declarable kind. A plugin naming a
  capability kind with no registration adapter is a typed error (enforced at descriptor validation,
  R2), so "existing kinds only" holds by construction.

- **R7 Precedence and collisions are an explicit, order-independent matrix.** Because the
  interleaved publisher order means a single `build_registry` slot cannot sit "after all built-ins,
  before all operator rows", precedence is decided by the unordered
  `{existing.variant, incoming.variant}` pair, not publish order. `Registry._check_collision` is
  extended: `operator-declared` overrides `built-in` or `system-plugin` where the kind's
  `builtin_override` permits, else a typed reserved-name error; `system-plugin` and `built-in` are
  peers (a typed error either way); two `system-plugin` rows on the same `(kind, name)` are a typed
  error. Existing operator-vs-operator and built-in-vs-built-in behavior is unchanged. Because
  system plugins are a **curated in-repo set**, a name collision among them (even between two
  not-enabled plugins, since all shipped plugins publish per R5) is a curation bug and a build error
  is the correct, loud outcome; resource-name namespacing that would let independent external
  plugins coexist is deferred (Future direction). Each new pairing gets its own clear message, not
  the generic "publisher ordering conflict".

- **R8 Plugin capabilities are configured at their consumption sites.** A plugin capability draws
  its config from the consuming resource that references it (a `vm-site`, a `session-template`),
  identically to a built-in capability. `[plugins]` carries only enablement; there is no per-plugin
  runtime config surface in v1.

- **R9 A not-enabled plugin is present-but-disabled via the registry axis, and disabled
  contributions are out of the default surface.** The plugin work produces the enablement axis and
  relies on the registry to distribute and gate it; it invents no parallel mechanism.
  - **Capabilities: present-but-disabled.** `_node_enablement` marks a not-opted-in plugin's
    capability rows `disabled`. A resource that references one is not-ready with the fold's
    synthesized "depends on `<cap>`, which is disabled; enable plugin `<name>`" (the hint comes from
    the disabled node, not a reference-time index probe). This is why capabilities publish
    unconditionally (R5): an operator-declared `vm-site` naming a not-yet-enabled plugin's platform
    gets a helpful enable hint, not an unknown-name hard error.
  - **Bundled manifests: enabled-only (a scoped v1 limitation).** A plugin's own declarable
    resources publish only when the plugin is enabled: a not-enabled plugin contributes no
    resources, which keeps a disabled plugin's resources out of collision checks against operator
    resources and is simpler than publishing-then-disabling them. **Known limitation, stated
    honestly:** unlike a capability (referenced by name from an operator resource, so
    present-but-disabled buys the clean enable-hint), a bundled _declarable_ resource **is** also
    referenceable by name, e.g. an operator `vm-template` with `extends = <plugin-template>`. Under
    enabled-only publication, referencing a not-enabled plugin's bundled resource yields the
    registry's unknown-name hard error, not the "enable plugin X" hint the capability side gives, so
    the two are inconsistent for a plugin that ships referenceable bundled resources. This is inert
    in v1 (the shipped index is empty; no real plugin ships bundled resources), so it is deferred:
    the **follow-on** that ships the first plugin with referenceable bundled resources should move
    manifests to present-but-disabled (with enablement-aware collision so a disabled plugin's
    resource never blocks an operator's name), for symmetry with the capability side.
  - **Default-surface rule: disabled hides, not-ready shows.** Disabled (enablement-axis)
    contributions are **hidden from the default `resource list`**, realizing the signal-not-noise
    motivation, while a not-ready (present, enabled, blocked) node such as a host-unsupported
    built-in still shows. An explicit `describe KIND/NAME` still renders the named row (annotating
    its disabled state), since hiding a resource an operator asked for by name would be
    user-hostile; the hide rule is list-only. A reference still surfaces the enable hint, and an
    explicit opt-in view (a `--include-disabled` flag or a future `agw plugins` surface) can reveal
    the disabled rows in a listing; that separation of "off by opt-in" from "on but blocked" is the
    coherent default.
  - **The plugin roster (doctor).** Because a plugin is an origin, not a resource (R12), the enabled
    state of the _plugin unit_ is shown as a `doctor` roster: `plugin <name>: <description>` tagged
    enabled or `disabled (not enabled in [plugins])`. Roster only: existence, description,
    enable-state; it never enumerates a disabled plugin's contributed capabilities or resources.
    Because every contributed resource carries the `system-plugin` origin, resource listings
    annotate provenance ("from plugin `<name>`") without the plugin being a resource.

- **R10 The trust and command doors are left open, not built.** The descriptor reserves (a) an
  optional declaration of the identity/level a plugin's capabilities act at (the least-privilege
  _convention_: a vm-level capability defaults to the admin user, an agent-level one to the agent
  user), typed to the existing `ScopeLevel` vocabulary, recorded and displayable but unenforced; and
  (b) a place for plugin-owned CLI commands, unpopulated in v1. Neither is wired to behavior. The
  future trust model is distribution trust plus explicit enablement, expressed through the origin
  tiers, not runtime sandboxing.

- **R11 The mechanism is proven, the product ships no demo.** The end-to-end path (descriptor to
  index to registration to unconditional publication to enablement-overlay to consumption) is proven
  by a test-fixture plugin. The shipped `agentworks/plugins/` starts with the framework and an empty
  installed set; no inert example plugin ships. The first real system plugin is a follow-on.

- **R12 A system plugin is an origin, not a resource.** A plugin is a _source_ of resources, not a
  referenced node in the resource graph; the plugin taxonomy lives on `Origin` (the `system-plugin`
  variant), not as a `plugin` resource kind. The enablement axis it produces lives on the resources
  it contributes (their graph nodes), not on a plugin node. The roster (R9) is a `doctor` surface,
  not a registry kind. Making a plugin a first-class declarable resource is coherent only for
  _external_ plugins under the future trust model; out of scope here.

- **R13 Enablement is a first-class, multi-source axis; leave the door open for operator-explicit
  disable.** Per operator direction, disablement is not a plugin-specific notion: system plugins are
  the _first_ producer of disabled state, not the last. The enablement axis (already first-class on
  the registry) is designed so `_node_enablement` **composes multiple sources**, and a node's
  `disabled` verdict carries **which source disabled it and why** (so a dependent's hint reads
  "enable plugin `azure`" vs, in the future, "re-enable this unit; the operator disabled it in
  config"). This SDD builds exactly one source (plugin opt-in) but requires the seam to be shaped
  for a future operator-explicit disable of **individual units at capability/resource granularity,
  including specific parts of a third-party plugin** the operator otherwise trusts. Concretely:
  `_node_enablement` takes the form of a composition over sources rather than a plugin-only
  computation; the disabled reason is carried on the enablement state, not hard-coded to "not
  enabled in [plugins]"; and nothing in the plugin producer assumes it is the only source. No
  operator-explicit-disable surface, config, or per-unit granularity is built here; the requirement
  is that adding one later needs no re-shaping of the axis or the producer seam. **One known
  future-work item that surface will bring** (noted so it is not a surprise): in v1 the surfaces
  that need the disabled _reason_ but do not read the fold-carried mark (the doctor roster,
  `describe`, the use-time gates) re-derive it from the row's `system-plugin` origin. An
  operator-explicit-disable source can disable a **built-in** node, which has no plugin origin to
  re-derive from, so those surfaces will need to read the reason from the disable source (or a
  persisted reason) rather than the origin. This is a display-layer follow-on, not an axis or
  producer change.

- **R14 The opt-in guarantee holds for every kind a plugin contributes; each consumer honors
  enablement per self-determined readiness.** R9's promise ("nothing a not-enabled plugin offers is
  available at a consumption site") must hold for all four capability kinds R6 allows. But _how_ a
  consumer honors a disabled dependency is the consumer's own choice (the registry's
  self-determined-readiness principle), not a uniform not-ready propagation. The four:
  - **vm-platform**: the `vm-site` **propagates**, it is not-ready when its single platform is
    disabled (a site serves no purpose without its platform), and use-time site resolution refuses a
    not-ready site. Already wired by the registry refactor.
  - **secret-backend**: the `secret` stays **ready** and consults backend enablement in its own
    resolution/validation (a secret maps to many backends; ready is not resolvable). Already wired
    by the refactor.
  - **git-credential-provider**: the `git-credential` **propagates**, like a vm-site it has a single
    provider and is therefore not-ready when that provider is disabled, with a use-time refusal.
    **Wired by this SDD** (a `not_ready` hook plus the use-time gate).
  - **harness**: the `session-template` stays **ready** and gates the harness at **use**
    (constructing or using a disabled harness is a typed error naming the plugin to enable), the
    secret model, not propagation. **Wired by this SDD** (the harness-construction/use sites read
    `enablement_of` and refuse a disabled harness).

  The two the refactor left un-wired (git-credential-provider, harness) are closed here because the
  plugin work is the first producer of a disabled node for _any_ kind: before it, nothing produced
  disabled state, so those consumers "opting out" of readiness was harmless; the moment a plugin can
  disable them, an un-gated harness or git-credential-provider would be silently usable while
  not-enabled, holing strictly-opt-in for half of R6's kinds. Which model each kind uses is the
  consuming resource's call (per above), not a blanket rule.

  **Operator-facing consequence** (falls out of the gates, stated so it is not a surprise):
  disabling a plugin that an operator's resources already depend on does not tear anything down. An
  already-running VM or session keeps running (nothing re-reads enablement mid-life); but the next
  operation that would (re)construct the disabled unit, a VM reinit/restart on a disabled platform,
  a git op needing a disabled provider, a session (re)build on a disabled harness, refuses loudly
  with the "enable plugin `<name>`" hint. Disable is a forward gate, not a retroactive teardown.

## Scope

In scope: the `system-plugin` origin variant (R1); the validated `Plugin` descriptor and the
code-side installed index with inverted, atomic, validating registration (R2, R3); import-time impl
registration reconciling the class- and instance-shaped registries (R5); the `[plugins]` config
section and loader (R4); the unconditional plugin capability-row publication and enabled-only
manifest publication in `build_registry`, publication-only and pure (R5); the per-capability-kind
registration adapter (R5, R6); the `_check_collision` precedence extension for `system-plugin` (R7);
the `_node_enablement` producer that marks a not-opted-in plugin's contributions disabled, composed
over sources with per-source reasons (R9, R13); **closing the enablement-consumer gap for the two
kinds the refactor left un-wired**, a `not_ready` hook + use-time gate on `git-credential`
(propagates), and a use-time enablement gate at the harness-construction sites for
`session-template` (errors at use) (R14); the "disabled hides, not-ready shows" default-surface rule
and the `doctor` plugin roster (R9); the reserved (unenforced) least-privilege-scope and
command-declaration descriptor fields (R10); a test-fixture plugin proving the path (R11); author
docs (`agentworks/plugins/README.md`) and an ADR.

Out of scope: external plugins and any external loading/installation/discovery; the trust model and
its enforcement; plugin-owned CLI commands; new capability or declarable kinds from plugins;
per-plugin runtime config beyond enablement; resource-name namespacing; plugin versioning; **the
operator-explicit-disable surface itself** (R13 shapes the seam only); migrating any existing
built-in into a plugin (an optional follow-on).

## Future direction (the broader plugin vision)

This initial structure is the first increment of a larger plugin system. In rough dependency order,
the increments beyond this one:

- **Operator-explicit disable (R13's door, built).** An operator surface to disable individual
  units, including specific capabilities or resources within an otherwise-trusted third-party
  plugin, as a second `_node_enablement` source composing with plugin opt-in. The axis and the
  producer seam are shaped for this here; only the operator surface and its config remain.
- **Plugin namespacing as the attribution and trust unit.** A plugin declares a namespace under
  which its resource names, capability names, config keys, and state live, so every contribution is
  attributable and two independent (external) plugins cannot silently collide. This SDD keeps flat
  global names plus collision errors (R7); namespacing is the upgrade that makes plugins a real
  distribution/trust unit.
- **Feature capabilities: lifecycle participation.** A new capability kind bound to one level (vm,
  workspace, or agent) that participates in that level's create/delete lifecycle, activated by
  opt-in on that level's template, handed a context (its config, the core resource and lineage, an
  SSH runner, its namespaced state). This is what lets a plugin _do_ something at init, not merely
  ship resources; the single biggest piece this SDD does not build.
- **Feature dependencies**, a **namespaced per-feature state store**, **namespaced plugin config and
  doctor contributions**, and **external plugins and the trust model** (the `external-plugin` origin
  becomes constructible; the boundary is distribution trust plus explicit enablement, not runtime
  sandboxing). One concrete hardening external plugins require: the shared bundled-manifest
  publisher must raise a typed error rather than `assert not manifests.issues` (a malformed
  third-party manifest must not crash on an `AssertionError`, stripped under `python -O`); this SDD
  already replaces the assert for its own publisher.

Already landed independently: the **registry readiness refactor** (the enablement axis, the fold,
the `_node_enablement` seam this SDD produces into) and the **harness capability** (ADR 0020), which
is why this SDD treats `harness` as an existing core-fixed kind.
