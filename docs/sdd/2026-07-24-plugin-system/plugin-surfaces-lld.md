# LLD (c): plugin surfaces (origin, publish step, config, presentation, doctor)

Implements HLA [components 1, 4, 5, 6, 9](./hla.md). Governs plan [Phases 1, 3, 5, 6](./plan.md);
FRD R1, R4, R5 (publication half), R9 (manifests and presentation), R10, R12. Owns the
`system-plugin` origin and its rendering, the `[plugins]` config loader, `plugins.publish_plugins`
and its `build_registry` wiring, the shared typed-error manifest loader body, the disabled-hides /
not-ready-shows default-surface rule, and the doctor plugin roster. It leans on LLD (a)'s descriptor
and adapters and LLD (b)'s enablement producer; it invents no new gate and no bespoke
disabled-roster dispatch.

## 1. The `system-plugin` origin (R1, Phase 1)

`resources/origin.py` (the reserved variant is documented at `origin.py:26-28`):

- Add `"system-plugin"` to the `variant` `Literal` (`origin.py:68`).
- Add a field `plugin: str | None = None` (populated for `system-plugin`, `None` for the other
  variants; typed broadly on the class like the existing `file`/`line`/`source`, `origin.py:69-71`).
- Add the factory:

  ```python
  @classmethod
  def system_plugin(cls, *, plugin: str, source: str) -> Origin:
      """A resource contributed by a system plugin (R1). `plugin` is the
      plugin name; `source` is a code-source identifier
      (`agentworks.plugins.<name>` for a capability row,
      `agentworks.plugins.<name>/manifests/<file>` for a bundled manifest).
      `file`/`line` are None."""
      return cls(variant="system-plugin", plugin=plugin, source=source)
  ```

- Extend the variant-contract docstring (`origin.py:58-66`): `system-plugin` has `plugin` set,
  `source` a `str`, `file`/`line` `None`. `external-plugin` **stays reserved and not constructible**
  (documented-only; no `Literal` entry, no factory).
- **Rendering.** `resources/render.py` `format_origin_line` (`render.py:18-42`) gains a branch
  before the final `raise`:

  ```python
  if origin.variant == "system-plugin":
      return f"system-plugin {origin.plugin} ({origin.source})" if origin.source else f"system-plugin {origin.plugin}"
  ```

  This is the `system-plugin <plugin> (<source>)` shape. `format_origin_location`
  (`render.py:45-58`) needs no change: a `system-plugin` row carries no `file:line`, so it correctly
  falls through to the labelled `format_origin_line` form.

`resources/inspect.py`'s `_ORIGIN_FILTER_MAP` (`inspect.py:120-124`) and its origin-count breakdown
(`inspect.py:202-208`) gain a `system-plugin` entry (filter key `plugin` -> `system-plugin`; a
`plugin_count`) so `agw resource list --origin plugin` and the header total stay honest once plugin
rows exist. (Phase 1 can add the render branch alone; the filter/count land with the presentation
work in Phase 6, whichever the implementer prefers, as long as no variant is unhandled when a plugin
row can exist.)

## 2. `[plugins]` config (R4, R8, Phase 3)

- `config/models.py` `Config` (`models.py:75`) gains `plugins_enabled: tuple[str, ...] = ()`,
  carried exactly like `secret_config_data` (`models.py:108`): a **setting**, not a resource, empty
  when the section is absent, present on **both** load paths. It is consumed only in
  `build_registry` (LLD b's source and this LLD's publish step); it is never published as a
  pseudo-resource (mirroring the `secret_config` non-publication note, `models.py:140`).
