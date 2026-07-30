# LLD (b): the finalize pass ordering

Implements HLA [component 3](./hla.md) (and the guard from component 7). Owns the finalize pass
order, the auto-declare hard-error sub-branch, the co-location of suppression-removal with
readiness-gating, the materialization fixpoint, the partial-readiness `secret describe` question,
and the anti-bypass guard's banned-pattern definition. Governs FRD R8, R12, R11 (guard), and the
atomic phase-3 landing.

## The ordered passes

`finalize` becomes these passes over the single retained graph (LLD a). Today's order is materialize
reserved-defaults, worklist loop (with inline miss dispatch), attach references + polish, cycle
detect, freeze (`registry.py:165-265`). The target order:

```text
0. reserved-defaults:  always-materialize reserved-default names (unchanged, registry.py:227)
1. build:              walk every declared row's dependencies(context) (total, non-throwing)
                       into the outbound edge map. No validation, no throwing on a bad block.
2. resolve:            for each edge whose target has no node, dispatch the miss policy:
                         - "error"                                  -> hard error now (absent = typo, R7)
                         - "auto-declare", name NOT in auto_declare_names -> hard error now (ungated)
                         - "auto-declare", allowed name             -> DEFER to materialize (pass 5)
3. cycle-detect:       three-coloring over the BUILT edge map (no re-derivation; fixes registry.py:542)
4. readiness-fold:     reverse-topological; hand each node its deps' DependencyState; store per node
                       (LLD c). A deferred allowed-name auto-declare target counts as satisfied.
5. materialize:        synthesize each deferred auto-declare target as a READY, ENABLED node the
                       reference set requires (R12: a target referenced ONLY by not-ready/disabled
                       nodes does not materialize). THEN walk each newly-materialized node's
                       dependencies(context), resolve its targets, and fold it -- LOOP until no new
                       node appears (the fixpoint).
6. attach:             inbound references + description polish, onto the graph
7. validate:           validate(config) over the ready + enabled set only (throwing, file:line)
8. freeze
```

## Subtlety 1: the miss policies do not partition into "hard vs gated" cleanly

