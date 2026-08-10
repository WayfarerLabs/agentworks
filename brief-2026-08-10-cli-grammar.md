# Task brief: the resource-CLI grammar break (2026-08-10)

From the saga lead, via the branch-seeded brief mechanism. You own this branch from pickup; the saga
lead reviews; the operator merges. Delete this file before the PR goes ready.

## Charter

Operator rulings (2026-08-09/10): the resource CLI gets its grammar break in the 0.14 window, when
breaking is cheap and the surface freezes for new eyes. This is a full SDD effort (FRD, HLA, plan,
phased artifact review; saga `docs/sdd/2026-08-04-next-steps/`). The end state gives every command
one crisp sentence: `explain` = the kind, `describe` = the thing, `graph` = the wiring, `list` = the
census.

1. **`agw resource describe-kind` becomes `agw resource explain`.** Same command, honest name, and
   the kubectl precedent means users arrive with the right intuition (`kubectl explain` is exactly
   this: the field reference for a type, working without live state — ours already answers on a
   broken config). Shape the argument grammar so a future field-path drill-down
   (`agw resource explain secret.env`) can land without another break; do not build the drill-down
   now.
2. **`agw resource describe KIND/NAME` is rebuilt as the kind-aware card** (operator ruling,
   2026-08-10, option B). It answers "what is this thing, where is it declared, is it healthy":
   identity, description, declaring file:line, origin/provenance, readiness — plus kind-specific
   facts contributed through a per-kind detail renderer registered where kinds already register
   their config models. Two consumers exist on day one: the secret card absorbs
   `agw secret describe`'s read-only content (backend mappings, inheritance chain), and the env
   scope detail follows the same seam. Every relationship section ("Referenced by", "Used by") moves
   out; one pointer line hands the reader to `graph`. **The kind-specific spelling stays** (operator
   ruling, 2026-08-10): `agw secret describe NAME` remains, as thin sugar over exactly the same card
   renderer as `agw resource describe secret/NAME` — the noun-group idiom (`vm describe`,
   `workspace describe`) is the dominant grammar here, and for a kind with no instances the resource
   card IS its describe. What retires is the divergent bespoke implementation, never the entry
   point: one renderer, two spellings, zero drift, with a test pinning both spellings to identical
   output. Heavyweight kind operations that touch backends (`agw secret verify`, resolution
   previews) stay kind commands.
3. **A new top-level `agw graph` owns every relational view.** One command, the focal point as the
   argument: bare `agw graph` renders the whole graph; `agw graph KIND/NAME [KIND/NAME...]` renders
   the subgraph involving those nodes; `--kind a,b` renders the induced subgraph on kinds. Nodes
   span declared resources AND live instances (session/workspace/vm) with the uniform `KIND/NAME`
   grammar — the chains that cross that boundary (session → workspace → vm → template → secret) are
   the command's whole value. Axes: `--up`/`--down`/`--both` (dependents vs dependencies),
   `--depth N`, `--format` with a tree default on a TTY. Propose the format set in the FRD with a
   stated consumer per format (tree for humans and json for tooling are obvious; dot/mermaid only if
   you can name who renders them). Leave room for a future path query ("why does A reach B") without
   building it.
4. **Unify `--write`.** `resource sample --write FILENAME` takes a value while
   `resource schema --write` is a bare flag with a fixed destination — same spelling, different
   semantics. Pick one meaning and make both conform.
5. **CLI-hygiene sweep (survey-first).** R1 inventories the full `agw` surface for convention drift
   and proposes dispositions before changing anything. Known starters from the saga lead's audit:
   exit-code conventions are split across command groups; the `--write` item above; flag naming
   oddities of the `--grant-all-workspaces` sort. Bundle what is cheap and clearly right into this
   break; disposition the rest explicitly (defer loudly or decline with a reason).

## Constraints

- **Scope discipline.** Three efforts in this saga were scope-corrected for machinery no owner
  priced; read `target-state.md`'s "Requirements are priced like code" before designing. For this
  effort concretely: the per-kind detail renderer ships with exactly its two day-one consumers (no
  speculative hooks on other kinds); every graph format and option needs a named consumer; if a fix
  round grows the diff instead of shrinking the problem, stop and raise it to the saga lead.
- **Breaking posture** (per the saga's compatibility rulings): no compat aliases, no warn window.
  Removed spellings fail as unknown commands; `describe`'s changed output is a documented behavior
  change. The upgrade guide (`docs/guides/upgrading-to-0.14.md`) and the guide topics that teach
  these commands ride the same PR that makes them true. This lands **before the 0.14.0 tag or not at
  all until the next breaking window** — it must never land just after the cut.
- **Completions are first-class work** (standing rule): a new top-level command, a rename, and a
  retirement all reshape the completion tree; the dynamic-element merge must cover `graph`'s node
  arguments.
- **Coordination:** the safer-migrations effort (`agw-safer-migrate`) will propose a command home
  for backup/restore in its FRD — this effort owns the grammar they land in; converge via the saga
  lead. The installer-plugins effort adds no CLI surface (verified in its design review), so no
  collision there. Build the graph command on the machinery at HEAD (`resources/inspect.py`, the
  frozen graph, wave 2's descriptor dispatch) — read it, not this brief.
- Saga vocabulary throughout; message-signatures and `Agentworks-Session` trailer rules apply; the
  always-consider rules (docs, sample config, completions, SDD artifacts) apply as ever.

## Definition of done

`explain`, `describe`, `graph`, and `list` each do one thing and say so; the kind-specific describe
spelling is retired with its content absorbed; all edges live in `graph` across resource and
instance nodes; `--write` means one thing; the hygiene sweep's dispositions are recorded; upgrade
guide, guide topics, command reference, and completions are in lockstep; gates green; SDD locked
truthfully; ships inside the 0.14 window.

-- agw-next-steps (saga lead session)
