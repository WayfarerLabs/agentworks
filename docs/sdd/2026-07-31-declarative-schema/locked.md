# Locked: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-07

Status: LOCKED. Both phases are on `main`. The artifacts in this directory are frozen; the only
sanctioned change is a dated entry appended to this file.

## What shipped

**Phase 1 (merged separately, PR #316, 2026-08-05).** TOML resource declarations are gone.
`config.toml` is settings-only and hard-errors on any resource-declaring section; the TOML loaders
were relocated into `migrate/` rather than deleted, so migration verification has a pre-side
independent of the emission mapping. ADR 0022 supersedes ADR 0016's dual-path stance.

**Phase 2 (this PR).** Every schema fact is authored once, in a model, and every other surface is
derived from it.

- **The capability-kind switchboard collapsed onto one table.** Seven sites independently enumerated
  the four capability kinds; each is now a derived view of one frozen, core-owned
  `CapabilityKindDescriptor`. Registration-time conformance replaced a type-and-cast seam.
- **`agentworks/schema/`** owns the model vocabulary: a strict, frozen, closed-world base;
  `SecretRef`/`ResourceRef` markers with an `x-agw-ref` encoding that survives into emitted schema;
  a total `extract_references`; and `iter_field_docs`, the one field-documentation stream that the
  sample renderer, `describe-kind`, and the onboarding effort's `agw guide` all read.
- **The capability contract flipped** from invoked validation to declared config.
  `Capability.validate`, `Capability.dependencies`, `SecretBackend.validate_mapping`, and
  `SecretBackend.dependencies` are gone. Core validates and extracts; capability code is never
  invoked for either.
- **Thirteen hand-rolled decoders became kind spec models.** `manifests/decode.py` went from 759
  lines to an adapter with no per-kind knowledge.
- **Defaulting moved into the model layer.** Consumer-side fallbacks are deleted, and the fields are
  non-optional after decode so mypy forbids re-adding them.
- **JSON Schema is emitted per kind plus an envelope**, with `agw resource schema` and a
  yaml-language-server modeline on written manifests.
- **The bundled sample corpus is deleted.** `agw resource sample` renders live from the registry,
  and `agw resource describe-kind KIND[/NAME]` renders the field reference from the same stream.

## The decisions worth keeping

**Config is declared per FACET, producer-side.** A capability declares the config it offers the way
it declares its API methods, and consumers choose which facet they drive; producers never know their
consumers. A facet is the level a capability is driven at (`vm`, `user`, `workspace`, `session`).
Facets are deliberately NOT scopes, and core owns the mapping, so admin and agent both resolve to
`user` by construction rather than by each capability encoding that they mean the same thing. Two
designs were rescinded before any model registered through them: schema slots, and
`config_model_for(consuming_kind)`, which made every producer enumerate its consumers. **Config
presence is not the support claim**, or facets become slots under a new name.

**Inheritance is not a dependency, and the requirement has two halves.** The FRD originally
specified only the excluding half, which would have been a regression on its own: the edges that
cross an inheritance edge in the runtime traversal are the non-capability ones (`vm-template` env
and `tailscale_auth_key`, `agent-template` `git_credentials`, `workspace-template` env), so cutting
the edge alone makes a child stop reporting its inherited env secrets. Every inheriting kind now
produces the runtime needs of its EFFECTIVE declaration, and only then is the edge excluded. The
relationship is typed on the EDGE, never inferred from the target's kind: filtering on
`isinstance(ref, TemplateReference)` means "points at a template" and would misclassify a future
uses-a-template edge. Inbound listings deliberately still cross, because a parent genuinely is
referenced by its children.

**`agw resource migrate` survives with a scheduled expiry.** It is the remediation for the breaking
changes this phase ships, which is runway, not capability. It should retire a release or two after
they land, like every other compatibility surface here. `cli/tests/test_migrate_separability.py`
enforces that removal stays a deletion: nothing outside `agentworks/migrate/` may import it except
the one CLI command that fronts it. The two halves (TOML conversion, manifest upgrade) can retire
independently.

**The schema must never reject what the loader accepts.** Under-reporting is sanctioned;
over-reporting underlines valid configuration in the operator's editor. This bit four times: an
owner-templated field emitted as required, a dropped nullable arm, and the YAML 1.1/1.2 gap in both
booleans and integers. The loader is PyYAML (1.1) and yaml-language-server is 1.2, so emitted
booleans and integers accept both spellings, derived from pyyaml's own resolver rather than
hand-listed. `010` is unfixable by any schema (same JSON type, different value) and is recorded as
such.

## Known gaps, deliberate

- **Map-keyed `backend_mappings` emission is not spliced.** The descriptor carries no record of
  where a map-keyed capability is hosted, so emission would have to hard-code
  `secret`/`backend_mappings` and reinstate the switchboard the descriptor exists to have killed.
  This needs a change to the roadmap's descriptor contract. Today's emission there is
  under-constrained but never wrong. The trigger has already fired: `onepassword` ships with a fully
  modeled mapping, so an operator writing it gets no completion, no `op://` check, and no key
  checking.
- **Inherited capability config**: the loader validates the merged blob while the schema validates
  the child's fragment, so a child partially restating an inherited config is legal at load and
  rejected by the schema. Nil exposure today (no shipped arm requires a field beyond its tag),
  carried as a tripwire test. The fix when it fires is conditional (`if: {required: [inherits]}`),
  not relaxing `required`, which would delete a real diagnostic from standalone templates.
- **FR14 (settings-section models and a config.toml schema) is descoped**, as the plan permitted.
  The settings layer is the largest remaining cluster of consumer-side re-defaulting, already
  enumerated, and `_warn_unexpected_keys` survives with nine call sites for the same reason.
- **`capabilities/facets.py` has no production consumer yet.** It is kept because the operator named
  the axis and two roadmap contracts commit to `config_for(facet)`, with wave 4's harness
  integrations as the named consumer. The cost is real: nine signatures carry a parameter nothing
  passes. If wave 4 is cancelled or re-scoped, this becomes dead and should go.
- No surface shows a resource's parsed spec values, and `cpus: 0` is accepted by both layers.
- The name-charset rule the samples state is enforced only on some kinds. Issues #279 and #308
  settled that tolerance deliberately, so the documentation was corrected rather than the behavior.

## Deliberately unused, do not delete as dead code

`describable_targets`, `implementation_reference`, and `capability_kind_reference` have no in-tree
caller by design: they are the service API the onboarding effort's `agw guide` consumes. That
coordination is recorded in `cli/agentworks/topics.py`, which is a live cross-effort agreement
rather than a dangling pointer.

## Where the concepts now live permanently

Per the SDD-is-not-permanent rule, nothing load-bearing lives only here. The descriptor and
declared-config contracts are in `cli/agentworks/capabilities/README.md`; plugin-author guidance is
in `cli/agentworks/plugins/README.md`; the operator-facing model and the upgrade note are in
`docs/guides/resources.md`; and **ADR 0023** records the schema model and the kind descriptor. Dated
entries were appended to `docs/sdd/2026-07-01-resource-manifests/locked.md` and
`docs/sdd/2026-07-01-vm-sites/locked.md`, whose recorded stances this effort revised.

## Deviations from the plan as written

- The schema package shipped at `agentworks/schema/`, not `resources/schema/`: a package under
  `resources/` cannot be the import leaf the design requires, since importing anything there loads
  every capability package.
- `render_validation_error` shipped and was retired at closeout with no production caller; the
  reusable-text property lives in `_problems`.
- Step 2.3's finalize work was deferred into its own step 2.3b rather than folded into 2.4 or 2.5.
- Step 2.4 shipped without an LLD, which is recorded as a waiver rather than an omission: it was a
  flip of a mechanism step 2.0 had already designed.

## What this effort learned that outlived it

Recorded because the guards that came out of it are now load-bearing, and because the same mistakes
recur.

**A silent wrong answer is worse than a crash**, and this phase produced one at nearly every step: a
discriminator that vanished on optional unions, a guard that stopped guarding while its comment
claimed otherwise, an FR17 implementation that would have dropped inherited secrets, an empty
`tailscale_auth_key` replacing a resolved default, a migrator that dropped operator config and
reported that it had verified nothing changed.

**Tests that pass for the wrong reason are the recurring failure.** Three separate cases derived an
expectation from the same source as the thing under test. The worst was self-referential: a fix for
a tautology was itself pinned tautologically, and the whole suite passed with the fix removed. The
guards that survive assert INVARIANTS rather than current answers:
`test_every_exemption_is_load_bearing`, `test_every_relationship_has_a_closure`, the
`_LEGACY_SIBLING_SHAPES` equality, the separability guard with its non-vacuity twin, and the
fresh-interpreter seating pins.

**Verify by execution, not recall.** A table of pydantic error types written from memory had four
wrong entries. Claims about registry state ("this is the shipped case", "the trigger has not fired")
were wrong twice at lead level. Prose review cannot find an ordering defect: the operator upgrade
rehearsal took 13 hand-edit rounds against a guide whose content was entirely correct.

**Independence must be bought on the right axis.** Migration verification was independent by using a
different PARSER (YAML 1.1 vs 1.2, which leaked) and later by using the same fold on both sides
(blind to fold-semantics bugs). Both shipped as bugs whose whole point was to catch bugs.