Today an auto-declare miss for a name **not** in `auto_declare_names` is an eager hard error
(`registry.py:337-343`, e.g. a typo'd `inherits = ["defualt"]`). That hard error must stay
**ungated** in pass 2, or a typo referenced only by a not-ready node would be silently dropped (a
delta **not** in R9). So pass 2 splits the auto-declare branch:

- `auto-declare` + name in `auto_declare_names`: defer to pass 5 (readiness-gated).
- `auto-declare` + name **not** in `auto_declare_names`: hard error **now**, exactly as today,
  regardless of the referrer's readiness.
- `error`: hard error now (this is the absent = typo case; the vm-site suppression that used to hide
  it is removed this phase).

Only the **allowed-name** auto-declare miss is readiness-gated. The `"error"` policy and the
disallowed-name auto-declare stay eager and ungated.

## Subtlety 2: readiness must be known before materialization (the R12 ordering)

R12 says a not-ready or disabled node contributes no materialized dependencies (the registry does
not predict-resolve the secrets of something that cannot run here). Readiness is known only after
the fold (pass 4), but materialization (pass 5) is what synthesizes auto-declared secrets. Hence the
order: **fold before materialize**. Pass 5 materializes a deferred target only if at least one
**ready, enabled** node references it; a target referenced solely by not-ready/disabled nodes is
left unmaterialized (its would-be secrets never appear in `secret list`/doctor/resolution,
preserving today's suppression behavior).

This is the exact behavior the vm-site edge-suppression achieved by a different mechanism (job 2: it
withheld a can't-run site's edges so its secrets never entered the worklist). Removing the
suppression (HLA component 5) and adding readiness-gated materialization are therefore **the same
atomic change**; see "Why phase 3 is atomic" below.

## Subtlety 3: a materialized node is NOT a leaf, so materialize must loop (load-bearing)

An auto-declared `secret` (`tailscale-auth-key`, the `git-token-*` family) has outbound
`secret -> secret-backend` edges (LLD d, HLA component 2). If pass 5 synthesized it without walking
those edges, `edges_of(secret)` would return nothing and resolution would find zero candidate
backends, so every VM create would fail to resolve its tailscale key. So pass 5 **loops**:
synthesize, then walk the new node's `dependencies(context)`, resolve its targets (pass-2 policy),
and fold it (pass-4 rule), until no new node appears.

**Termination premise**, stated precisely: a late-materialized node's out-edges target **only
already-present, already-folded nodes**. For an auto-declared secret this holds because its edges
target backend nodes (built-in `secret-backend` rows, always present and folded before any secret
materializes). The premise is **not** the false "secrets have no edges" (an earlier draft's error);
it is that the materialization frontier only ever points back into the settled set. The loop asserts
progress (each iteration adds >= 1 node or stops) and has a hard iteration cap equal to the node
count as a safety net (a cap hit is a framework bug, raised loudly, not silently truncated).

**Two invariants a future late-materialized kind must honor** (recorded after the phase-3 review, so
they are not rediscovered when a kind beyond `secret` gains `miss_policy = "auto-declare"` with real
out-edges): (1) **cycle-checking**, cycle detection (pass 3) runs before materialize (pass 5), so
edges introduced by materialized nodes are **not** cycle-checked; today this is safe because the
only late-materialized kind (`secret`) has out-edges only to already-present backend nodes (which
cannot close a cycle back to the secret), but a late-materialized kind whose out-edges could form a
cycle must re-run cycle detection over the grown graph. (2) **fold ordering within a materialize
pass**, the pass folds `sorted(deferred)` targets; if a deferred target ever depended on **another
deferred target** (rather than on already-folded present nodes), the sorted walk could fold the
first before the second's `DependencyState` exists, and it is never re-folded, producing a stale
verdict. Today this cannot happen (a materialized secret's deps are present backends, not other
deferred targets); a future kind that breaks the "frontier points into the settled set" premise must
topologically order the walk or re-loop until each target's own deps are settled.

## Subtlety 4: the partial-readiness `secret describe` question

When a **separately-materialized** secret is referenced by both a ready node and a not-ready node,
does the not-ready referrer's edge still contribute to the secret's inbound `dependents_of` (and
thus its `describe` "Referenced by" output)?

Decision: **yes, the inbound edge is recorded regardless of the referrer's readiness.** Readiness
gates **materialization** (does this node come into existence), not **reference attribution** (who
points at an already-existing node). Once a secret exists (because a ready node needs it, or because
the operator declared it), every edge into it, from ready and not-ready referrers alike, is a true
structural fact and appears in `dependents_of`. This matches today's behavior for an
operator-declared secret referenced by a disabled site (the reference is real; only the
auto-declaration of a site-only secret is suppressed). The gate is "does a not-ready node cause a
**new** node to materialize" (no), not "does a not-ready node's edge count" (yes, if the target
exists for another reason).

## Why phase 3 is atomic

The vm-site edge-suppression (`sites.py:60-71`) does two jobs: (1) avoid the platform kind's
`"error"` miss when the platform is absent or host-unsupported, and (2) keep a can't-run site's
config-implied secrets from auto-declaring. R13 (unconditional publication) replaces job 1 (the
platform node is always present, so the edge always resolves). R12 readiness-gated materialization
replaces job 2. Landing the suppression removal without **both** replacements opens a non-green
window:

- Remove suppression before R13: the bundled `wsl2` site's platform edge dangles on non-Windows
  hosts and hard-errors every command.
- Remove suppression before R12 gating: a host-disabled site's secrets materialize (R12 regression,
  they appear in `secret list`).

So R13, the fold, R12 gating, and suppression removal are **one commit** (plan phase 3).

## The anti-bypass guard (R11)

The guard is a test (`tests/.../test_graph_guard.py`) pinning that the banned patterns cannot
return. Its baseline is the caller inventory's guard section. This LLD defines the banned patterns
precisely enough to be a real test:

**Banned:** re-deriving the graph's structure or readiness **outside the build**:

1. Re-walking resources' `dependencies()` to reconstruct the edge set (was: cycle detection, `walk`,
   node factories). Detection: an AST/grep guard that no module **outside** `resources/graph.py` and
   `resources/registry.py` (the builder) calls `dependencies(` on a resource for edge
   reconstruction, plus a behavioral assertion that cycle detection, `collect_secrets_for`, and the
   node factories read the graph query API.
