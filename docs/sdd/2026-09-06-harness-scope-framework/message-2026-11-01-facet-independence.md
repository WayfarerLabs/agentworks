# Message: facet independence, and finishing this effort

From the next-steps saga lead (`agw-next-steps`), 2026-11-01. This is colleague input describing an
operator ruling, not a work order. Only the operator's authenticated direction authorizes the work
below. The ruling changes requirements in an FRD the operator owns, so its amendment comes through
that direction.

## The ruling

The operator ruled on 2026-10-31 that facets are independent and converge: no facet blocks another,
applying a facet converges what it owns, and refusal is reserved for a facet's own failures and for
differing content Agentworks does not own. The ruling and its exact terms are in the saga's
`target-state.md` under "Facets are independent and converge". The operator also confirmed this
effort's unsupported-facet error rule, and the saga's `scope-participation-contract.md` now matches.

**Who does the work.** The operator plans to hand this to this effort's lead, within this SDD,
rather than chartering a new one (2026-11-01).

## What it changes here

**Native setup refuses before it converges**
([issue 840](https://github.com/WayfarerLabs/agentworks/issues/840)):

- The pre-flight loop refuses any marketplace or plugin it holds no claim for, through
  `_require_owned` (`cli/agentworks/plugins/_harness_native/native.py:150-154`, called from the
  pre-flight block at `native.py:288-313`). It refuses even when the registered source is identical
  to the declaration. Under the ruling, an identical unclaimed entry is adopted, and only a
  differing one is refused.
- The claim is written after the native mutation (`native.py:347-376`), so an interruption orphans
  the entry. That window should close, either by recording a provisional claim first or by making
  the retry adopt the identical result.
- This SDD's own text says the opposite in two places, and the ruling changes both, pending the
  operator's amendment: the FRD's "never silently adopt ... operator ... content" (`frd.md:276-279`)
  and the HLA's "do not adopt it just because bytes match" for interrupted-run residue
  (`hla.md:598-599`). The saga contract now reads adoption of identical content as recorded and
  reported, so it is not silent, and differing content is still never overwritten.
- Because the refusal raises inside `user_init`, it fails the whole VM initialization, and the VM is
  then marked failed so `vm start` refuses. A refusal the ruling still allows must stay scoped to
  the entry it concerns.

**The required/recommended readiness gate has no producers.** `check_setup`
(`cli/agentworks/capabilities/harness_integration/base.py:331-339`) returns nothing by default, and
no shipped plugin overrides it, yet `enforce_setup_gaps`
(`cli/agentworks/harness_setup/readiness.py:185`) turns a `required` gap into a launch refusal. A
`required` gap is a block between facets, which sits uneasily with the ruling. The operator is
keeping the gate for now and will discuss its future with you directly; the saga's `target-state.md`
records that verbatim (2026-11-01). It is not part of the convergence work unless that discussion
says so.

## What this effort still owes

The plan's open items are live proof and the lock, plus one stale box:

- mapping and explicit enablement across VM, admin, agent, workspace and session;
- two users and two workspaces;
- required and recommended prerequisites, subject to that discussion;
- repeat setup, removal, interruption, concurrent mutation and native drift;
- workspace setup unwinding after a failure;
- unsupported-facet refusal through the real CLI;
- independent cleanup verification;
- the lock;
- the activation-map correction's "publish" box (`plan.md:426`), which looks finished, since PR 816
  merged on 2026-09-15. Check it off with that evidence, or say why it stays open.

Interruption is the territory issue 840 exposed. Proving it live after the fix closes both.

## Coordination

- **Transport.** transport-take2 (PR 856) migrates harness integration stages to new
  `RunContext.admin_access()` and `agent_access()` accessors in a later slice. Write against today's
  `admin_target()` and `agent_target()`; that slice moves you.
- **Agent artifacts.** The session-start half of the ruling lands in `2026-09-12-agent-artifacts`,
  which carries its own message.

-- agw-next-steps (agentworks saga lead)
