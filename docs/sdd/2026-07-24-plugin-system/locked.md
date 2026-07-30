# Locked: system plugins (initial structure)

**Locked 2026-07-30.** The SDD is complete; all six implementation phases landed on
`feat/plugin-system-sdd` (PR #237, stacked on and now rebased over the merged registry readiness
refactor) and the full gate is green (2721 tests, `ruff format --check`, `ruff`, `mypy` over
`agentworks/` + `tests/`, and the JS linters). A capstone verification pass signed off R1-R14 before
merge. The lock binds when this file lands on `main`; pre-merge edits on the branch remain ordinary
in-flight changes.

## What shipped

**System plugins**: in-repo, in-release units that bundle capability implementations (of the four
existing core-fixed kinds) and declarable resources (YAML manifests), strictly opt-in via
`[plugins] enabled = [...]`. The plugin work is the **first producer** of the registry's enablement
axis: a not-opted-in plugin's capability rows are **present-but-disabled**, so a resource that
references one is cleanly not-ready ("enable plugin `<name>`") rather than an unknown-name error. It
builds no bespoke publish-gate, disabled-roster dispatch, or reference-time diagnosis; the landed
registry-readiness-refactor already distributes and gates enablement, and this effort produces it.

The mechanism is proven end to end by a test fixture; the **shipped index is empty**
(`_INSTALLED_MODULES = ()`), so no real plugin ships. Migrating the first real plugin is a
follow-on.

Delivered across six phases (each twice-reviewed: an `agentworks-reviewer` pass and a fresh-eyes
generic pass, the load-bearing Phase 4 at top tier):

- **Phase 1** the `system-plugin` `Origin` variant (constructible, renderable; see the commit map
  below).
- **Phase 2** the plugin framework: the frozen `Plugin` descriptor, the atomic validating
  `register_plugin` (validate-whole -> precheck-and-prepare with zero mutation -> pure seat loop, so
  seating is all-or-nothing by construction), the per-kind `CapabilityAdapter` table (the
  class-vs-instance seat trap confined to `secret-backend`), the inverted `SYSTEM_PLUGINS` index,
  the `seated_plugin` test helper, and the `_check_collision` `system-plugin` matrix.
- **Phase 3** the `[plugins]` config section (a setting, both load paths, unknown keys a hard
  `ConfigError`).
- **Phase 4** the enablement producer (reason-carrying, composed over `EnablementSource`s via
  `compose_enablement`) plus R14 four-kind consumer gating. Additive against the landed fold: the
  scaffold `_node_enablement()` method is removed in favor of injected
  `finalize(enablement_sources=...)`; `build_graph`/`_Node`/`build_context` untouched.
- **Phase 5** `publish_plugins` + `build_registry` wiring (unconditional capability rows,
  enabled-only manifests, up-front unknown-name error) and the shared typed-error manifest loader
  body (retiring the `-O`-strippable `assert`).
- **Phase 6** the surfaces (disabled-hides / not-ready-shows, `--include-disabled`, the origin
  filter/count, the bespoke doctor roster) and the docs promotion.

## Requirement coverage (R1-R14)

All satisfied and test-pinned (capstone-verified). Highlights:

- **R5/R13** capability rows publish **unconditionally**; enablement is an **overlay**, not a
  publish gate. Enablement is a first-class **multi-source** axis (`compose_enablement`,
  first-source-wins), so an operator-explicit-disable source slots in later with no re-shaping.
