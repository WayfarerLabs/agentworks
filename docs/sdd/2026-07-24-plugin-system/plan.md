# Plan: system plugins (initial structure)

Implements the [FRD](./frd.md) per the [HLA](./hla.md). Built on the landed registry readiness
refactor; the plugin work is the **first `_node_enablement` producer**, so most of the mechanism
(the enablement axis, the fold, the gating consumers) already exists and is not re-touched.

**Phasing principle: every phase ends green** (full gate: `ruff format --check`, `ruff`, `mypy` over
`agentworks/` and `tests/`, the JS linters via `./scripts/lint-files.sh`, the suite passes) and is a
clean, reviewable boundary. One PR (#237), rebased onto `feat/registry-readiness-refactor`. Phases 1
through 4 are inert additions (no real config's behavior changes); phase 5 (the `build_registry`
wiring) is where a real `[plugins]` config first changes behavior; phase 6 is surfaces + docs.

The plan is lead-owned; the LLDs and each phase's implementation are delegated to `agentworks-dev`
and reviewed by `agentworks-reviewer` (tier >= dev) plus a fresh-eyes pass.

## Definitions of done, global

- **DoD-green**: the full gate passes; no unjustified `# type: ignore`.
- **DoD-behavior**: the FRD requirements each get a test; the registry consumers' existing behavior
  under a disabled producer (fold hint, materialization gate, resolution/validation exclusion) is
  pinned against the fixture plugin, not re-implemented.
- **DoD-docs**: permanent docs/help made true by a phase land in that phase (SDD lockstep rule); the
  load-bearing content lives outside `docs/sdd/` (`agentworks/plugins/README.md`, an ADR).

## Phase 0: artifacts (this phase)

- [x] FRD rewritten against the landed registry model (present-but-disabled nodes;
      `_node_enablement` producer; generalized multi-source enablement, R13; Fable hardening folded
      in).
- [x] HLA rewritten (the enablement producer is the load-bearing component; publication is
      unconditional; disabled-hides/not-ready-shows).
- [x] Branch rebased onto `feat/registry-readiness-refactor` (keep current if the base moves).
- [x] Three LLDs authored and reviewed:
  - [x] (a) [plugin framework](./plugin-framework-lld.md): the `Plugin` descriptor + validation, the
        inverted atomic `register_plugin`, the per-kind `CapabilityAdapter`, the installed index +
        seat/unseat helper, and the `_check_collision` `system-plugin` matrix.
  - [x] (b) [enablement producer](./enablement-producer-lld.md): the `_node_enablement` composition
        over sources, the reason-carrying enablement, and the plugin source.
  - [x] (c) [surfaces](./plugin-surfaces-lld.md): the origin variant + rendering, the
        `build_registry` publish step, the `[plugins]` loader, the disabled-hides rule, and the
        doctor roster.
- [x] Plan + LLDs pass `agentworks-reviewer` + a fresh-eyes pass (both, plus focused delta
      re-reviews: the manifest-rationale and collision-layer Important findings, and the R14
      four-kind-gating Blocking, all closed clean).

## Phase 1: the `system-plugin` origin (R1)

Governs: R1. LLD: (c). Pure vocabulary; mergeable alone.

- [x] Add `"system-plugin"` to `Origin.variant`, a `plugin: str | None` field, and
      `Origin.system_plugin(*, plugin, source)` (`file`/`line` `None`). Extend the variant-contract
      docstring; `external-plugin` stays documented-only.
- [x] Rendering: `resources/render.py` + the doctor/list/describe surfaces render
      `system-plugin <plugin> (<source>)`.
- [x] Tests: origin construction + rendering; `external-plugin` still not constructible.

**DoD:** DoD-green; the origin is constructible and renders; nothing yet produces one.

## Phase 2: the plugin framework, provable in isolation (R2, R3, R5-registration, R6, R7)

Governs: R2, R3, R6, R7, R5's registration half. LLD: (a). No `build_registry` wiring yet, so no
real config changes behavior; the fixture proves the framework.

- [x] `Plugin` descriptor (`plugins/base.py`): frozen; `name`, `description`, impls-by-kind,
      `manifests`, reserved `required_scopes: tuple[ScopeLevel, ...]` and a real `commands`
      placeholder frame; `__post_init__` normalizes the capabilities mapping to immutable.
- [x] `register_plugin` (`plugins/registration.py`): validate the whole descriptor first (name
      non-empty + `/`-free; every kind has an adapter; every impl a class with a non-empty +
      `/`-free `name`; no intra-descriptor collisions), THEN seat every impl atomically
      (all-or-nothing); idempotent per impl name; typed error on a cross-plugin impl-name collision.
      Export a seat/unseat snapshot context manager for tests.
- [x] The per-kind `CapabilityAdapter` + `CAPABILITY_ADAPTERS`: seat (class vs instance) + build-row
      (`VMPlatformEntry`/etc. with a supplied origin); a row is built only for an actually-seated
      impl.
- [x] Installed index (`plugins/__init__.py`): `SYSTEM_PLUGINS`, populated by the index importing
      each shipped module and calling `register_plugin(module.PLUGIN)` itself (inverted control);
      typed error on a duplicate plugin name; ships empty.
- [x] Two collision layers, reconciled (R7): a **capability** name-clash (built-in/plugin,
      plugin/plugin) is caught at **seating** in `register_plugin` (the impl name is the registry
      key), with a message naming the occupant's real origin (core vs plugin); `_check_collision`'s
      `system-plugin` matrix is for **declarable (manifest) rows** and operator-override. Extend
      `_check_collision` by the unordered variant pair, applying the unordered normalization only to
      system-plugin-involving pairs (the built-in/operator directional asymmetry is preserved
      verbatim); each pairing its own message; existing pairings untouched.
- [x] Tests (fixture-driven): descriptor validation rejects missing-name / instance-not-class /
      unknown-kind / colliding-impl with a typed attributed error; atomic registration seats nothing
      on a mid-descriptor failure; the `CAPABILITY_ADAPTERS.keys()` == capability-kinds guard; the
      collision cases target the **layer that actually fires** (capability clash, the seating guard;
      declarable/operator clash, `_check_collision`); the seat/unseat helper round-trips.

**DoD:** DoD-green; the framework registers, validates, and adapts across all four kinds; the
collision matrix holds; the shipped index is empty; nothing publishes yet.

## Phase 3: config `[plugins]` (R4, R8)

Governs: R4. LLD: (c).

- [x] `Config.plugins_enabled: tuple[str, ...]` (empty when absent); a loader parses
      `[plugins] enabled = [...]`; unknown keys in the section are a config error; present on both
      load paths; never a Registry resource.
- [x] Tests: parse present/absent/unknown-key; the value reaches `Config` on both paths.

**DoD:** DoD-green; enablement is a config setting; nothing consumes it yet.

## Phase 4: the `_node_enablement` producer + consumer gating (R9, R13, R14), the load-bearing phase

Governs: R9 (capability side), R13, R14. LLD: (b). This is where a not-opted-in plugin's
contributions first become present-but-disabled, exercised via a fixture whose rows are published by
a test (Phase 5 wires it into `build_registry`), and where the two un-wired kinds' consumers start
honoring enablement.

- [x] Reason-carrying enablement: extend the refactor's `Enablement`/disabled state with an optional
      **reason** + **source identity** (additive; the fold/gate/consumers are untouched except where
      a dependent reads the hint). The vm-site's "enable its unit" hint reads the carried reason, so
      it renders "enable plugin `<name>`".
- [x] Enablement becomes a **composition over sources**:
      `(rows) -> Mapping[(kind,name), DisabledMark]` per source; a node is disabled if any source
      disables it (first-source-wins the reason, deterministic per the LLD); all-enabled when no
      source fires. **Layering:** the `Registry` stays config-agnostic; `build_registry` constructs
      the sources bound to config and injects them at finalize (a defaulted `enablement_sources`
      input replacing the no-arg `_node_enablement()`; the refactor's 4 monkeypatch tests migrate to
      a stub source).
- [x] The **plugin source**: a `system-plugin`-origin row whose `plugin` is not in
      `config.plugins_enabled` is disabled with the remediation reason "enable plugin `<name>`" (the
      doctor roster, not this mark, renders the "not enabled in [plugins]" state). Reads frozen row
      origins + the bound enabled set; no new probe.
- [x] **Close the consumer-gating gap for the two un-wired kinds (R14), per self-determined
      readiness:** `git-credential` gains a `not_ready(deps)` hook that propagates its single
      provider's disabled state (mirroring `vm-site`) plus a use-time refusal at the provider
      resolution sites (`vms/initializer/credentials.py`, `git_credentials/__init__.py`);
      `session-template` stays ready but a shared `ensure_harness_enabled(registry, name)` at the
      two session-build call sites (`_create_build.py`, `_lifecycle.py`, which hold the registry,
      since `harness_for` itself threads none) reads `enablement_of("harness", name)` and raises a
      typed "enable plugin `<name>`" error when the harness is disabled (the secret model; the
      read-only display path stays ungated). Both are additive against the already-produced
      enablement; neither touches the fold or the producer. Because the harness gate sits at the
      call sites (not the factory), add a **drift guard** so a future third caller of
      `pending_session_node` / `live_session_node` cannot silently bypass it: a comment on both
      factories pointing at `ensure_harness_enabled`, plus a test asserting those factories' only
      callers gate (analogous to the `CAPABILITY_ADAPTERS.keys()` adapter-drift guard).
- [x] Tests: a not-opted-in plugin capability node reads `enablement_of == disabled` with the plugin
      reason; **each of the four kinds is proven through its actual consumer**, a `vm-site` is
      not-ready with "enable plugin `<name>`" (existing fold); a disabled plugin backend is excluded
      from resolution/validation; a `git-credential` on a disabled plugin provider is not-ready and
      refused at use; a `session-template` on a disabled plugin harness lists ready but raises the
      typed enable-plugin error at harness construction; materialization withholds a disabled node's
      deps; a **stub second source** composes (proving the R13 seam) without a real operator
      surface.

**DoD:** DoD-green; DoD-behavior for R9-capability + R13-seam + R14 (all four kinds gated through
their real consumer); enablement is multi-source and reason-carrying; the plugin source marks
not-opted-in plugin contributions disabled.

## Phase 5: `build_registry` wiring, publication joins enablement (R5-publication, R9-manifests)

Governs: R5's publication half, R9's manifest gating. LLD: (c). First phase where a real `[plugins]`
config changes behavior.

- [x] `plugins.publish_plugins(registry, config)`: publish a capability row for **every shipped
      plugin's** impl unconditionally (`system-plugin` origin); load **enabled plugins'** manifests
      via the shared loader body (which now raises a typed error instead of
      `assert not manifests.issues`). Resolve enabled names up front; an unknown name is a single
      typed config error before any publish (not a `KeyError`, not in the post-finalize block).
- [x] Wire `publish_plugins` into `build_registry` between the built-in capability rows and
      `config.publish_to`; publication-only, so `build_registry` stays pure.
- [x] Tests (DoD-behavior): enabled plugin, capability row + manifest resource present, enabled, and
      consumable at their site with `system-plugin` origin; not-enabled plugin, capability row
      **present-but-disabled** (a reference is not-ready with the enable hint, NOT unknown-name),
      its manifest resources absent; unknown enabled name, typed config error precedes any publish;
      `build_registry` purity preserved (no module state mutated).

**DoD:** DoD-green; DoD-behavior for R5, R9-manifests; publication is unconditional and pure;
enablement gates manifests and (via Phase 4) overlays capabilities.

## Phase 6: surfaces + docs (R9-presentation, R10, R11)

Governs: R9 (disabled-hides + roster), R10, R11. LLD: (c).

- [x] **Disabled hides, not-ready shows**: `resource list` / `describe` hide `disabled` rows by
      default (reading `enablement_of`) while still showing `not-ready` rows; an
      `--include-disabled` opt-in reveals them; provenance annotates "from plugin `<name>`" off the
      `system-plugin` origin.
- [x] **Doctor plugin roster**: iterate `SYSTEM_PLUGINS` vs `config.plugins_enabled`;
      `plugin <name>: <description>` tagged enabled or `disabled (not enabled in [plugins])`; roster
      only; reserved `required_scopes` render informationally (R10); a bespoke doctor surface, not a
      `KIND_REGISTRY` hook (R12).
- [x] **Docs (DoD-docs)**: `agentworks/plugins/README.md` (authoring a system plugin, the
      descriptor, the enablement model), `sample-config.toml` (`[plugins]`),
      `docs/guides/resources.md` (the "three origins" operator-facing sentence becomes four:
      document the `system-plugin` origin, which is first observable once Phase 5 publishes plugin
      rows and Phase 6 presents them), and an ADR recording the decisions (plugin-as-origin,
      enablement-as-first-class-multi-source-axis, the door for operator-explicit disable, and the
      deliberate `[plugins]` strict-unknown-key stance vs the soft-warn convention, so future
      config-section authors know which precedent to follow). Regenerate completions if any CLI
      surface changed (`--include-disabled`).
- [x] Tests (DoD-behavior, R11): the full fixture end-to-end (descriptor to index to registration to
      unconditional publication to enablement-overlay to consumption + hidden-when-disabled +
      roster); the shipped index is empty; the reserved fields are inert.

**DoD:** DoD-green; the operator surfaces are correct (disabled hidden, roster shown); docs + ADR
land; the fixture proves the whole path; no demo plugin ships.

## Closeout

- [x] All FRD requirements satisfied and test-pinned; the registry consumers verified under a real
      disabled producer. (Capstone verification pass: R1-R14 each satisfied with a named pinning
      test, all four kinds gated through their real consumer, additive-ness intact.)
- [x] Load-bearing content promoted to permanent homes (`plugins/README.md`, the ADR); nothing
      operator/contributor-facing depends on `docs/sdd/`. (The R9 manifest known-limitation +
      follow-on, previously only in the FRD, is now in ADR 0021 and the README per the capstone.)
- [x] `locked.md` written; the door for operator-explicit disable (R13) recorded for the follow-on.
- [x] The registry refactor's `locked.md` seam note reconciled (supersession addendum recorded:
      `_node_enablement()` -> injected `finalize(enablement_sources=...)`), per the PR design
      review.
