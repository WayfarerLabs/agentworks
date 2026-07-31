# 21. System Plugins as an Origin over a First-Class Enablement Axis

Date: 2026-07-30

## Status

Accepted. Builds on the capability / declarable-resource split of
[ADR 0016](0016-yaml-resource-manifests.md) and the registry readiness refactor that made node
enablement a produced axis. The authoring model and the operator-facing behavior are documented in
`cli/agentworks/plugins/README.md` and `docs/guides/resources.md`.

## Context

Agentworks ships capability implementations (VM platforms, harnesses, git-credential providers,
secret backends) and bundled resource manifests baked into the core. Some of these are separable:
they belong to one backend or vendor, not every install wants them loaded, and a later effort will
distribute them from outside the core tree. We needed a model for "a bundle of capability
implementations and manifests that ships with agentworks but is separable and opt-in" without
reopening the resource model.

Three questions had to be answered coherently:

1. **What is a plugin, in registry terms?** A new resource kind? A new capability? Something else?
2. **How does opt-in compose with the readiness model?** The registry already distinguishes
   readiness (can this run on this host) from presence. Where does "the operator has not turned this
   on" live, and how does a reference to a not-turned-on contribution behave?
3. **How strict is the `[plugins]` config section?** The codebase has a soft convention
   (`_warn_unexpected_keys`) where an unknown key in a config table accumulates a warning and loads
   on. Does `[plugins]` follow it?

## Decision

### A plugin is an origin, not a resource kind

A plugin does not introduce a resource kind and never publishes a new kind. It contributes
implementations of the four existing capability kinds and, optionally, declarable resources bundled
as YAML manifests. Its contributions carry a fourth `Origin` variant, **`system-plugin`**, alongside
`operator-declared`, `built-in`, and `auto-declared`. The plugin itself is not a resource: it is the
provenance stamped on the resources it contributes, and the surfaces attribute those resources back
to it (`from plugin <name>`).

This keeps the resource model closed (kinds are code-defined; plugins fill existing kinds, never add
kinds) while making a plugin's contributions fully first-class and inspectable. The doctor plugin
roster is therefore a **bespoke** surface, not a per-kind `KIND_REGISTRY` hook: a plugin is an
origin, so it has no kind to dispatch on.

### Enablement is a first-class, multi-source axis; contributions are present-but-disabled

Opt-in is modeled as **enablement**, a distinct axis from readiness, produced at registry finalize
by composing enablement sources. The plugin opt-in source is the first such producer: a
`system-plugin`-origin row whose plugin is not listed in `[plugins].system` is marked `disabled`.

