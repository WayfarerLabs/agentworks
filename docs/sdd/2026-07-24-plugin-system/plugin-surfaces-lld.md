# LLD (c): plugin surfaces (origin, publish step, config, presentation, doctor)

Implements HLA [components 1, 4, 5, 6, 9](./hla.md). Governs plan
[Phases 1, 3, 5, 6, and 7's publication/collision half](./plan.md); FRD R1, R4, R5 (publication
half), R9 (manifests and presentation), R10, R12. Owns the `system-plugin` origin and its rendering,
the `[plugins]` config loader, `plugins.publish_plugins` and its `build_registry` wiring, the shared
typed-error manifest loader body, the disabled-hides / not-ready-shows default-surface rule, and the
doctor plugin roster. It leans on LLD (a)'s descriptor and adapters and LLD (b)'s enablement
producer; it invents no new gate and no bespoke disabled-roster dispatch.

> **Phase 7 revision (2026-07-30, reopened SDD):** section 3 step 3 and the new section 3b replace
> the Phase 5 enabled-only manifest gate with **manifest present-but-disabled parity** (the R9
> resolution): bundled manifests publish unconditionally, a disabled plugin's declarable rows are
> published **weak** so they never block a stronger row, and the reference-side use gate is LLD
> (b)'s "declarable-reference gap" section. The Phase 5 checkboxes and acceptance bullets remain the
> immutable record of what Phase 5 shipped; the sections below describe the target state Phase 7
> implements.

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

3. **Bundled manifests for every shipped plugin, unconditionally** (R9, revised for Phase 7). For
   each shipped `plugin` whose `plugin.manifests` is set (enabled or not), load and publish via the
   shared body (section 4), stamping each entry
   `Origin.system_plugin(plugin=plugin.name, source=f"agentworks.plugins.{plugin.name}/manifests/{file.name}")`.
   The `enabled` set (`publish.py:94`) no longer gates publication; it decides only the row
   **strength** (section 3b): a not-enabled plugin's manifest rows publish **weak**, an enabled
   plugin's publish normally. The Phase 5 rationale for enabled-only ("a not-enabled plugin offers
   no resources an operator references by name") is **superseded**: bundled declarables are
   referenceable by name (an agent-template's `user_install_commands`, a template's `inherits`), and
   an absent row makes such a reference an unknown-name hard error at `_resolve_misses`
   (`resources/registry.py:525-527`, the `miss_policy="error"` branch), while a present-but-disabled
   row gets the enable hint. The `publish.py` module docstring's "two deliberate asymmetries"
   paragraph (`publish.py:12-22`) pins the superseded rationale and is rewritten in the same change
   (doc lockstep).

## 3b. Manifest present-but-disabled parity (R9 resolution, Phase 7)

Bundled declarable resources behave like capabilities under enablement: present when shipped,
disabled by the same overlay, never an unknown-name hard error, never a block on a stronger row.
Three pieces; the third (the reference-side use gate) is LLD (b)'s, cross-referenced at the end.

### 3b.1 Unconditional publication rides the existing overlay (changed: `publish_plugins` step 3)

Dropping the enabled-only gate needs **no new enablement logic**: `plugin_enablement_source`
(`plugins/enablement.py:42-52`) already iterates every present row of **every kind** and marks any
`system-plugin`-origin row whose `origin.plugin` is not enabled, so a manifest row is disabled by
the identical overlay that disables a capability row the moment it is published with the
`system_plugin(...)` origin step 3 already stamps. No per-manifest enablement source, no publish
gate. The disabled rows then get the rest for free: hidden from the default `resource list` (section
6 filters on `enablement_of`, kind-agnostic), rendered by explicit `describe` with the `Disabled:`
line, and excluded from ready-set validation (`registry.py:425-427`).

### 3b.2 The bundleable-kind allowlist (additive: `PLUGIN_MANIFEST_KINDS`)

