# Model-declared runtime interpolation: requirements

Status: design checkpoint. The operator authorized this SDD and the opt-in model pattern on
2026-11-13. New field adoption is proposed in the [migration inventory](migration-inventory.md) for
discussion before implementation.

## Purpose

Make runtime interpolation an explicit, reusable configuration-model contract. Component authors
choose which fields participate through an annotation. Operators can discover those fields through
the normal resource reference and generated config. Core and system plugins use the same pattern.

## FR1. Author-controlled participation

An annotation declares that a string value supports runtime interpolation. Unmarked values remain
literal to this mechanism. Adopting the pattern is optional for a component; the framework must not
require every string or every plugin to participate. The same declaration supplies field
documentation, emitted schema metadata and runtime interpolation eligibility. An annotation does not
add a YAML setting or tag.

## FR2. Preserve the existing language and consumers

Preserve the `${agw:NAME}` grammar, escaping, finite context catalogue, single-pass replacement and
field-aware failures documented in the permanent
[runtime-context reference](../../arch/runtime-context.md). Migrate existing env, Shell command and
native extra-argument support to the declared pattern.

Keep unavailable context unavailable. The consuming invocation supplies managed user, VM, agent,
workspace and session facts; no ambient shell or workstation environment lookup, cross-env
references, expressions or recursive replacement is introduced.

## FR3. Preserve interpretation boundaries

Keep loaded declarations reusable and unrendered. Rendering occurs when an invocation has selected
the applicable values and knows its target context, not during model construction or config loading.

Env scope precedence, managed identity filtering, secret exclusion and offline preview remain
intact. Shell selects its launch field before rendering. Existing env and Shell pre-secret
validation remains intact. Native argv values render before quoting and artifact conflict planning.
Returned command text and inserted data are not processed again.

Secrets, secret reference names, resource-reference names and opaque workload input retain their
existing literal contracts. A new opt-in must identify its consumer, available context, rendering
stage and destination handling.

## FR4. Migration pass

Audit core and shipped system plugins for obvious additional consumers, and present a field-level
recommendation with evidence and costs. The inventory is the discussion surface, not acceptance of
every candidate. Incorporate the operator's disposition before implementing new field support.
Migrate the agreed fields in the same effort; do not limit adoption to the six existing declarations
merely because they exist today.

Keep ordinary downstream shell behavior, existing path conventions and collection merge rules unless
an explicitly discussed change requires otherwise. New fields gain only the reserved namespace
behavior, with literal escaping documented.

## FR5. Discovery and permanent documentation

Resource explanation, generated configuration and emitted JSON Schema identify opted-in values,
including collection elements and env's scalar shorthand. A dedicated `concept-templating` guide
explains normal use, context availability, escaping, literal secrets and command quoting, and
signposts current resource commands for field facts.

Ship the guide, permanent runtime-context reference updates and model-author guidance with the
implementation that makes them true. No operator or component author should need this SDD to use the
completed pattern.

## FR6. Compatibility and scope

Preserve existing temporary Shell/session-env alias eligibility, warning aggregation, suppression
and doctor visibility. New opt-ins use the namespaced language. Track the separate cutoff alignment
in [issue #878](https://github.com/WayfarerLabs/agentworks/issues/878); this SDD does not change the
operator's two-minor-release decision.

This effort does not rework transport, add context names, unify owner-templated resource defaults,
interpolate arbitrary imported files, or rewrite every existing shell expression.

## Operator direction recorded

- 2026-11-13: "it's really just a pattern that people can choose to join or not"; the operator
  identifies the annotation as the opt-in.
- 2026-11-13: audit core and system plugins for obvious migrations, assemble the list and discuss it
  before implementation.
