# Simplification Pass: High-Level Architecture

This effort ships subtraction. The architecture is two doctrines that serve as the deletion
criteria, and a wave sequence that keeps every merge green and locally judged. There is no program
topology: no LLD gates, no stacks, no central coordination beyond wave ordering.

## Doctrine 1: validate at boundaries, trust the interior

The authority is the `development-principles` rule, principle 3, which carries the five trust
boundaries, the who-can-call-this provenance test, interior trust, and the convention that a
surviving validator names its boundary in its docstring. R2.1 gates wave 1 on that list. What
follows is only what this codebase adds to it.

Where those boundaries land here: provider SDK responses, subprocess output (`op`, `tailscale`,
git), PyPI, and GitHub content (per the `github-input-trust` rule) are the external-process
boundary. Database files, backups, restore inputs, and any operator-selected file cross executions,
so `inspect_schema`'s classification and backup qualification are boundary work and stay.
`verify_secrets` is the standing example of the caller-provenance boundary, reached by a tester's
script with a bare string where every in-repo caller passes the enum; that is operator ruling 1
applied to provenance, designing the boundary against the real channel when the channel exists
rather than holding one open for a channel that might.

Wave 1 applies the rule as a gate: classify the input's provenance, then delete only the
clearly-interior validators.

On the future external-plugin boundary: dynamically loaded third-party code is outside our mypy run,
and importing it executes it, so the seam that stays is registration-time constructibility and
call-shape compatibility (the keyword names and kinds the framework actually calls with). Nothing
more is designed, cleaned, or promoted in this pass; the full boundary is designed against the real
loader and contribution channel when that effort starts, with the findings and this sketch as seed.

## Doctrine 2: tests assert behavior; agreement is derived

The unit of protection is the invariant, never the spelling of an authored artifact (prose, config,
workflows, CSS tokens, our own source shape). Shapes wave 1 deletes and must not regenerate: source
assertions that pin the spelling of our own code, verbatim pins of authored files, required-phrase
lists, wording blacklists, mutate-one-string-assert-raises loops. Shapes it keeps: exit codes and
error types, derivation parity against a canonical source, structural presence, thresholds,
injection and redaction defenses, and boundary guards as defined below. Replacements are the
exception, added only for a real invariant that can actually regress, and never as a new production
contract whose only consumer is a test.

### What a source-scanning guard protects, not how it inspects

Source-scanning guards are a large family; [sweep-inventory.md](sweep-inventory.md)'s group 6, not
this doctrine, carries the file list and the per-guard decision. They do not sort as one shape
(operator ruling 9), and the always-on `no-prose-policing-tests` rule draws the line that decides
them: its target is "the spelling of our own source code". Inspecting source with `ast` is a
technique, not a smell; what decides is what the assertion protects.

- **Keep** a guard enforcing a boundary the type system cannot express, that an ordinary edit can
  regress: import and layering boundaries, consent confinement
  (`guide/test_power_import_boundary.py` is the standing example, forbidding the guide package from
  reaching `subprocess`, sockets, or secrets), and drift against a canonical source.
- **Delete** a guard pinning how our code is written rather than what it may reach: identifier
  spellings, call-graph shape, statement order. The `phase7` corpus was this family, and so are
  three of the four banned patterns in `resources/test_graph_guard.py`: the `dependencies()`
  re-walk, the lazy readiness recompute, and the `references` field. Each says which call shapes may
  appear in which module.

