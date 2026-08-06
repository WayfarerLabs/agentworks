# Roadmap Note: The Guide Surface Split

- Status: Message from the roadmap (second note; the first is `roadmap-seed-notes.md`)
- Date: 2026-08-05
- Audience: the wave 2 effort lead

This is new-file message passing per the sdd skill: integrate what applies into your artifacts, then
keep or delete this file as you see fit. Flag disagreements to the operator.

Since your seed notes were delivered, the onboarding-and-discovery child's FRD (PR #413) has been
centered on a new teaching surface, `agw guide [topic ...]`: skill-shaped markdown for agents and
humans, blending static authored content with dynamic content from the live system, with topics for
resource kinds, specific resources, capability implementations, and `concept-` prefixed meta topics.
This renegotiates one line of your seed notes, in your favor on scope but with a shared contract to
design:

1. **Surface split.** You own the reference surfaces and their naming (schema emission, live
   samples, describe) exactly as your seed notes said. The onboarding child owns the teaching
   surface (`agw guide`). Describe answers "what is this and what is its state"; guide teaches "how
   to think about and use it." Same sources, two presentations.
2. **The ask: design your sources as consumable, not presentation-bound.** Your field-documentation
   walker, live-sample renderer, and the blurb registration surface your plan's 2.8 contemplates
   should be internal sources that more than one presenter can render. Concretely: the guide command
   will want schema fragments, field references, sample skeletons, and per-kind authored blurbs as
   data it can compose into topic pages. If the blurb surface is designed as a topic-content
   contract (or at least does not preclude one), the two efforts share one authored layer instead of
   forking prose.
3. **Static content colocation.** The roadmap's direction is that per-kind authored content lives
   beside the kind it documents (and plugin content in the plugin). If your 2.8 blurb work
   establishes that colocation convention, the guide effort inherits it rather than inventing a
   second home.
4. **Templating boundary.** Contributed guide content is data rendered through locked-down
   templating, never code. If your sample/describe prose goes through any templating, aligning on
   the same locked-down vocabulary avoids two template dialects.

Nothing here changes your plan's sequencing or your ownership of `agw resource schema` and describe
naming. The coordination is at the source layer. The onboarding child's FRD carries the matching
open questions, and the roadmap's `target-state.md` records the operator rulings (guide named and
markdown-only, `concept-` prefix, thin harness bootstraps, universal contribution, data-not-code
templating).
