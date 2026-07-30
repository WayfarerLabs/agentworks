# Locked: system plugins (framework + first migration)

**Locked 2026-07-30.** The SDD is complete. It shipped in two parts on `feat/plugin-system-sdd`
(pull request 237): the plugin **framework** (Phases 1-6, the initial structure) and, after the SDD
was reopened to make the migration the true test of the model, the **manifest parity** (Phase 7) and
the **migration of four world-specific bundles out of the core** (Phases 8-11). A capstone
verification pass signed off R1-R14 + R11.1 before merge; the full gate is green (2798 tests,
`ruff format --check`, `ruff`, `mypy` over `agentworks/` + `tests/`, the JS linters). The lock binds
when this file lands on `main`; pre-merge edits on the branch remain ordinary in-flight changes.

## What shipped

**System plugins**: in-repo, in-release units that bundle capability implementations (of the four
existing core-fixed kinds) and declarable resources (YAML manifests), strictly opt-in via
`[plugins] enabled = [...]`. The plugin work is the **first producer** of the registry's enablement
axis: a not-opted-in plugin's contributions are **present-but-disabled**, so a reference to one is
cleanly not-ready (a capability) or refused-at-use (a bundled declarable) with an "enable plugin
`<name>`" hint, never an unknown-name error.

**The model is proven by migrating real functionality, not just a fixture.** Four world-specific
bundles moved out of the core into shipped plugins, exercising all four capability kinds and the
bundled-manifest path against their real consumers:

- **onepassword** (secret-backend) `onepassword`.
- **claude** (harness `claude-code` + the `claude` install-command).
- **proxmox** (vm-platform).
- **azure** (vm-platform `azure-vm` + git-credential `azdo` + the `az-cli` install-command), one
  plugin, three contributions.

The core keeps only the universal path: `lima`/`wsl2` platforms and their built-in sites, the
`shell` default harness, `env-var`/`prompt` secret backends, the `github` git-credential provider,
and generic dev-tool install-commands.

## Requirement coverage (R1-R14 + R11.1)

All satisfied and test-pinned (capstone-verified). Highlights:

- **R5/R13** capabilities AND bundled manifests publish **unconditionally**; enablement is an
  **overlay**, a first-class **multi-source** axis (`compose_enablement`, first-source-wins), so an
  operator-explicit-disable source slots in later with no re-shaping.
- **R6** a plugin may bundle a manifest resource only of a declarable kind whose consumption gate
  exists (`PLUGIN_MANIFEST_KINDS` allowlist) and not a reserved auto-declared name; the opt-in
  guarantee holds by construction.
- **R7** two disjoint collision layers (capability clashes at seating; declarable/operator at
  `_check_collision`). Manifest parity added enablement-aware collision: a not-opted-in plugin's
  declarable rows publish **weak** (add-if-absent, silently yielding), and `_check_collision`
  returns a `_CollisionDecision` (`OVERWRITE`/`KEEP_EXISTING`/raise) so an operator's legacy TOML
  row wins without error in either publish order. A finalize guard pins weak-implies-disabled.
- **R9** present-but-disabled parity for BOTH capabilities (not-ready fold verdict) and bundled
  declarables (a use-gate, `ensure_reference_enabled`/`ensure_recipe_enabled`, since a declarable
  has no `not_ready` consumer). Disabled hides from the default list; describe shows it; the doctor
  roster lists plugins.
- **R11** proven by the fixture AND the four real migrations; the migration pattern is uniform (impl
  `git mv` into the plugin package; the core `publish_to` skips the `plugin_seated_names(kind)` so a
  migrated impl publishes once with a `system-plugin` origin, applied to all four kinds).
- **R11.1** the guided opt-in **breaking change**: a config using a migrated bundle now needs the
  matching `[plugins] enabled` entry, or the resource is not-ready / refused-at-use with the enable
  hint (never a silent failure). The default local path is unaffected.