- A loader `_load_plugins(data, issues, decls)` in `config/loaders_secrets.py` (beside
  `_load_secret_config`, `loaders_secrets.py:184`, since both are top-level settings tables),
  reading `[plugins] enabled = [...]`:
  - Absent `[plugins]` table -> `()`.
  - `[plugins]` present but not a table -> `ConfigError`.
  - `enabled` absent -> `()`; present and not a list of strings -> `ConfigError`.
  - **Unknown keys in `[plugins]` are a hard `ConfigError`**, not a collected warn-issue. This
    **diverges** from `_warn_unexpected_keys` (`loaders_core.py:40-58`), which `secret_config` uses
    to accumulate a soft issue. The divergence is deliberate and pinned: R4 requires unknown keys to
    be a config error precisely because `[plugins]` is an opt-in gate, a typo'd key (`enabeld`, or a
    future per-plugin key used a release too early) must fail loudly, not silently leave plugins
    un-enabled behind a warning the operator may miss. Message names the section and the offending
    keys. **Required in-code note (review disposition):** the loader carries an explicit comment
    stating this is a deliberate departure from the soft `_warn_unexpected_keys` convention and WHY,
    so a future section author does not "consistency-fix" it back to soft-warn; the same rationale
    is recorded in the Phase 6 ADR as the project stance on strict-vs-lenient config sections.
- `config/load.py` calls `_load_plugins(data, issues, decls)` on the **settings** path (`data`, not
  `resource_data`, `load.py:152`), so it is populated identically under
  `load_config(resources=False)`, and passes `plugins_enabled=...` into the `Config(...)`
  construction (`load.py:176-201`).

## 3. `plugins.publish_plugins(registry, config)` (R5 publication, R9 manifests, Phase 5)

`plugins/publish.py`. Publication only; impls were seated at import (LLD a), so this mutates no
module-level state and `build_registry` stays pure.

Order inside the function:

