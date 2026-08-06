# Roadmap Note: Producer-Oriented Facet Config

- Status: Message from the roadmap (fourth note; revises the third)
- Date: 2026-08-06
- Audience: the wave 2 effort lead

New-file message passing per the sdd skill: integrate what applies, then keep or delete this file.
Flag disagreements to the operator.

The operator revised the config resolution direction overnight, producer-oriented, and named the
axis. This supersedes the third note's `config_model_for(consuming_kind)` parameter shape; the rest
of that note (no slot vocabulary, one model for ordinary capabilities, the conformance reframing)
stands.

1. **Producer-oriented offering.** A capability offers a fixed set of configs the same way it offers
   a fixed set of API methods; consumers choose which one they drive. Producers never know their
   consumers.
2. **The axis is the facet**: the level a capability is driven at, values `vm`, `user`, `workspace`,
   `session`. Core asks `config_for(facet)` (names indicative). Facets are deliberately not scopes:
   core owns the scope-to-facet mapping (admin and agent both resolve to the user facet; session
   start and resume share the session facet), so the same answer for admin-template and
   agent-template falls out by construction.
3. **The ordinary case stays invisible.** A capability with one config declares it without naming
   any facet; only the harness-integration kind declares per-facet configs, in wave 4.
4. **Readable at finalize.** Core reads the facet-config association before any method runs, so it
   must be declared data, not just a signature annotation.
5. **Vocabulary note.** "Facet" here is a plain noun (the pairing of one level's methods and
   config). The earlier machinery meaning retired on 2026-08-05 (declaration contracts,
   support-by-presence, grants) stays dead; support is still carried by the integration's
   implementation, never by config presence.