- **R14** all four capability kinds gated through their real consumer: vm-platform (vm-site
  propagates), secret-backend (excluded at resolution + the enable-plugin hint on the failure path),
  git-credential-provider (git-credential propagates + use-refusal), harness (session-template stays
  ready, `ensure_harness_enabled` gates at use).

## Where the load-bearing content lives (SDD is deletable)

Nothing operator- or contributor-facing depends on `docs/sdd/` (grep-confirmed, capstone-verified):

- **Authoring a plugin** (descriptor, inverted index, atomicity, adapters, the enablement model, the
  bundleable-kind allowlist): `cli/agentworks/plugins/README.md`.
- **The operator model + the upgrade note** (the `system-plugin` origin, opt-in via `[plugins]`,
  disabled-hides / not-ready-shows, `--include-disabled`, the doctor roster, and "Azure/Proxmox/
  1Password/Claude Code are now opt-in, add `[plugins] enabled`"): `docs/guides/resources.md`,
  `cli/README.md` ("System Plugins"), `cli/agentworks/sample-config.toml` (`[plugins]`),
  `docs/guides/proxmox.md`.
- **The decisions** (plugin-as-origin; enablement as a first-class multi-source axis; the
  operator-explicit-disable door; the weak-row / `_CollisionDecision` policy; the deliberate
  `[plugins]` strict-unknown-key stance; the migration + breaking change): ADR
  `0021-system-plugins.md`.

## The breaking change (release note)

The migration is a **guided opt-in breaking change**: `azure-vm`, `azdo`, `proxmox`, `onepassword`,
`claude-code`, and the `az-cli` / `claude` install-commands are disabled by default. An existing
operator adds the matching plugin(s) to `[plugins] enabled` to restore them; the "enable plugin
`<name>`" hint names the exact fix. The release carries a `BREAKING CHANGE` changelog entry
(release-please, `cli/CHANGELOG.md`). The default local path (`lima`/`wsl2` + `shell` +
`env-var`/`prompt` + `github`) is unaffected.

## The door left open for the follow-on (R13, Future direction)

The enablement axis is **multi-source by construction**, so **operator-explicit disable** of
individual units (including parts of a third-party plugin) is a new `EnablementSource` ordered at
the `build_registry` assembly point, with no re-shaping of the axis, the fold, or the consumers. A
display follow-on is noted (an operator-disabled built-in has no `system-plugin` origin to re-derive
its reason from). External plugins, the broader trust model, plugin-owned CLI commands, and a
feature-capability kind (which the `claude_marketplaces`/`claude_plugins` core-retained machinery
would move under) remain reserved (R10), not built.

## Relationship to the registry readiness refactor

This effort is the first `_node_enablement` producer the refactor
(`docs/sdd/2026-07-27-registry-readiness-refactor/`) designed the seam for. The refactor's
`locked.md` carries a supersession addendum recording that the seam moved from an overridden
`_node_enablement()` method to injected `finalize(enablement_sources=...)`; that matches the shipped
seam. The refactor's `test_readiness_fold.py` monkeypatch tests were migrated to the
source-injection seam.

## Phase commit map (branch `feat/plugin-system-sdd`)

Framework (initial structure): P1 `499c57bb` (origin), P2 `ac7ce675` (framework), P3 `00388c7b`
(`[plugins]` config), P4 `0efa2aa7` (enablement producer + R14 gating), P5 `098fe53c`
(`build_registry` wiring), P6 `39400930` (surfaces + docs).

Migration: reopened `521de8e6`; design `5c621ffb` .. `ca60d89f`; P7 `db1af7e5` (manifest parity); P8
`b8ebaf84` (onepassword); P9 `5ba96155` (claude); P10 `dc1bea34` (proxmox); P11 `0e938401` (azure);
closeout at re-lock.

Every phase was reviewed by an `agentworks-reviewer` pass and a fresh-eyes generic pass (the
load-bearing Phase 4 and the manifest-parity design at top tier); two capstone verification passes
(framework, then the whole effort) signed off before merge.