1. **Resolve enabled names first, before any publish.** Collect
   `unknown = [n for n in config.plugins_enabled if n not in SYSTEM_PLUGINS]`; if non-empty, raise a
   single typed `ConfigError` listing all unknowns (a typo or an uninstalled plugin, R4), **never**
   a `KeyError`, and **before** any `registry.add`. This is the up-front resolution the FRD requires
   (`build_registry`'s post-finalize block never sees an unknown enabled name). The Phase 3 loader
   does not normalize `config.plugins_enabled`, so this resolution is also where a **duplicate**
   name (`["azure", "azure"]`) or an **empty** name (`[""]`) is handled: membership-testing against
   `SYSTEM_PLUGINS` already makes an empty or duplicated name a no-op for a real plugin (an empty
   name is an unknown -> `ConfigError`; a duplicate resolves to the same plugin, and publication
   iterates `SYSTEM_PLUGINS` not the enabled list, so it cannot double-publish), so no separate
   dedup pass is needed, the set membership carries it.
2. **Capability rows for every shipped plugin, unconditionally** (R5, mirroring the built-ins'
   unconditional publication, `vm_platform/__init__.py:89`). For each `plugin` in
   `SYSTEM_PLUGINS.values()`, for each `(kind, impls)` in `plugin.capabilities`, for each impl:

   ```python
   origin = Origin.system_plugin(plugin=plugin.name, source=f"agentworks.plugins.{plugin.name}")
   name = impl.name
   registry.add(kind, name, CAPABILITY_ADAPTERS[kind].build_row(name, origin), origin)
   ```

   Enablement does **not** gate this: LLD (b)'s producer marks a not-opted-in plugin's rows
   `disabled`. `build_row` reads the seated impl (LLD a), so a row exists only for an
   actually-seated impl. Note the refactor's R13 already made every built-in capability publish
   unconditionally, so there is no publish gate to honor and no `_BUILTIN_*`-vs-full-registry split
   (the earlier "host-support publish gate for plugin rows" review finding is **superseded**; there
   is no gate). **Seating is likewise unconditional** and happens at import (the index calls
   `register_plugin` for every shipped plugin, LLD a), independent of `[plugins]`, so a not-opted-in
   plugin's impls are still seated in the four code registries. That is what keeps the R14 harness
   use-gate (LLD b) reachable for a disabled plugin: `_resolve_template` -> `harness_for(name)`
   finds the seated impl and reaches `ensure_harness_enabled` (which raises the enable-plugin
   error), rather than hitting an unknown-harness `ConfigError` or `_impl_for`'s fail-fast. Phase
   5's tests pin this: a disabled plugin's harness resolves through `_resolve_template` far enough
   to hit the use-gate.

3. **Bundled manifests for enabled plugins only** (R9). For each **enabled** `plugin` whose
   `plugin.manifests` is set, load and publish via the shared body (section 4), stamping each entry
   `Origin.system_plugin(plugin=plugin.name, source=f"agentworks.plugins.{plugin.name}/manifests/{file.name}")`.
   Manifests are enabled-only because a not-enabled plugin offers no resources: there is no external
   reference needing an enable hint for them (they are the plugin's own offering, not a name an
   operator writes), so gating publication is simpler than publish-then-disable and keeps a
   not-enabled plugin's resources out of collision checks against operator resources (R9).

## 4. The shared, typed-error manifest loader body (R9, Phase 5)

Factor the load and iterate that `manifests/builtin.py` (`builtin.py:23-51`) and `publish_plugins`
share into one helper in `manifests/` (e.g.
`publish_manifest_package(registry, package, origin_for)` where `package` is an importlib-resources
anchor and `origin_for(file_name) -> Origin` stamps the per-file origin). The helper resolves the
directory via `importlib.resources` exactly as `builtin.py:38-42` does, calls `load_manifests`, and
**raises a typed error** (`ConfigError`) on `manifests.issues` instead of
`assert not manifests.issues` (`builtin.py:44`). The assert is stripped under `python -O` and is the
wrong failure mode for the eventual external-plugin path; a typed raise is correct for first-party
bundles now regardless (FRD Future direction pre-pays this hardening). `builtin.py` migrates to the
shared helper (leave-it-nicer, and it retires the assert); its origin is
`Origin.built_in(source="agentworks.manifests.builtin/<file>")`, the plugin's is the
`system_plugin(...)` form above.

## 5. `build_registry` wiring, staying pure (R4, R5, R7, Phase 5)

In `bootstrap.build_registry` (def at `bootstrap.py:34`; the publisher region is `~82-106`, interior
anchors `95`/`96`/`98`/`105-106`):

- Insert `plugins.publish_plugins(registry, config)` **between** the built-in capability rows
  (`vm_platforms.publish_to(registry)`, `bootstrap.py:95`) and `config.publish_to(registry)`
  (`bootstrap.py:96`). Publication-only, so purity holds.
- Pass LLD (b)'s source to finalize:
  `registry.finalize(enablement_sources=[plugins.plugin_enablement_source(config)])` (replacing the
  bare `registry.finalize()` at `bootstrap.py:98`).

Precedence is **not** a function of the insertion point: `_check_collision` decides by the unordered
variant pair (LLD a), so the result is identical wherever the plugin rows land relative to operator
rows. The unknown-enabled-name error is raised inside `publish_plugins` (step 1), not in the
post-finalize `secrets.validate_chain` / `validate_sites` block (`bootstrap.py:105-106`).

## 6. Disabled hides, not-ready shows (R9 presentation, Phase 6)

The plugin work is the **first** producer of `disabled`, so `resources/inspect.py` is where the
default-surface rule is set:

- `list_resources` (`inspect.py:139`) gains `include_disabled: bool = False`. In the per-row loop
  (`inspect.py:184-201`), skip a row when
  `registry.graph.enablement_of(kind, name) is Enablement.disabled` and `not include_disabled`.
  **Not-ready rows still show**: the filter is on the enablement axis (`enablement_of`), never on
  readiness (`not_ready_reason_for` stays as-is, `inspect.py:218-229`), so a host-unsupported
  built-in (present, enabled, blocked) continues to list with its `(not ready)` marker. This is the
  coherent default: "off by opt-in" hides, "on but blocked" shows.
- `describe_resource` (`inspect.py:266`) is an **explicit** lookup by name, so it always renders the
  named row even when disabled, annotating its state. It reads `enablement_of` (the **binary** axis)
  to decide whether to add a `Disabled: <...>` line, and derives the line's **text** from the row's
  `system-plugin` origin plus config, exactly as the doctor roster does
  (`Disabled: not enabled in [plugins] (plugin <origin.plugin>)`), **not** from a per-node reason:
  the disabled reason lives on the transient `DisabledMark`, never on the frozen graph node (LLD b,
  `build_graph` untouched), so there is nothing to read off the node. It does not suppress the row
  (an operator debugging a specific disabled resource asked for it by name).
- **Provenance annotation.** A `system-plugin` row renders `from plugin <name>` (off
  `origin.plugin`) in the list DESCRIPTION cell / describe header, so a plugin's contributed
  resources are attributable without the plugin being a resource (R12). This reads the origin
  already on each row (LLD c step 1), not a separate lookup.
- **CLI and completions.** `agw resource list` gains `--include-disabled` (default off). Regenerate
  the completion tree (the always-consider-completions rule) and verify the new flag appears.

## 7. The doctor plugin roster (R9, R10, R12, Phase 6)

A **bespoke** doctor surface, not a `KIND_REGISTRY`-dispatched hook (a plugin is an origin, not a
resource kind, R12). A new `_check_plugins(config)` group in `doctor.py`, added to `run_checks`
(`doctor.py:91-117`) beside `_check_vm_platforms` (`doctor.py:226`):

- Iterate `SYSTEM_PLUGINS` (import from `agentworks.plugins`) against `config.plugins_enabled`. For
  each: `plugin <name>: <description>`, tagged `ok` when enabled, `info`
  `disabled (not enabled in [plugins])` when not. **Roster only**: existence, description,
  enable-state; it **never** enumerates a disabled plugin's contributed capabilities or resources
  (that is what the enablement axis and the reference hint are for).
- The reserved `required_scopes` (R10), when populated, render as an **informational** line under
  the plugin's row (`least privilege: <levels>`), unenforced. Empty (the v1 default) renders
  nothing.
- Empty `SYSTEM_PLUGINS` (the shipped build) renders an empty-but-present group
  (`No system plugins installed.`), so the surface exists and is testable before any plugin ships.

## What does not change

The capability kinds and `KIND_REGISTRY`; the manifest loader's envelope/decode; the finalize passes
(this LLD publishes into them and reads their output); the CLI entry flow and command registration
(no plugin owns a command in v1). `build_registry`'s purity is preserved (publication-only step,
opaque source callable). The `_check_collision` policy change is LLD (a)'s.

