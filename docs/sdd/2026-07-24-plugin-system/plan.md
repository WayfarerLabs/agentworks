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
- [ ] Three LLDs authored and reviewed:
  - [ ] (a) [plugin framework](./plugin-framework-lld.md): the `Plugin` descriptor + validation, the
        inverted atomic `register_plugin`, the per-kind `CapabilityAdapter`, the installed index +
        seat/unseat helper, and the `_check_collision` `system-plugin` matrix.
  - [ ] (b) [enablement producer](./enablement-producer-lld.md): the `_node_enablement` composition
        over sources, the reason-carrying enablement, and the plugin source.
  - [ ] (c) [surfaces](./plugin-surfaces-lld.md): the origin variant + rendering, the
        `build_registry` publish step, the `[plugins]` loader, the disabled-hides rule, and the
        doctor roster.
- [ ] Plan + LLDs pass `agentworks-reviewer` + a fresh-eyes pass.

## Phase 1: the `system-plugin` origin (R1)

Governs: R1. LLD: (c). Pure vocabulary; mergeable alone.

- [ ] Add `"system-plugin"` to `Origin.variant`, a `plugin: str | None` field, and
      `Origin.system_plugin(*, plugin, source)` (`file`/`line` `None`). Extend the variant-contract
      docstring; `external-plugin` stays documented-only.
- [ ] Rendering: `resources/render.py` + the doctor/list/describe surfaces render
      `system-plugin <plugin> (<source>)`.
- [ ] Tests: origin construction + rendering; `external-plugin` still not constructible.

**DoD:** DoD-green; the origin is constructible and renders; nothing yet produces one.

## Phase 2: the plugin framework, provable in isolation (R2, R3, R5-registration, R6, R7)

Governs: R2, R3, R6, R7, R5's registration half. LLD: (a). No `build_registry` wiring yet, so no
real config changes behavior; the fixture proves the framework.

- [ ] `Plugin` descriptor (`plugins/base.py`): frozen; `name`, `description`, impls-by-kind,
      `manifests`, reserved `required_scopes: tuple[ScopeLevel, ...]` and a real `commands`
      placeholder frame; `__post_init__` normalizes the capabilities mapping to immutable.
- [ ] `register_plugin` (`plugins/registration.py`): validate the whole descriptor first (name
      non-empty + `/`-free; every kind has an adapter; every impl a class with a non-empty +
      `/`-free `name`; no intra-descriptor collisions), THEN seat every impl atomically
      (all-or-nothing); idempotent per impl name; typed error on a cross-plugin impl-name collision.
      Export a seat/unseat snapshot context manager for tests.
- [ ] The per-kind `CapabilityAdapter` + `CAPABILITY_ADAPTERS`: seat (class vs instance) + build-row
      (`VMPlatformEntry`/etc. with a supplied origin); a row is built only for an actually-seated
      impl.
- [ ] Installed index (`plugins/__init__.py`): `SYSTEM_PLUGINS`, populated by the index importing
      each shipped module and calling `register_plugin(module.PLUGIN)` itself (inverted control);
      typed error on a duplicate plugin name; ships empty.
- [ ] `_check_collision` `system-plugin` matrix (R7): decide by the unordered variant pair
      (operator-overrides-where-`builtin_override`-permits; `system-plugin`/`built-in` peers;
      plugin/plugin collide), each with its own message; existing pairings untouched.
- [ ] Tests (fixture-driven): descriptor validation rejects missing-name / instance-not-class /
      unknown-kind / colliding-impl with a typed attributed error; atomic registration seats nothing
      on a mid-descriptor failure; the `CAPABILITY_ADAPTERS.keys()` == capability-kinds guard; the
      R7 matrix per pairing; the seat/unseat helper round-trips.

**DoD:** DoD-green; the framework registers, validates, and adapts across all four kinds; the
collision matrix holds; the shipped index is empty; nothing publishes yet.

## Phase 3: config `[plugins]` (R4, R8)

Governs: R4. LLD: (c).

- [ ] `Config.plugins_enabled: tuple[str, ...]` (empty when absent); a loader parses
      `[plugins] enabled = [...]`; unknown keys in the section are a config error; present on both
      load paths; never a Registry resource.
- [ ] Tests: parse present/absent/unknown-key; the value reaches `Config` on both paths.

**DoD:** DoD-green; enablement is a config setting; nothing consumes it yet.

## Phase 4: the `_node_enablement` producer (R9, R13), the load-bearing phase

Governs: R9 (capability side), R13. LLD: (b). This is where a not-opted-in plugin's contributions
first become present-but-disabled, exercised via a fixture whose rows are published by a test (Phase
5 wires it into `build_registry`).

- [ ] Reason-carrying enablement: extend the refactor's `Enablement`/disabled state with an optional
      **reason** + **source identity** (additive; the fold/gate/consumers are untouched except where
      a dependent reads the hint). The vm-site's "enable its unit" hint reads the carried reason, so
      it renders "enable plugin `<name>`".
