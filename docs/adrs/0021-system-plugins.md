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
`system-plugin`-origin row whose plugin is not listed in `[plugins] enabled` is marked `disabled`.

The load-bearing choice is that **capability rows publish unconditionally**, opted in or not. A
not-opted-in plugin's rows are therefore **present-but-disabled**, not absent. That is what lets a
reference to one (an operator `vm-site` naming a not-enabled plugin's platform) resolve to
**not-ready with a remediation hint, "enable plugin `<name>`"**, rather than an unknown-name hard
error. An absent row could only produce the unhelpful unknown-name failure. Manifests, by contrast,
publish for enabled plugins only (a scoped v1 limitation recorded under Negative consequences
below): gating publication is simpler than publish-then-disable and keeps a not-opted-in plugin's
resources out of collision checks.

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

Consistently, an `[plugins] enabled` entry that is not an installed plugin is a typed config error
raised up front, before anything publishes, never a `KeyError` from deep in publication.

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

### Negative

- Publishing every shipped plugin's capability rows unconditionally means a not-opted-in plugin's
  rows exist (disabled) in the registry, so every reader must understand the enablement axis to
  interpret them. The surfaces (hidden by default, the roster, the `Disabled:` line) are the
  compensating investment.
- `[plugins]` now diverges from the soft-warn convention, so the codebase carries two
  config-strictness policies. The in-code comment and this ADR are the cost of keeping that
  divergence legible.
- Capabilities and bundled manifests are treated asymmetrically for a not-opted-in plugin, and the
  asymmetry is a known, scoped limitation. A capability publishes unconditionally (present-but-
  disabled), so an operator resource referencing it gets the "enable plugin `<name>`" hint; a
  bundled _declarable_ resource, by contrast, publishes only when the plugin is enabled. A
  declarable resource is also referenceable by name (for example an operator `vm-template` with
  `extends = <plugin-template>`), so referencing a not-enabled plugin's bundled resource yields the
  registry's unknown-name hard error rather than the enable hint, the two sides are inconsistent for
  a plugin that ships referenceable bundled resources. This is inert in the initial structure (the
  shipped index is empty; no plugin ships bundled resources), so it is deferred: the follow-on that
  ships the first plugin with referenceable bundled resources should move manifests to present-but-
  disabled with enablement-aware collision (so a disabled plugin's resource never blocks an
  operator's name), for symmetry with the capability side.

## Alternatives Considered

- **A plugin as its own resource kind.** Rejected: it would reopen the closed-kind model, require a
  `KIND_REGISTRY` entry and per-kind surfaces for something that is fundamentally provenance, and
  still not answer how a plugin's _contributed_ capability rows attribute back to it.
- **Gate publication on enablement (don't publish a not-opted-in plugin's capability rows).**
  Rejected: an absent row makes a reference to it an unknown-name hard error, losing the
  `enable plugin <name>` remediation. Present-but-disabled is what makes the friendly hint possible.
  (Manifests are the deliberate v1 exception, a known, scoped limitation recorded under Negative
  consequences: a bundled declarable resource is also referenceable by name, so the follow-on should
  bring manifests to present-but-disabled for symmetry.)
- **Hard-code the plugin opt-in check instead of a composed source.** Rejected: it would make the
  operator-explicit-disable follow-on a fold rewrite rather than an added source, for no v1 saving.
- **Follow the soft-warn convention for `[plugins]` unknown keys, for consistency.** Rejected: a
  gate that silently loads past a typo defeats its own purpose; loud failure is correct here even at
  the cost of a second strictness policy.