## Acceptance (Phases 1, 3, 5, 6 tests must pin)

- **Phase 1**: `Origin.system_plugin(plugin=..., source=...)` constructs and renders
  `system-plugin <plugin> (<source>)`; `external-plugin` is still not constructible; no `Origin`
  variant is unhandled by `format_origin_line`.
- **Phase 3**: `[plugins] enabled = [...]` parses to `Config.plugins_enabled` on both load paths;
  absent section -> `()`; an unknown key in `[plugins]` is a hard `ConfigError` (not a warn-issue);
  a non-list `enabled` is a `ConfigError`.
- **Phase 5**: an **enabled** plugin's capability row and manifest resource are present, enabled,
  and consumable at their site with a `system-plugin` origin; a **not-enabled** plugin's capability
  row is **present-but-disabled** (an operator `vm-site` referencing it is not-ready with
  `enable plugin <name>`, not unknown-name) and its manifest resources are **absent**; an unknown
  enabled name is a typed `ConfigError` raised **before** any publish (not `KeyError`, not
  post-finalize); the shared manifest body raises a typed error (not `AssertionError`) on a
  malformed bundle, and `builtin.py` routes through it; `build_registry` mutates no module-level
  state (purity preserved).
- **Phase 6**: `resource list` hides disabled plugin rows by default while still showing a not-ready
  built-in; `--include-disabled` reveals them with `from plugin <name>` provenance; `describe` of a
  disabled row still renders it; the doctor roster lists `plugin <name>: <description>` tagged
  enabled / `disabled (not enabled in [plugins])`, never enumerating contributions, renders
  `required_scopes` informationally when set, and shows the empty-state for the shipped empty index;
  completions include `--include-disabled`.