- [ ] Enablement becomes a **composition over sources**:
      `(rows) -> Mapping[(kind,name),     DisabledMark]` per source; a node is disabled if any
      source disables it (first-source-wins the reason, deterministic per the LLD); all-enabled when
      no source fires. **Layering:** the `Registry` stays config-agnostic; `build_registry`
      constructs the sources bound to config and injects them at finalize (a defaulted
      `enablement_sources` input replacing the no-arg `_node_enablement()`; the refactor's 4
      monkeypatch tests migrate to a stub source).
- [ ] The **plugin source**: a `system-plugin`-origin row whose `plugin` is not in
      `config.plugins_enabled` is disabled with the remediation reason "enable plugin `<name>`" (the
      doctor roster, not this mark, renders the "not enabled in [plugins]" state). Reads frozen row
      origins + the bound enabled set; no new probe.
- [ ] Tests: a not-opted-in plugin capability node reads `enablement_of == disabled` with the plugin
      reason; a `vm-site` referencing it is not-ready with "enable plugin `<name>`" (via the
      existing fold, not new code); materialization withholds its deps and resolution/validation
      exclude a disabled plugin backend (the refactor's consumers, pinned under this producer); a
      **stub second source** composes (proving the R13 seam) without a real operator surface.

**DoD:** DoD-green; DoD-behavior for R9-capability + R13-seam; enablement is multi-source and
reason-carrying; the plugin source marks not-opted-in plugin contributions disabled.

## Phase 5: `build_registry` wiring, publication joins enablement (R5-publication, R9-manifests)

Governs: R5's publication half, R9's manifest gating. LLD: (c). First phase where a real `[plugins]`
config changes behavior.

- [ ] `plugins.publish_plugins(registry, config)`: publish a capability row for **every shipped
      plugin's** impl unconditionally (`system-plugin` origin); load **enabled plugins'** manifests
      via the shared loader body (which now raises a typed error instead of
      `assert not manifests.issues`). Resolve enabled names up front; an unknown name is a single
      typed config error before any publish (not a `KeyError`, not in the post-finalize block).
- [ ] Wire `publish_plugins` into `build_registry` between the built-in capability rows and
      `config.publish_to`; publication-only, so `build_registry` stays pure.
- [ ] Tests (DoD-behavior): enabled plugin, capability row + manifest resource present, enabled, and
      consumable at their site with `system-plugin` origin; not-enabled plugin, capability row
      **present-but-disabled** (a reference is not-ready with the enable hint, NOT unknown-name),
      its manifest resources absent; unknown enabled name, typed config error precedes any publish;
      `build_registry` purity preserved (no module state mutated).

**DoD:** DoD-green; DoD-behavior for R5, R9-manifests; publication is unconditional and pure;
enablement gates manifests and (via Phase 4) overlays capabilities.

## Phase 6: surfaces + docs (R9-presentation, R10, R11)

Governs: R9 (disabled-hides + roster), R10, R11. LLD: (c).

- [ ] **Disabled hides, not-ready shows**: `resource list` / `describe` hide `disabled` rows by
      default (reading `enablement_of`) while still showing `not-ready` rows; an
      `--include-disabled` opt-in reveals them; provenance annotates "from plugin `<name>`" off the
      `system-plugin` origin.
- [ ] **Doctor plugin roster**: iterate `SYSTEM_PLUGINS` vs `config.plugins_enabled`;
      `plugin <name>: <description>` tagged enabled or `disabled (not enabled in [plugins])`; roster
      only; reserved `required_scopes` render informationally (R10); a bespoke doctor surface, not a
      `KIND_REGISTRY` hook (R12).
- [ ] **Docs (DoD-docs)**: `agentworks/plugins/README.md` (authoring a system plugin, the
      descriptor, the enablement model), `sample-config.toml` (`[plugins]`), and an ADR recording
      the decision (plugin-as-origin, enablement-as-first-class-multi-source-axis, the door for
      operator-explicit disable). Regenerate completions if any CLI surface changed
      (`--include-disabled`).
- [ ] Tests (DoD-behavior, R11): the full fixture end-to-end (descriptor to index to registration to
      unconditional publication to enablement-overlay to consumption + hidden-when-disabled +
      roster); the shipped index is empty; the reserved fields are inert.

**DoD:** DoD-green; the operator surfaces are correct (disabled hidden, roster shown); docs + ADR
land; the fixture proves the whole path; no demo plugin ships.

## Closeout

- [ ] All FRD requirements satisfied and test-pinned; the registry consumers verified under a real
      disabled producer.
- [ ] Load-bearing content promoted to permanent homes (`plugins/README.md`, the ADR); nothing
      operator/contributor-facing depends on `docs/sdd/`.
- [ ] `locked.md` written; the door for operator-explicit disable (R13) recorded for the follow-on.