The load-bearing choice is that **capability rows publish unconditionally**, opted in or not. A
not-opted-in plugin's rows are therefore **present-but-disabled**, not absent. That is what lets a
reference to one (an operator `vm-site` naming a not-enabled plugin's platform) resolve to
**not-ready with a remediation hint, "enable plugin `<name>`"**, rather than an unknown-name hard
error. An absent row could only produce the unhelpful unknown-name failure. **Bundled manifests
publish the same way**, unconditionally and present-but-disabled: a not-opted-in plugin's declarable
rows publish "weak" (add-if-absent, silently yielding to any operator or built-in name, so a
disabled plugin never blocks an operator's resource), and a reference to a disabled declarable
resource is refused at use with the same "enable plugin `<name>`" guidance (a use-gate, since a
declarable resource has no `not_ready` consumer). A plugin may bundle a manifest resource only of a
declarable kind whose consumption gate exists (an allowlist), so the opt-in guarantee holds by
construction. (An earlier iteration of this effort published manifests enabled-only and deferred
this parity; it was resolved within the same effort once the migration shipped real plugins with
name-referenced bundled manifests, at which point the capability/manifest asymmetry became
operator-reachable. See the migration notes below.)

The disabled-hides / not-ready-shows default surface rule follows from the axis distinction:
`agw resource list` hides `disabled` rows by default (off by opt-in) while still showing `not-ready`
rows (on but blocked on this host). `--include-disabled` reveals the former; `describe` always
renders a named row.

### Enablement is composed over sources, leaving the door open for operator-explicit disable

Enablement is deliberately built as a **composition over sources** (a node is disabled if any source
disables it), not a single hard-coded plugin check. v1 ships exactly one source (the plugin opt-in),
but the seam is real: a later effort can add an operator-explicit disable source (turning off a
specific built-in or plugin contribution by name) without touching the fold, the consumers, or the
binary axis they read. Recording this now fixes the shape so the follow-on is additive.

### `[plugins]` rejects unknown keys hard, diverging from the soft-warn convention

An unknown key in the `[plugins]` table is a hard `ConfigError`, **not** a collected warn-issue.
This is a deliberate departure from the `_warn_unexpected_keys` convention other settings tables
use. The rationale is specific to this section: `[plugins]` is an opt-in **gate**. A typo'd key
(`enabeld`, or a per-plugin key used a release too early) under the soft convention would load on
with a warning the operator may miss, silently leaving plugins un-enabled and capabilities
mysteriously absent. A gate must fail loudly. The loader carries an in-code comment pinning this so
a future contributor does not "consistency-fix" it back to soft-warn, and this ADR is the
project-level record of the stance: **a config section that gates behavior on its own correctness
should reject unknown keys hard; a section that merely configures should follow the soft-warn
convention.** Future config-section authors choose the precedent that matches their section's role.

Consistently, a `[plugins].system` entry that is not an installed plugin is a typed config error
raised up front, before anything publishes, never a `KeyError` from deep in publication.

**Degradation contract.** A `[plugins]` (or any) config error that breaks `build_registry` fails
only the commands that build the registry (they surface the typed error and exit non-zero);
DB-backed commands that do not build the registry, notably `agw vm list`, keep working off the
database. This is the pre-existing registry-config-error behavior (a broken `[plugins]` section is a
new way to reach it, not a new behavior), and it is desirable: an operator whose config is
temporarily broken can still inspect existing VMs. `agw doctor` degrades gracefully the same way,
the registry check FAILs, the registry-dependent groups skip with a pointer, and the run still
completes with a summary.

## Consequences

### Positive

- The resource model stays closed and the plugin model stays simple: a plugin is provenance plus a
  set of contributions, with no new kind, no new gate, and no bespoke disabled-roster dispatch.
- A not-enabled plugin gives operators a precise, actionable path (`enable plugin <name>`) instead
  of an unknown-name dead end, because its rows are present-but-disabled rather than absent.
- Enablement being a produced, multi-source axis means the operator-explicit-disable follow-on is
  purely additive: a new source, no consumer changes.
- The strict `[plugins]` stance is recorded as a reusable decision rule, so the
  strict-versus-lenient question is settled for future config sections rather than re-litigated per
  section.
- The model is proven, not just built: four world-specific bundles were migrated out of the core
  into shipped plugins in the same effort, `onepassword` (secret-backend), `claude` (harness + the
  `claude` install-command), `proxmox` (vm-platform), and `azure` (the `azure-vm` platform, the
  `azdo` git-credential provider, and the `az-cli` install-command). Together they exercise all four
  capability kinds and the bundled-manifest path against their real consumers, and they establish
  the migration pattern (impl `git mv` into the plugin package; the core `publish_to` skips the
  plugin-seated name so it is published once with a `system-plugin` origin, not twice). The core
  keeps only the universal path (`lima`/`wsl2`, `shell`, `env-var`/`prompt`, `github`).

### Negative

- Publishing every shipped plugin's capability rows unconditionally means a not-opted-in plugin's
  rows exist (disabled) in the registry, so every reader must understand the enablement axis to
  interpret them. The surfaces (hidden by default, the roster, the `Disabled:` line) are the
  compensating investment.
- `[plugins]` now diverges from the soft-warn convention, so the codebase carries two
  config-strictness policies. The in-code comment and this ADR are the cost of keeping that
  divergence legible.
- Capability and bundled-manifest publication both go through the enablement overlay, which cost a
  collision-contract generalization to make correct. A not-opted-in plugin's declarable rows publish
  "weak" (add-if-absent, silently yielding), so `Registry.add` gained a `weak` mode, a
  `_CollisionDecision` return (`OVERWRITE` / `KEEP_EXISTING` / raise) so an operator's legacy row
  wins without error in either publish order, and a finalize guard pinning weak-implies-disabled.
  That is more machinery than an enabled-only manifest gate, and it is the price of the
  capability/manifest parity, referencing a not-enabled plugin's bundled resource gives the same
  "enable plugin `<name>`" guidance a capability reference does, never an unknown-name error.
  Because a declarable resource has no `not_ready` consumer, that guidance is a use-refusal at the
  consumption entry rather than a fold verdict, so every bundleable declarable kind must have a
  use-gate (an allowlist enforces this). An earlier iteration published manifests enabled-only and
  deferred this parity as a scoped limitation; it was resolved within the same effort once the
  migration shipped real plugins (azure's `az-cli`, claude's `claude` install-commands) with
  name-referenced bundled manifests, at which point the asymmetry became operator-reachable.
- Migrating the four bundles is a **breaking change for existing operators**: an azure/proxmox/
  1Password/Claude-Code user's working config now needs the matching `[plugins].system` entry, or
  the resource is not-ready (or refused at use) with an "enable plugin `<name>`" hint. This is
  deliberate (the whole point is that world-specific functionality is opt-in), guided (the hint
  names the exact fix, never a silent failure or an unknown-name dead end), and bounded (the default
  local path is untouched). The upgrade note lives in `docs/guides/resources.md`; the release
  carries a `BREAKING CHANGE` changelog entry.

## Alternatives Considered

- **A plugin as its own resource kind.** Rejected: it would reopen the closed-kind model, require a
  `KIND_REGISTRY` entry and per-kind surfaces for something that is fundamentally provenance, and
  still not answer how a plugin's _contributed_ capability rows attribute back to it.
- **Gate publication on enablement (don't publish a not-opted-in plugin's capability rows).**
  Rejected: an absent row makes a reference to it an unknown-name hard error, losing the
  `enable plugin <name>` remediation. Present-but-disabled is what makes the friendly hint possible,
  for bundled declarable resources (published weak) as well as capability rows, so a reference to a
  not-enabled plugin's install-command or template gets the same guidance.
- **Hard-code the plugin opt-in check instead of a composed source.** Rejected: it would make the
  operator-explicit-disable follow-on a fold rewrite rather than an added source, for no v1 saving.
- **Follow the soft-warn convention for `[plugins]` unknown keys, for consistency.** Rejected: a
  gate that silently loads past a typo defeats its own purpose; loud failure is correct here even at
  the cost of a second strictness policy.