- [x] When Phase 4 removes `_node_enablement()`, confirm the registry `locked.md` supersession note
      (added at design time) still matches the shipped seam. (Confirmed: the method is removed and
      `build_registry` injects `finalize(enablement_sources=[plugin_enablement_source(config)])`,
      matching the addendum verbatim.)

> **Note (2026-07-30): the framework closeout above completed and `locked.md` was written
> (`07abe235`), but the SDD was then REOPENED (owner: Scot) to make the migration the true test of
> the model. `locked.md` was removed (an active SDD has no lockfile); the boxes above stay checked
> as the immutable record of the framework's completion. The scope-expansion phases below (7-11 + a
> new closeout) are the reopened work, in the same PR (#237).**

## Scope expansion (2026-07-30): migrate real plugins + manifest parity

Per operator direction, the empty-index framework is not the end state: **azure**, **claude**,
**proxmox**, and **1password** move out of the core into shipped system plugins, the ultimate test
of the model across all four capability kinds and the bundled-manifest path (R11, R11.1). Because
these ship name-referenced bundled manifests (`az-cli`, `claude` install-commands), the R9 manifest
asymmetry an earlier draft deferred becomes operator-reachable and is **closed here** as
present-but-disabled parity. Same phasing principle (every phase ends green), same one PR (#237),
same delegated-dev + two-review cadence. The new LLD work (manifest parity + the migration
mechanics) and the `migration-strategy.md` are authored before implementation and reviewed.

### Phase 7: manifest present-but-disabled parity (R9 resolution). LLD: (b) + (c) extension

Governs: R9 (manifest side). Makes bundled declarable resources behave like capabilities under
enablement, so a reference to a not-enabled plugin's install-command / template surfaces the enable
guidance (a use-refusal), not an unknown-name error. No plugin migrates yet; proven by the fixture
(given a bundled manifest) + a stub.

- [x] `publish_plugins` publishes bundled manifests **unconditionally** (drop the enabled-only
      gate), stamped `system-plugin` origin, so the existing `plugin_enablement_source` disables a
      not-opted-in plugin's manifest rows by the same overlay it uses for capability rows (no new
      gate, no per-manifest enablement logic).
- [x] **Enablement-aware collision via "weak" rows** (LLD c 3b; a `finalize`-time `enablement_of`
      check is impossible since collision runs at `add()` before enablement is composed): a
      not-enabled plugin's manifest rows are added `weak` (add-if-absent, silently overwritable,
      never error), so a disabled plugin declarable row never blocks an operator/built-in/enabled
      name in ANY publish order (including the deprecated operator TOML install-command / apt
      publishers that run BEFORE `publish_plugins`). A `finalize` guard pins weak-implies-disabled.
      Two enabled system-plugin rows on one name still collide (curation bug, caught by an
      enable-every-plugin CI fixture). **Also fix the enabled-strong direction** (review finding):
      an ENABLED plugin manifest row colliding with a pre-existing operator TOML row on an
      `builtin_override == "allow"` kind must let the operator win (not error), symmetric with the
      built-in-over-operator direction, so an operator's legacy `[system_install_commands] az-cli`
      does not break when they enable azure.