2. A `*_REGISTRY.get(...)` **availability probe** in edge production or readiness (was:
   `VM_PLATFORM_REGISTRY` in `site_disabled_reason`, `SECRET_BACKEND_REGISTRY` in the resolver).
   Detection: an AST guard that the four capability registries are not read outside the **sanctioned
   reader set** (below). The banned pattern is a _consumer probing the live registry to decide
   availability_; a registry read is honest when it is one of these classes, each verified during
   the Phase 6 review as a class lookup or pre-graph validation, not an availability probe: (a) the
   **publishers** (own/iterate the registry to publish rows); (b) the **graph builder + fold**
   (`resources/graph.py`, stamping impls via `_impl_for` and calling the non-constructing
   `not_ready`); (c) **edge production and the finalize `validate` pass** fetching the capability
   _class_ to extract config-implied edges or validate the owned blob (`vms/sites.py`,
   `git_credentials/credential.py`, `sessions/template.py`, `secrets/base.py`), a host-agnostic type
   lookup, and during the pass that _produces_ the graph there is no node to read from yet; (d)
   **op-time capability construction** to run an operation (`git_credentials/__init__.py`,
   `vms/initializer/credentials.py`, and `resolve_site` after `ensure_site_ready` already read
   `readiness_of`); (e) **pre-graph decode/load/migrate validation** (`manifests/decode.py`'s
   shadow-name membership test, `config/loaders_secrets.py`'s deprecated `[secret_backends]` shape
   check, `migrate/planning.py`'s dry-run). The earlier "publisher + builder only" phrasing did not
   describe this final state; the guard's unit is the documented per-module allow-list.
3. A lazy readiness recompute instead of reading `readiness_of` (was: `inspect.disabled_reason_for`,
   `site_disabled_reason` callers, `doctor`). Detection: `not_ready` is called **only** by the fold
   and the graph builder; every projection surface reads `readiness_of`.
4. Reading inbound edges/usage off a resource dataclass `references` field (was: section E readers).
   Detection: the `references` field no longer exists on the dataclasses (a structural assertion),
   so any reader fails to compile/type-check; the guard additionally asserts no
   `getattr(_, "references")` remains.

**Exempt (whitelisted), or the honest path trips the guard:**

- A capability computing **its own** config-implied refs from **its own** config via
  `dependencies(config)` at construct (`capabilities/base.py:306`, for `_secret_refs`). This derives
  one node's refs from its own config; it does not re-walk the graph. The guard whitelists the
  construct-time call site by module + method.
- The graph **builder** handing a resource's `dependencies(context)` a controlled context (the
  available-backend list a `secret` reads, HLA component 2). This is a builder input during the
  build, not a consumer reaching into the live registry. The guard whitelists the builder's own
  call.
- The graph builder's **`_impl_for`** helper (`resources/graph.py`) reading the four capability code
  registries (`VM_PLATFORM_REGISTRY` and peers) to stamp each capability node's impl, and the
  **fold** (`fold_readiness` / `node_readiness` / `_capability_node_readiness`, same module) reading
  that impl to call its non-constructing `not_ready`. These run _during the build_ and are the
  sanctioned builder-reads-registry path (Phase 3 landed the fail-fast `_impl_for`); LLD a/c phrase
  the fold as reading the impl "off the graph node," which is the same object by construction. The
  guard whitelists `resources/graph.py`'s builder + fold functions (by module) so its
  banned-pattern-2 scan (no `*_REGISTRY` read outside the sanctioned reader set in pattern 2 above)
  and banned-pattern-3 (`not_ready` called only by the fold + the vm-site's fold hook) do not trip
  on the honest path. The AST detectors match qualified (`mod.REGISTRY`) and aliased-import reads,
  not only bare names, and the `references`-field detector matches annotated, plain, and property
  declarations; the residual limits (whole-file allow-list scoping; deep call-through-a-variable
  indirection) are documented in the guard test itself.

The guard must encode **all** these exemptions, or the sanctioned single-derivation and the banned
re-walk are indistinguishable calls. The guard lands in phase 6, after every bypass is migrated
(phases 4-5), so it goes green the moment the last consumer moves.

## Acceptance

- The pass order is exactly 0-8 above; a test asserts fold-before-materialize (R12) and
  cycle-before-fold (the fold needs an acyclic graph).
- R9.3: a config with both a malformed capability block and a cycle reports the **cycle** first (the
  malformed block no longer fails at decode).
- R12: a host-disabled site's config-implied secret does not materialize; a ready site's does; a
  secret referenced by **both** still materializes and its `dependents_of` includes the not-ready
  referrer (subtlety 4).
- The materialize loop resolves an auto-declared `tailscale-auth-key`'s backend edges (a VM-create
  fixture resolves the key; the earlier no-loop regression is pinned against).
- The guard test fails if any banned pattern is reintroduced and passes with both exemptions
  present.