R9's guarantee ("nothing a not-enabled plugin offers is available at a consumption site") is only
real for a declarable kind whose consumption sites are actually gated (LLD b's named-row rule). That
invariant is **enforced at publish, not documented**: a frozen set in `plugins/publish.py`

```python
PLUGIN_MANIFEST_KINDS = frozenset({
    "system-install-command", "user-install-command",
    "apt-package", "apt-source",
    "vm-template", "agent-template", "admin-template", "session-template",
})
```

names exactly the declarable kinds whose gates Phase 7 wires (LLD b's site table).
`publish_manifest_package` (`manifests/package.py:34`) gains an **additive** parameter
`allowed_kinds: frozenset[str] | None = None` (`None`, the builtin caller's default, permits every
decoder kind, so `builtin.py` is untouched); a plugin document whose `kind` falls outside the set
raises a typed `ConfigError` naming the kind and the file, which `_publish_plugin_manifests`'s
existing re-raise (`publish.py:120-123`) attributes to the plugin. Expanding the allowlist is a
deliberate act: wire the kind's consumption gate (LLD b), add the kind here, and pin both with a
test. The excluded kinds (`secret`, `git-credential`, `vm-site`, `workspace-template`,
`named-console-template`) are the ones whose consumption paths Phase 7 does not gate; a plugin
cannot bundle them, so no silent hole exists. This narrows R6's letter ("declarable YAML resources
of existing declarable kinds") for the bundled-manifest path; the FRD now carries that
bundleable-declarable constraint, so this is **reconciled**, not an open flag.

**IMPORTANT 3: a plugin must not capture a reserved auto-declared name.** Four of the bundleable
kinds (`vm-template`, `agent-template`, `admin-template`, `session-template`) auto-declare the
reserved name `"default"` (`vms/kinds.py:54,104`, `agents/kinds.py:50`, `sessions/kinds.py:47`):
`_materialize_reserved_defaults` (`registry.py:469-475`) synthesizes it at finalize **only if the
slot is free**. A plugin bundling a `default`-named template lands in that free slot with no
collision (nothing else has published it yet), so `_materialize_reserved_defaults` then skips
synthesis and the plugin's row **becomes** the reserved default: disabled, it gates every
implicit-default use behind "enable plugin X"; enabled, it silently shadows the framework default.
No collision fires, so the enable-every-plugin fixture would not catch it. The same
`publish_plugins` publication (via `publish_manifest_package` in plugin mode,
`allowed_kinds is not None`) therefore **also rejects** any bundled `(kind, name)` where `name` is
in `KIND_REGISTRY[kind].auto_declare_names` (the reserved set, `None`-guarded to empty), raising a
typed `ConfigError` naming the plugin, kind, and reserved name. Builtin manifests
(`allowed_kinds is None`) are unaffected. Pinned with a test: a fixture plugin bundling a `default`
`agent-template` is a typed error, not a silent shadow.

### 3b.3 Enablement-aware collision: the weak-row model (chosen design)

**The constraint.** A disabled plugin's declarable row must never block a stronger row: the
operator, a built-in, or an enabled plugin must win **as if the disabled row were absent**, with no
collision error, while two **enabled** system-plugin rows on one name still collide (curation bug).
But `_check_collision` runs inside `add` (`registry.py:130-132`), enablement is computed at
`finalize` (`registry.py:357-362`), after every `add`, and the `Registry` is config-agnostic (it
imports neither `Config` nor `plugins`), so the collision check cannot ask "is this plugin row
disabled". The one component that knows each plugin's enablement **at publish time** is
`publish_plugins` (it holds `config.plugins_enabled`, `publish.py:94`).

Two collision cases need a "keep existing, drop incoming, no error" outcome the binary
`_check_collision` contract (return = incoming overwrites, raise = error) cannot express today: the
**disabled** case (a weak plugin row must yield to any occupant) and the **enabled-over-operator**
case (BLOCKING 1 below, an enabled plugin row must yield to an operator's row on an
`builtin_override = "allow"` kind). Both are solved by one small generalization: `_check_collision`
returns a **decision** instead of `None`, and `add` gains a weak short-circuit ahead of it.

**The decision type (additive).** `_check_collision` (and `_check_system_plugin_collision`) return

```python
class _CollisionDecision(Enum):
    OVERWRITE = "overwrite"          # store incoming, replacing existing (today's "return")
    KEEP_EXISTING = "keep-existing"  # drop incoming, existing stands, no error (new)
```

Every branch that currently `return`s (built-in over built-in, allow-kind operator over built-in,
the operator-override "allow" path) now `return`s `OVERWRITE`; every `raise` is unchanged. The **one
new** `KEEP_EXISTING` producer is the enabled-plugin-over-operator allow-kind branch (below). `add`
acts on the decision: `OVERWRITE` stores the stamped incoming row, `KEEP_EXISTING` is a no-op.

**Weak rows (the disabled case).** `publish_plugins` publishes a **not-enabled** plugin's manifest
rows as _weak_; weakness is a publisher-declared property of the add (like `origin`), so the
`Registry` honors it without knowing why. Pinned mechanics, each labeled:

- **Additive:** `Registry.add` gains a keyword-only `weak: bool = False`; every existing caller is
  untouched.
- **Additive:** the registry tracks `self._weak: set[tuple[str, str]]`, the keys currently occupied
  by a weak row. Consulted only during `add`; meaningless after finalize.
- **Semantics (pinned, order-symmetric), in `add`, ahead of `_check_collision`:**
  - weak incoming, slot occupied (by anything, weak or strong): **no-op** (`KEEP_EXISTING`); no
    collision check runs, nothing errors.
  - weak incoming, slot free: the row lands normally (stamped via `dataclasses.replace`,
    `registry.py:133-134`) and the key is recorded weak.
  - strong incoming, existing weak: the incoming row **replaces silently** (no `_check_collision`),
    the key leaves the weak set.
  - strong incoming, existing strong: `_check_collision` runs and returns a decision; the variant
    matrix (`registry.py:137-237`) is **unchanged** except that each branch now names its
    `_CollisionDecision`, and the enabled-plugin-over-operator allow branch returns `KEEP_EXISTING`.
- **Changed:** `publish_plugins` step 3 passes `weak=True` for a not-enabled plugin's manifest rows.
  Enabled plugins' manifest rows and **all** capability rows publish strong (capability name clashes
  stay caught at seating in `register_plugin`, which is enablement-independent, LLD a).
- **Additive (invariant guard):** at `finalize`, after `compose_enablement`, every key still in the
  weak set that still holds its weak row must appear in the composed `marks`; a survivor with no
  mark is a `StateError` (a publisher declared weak rows without a disabling source, a framework
  bug). This enforces "weak implies disabled" by construction, config-agnostically: the registry
  compares two sets it already holds.

**BLOCKING 1: the enabled plugin row must not break an operator's legacy TOML row (both orders).**
An operator who legally overrides an install-command / apt entry today (an allow-kind, e.g.
`[system_install_commands] az-cli` in TOML) must keep winning after the azure plugin ships that same
name and is **enabled**. Both encounter orders are genuinely reachable, because the operator's TOML
and YAML surfaces sit on opposite sides of `publish_plugins`:

- **Operator-first** (deprecated TOML, `install_commands.publish_to` / `apt.publish_to` at
  `bootstrap.py:96-97`, before `publish_plugins` at `:107`): existing operator-declared, incoming
  enabled system-plugin. Today `_check_system_plugin_collision`'s reverse branch
  (`registry.py:222-229`) **always raises** under a stale "not reachable under build_registry's
  publish order" comment. That comment is **false post-migration**. The fix: this branch consults
  `builtin_override` **symmetrically** with the forward branch: on an `"allow"` kind it returns
  `KEEP_EXISTING` (operator wins, plugin row dropped, no error); on a `"reserved"` kind it still
  raises (a plugin's reserved declarable cannot be shadowed, unchanged). The stale comment is
  deleted.
- **Plugin-first** (operator YAML `ManifestSet`, `manifests.publish_to` at `bootstrap.py:109`, after
  `publish_plugins`): existing enabled system-plugin, incoming operator-declared. The **forward**
  branch (`registry.py:209-221`) already handles this: `"allow"` returns (now `OVERWRITE`, operator
  replaces the plugin row, operator wins); `"reserved"` raises. Unchanged in effect.

So on an allow-kind the operator wins in **either** order (drop-incoming when operator is existing,
overwrite when operator is incoming); on a reserved-kind an operator-vs-enabled-plugin name clash is
a reserved-name error in either order. The **disabled** plugin case never reaches these branches
(weak short-circuit), so this fix is purely about the **enabled** row. This makes the reverse and
forward system-plugin-vs-operator branches symmetric on `builtin_override`, closing the asymmetry
the "always raise" reverse branch encoded.

_Why not make the enabled allow-kind plugin row weak instead?_ Weakness means "disabled"
(add-if-absent, silently overwritable) and the finalize guard enforces weak-implies-disabled; an
enabled weak row would violate that guard and would also be silently overwritable by a **later**
weak row, neither of which is wanted. The `KEEP_EXISTING` decision is the precise tool: it expresses
"operator wins" without touching enablement.

**Pinned outcome matrix** (declarable rows; capability clashes are seating's, LLD a). "op allow" and
"op reserved" split the operator column by the kind's `builtin_override`:

| existing \ incoming          | weak (disabled plugin)  | strong system-plugin (enabled)    | operator (allow-kind)         | operator / built-in (reserved-kind) |
| ---------------------------- | ----------------------- | --------------------------------- | ----------------------------- | ----------------------------------- |
| _(slot free)_                | weak row lands          | row lands                         | row lands                     | row lands                           |
| weak (disabled plugin)       | no-op (first weak wins) | replaces silently                 | replaces silently             | replaces silently                   |
| strong system-plugin         | no-op                   | `ConfigError` (curation bug)      | operator OVERWRITEs (op wins) | reserved-name `ConfigError`         |
| operator (allow-kind)        | no-op                   | KEEP_EXISTING (op wins) **[fix]** | existing op-vs-op matrix (R7) | n/a                                 |
| operator/built-in (reserved) | no-op                   | reserved-name `ConfigError`       | n/a                           | existing matrix (unchanged)         |

Consequences worth pinning: a disabled plugin row never errors and never displaces, **including on
`builtin_override = "reserved"` kinds** (as-if-absent means no reserved-name error either); an
**enabled** plugin row on an allow-kind yields to an operator's row in either encounter order
(BLOCKING 1 fix), and errors (reserved-name) against an operator on a reserved-kind; two enabled
plugins on one name error (curation bug), which the enable-every-shipped-plugin fixture pins in CI;
enabled-vs-disabled resolves to the enabled row in either order. **Known tradeoff (acknowledged, not
a bug):** two _disabled_ plugins sharing a name silently keep the first-published row, and "first"
is `_INSTALLED_MODULES` insertion order, an implementation detail; the surviving row's enable hint
names its own plugin, and the moment both are enabled the curation `ConfigError` fires, so CI
catches the real problem while the harmless disabled-overlap stays quiet. **FRD/R7: reconciled**
(the lead's amendment landed): R7's "two not-enabled plugins ... a build error" now holds for
capability seating only; for declarable rows the rule is "operator/enabled wins; two _enabled_
system-plugin rows still collide", matching the plan's Phase 7 wording. No open FRD flag remains.

**Why not (B), "publish disabled manifests last, add-if-absent".** Rejected on four grounds, all in
the code:

1. Its premise (a plugin row is always the existing row, an operator row the incoming one) is false
   at HEAD in one direction: the deprecated operator TOML install-command publisher runs **before**
   `publish_plugins` (`bootstrap.py:97` vs `bootstrap.py:107`), so an operator's TOML
   `user-install-command` row is existing when the plugin's manifest row arrives. (B) must therefore
   also move the disabled-manifest step below `manifests.publish_to` (`bootstrap.py:109`), and the
   semantics it needs there is exactly add-if-absent, i.e. (B) still requires (A)'s registry
   mechanism, minus the symmetric direction.
2. It reintroduces the order-dependence R7 and section 5 deliberately retired ("precedence is not a
   function of the insertion point"); any future publisher added after the disabled-manifest step
   silently changes precedence.
3. It splits `publish_plugins` into two `build_registry` touch points bracketing the operator
   publishers, complicating the one assembly point for no gain.
4. (A)'s weak semantics is symmetric (weak never displaces; anything displaces weak), so it is
   correct under any publisher ordering, current and future, and it is smaller: one flag, one set,
   two guard clauses ahead of an untouched matrix.

**Why not (C), finalize-time collision arbitration** (defer all collision decisions to where
enablement is known): `add` stores exactly one row per `(kind, name)` (`registry.py:133-134`), so
deferring arbitration means retaining multi-occupancy per key until finalize, reshaping the store
and every existing collision test to solve only the case (A) solves with a set.

**Note on the built-in branch (landed asymmetry, deliberately left).** The
`{system-plugin, built-in}` branch (`registry.py:206-207`) errors either way and does **not**
consult `builtin_override`, unlike the operator branches. This is not an oversight to "fix" here: in
the migration each phase **removes** its built-in counterpart when it flips a bundle to a plugin (a
platform/harness/etc. is either core-built-in or plugin-shipped, never both), so a live
`{system-plugin, built-in}` collision on the same name does not arise for a migrated bundle. The
branch stays a plain error; a future reader should read the asymmetry as intentional, not a missing
`builtin_override` consult.

### 3b.4 The reference side

A surviving present-but-disabled declarable row resolves cleanly at `_resolve_misses` (a present
target is not a miss, `registry.py:512-513`), so nothing on the publication side surfaces the enable
hint at consumption. The consumption-side rule (the named-row rule, the `ensure_reference_enabled` /
`ensure_recipe_enabled` helpers, and the exact gate sites) is LLD (b)'s "declarable-reference gap"
section; the allowlist above is its enforcement twin.

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

In `bootstrap.build_registry` (def at `bootstrap.py:34`; anchors below are the post-Phase-5 landed
lines):

- `plugins.publish_plugins(registry, config)` sits **between** the built-in capability rows
  (`vm_platforms.publish_to(registry)`, `bootstrap.py:101`) and `config.publish_to(registry)`
  (`bootstrap.py:108`), at `bootstrap.py:107`. Publication-only, so purity holds.
- LLD (b)'s source is passed to finalize:
  `registry.finalize(enablement_sources=[plugins.plugin_enablement_source(config)])`
  (`bootstrap.py:110`).

Precedence is **not** a function of the insertion point: `_check_collision` decides by the unordered
variant pair (LLD a), so the result is identical wherever the plugin rows land relative to operator
rows. The Phase 7 weak-row semantics preserves this (section 3b.3: weak never displaces, anything
displaces weak, in either encounter order), so `build_registry` needs **no reordering** for manifest
parity. The unknown-enabled-name error is raised inside `publish_plugins` (step 1), not in the
post-finalize `secrets.validate_chain` / `validate_sites` block (`bootstrap.py:117-118`).

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
(this LLD publishes into them and reads their output; Phase 7 adds only the weak-survivor guard at
the end of enablement composition, section 3b.3); the CLI entry flow and command registration (no
plugin owns a command in v1). `build_registry`'s purity is preserved (publication-only step, opaque
source callable). The `_check_collision` variant **matrix** is LLD (a)'s and stays untouched; Phase
7's weak short-circuit (section 3b.3) runs before it and changes no matrix pairing.

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
- **Phase 7, publication + collision** (fixture-driven; a fixture plugin shipping a bundled
  manifest; the reference-side cases are LLD b's list):
  - Not enabled: the fixture's manifest resource is **present-but-disabled** (`enablement_of` reads
    `disabled` with the plugin mark), hidden from default `list`, shown by `describe` with the
    `Disabled:` line; enabling the plugin makes it enabled and consumable, same row, same origin.
  - Disabled overwrite, both encounter orders: an operator resource with the same `(kind, name)` as
    a **disabled** plugin's manifest resource wins with **no collision error**, whether the operator
    row publishes first (deprecated TOML path, before `publish_plugins`) or last (operator
    `ManifestSet`); the surviving row's origin is `operator-declared`, and no enablement mark
    attaches to it. Same for a built-in row and for an enabled plugin's row over a disabled one.
  - **BLOCKING 1, enabled allow-kind operator override, both orders:** an operator declaring an
    allow-kind entry (e.g. `system-install-command` `az-cli`) with the **enabled** azure plugin
    shipping the same name finalizes with the **operator row winning and no error**, in **both**
    orders: operator via deprecated TOML (existing, plugin incoming, `KEEP_EXISTING`) and operator
    via YAML `ManifestSet` (incoming, plugin existing, `OVERWRITE`); the survivor is
    `operator-declared`. On a **reserved**-kind (a template) the same operator-vs-enabled-plugin
    name clash is a reserved-name `ConfigError` in either order.
  - As-if-absent on reserved kinds: a **disabled** plugin row on a `builtin_override = "reserved"`
    kind does not trigger the reserved-name error against an incoming operator row.
  - Weak add-if-absent: a disabled plugin's manifest row does not displace an existing occupant of
    any variant, and a slot-free weak row lands and carries the enable hint.
  - Curation bug still loud: two **enabled** plugins publishing the same declarable `(kind, name)`
    raise the two-system-plugins `ConfigError`; the **enable-every-shipped-plugin fixture** (build a
    registry with every `SYSTEM_PLUGINS` name enabled) finalizes cleanly, pinning shipped-set
    curation in CI while disabled-state overlap stays silent.
  - The weak-survivor guard: publishing a weak row and finalizing with **no** enablement source is a
    `StateError` (weak implies disabled, enforced).
  - Allowlist: a fixture plugin bundling a manifest of an excluded kind (e.g. `secret`) is a typed
    `ConfigError` naming the plugin, the kind, and the file; `builtin.py`'s call path (no
    `allowed_kinds`) still publishes every decoder kind.
  - **IMPORTANT 3, reserved-name capture:** a fixture plugin bundling a `default`-named template
    (any of `vm-template` / `agent-template` / `admin-template` / `session-template`) is a typed
    `ConfigError` naming the plugin, kind, and reserved name, so the plugin cannot shadow the
    framework's auto-declared default; `builtin.py`'s path is unaffected.