- [x] **Reject bundling a reserved/auto-declared name** (review finding): plugin manifest
      publication rejects a bundled resource whose name is in the kind's `auto_declare_names` /
      reserved set (the template kinds auto-declare `default`), so a plugin cannot shadow or gate a
      reserved default.
- [x] **Reference-to-disabled-declarable use-gate**: a reference to a present-but-disabled
      declarable resource (an agent template's `user_install_commands` naming a disabled plugin's
      install-command; a `vm-template` `inherits` naming a disabled plugin's template) is **gated at
      use** with the enable guidance (a typed error at the mutation/consumption entry + a `describe`
      annotation), not unknown-name and not silent use. The R14 use-gate model generalized to
      declarable references, NOT a fold not-ready verdict (per LLD (b): a fold-edge would suppress
      the referrer's own secret materialization and validation). Gate sites pinned in LLD (b) MUST
      cover EVERY consumption path including `session create --new-agent` (the ephemeral-agent
      realize path via `agents/realize.py`, which the first design missed); the runner caller-drift
      guard must walk to the real command entry points (a shallow immediate-caller check misses the
      multi-hop chain). The bundleable-kind allowlist (R6) guarantees every bundled kind has such a
      gate.
- [x] Tests: a fixture plugin shipping a bundled manifest, not enabled, its manifest resource is
      present-but-disabled, hidden from `list`, shown by `describe`, and a reference to it is
      refused at use with "enable plugin `<name>`" (via EVERY gate site including the session
      new-agent path); enabling the plugin makes it consumable; an operator resource (YAML AND
      legacy TOML) with the same name as a disabled plugin's manifest resource wins with no
      collision error; an enabled plugin row vs a legacy operator TOML row on an allow-kind lets the
      operator win in BOTH encounter orders; a bundled reserved/`default` name is rejected; two
      enabled plugins colliding still error.

**DoD:** DoD-green; DoD-behavior for R9 manifest parity; the capability/manifest asymmetry is
closed; no migration yet.

### Phases 8-11: migrate the four bundles. Migration LLD + `migration-strategy.md`

Governs: R11, R11.1. Each phase moves one bundle's impl(s) (and, for claude and azure, its manifest)
out of the core into a shipped plugin package `agentworks/plugins/<name>/` (the package
`__init__.py` carries `PLUGIN`; impl submodules move in via git rename to preserve history), appends
the package to `_INSTALLED_MODULES`, drops the impl from the core `*_REGISTRY` + `publish_to` +
`__all__`, flips its origin `built-in` -> `system-plugin`, makes it opt-in, and lands the lockstep
docs. Shared helpers stay in core (`vm_platform/base.py`, `bootstrap_script.py`, `cloud_init.py`
(shared by azure AND proxmox), the four capability `base.py`s, `secrets/base.py`); `harness_for` /
`ensure_harness_enabled` stay in core (they key by registry name, not the concrete class). Ordered
so the first migration is a clean capability-only case, then the manifest-carrying and test-heavy
ones. Each: the breaking-change (opt-in, guided enable hint) behavior pinned; every test / help /
sample-config / guide reference updated. The two manifest-carrying plugins (claude, azure) rely on
Phase 7.

- [x] **Phase 8: 1password** (`onepassword` secret-backend; no manifest). The clean capability-only
      migration proving the mechanics: the impl moves, core `SECRET_BACKEND_REGISTRY` drops it, the
      plugin seats it (the instance-seated kind, exercising the adapter `prepare` instance path),
      origin `system-plugin`; a `secret` mapping `onepassword` is excluded from resolution until
      enabled; the resolver gates on the published row before the seated impl. (Phase 8 also added
      the secret-backend enable-plugin hint, LLD b, and the one-line `plugin_seated_names` skip in
      the core `secret-backend` `publish_to` so the plugin-seated impl is not double-published; see
      migration-strategy section 2.)
- [x] **Phase 9: claude** (`claude-code` harness + the `claude` install-command manifest; needs
      Phase 7). First manifest-carrying migration, small (~6 harness test files + one manifest
      entry): the harness impl moves, core `HARNESS_REGISTRY` drops it, origin `system-plugin`;
      `shell` stays the default harness so the common path is unaffected; a `session-template` with
      `harness = "claude-code"` stays ready but `ensure_harness_enabled` refuses it until enabled;
      the `claude` install-command becomes a bundled manifest, present-but-disabled (Phase 7), so a
      template referencing it while claude is not enabled is gated with the enable hint, not
      unknown-name.
- [x] **Phase 10: proxmox** (`proxmox` vm-platform + its `proxmox_api.py` sibling; no manifest). The
      **test-invasive** migration: proxmox is the shared orchestrated-test fixture platform (~40
      files). **Corrected during implementation** (an earlier framing said "repoint incidental
      fixtures to lima"): the shared orchestrated fixture (`make_config` in
      `tests/orchestrated_fixtures.py`) bakes in proxmox's config secret (`proxmox-token`), and ~17
      of the 24 orchestrated files assert on that secret-resolution boundary
      (`secret_union == ("proxmox-token",)`). No core platform (`lima`/`wsl2`) declares a config
      secret, so repointing to lima would delete the very coverage those tests exist for. Instead
      the shared fixture keeps `proxmox` but **explicitly enables the plugin**
      (`[plugins] enabled = ["proxmox"]` in `make_config`), which is honest (the fixture opts in
      exactly as a real proxmox operator would) and preserves the secret-boundary coverage;
      proxmox-specific tests enable the plugin too; the genuinely secret-free incidental tests may
      stay (harmlessly enabled) or move to lima. A `vm-site` on `proxmox` is not-ready with the
      enable hint until enabled (pinned by dedicated opt-in tests). `db/migrations.py`'s frozen
      `ProxmoxPlatform` import is repointed to the plugin package.
- [x] **Phase 11: azure** (`azure-vm` platform + `azdo` git-credential + the `az-cli`
      install-command manifest; needs Phase 7). One plugin contributing **three kinds + a bundled
      manifest**, the fullest exercise (a multi-kind plugin validating the Phase 7 parity end-to-end
      alongside claude). `azdo` is part of the azure plugin, not a standalone plugin (matches prior
      art). The largest impl (~954 lines).

**DoD (each):** DoD-green; the bundle is gone from the core (registry + imports + `__all__`), lives
in its plugin package, is opt-in with the guided enable hint through its real consumer, and its
docs/sample-config/help are lockstep-updated.

### Migration closeout

- [ ] `migration-strategy.md` authored and kept accurate (current-state inventory, per-bundle
      before/after, the opt-in breaking change, the re-enable path).
- [ ] Operator-facing: `sample-config.toml` shows an example `[plugins] enabled`;
      `docs/guides/     resources.md` (or a migration note) documents the opt-in change and how to
      re-enable; the doctor roster lists the four shipped plugins; completions still correct.
- [ ] ADR 0021 updated: the R9 manifest limitation is now RESOLVED (present-but-disabled parity),
      not deferred; the migration + breaking change recorded.
- [ ] Capstone verification pass (R1-R14 + R11.1, all four migrated bundles gated through their real
      consumer, the default local path unaffected); then `locked.md` re-written.
