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
2. A `*_REGISTRY.get(...)` availability probe in edge production or readiness (was:
   `VM_PLATFORM_REGISTRY` in `site_disabled_reason`, `SECRET_BACKEND_REGISTRY` in the resolver).
   Detection: grep guard that `VM_PLATFORM_REGISTRY` / `SECRET_BACKEND_REGISTRY` / the other
   capability registries are not read outside their publisher and the graph builder.
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

The guard must encode **both** exemptions, or the sanctioned single-derivation and the banned
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