**Corrected 2026-08-16** (raised by the sweep's decision inventory, accepted by the effort lead): an
earlier revision of that second bullet named the whole of `resources/test_graph_guard.py` as the
example, and it was wrong on its own criterion. The file's fourth banned pattern, a capability
registry probed outside the builder and publishers, governs **what a module may reach** rather than
how the reaching is spelled, which puts it in the keep bullet beside the import-boundary guard. It
is also the one guard here with no observational twin available even in principle: a consumer that
re-derives the graph correctly agrees with it until the two diverge, so no behavioral test can
distinguish a second derivation from the first. That detector and its allow-list stay; the other
three go. An example that contradicts its own doctrine teaches the error to everyone who reads it,
which is why this is corrected in place rather than left to the inventory.

"Statement order" there means order pinned **lexically**, by reading the source. Order often matters
behaviorally, and asserting its consequence is not the same shape: PR #523 hoisted a policy check
above the work it guards and pinned that with tests asserting no row was deleted and no SSH config
rewritten. Those never read the source, they fail only when the order actually breaks something, and
they are exactly the observational twins the paragraph below prefers. Delete the pin that says a
statement comes first; keep the test that says what goes wrong when it does not.

Some guards read as both bullets at once, protecting a genuine behavioral property through
structural inspection. There a structural guard yields to an observational twin wherever one exists
or is cheap to write, and stays until then. PR #523 set the precedent when it deleted a lexical
Tailscale-ordering pin whose property observational tests already covered.

### `match=` splits three ways

It appears at 663 sites, and the criteria above do not decide it on their own (operator ruling 10).
Deleting it wholesale drops real branch coverage; preserving it by adding a production discriminator
is exactly what R2.2 forbids. So:

1. The raised type already discriminates: **delete** the `match=`, keep the `raises`.
2. The match is the only thing distinguishing same-type branches of one function: **discriminate
   structurally** where the code already offers a handle, meaning the exception type, an exception
   attribute, or the cause chain, and assert on that instead. Where our own authored wording is
   genuinely the only discriminator, **delete the assertion and accept the reduced branch
   coverage**, which is what R2.4 directs; R2.2 forbids adding a production discriminator to
   preserve it.
3. It pins a sentence for its own sake: **delete**, per R2.4.

No case licenses matching wording we author. `no-prose-policing-tests` permits pinning a token only
where the prose arrives from outside the repository, so a `match=` against a provider's or an
upstream tool's error text is the one surviving form; every message this repository writes is
authored prose, error messages included.

**Corrected 2026-08-16** (sweep inventory, verified at HEAD). Two numbers here were wrong. The count
was 696 and is 663, the wave 1 landings having taken the rest; a textual grep answers 664, one of
which is a docstring mentioning `match=` rather than a site. And this taxonomy is keyed on a pytest
spelling the website suite does not use: `website/tests` is `unittest` and carries **51
`assertRaisesRegex` sites**, which are the same three cases and which a `match=`-keyed scan misses
entirely. Read every rule above as governing both spellings.

Case 2's "where the code already offers a handle" turned out to be the common case rather than the
rare one, which is worth stating because it sized a whole batch. Two handles already exist in
production and neither needs a change: `AgentworksError` carries `entity_kind` and `entity_name`
(populated at 286 raise sites, already asserted on at 75 test sites), and `schema.errors._problems`
exposes `path`, `unknown_field` and `alternatives` and is already imported by
`cli/tests/schema/test_errors.py`. Check for a handle before falling through to the delete arm, and
check that it DISCRIMINATES: the platform-config family carries `entity_kind` identically on all
sixteen sites, so the handle is present and useless there.

## Guidance delivery

Wave 0 first resolves rule delivery (issue #511), then amends the two existing rules
(`development-principles`, `no-prose-policing-tests`); no new files, personas, or delivery
mechanisms. The expected delivery resolution is subtraction-shaped: the `globs`/`paths:` frontmatter
is what forces lazy loading, so after the probes confirm the emission shape, the twelve broad
always-on rules drop the filter and load eagerly, as the frontmatter-free `always-consider-*` rules
already do. Wave 0 completes on one of two measurable branches (FRD R1.3): verified unconditional
delivery, or an operator-approved fallback that places the full criteria text into every affected
lane; a citation alone cannot supply the contents of a rule a target never loads. Wave 1 delegation
charters cite the two amended rules regardless, which costs a sentence per charter.

## Waves and vehicle

- **Wave 0**: the delivery resolution plus one small amendment PR, merged first (FRD R1).
- **Wave 1**: independent, contained deletion work off main, each item judged locally against the
  two doctrines, each PR green on the full suite. No ordering between items; PR batching per the
  plan. Precedes the CLI grammar rewrite (saga `phasing.md`).
- **Wave 2**: process and rule subtraction PRs under the net-deletion constraint, in parallel with
  wave 1 on its own session (file-disjoint: `.rulesync/` and the skills tree versus `cli/` and
  `website/`).
- **Reassess**: waits for both waves and for the CLI grammar rewrite landing (the saga's
  `phasing.md` puts the rewrite between wave 1 and this reassessment, so the effort cannot close or
  lock early); the lead writes the reassessment and the candidate proposals; the operator decides
  what, if anything, is promoted.

## Risks

- **Deleting a validator that was quietly load-bearing.** Mitigation: the provenance gate (R2.1)
  plus the full suite on every PR; where a real invariant loses its only guard, the replacement
  lands in the same PR (R2.3).
- **Scope creep back into redesign.** Mitigation: R2.2 is a hard stop; anything needing a new type,
  contract change, or design pass is set aside for the reassessment rather than absorbed into a
  wave.