- **R7** two disjoint collision layers: a **capability** name-clash fires at **seating**
  (`register_plugin`), never reaching `_check_collision`; the `_check_collision` `system-plugin`
  matrix covers **declarable (manifest)** rows + operator-override, decided on the unordered variant
  pair for the two symmetric pairings, with the operator-override path kept **directional** (a
  plugin can never clobber an operator's declaration).
- **R9 + R14** a not-enabled plugin is present-but-disabled, and **all four capability kinds** honor
  enablement through their real consumer: `vm-platform` (vm-site propagates), `secret-backend`
  (excluded from resolution/validation), `git-credential-provider` (git-credential propagates +
  use-refusals), `harness` (session-template stays ready, `ensure_harness_enabled` gates at the two
  session-build call sites, pinned by a per-function AST drift guard).
- **R10** the reserved `required_scopes` / `commands` descriptor fields are present but inert.
- **R11** proven by the fixture; the shipped index is empty.
- **R12** a plugin is an **origin**, not a resource kind; the roster is a bespoke doctor surface.

## Known limitation, stated honestly (R9 manifests)

A not-opted-in plugin's **capabilities** are present-but-disabled (a reference gets the enable
hint), but its bundled **declarable** resources publish **enabled-only**. A declarable resource is
also referenceable by name (e.g. `extends = <plugin-template>`), so referencing a not-enabled
plugin's bundled resource yields the registry's unknown-name error rather than the enable hint, the
two sides are inconsistent for a plugin that ships referenceable bundled resources. This is **inert
in the initial structure** (empty index; no plugin ships bundled resources). The follow-on that
ships the first such plugin should move manifests to present-but-disabled with enablement-aware
collision, for symmetry with the capability side. Recorded in **ADR 0021** (Negative consequences)
and `plugins/README.md` so it survives this SDD's deletion.

## Where the load-bearing content lives (SDD is deletable)

Nothing operator- or contributor-facing depends on `docs/sdd/` (grep-confirmed, capstone-verified):

- **Authoring a plugin** (the descriptor, inverted index, atomicity, collision behavior, the
  enablement model, the four kinds + manifests, the reserved fields) is in
  `cli/agentworks/plugins/README.md`.
- **The operator model** (the `system-plugin` origin, opt-in via `[plugins]`, disabled-hides /
  not-ready-shows, `--include-disabled`, the doctor roster) is in `docs/guides/resources.md`
  ("System plugins") and `cli/agentworks/sample-config.toml` (`[plugins]`).
- **The decisions** (plugin-as-origin; enablement as a first-class multi-source axis; the
  operator-explicit-disable door; the deliberate `[plugins]` strict-unknown-key stance vs the
  soft-warn convention) are in **ADR `0021-system-plugins.md`**.

## The door left open for the follow-on (R13, Future direction)

The enablement axis is **multi-source by construction**, so **operator-explicit disable** of
individual units (including parts of a third-party plugin an operator otherwise trusts) is a new
`EnablementSource` ordered ahead of or behind the plugin source at the `build_registry` assembly
point, with no re-shaping of the axis, the fold, or the consumers. Two display follow-ons are noted:
a built-in node an operator explicitly disables has no `system-plugin` origin to re-derive a reason
from (the reason lives on the transient `DisabledMark`, not the frozen node), so a "why disabled"
surface would recompute the sources or add an optional `_Node.disabled_by` field; and the R9
manifest symmetry above. External plugins, the broader trust model, and plugin-owned CLI commands
remain reserved (R10), not built.

## Relationship to the registry readiness refactor

This effort is the first `_node_enablement` producer that the refactor
(`docs/sdd/2026-07-27-registry-readiness-refactor/`) designed the seam for. The refactor's
`locked.md` carries a supersession addendum recording that the seam moved from an overridden
`_node_enablement()` method to injected `finalize(enablement_sources=...)`; that addendum matches
the shipped seam verbatim (confirmed at closeout: the method is removed and `build_registry` injects
`finalize(enablement_sources=[plugin_enablement_source(config)])`). The refactor's four
`test_readiness_fold.py` monkeypatch tests were migrated to the source-injection seam with identical
verdicts.

## Phase commit map (branch `feat/plugin-system-sdd`)

- Phase 1 (system-plugin origin): `499c57bb`.
- Phase 2 (plugin framework): `ac7ce675`.
- Phase 3 (`[plugins]` config): `00388c7b`.
- Phase 4 (enablement producer + R14 gating): `0efa2aa7`.
- Phase 5 (`build_registry` wiring): `098fe53c`.
- Phase 6 (surfaces + docs): `39400930`.

Each phase was reviewed by an `agentworks-reviewer` pass and a fresh-eyes generic pass (Phase 4 at
top tier); a capstone verification pass signed off R1-R14 and the deletable-SDD property before
merge.
