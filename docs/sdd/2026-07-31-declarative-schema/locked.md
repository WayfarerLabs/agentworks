# Locked: TOML Resource Sunset and the Declarative Schema Model

Date: 2026-08-07

Status: LOCKED. Both phases are on `main`. The artifacts in this directory are frozen; the only
sanctioned change is a dated entry appended to this file.

## What shipped

**Phase 1 (merged separately, PR #316, 2026-08-05).** TOML resource declarations are gone.
`config.toml` is settings-only and hard-errors on any resource-declaring section. ADR 0022
supersedes ADR 0016's dual-path stance.

**Phase 2 (this PR).** Every schema fact is authored once, in a model, and every other surface is
derived from it.

- **The capability-kind switchboard collapsed onto one table.** Seven modules independently
  enumerated the four capability kinds; each is now a derived view of one frozen, core-owned
  `CapabilityKindDescriptor`, through five cached accessors. Registration conformance replaced a
  type-and-cast seam.
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
- **`agw resource migrate` is deleted, both halves**, along with the frozen TOML oracle it needed,
  the `ruamel-yaml` and `tomlkit` dependencies it dragged in, and the `agw resource migrate`
  completion providers. The upgrade path is `docs/guides/upgrading-to-0.14.md`.

## The decisions worth keeping

**Remediation is precise errors plus guide content, not automated migration tooling** (operator
ruling, 2026-08-07, mid-effort, recorded in the roadmap's `target-state.md`). The migrator required
a frozen re-implementation of the old shapes as a verification oracle, and every divergence between
oracle and model surfaced to the operator as a self-blaming failure ("this is a migrate-tool bug,
report this error") on valid input. Its deliberate deletability, enforced by a separability guard
that let nothing outside `migrate/` import it, is what let it be removed before release rather than
maintained to a scheduled expiry. The guard retired with it, having done exactly its job.

**Config is declared per FACET, producer-side, and the mechanism is not built yet.** A capability
declares the config it offers the way it declares its API methods; consumers choose which facet they
drive; producers never know their consumers. Facets are deliberately NOT scopes, and core owns the
mapping, so admin and agent both resolve to `user` by construction. **Config presence is not the
support claim**, or facets become the rescinded slot mechanism under a new name. Two designs were
rescinded before any model registered: schema slots, and `config_model_for(consuming_kind)`, which
made every producer enumerate its consumers. `capabilities/facets.py` and the `facet` parameter on
nine signatures shipped and were then REMOVED (2026-08-07): no mechanism without a consumer. The
contract stays settled here and in the roadmap; wave 4 reintroduces the parameter additively when
the harness-integration kind actually offers per-facet configs. The reasoning survives in
`capabilities/base.py`, which is the point: what was deleted was the unused parameter, not the
design.

**Validation is unconditional.** No readiness verdict, enablement verdict, or property of the host
decides whether a declaration is well-formed; the declared model is the only authority, and a model
that legitimately accepts open keys keeps accepting them. Openness is a fact about the model, never
an accident of environment. The finalize pass used to skip not-ready and disabled rows, which
inverted two questions: validation is pure and answerable from the document alone, readiness is
environmental and is computed from config the validate pass had not yet checked. That let a typo
decide its own fate. A misspelled `vm_host` read as an absent one, which made a remote lima site
look local, which made it not-ready for want of `limactl`, which suppressed the very error that
named the typo, while the same document was correctly refused on a host that happened to have
`limactl` installed. Closed-world config that is closed only on some hosts is not closed. R9.9's
enablement-keyed skip of a disabled backend's mapping closed for the same reason (operator ruling):
deferring validation to enablement time surfaces the error at the worst moment, when the operator is
trying to turn something on.

**Inheritance is not a dependency, and the requirement has two halves.** The FRD originally
specified only the excluding half, which would have been a regression on its own: the edges that
cross an inheritance edge in the runtime traversal are the non-capability ones (`vm-template` env
and `tailscale_auth_key`, `agent-template` `git_credentials`, `workspace-template` env), so cutting
the edge alone makes a child stop reporting its inherited env secrets. Every inheriting kind now
produces the runtime needs of its EFFECTIVE declaration, and only then is the edge excluded. The
relationship is typed on the EDGE, never inferred from the target's kind: filtering on
`isinstance(ref, TemplateReference)` means "points at a template" and would misclassify a future
uses-a-template edge. Inbound listings deliberately still cross.

**Inheritance resolution folds ONE accumulator over the chain's declarations.** Each resolver used
to resolve every parent to a fully defaulted template and merge those, so a parent that declared
nothing overwrote an earlier parent's real values with built-in defaults, order-dependently and
silently, and `VMTemplate.dependencies` then built the secret edge from the wrong value: the graph
gated one secret while the VM provisioned with another. All four resolvers now flatten the chain to
its declarations, and `_merge_template` is the only writer, so the fully-defaulted-parent state is
unrepresentable rather than guarded against. A misspelled parent name was a second entrance to the
same defect and is closed by the same change. Diamonds linearize: the naive chain re-applied a
grandparent on top of the parent that overrode it.

**Traversals of operator-controlled input go through shared, cycle-safe helpers** (roadmap ruling,
`target-state.md`). `agentworks/traversal.py` holds two iterative walks, split on whether reaching a
node twice by a different route means something: reference extraction says yes (two sibling blocks
of one model are two blocks an operator wrote), the inheritance chain says no. Bounded walks over
code-shaped structures, a model class's own fields, may stay recursive; the discipline applies where
the input's size or shape is the operator's to choose.

**The schema must never reject what the loader accepts.** Under-reporting is sanctioned;
over-reporting underlines valid configuration in the operator's editor. This bit four times: an
owner-templated field emitted as required, a dropped nullable arm, and the YAML 1.1/1.2 gap in both
booleans and integers. The loader is PyYAML (1.1) and yaml-language-server is 1.2, so emitted
booleans and integers accept both spellings, derived from pyyaml's own resolver rather than
hand-listed. `010` is unfixable by any schema (same JSON type, different value) and is recorded as
such. The same direction governs extraction: an ambiguous union extracts nothing rather than
inventing a dependency on a resource the operator never wrote, which finalize would then refuse.

## Known gaps, deliberate

- **Map-keyed `backend_mappings` emission is not spliced.** The descriptor carries no record of
  where a map-keyed capability is hosted, so emission would have to hard-code
  `secret`/`backend_mappings` and reinstate the switchboard the descriptor exists to have killed.
  This needs a change to the roadmap's descriptor contract. Today's emission there is
  under-constrained but never wrong. The trigger has fired: `onepassword` ships with a fully modeled
  mapping, so an operator writing it gets no completion, no `op://` check, and no key checking.
- **Inherited capability config**: the loader validates the merged blob while the schema validates
  the child's fragment, so a child partially restating an inherited config is legal at load and
  rejected by the schema. Nil exposure today (no shipped arm requires a field beyond its tag),
  carried as a tripwire test. The fix when it fires is conditional (`if: {required: [inherits]}`),
  not relaxing `required`, which would delete a real diagnostic from standalone templates.
- **FR14 (settings-section models and a config.toml schema) is descoped**, as the plan permitted.
  The settings layer is the largest remaining cluster of consumer-side re-defaulting, and
  `_warn_unexpected_keys` survives with THREE call sites for the same reason (`operator`,
  `secret_config`, `session.config`; earlier counts in this effort said six and eight, both of which
  had counted imports and an `__all__` entry as call sites). Settings that NAME resources are
  references, and a dangling one is a hard error (operator ruling, 2026-08-07: config errors are
  hard errors, full stop). There are exactly two: `[secret_config].backends` and `defaults.site`.
  They are shape-checked at settings load, because the registry does not exist yet, and their names
  are resolved once at the composition boundary after finalize, raising the same shape a dangling
  MANIFEST reference already does (`registry.py:655`). That boundary is in
  `bootstrap.build_registry`, deliberately NOT in `Registry.finalize`: the Registry is
  config-agnostic by construction, and making it finalize against settings would force every
  hand-built test registry to carry a Config or opt out. Settings stay strings checked at a boundary
  rather than becoming typed references, because a `ResourceReference` carries the `(kind, name)` of
  a declaring ROW, and inventing a `("config", "defaults")` source is the pseudo-resource ADR 0016
  forbids; it would surface in `describe`'s "Referenced by" as though a resource pointed at it. What
  settings share with manifest references is the obligation, not the type.

  **`[secret_backends.*]` is a resource DECLARATION and fails hard as one.** It is in
  `KIND_SECTIONS`, and it now reaches the ordinary resource-section error with its own accurate
  remediation as a second clause: the section carried no configuration, so there is nothing to
  rewrite and the answer is to delete it. Previously it split on the backend NAME, warning for
  built-ins and erroring for anything else against the built-in registry alone, so a correctly
  spelled, enabled, healthy plugin backend was reported as unknown. The severity split was the
  visible half. The damaging half was that it was refused by the SETTINGS load, which
  `resources=False` cannot skip, so a section carrying no configuration took down
  `resource sample --write` and `resource schema --write`, the two commands the 0.14 rewrite depends
  on, and truncated `agw doctor` to a single fail row. An entire section of the upgrade guide
  existed to work around that and is deleted with it.

  **Three claims made about this area during the effort were wrong, and are corrected here because
  the wrong versions are more memorable than the right ones.** `defaults.site` naming an unknown
  site was never a doctor warning; `validate_sites` has hard-errored on it since `fd69f8a0`, and
  `vms/sites.py`'s degradation note is about NOT-READY sites, which is a different question.
  `[secret_config].backends` was never checked only at use time; `validate_chain` called
  `active_backends` eagerly from the same boundary. And `defaults.runup_git_credentials` names
  nothing at all: it is a `bool`. What was actually wrong was smaller and duller than any of that:
  two hand-written checks at one boundary, which is the switchboard pattern this effort collapsed
  everywhere else, now one table.

- **A field carrying two markers keeps only the first.**
  `Annotated[str, SecretRef(...), ResourceRef(...)]` emits the secret edge alone, with no refusal.
  It is the same family as the gaps closed above, and the machinery to refuse it now exists one
  predicate away in the fail-closed check that reads which markers the classifier could place. Left
  open because no shipped field carries more than one marker (verified by sweeping every declarable
  kind's spec model) and because a field that is simultaneously a secret name and a resource name is
  a modeling error rather than a shape to support: the honest close is a refusal, not two edges.
  Trigger: the first author who writes one.
- **Two union shapes extract no edges, by design.** An undiscriminated union of two or more models
  addresses no arm without guessing which; a union tagged by non-string literals is outside the
  framework's rule that every discriminator is a capability or kind NAME. Both are fixture-only; no
  shipped field has either shape.
- **Nested collections carrying a marker are refused, not supported.** `FieldShape` is deliberately
  flat (a field, and one value inside it), so a third level is a contract change across extraction,
  error-path segments, and field-doc rendering. Registration refuses the shape with an author-facing
  message rather than silently extracting nothing.
- `agw guide` **is not built.** It is named in FR2 as the vehicle that fills the migrator's role,
  and the roadmap gates the 0.14.0 cut on the guide's first slice, but that slice ships
  `concept-onboarding` and no migration topic is contracted by name. If the sunset reaches operators
  first, `docs/guides/upgrading-to-0.14.md` carries the break alone, which it is written to do.
- `set`/`frozenset` fields are unwritable from any document (`AgwModel` is `strict=True`, so a list
  is never coerced), while extraction reads them as collections. An over-report on an unreachable
  path; pre-existing.
- No surface shows a resource's parsed spec values, and `cpus: 0` is accepted by both layers.
- The name-charset rule the samples state is enforced only on some kinds. Issues #279 and #308
  settled that tolerance deliberately, so the documentation was corrected rather than the behavior.

## Deliberately unused, do not delete as dead code

`describable_targets`, `implementation_reference`, and `capability_kind_reference` have no in-tree
caller by design: they are the service API the onboarding effort's `agw guide` consumes, and FR2
names that command as the remediation vehicle for this effort's breaking changes. The coordination
is recorded in `cli/agentworks/topics.py`, which is a live cross-effort agreement rather than a
dangling pointer.

## Where the concepts now live permanently

Per the SDD-is-not-permanent rule, nothing load-bearing lives only here. The descriptor and
declared-config contracts are in `cli/agentworks/capabilities/README.md`; plugin-author guidance is
in `cli/agentworks/plugins/README.md`; the operator-facing model is in `docs/guides/resources.md`;
the release-scoped upgrade path is in `docs/guides/upgrading-to-0.14.md`, split out so it can be
deleted rather than unpicked, with the hard error that points at it carrying a comment naming
everything its retirement must take; and **ADR 0023** records the schema model and the kind
descriptor. Dated entries were appended to the lockfiles of `2026-07-01-resource-manifests`,
`2026-07-01-vm-sites`, and `2026-07-27-registry-readiness-refactor`, whose recorded stances this
effort revised.

## Deviations from the plan as written

- The schema package shipped at `agentworks/schema/`, not `resources/schema/`: a package under
  `resources/` cannot be the import leaf the design requires, since importing anything there loads
  every capability package.
- `render_validation_error` shipped and was retired at closeout with no production caller.
- Step 2.3's finalize work was deferred into its own step 2.3b.
- Step 2.4 shipped without an LLD, recorded as a waiver: it was a flip of a mechanism step 2.0 had
  already designed.
- `red-window-inventory.md` was retired once the window it bounded had closed; see the 1.2f section
  of the plan.
- The effort re-opened after review. See below.

## The rework round

The roadmap lead's review returned request-changes on three blockers and a long should-fix list, and
an operator ruling deleting the migrator landed alongside it. Both blockers that survived the
deletion were silent wrong answers, and the round found more than the review did: the diamond
linearization, the misspelled-parent entrance to B2, a third instance of the vanishing-union-arm
family, an upgrade guide whose lead-off step could not run, and 371 tests that were not earning
their place.

**Two operator rehearsals bracketed it, and both were load-bearing.** The migrator-era rehearsal
took 13 hand-edit rounds against a guide whose CONTENT was entirely correct and whose ORDER could
not work. After the rewrite, an independent rehearsal walked the replacement in 9 rounds with 2
refusals and diffed the migrated registry against a real pre-sunset build: byte-identical resource
set across 28 rows, zero field loss. It still found the same class of defect one more time, in a
narrower population: the guide led with a step that dies on any `[secret_backends.<plugin>]`
section, while telling the operator 199 lines later that such a section could outlive the rewrite.

## What this effort learned that outlived it

Recorded because the guards that came out of it are load-bearing, and because the same mistakes
recur.

**A silent wrong answer is worse than a crash**, and this effort produced one at nearly every step:
a discriminator that vanished on optional unions, a guard that stopped guarding while its comment
claimed otherwise, an FR17 implementation that would have dropped inherited secrets, a migrator that
dropped operator config and reported it had verified nothing changed, a diamond that resolved to its
grandparent's value, and a typo that suppressed the error naming it.

**Tests that pass for the wrong reason are the recurring failure.** The worst was self-referential:
a fix for a tautology was itself pinned tautologically, and the whole suite passed with the fix
removed. The paring pass found more of the same by asking one question of every test, what mutation
does this catch that nothing else catches: an assertion disjoint by construction, a `"-" in stdout`
check that was true twice over because the header rule is dashes and `secret-backend` has a hyphen,
a backend-extraction test that bypassed the entry point it existed to verify, and a
marked-collection test pointed at a field where no mutation could change the answer. The guards that
survive assert INVARIANTS rather than current answers.

**Two faces of one fact are two authored copies.** An LLD justified a hand-written schema fragment
as "the exception that proves" the author-once rule, reasoning that the authored place was the type
and the validator and the schema hook were its two faces. That sentence reads well and is false.
Nothing made them agree, and a third consumer arrived that could read neither, so `describe-kind`
told operators to rewrite every plaintext env value for nothing. The exception was never proving the
rule.

**Verify by execution, not recall.** A table of pydantic error types written from memory had four
wrong entries. Claims about registry state were wrong repeatedly at lead level, and the tell is
always the same: a registry read with plugins seated cannot show you the sparse case, so a check has
to name which registry it read. Prose review cannot find an ordering defect.

**Independence must be bought on the right axis.** Migration verification was independent by using a
different PARSER (YAML 1.1 vs 1.2, which leaked) and later by using the same fold on both sides
(blind to fold-semantics bugs). Both shipped as bugs whose whole point was to catch bugs.

**Deleting a thing means deleting what it dragged in.** The migrator's removal also took a 744-line
frozen oracle, two runtime dependencies whose justifying comment named a file that no longer
existed, four completion providers, a settings overlay, and five newly-dead helpers. What made that
a deletion rather than an excavation was a guard written a day earlier for exactly this, which is
the case for building deletability into anything shipped as runway.

## 2026-08-08: authentication and placement became explicit on three platforms

Recorded here because it supersedes shapes this effort shipped. The plan, FRD, and HLA are NOT
edited: they describe what this effort built, accurately, and a completed checkbox is not corrected
by a later change. In particular the 2.3 inventory still names azure's nested `service_principal`
model and aws's nested `credentials` model, which is what existed when that step ran.

**What changed.** Azure's `service_principal` block, AWS's `credentials` block, and lima's `vm_host`
field are replaced by required nested discriminated unions: `auth: {mode: ambient}` or
`{mode: service-principal, ...}` on azure, `auth: {mode: ambient}` or `{mode: access-key, ...}` on
aws, and `placement: {mode: local}` or `{mode: ssh, host: ...}` on lima. No default and no omission
alias, so every existing manifest for those three platforms crosses the break. Proxmox is unchanged
because it has one valid shape, and wsl2 has no choice to model.

**Why it belongs on THIS lockfile.** All three were the same defect this effort spent itself
removing, in a shape it did not name: absence selecting a MECHANISM rather than supplying a default
value. Omitting azure's credential block chose the ambient chain; omitting aws's did the same; and
lima inferred local versus remote from whether `vm_host` was present. A manifest could not
distinguish a deliberate choice from a forgotten one, and neither could a reviewer, `doctor`, or the
graph.

**It closes the readiness self-masking case structurally, not just at the validation layer.** The
"Validation is unconditional" decision above tells that story at length: a misspelled `vm_host` read
as an absent one, which made a remote lima site look local, which made it not-ready for want of
`limactl`, which suppressed the very error that named the typo. This effort fixed the layer that
SUPPRESSED the error. The placement union removes the shape that made a typo look like a choice, so
there is no longer an inference to mis-fire. Both fixes are load-bearing and neither replaces the
other.

**What was promoted, per the SDD-is-not-permanent rule.** The modeling rule that came out of the
design is in `cli/agentworks/capabilities/README.md` ("Modeling a Config That Has Variants"):
absence supplies a default value and must not select a mechanism; the discriminator selects a SHAPE
rather than a concept, and the operational test is whether the required field sets differ; adding an
arm is the additive extension path, so pre-grouping against a variant that does not exist is
mechanism without a consumer; and arm names select a mechanism rather than a position.

**Two claims in this effort's code were made false by it and corrected there.** `_shape.py`'s
`_tags_of` justified its non-string-tag boundary on "every discriminator in this framework is a
capability or kind NAME", which stopped being true when `mode` arrived; the boundary now stands on
the reason that survives, which is that a tag is an identifier the operator writes. And `item_arms`
said all four discriminated unions were top-level capability configs; three now are not, so what
remains unshipped is the COLLECTION of tagged blocks rather than the nesting.

**One thing this effort's union machinery got right, worth recording as evidence.** The three new
unions needed NO change to `_shape.py`'s classification, `extract.py`'s arm walk, `base.py`'s marker
refusal, or `field_tree.py`'s expansion, despite being the first discriminated unions here whose
arms are not capability configs. The one thing that did break was downstream of them:
`manifests/describe.py` built its alternatives line as a raw f-string, the only description in that
renderer skipping `plain_text`, which nothing had noticed because capability arms carry plain
one-line summaries while a plain union falls back to a docstring.
