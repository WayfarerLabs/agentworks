# Message: Onboarding Topic Content Contract

- Date: 2026-08-06
- From: onboarding-and-discovery effort
- To: declarative-schema phase 2 effort, before plan section 2.8
- Status: Proposed coordination contract

## Coordination request

Wave 2 currently plans a blurb registration surface for samples and describe. Onboarding R14
requires one generic topic-content contract shared by resource kinds, capability implementations,
and plugins. Please use the prose portion of this contract as wave 2's blurb source, or raise a
concern before implementing plan section 2.8, rather than create a separate blurb registry that must
later be adapted.

Wave 2's current branch is provisional. The onboarding effort will depend only on the contract that
eventually lands on `main`.

## What a contribution declares

A `TopicContribution` is immutable, strictly validated data:

- `topic`: one canonical, completion-safe slug.
- `title`: one display title.
- `summary`: one short authored paragraph for indexes and reference introductions.
- `anchor`: the documented concept, resource kind, resource, or capability implementation. A kind,
  resource, or implementation anchor is `me` for dynamic blocks. Concept topics use a small,
  core-defined set of named roots; the onboarding HLA will settle the initial root vocabulary.
- `blocks`: an ordered tuple from a closed block vocabulary.
- `related_topics`: exact topic slugs for progressive navigation.

The initial block vocabulary is:

- `Overview`: static markdown prose shared by describe and guide. This is the proposed replacement
  for wave 2's separate blurb.
- `Teaching`: static markdown prose used by guide for workflows, caveats, and next actions.
- `AgentContract`: static markdown prose whose placement may be foregrounded in agent mode without
  changing its substance.
- `InstanceList`: core-rendered resources for the anchored kind or implementation.
- `State`: core-rendered enablement and readiness for `me`.
- `Relationships`: core-rendered inbound and outbound declared relationships for `me`.
- `FieldReference`: core-rendered wave 2 field-documentation facts.
- `Sample`: core-rendered wave 2 live sample.
- `TopicLinks`: core-rendered related-topic links.

Contributions contain strings and block records only. They cannot contain functions, expressions,
imports, arbitrary attribute paths, format-string evaluation, or contributor-selected renderer
names. Dynamic blocks are core code selected from the closed vocabulary and use the resource graph
itself in a gated read-side mode over already-materialized facts. This is not a second projection
structure kept in lockstep. Powers are absent or permission-gated in that mode.

## Sizing and presentation

One topic should answer one operator question at skill-sized depth. It links to subtopics rather
than recursively embedding every field, resource, or workflow. The top-level guide remains an index
and golden-path entry, not a generated manual dump.

Reference and teaching presentations share sources but remain distinct:

- describe renders `summary`, `Overview`, and wave 2 field facts;
- guide renders the same `summary` and `Overview`, then teaching, state, samples, and links as the
  topic declares;
- schema emission and `FieldDoc` remain presentation-free fact sources;
- guide calls service APIs for field docs and samples, never rendered CLI output.

## Colocation and registration

- A resource kind's topic data lives beside that kind.
- A capability implementation's topic data lives beside that implementation.
- A plugin bundles its topic data in its inert plugin descriptor.
- Core concept topic data lives beside the subsystem it teaches.
- A participant with no useful content contributes nothing. No empty topic is generated.

The capability-kind descriptor may transport implementation topic data, but does not own or alter
the contract because ordinary kinds and concepts use it too. One core topic catalog validates all
contributors. Duplicate slugs are errors, never load-order precedence.

## Requested wave 2 alignment

Before implementing declarative-schema plan section 2.8, please confirm or raise concerns with:

1. using `summary` plus `Overview` as the sole authored blurb source for both describe and guide;
2. keeping `FieldDoc`, schemas, and sample inputs presentation-free;
3. exposing reusable service records or render functions for field reference and samples;
4. rendering disabled implementations from registered models without constructing them;
5. avoiding a standalone blurb registry or rendered-output adapter.

The onboarding HLA will incorporate the resolved coordination outcome.
