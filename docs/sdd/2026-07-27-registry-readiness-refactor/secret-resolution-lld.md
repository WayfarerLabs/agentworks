# LLD (d): the secret-resolution layer over the graph

Implements HLA [component 6a](./hla.md) (and the resolution parts of components 2 and 6). Owns the
graph read, the present/enabled/ready/opted-in walk, the eager fail-fast reachability check,
batching, and the op-time held-capability refs decision. Governs FRD R9.6, R9.9, R9.10, R9.11, R11.

**Acceptance line (load-bearing, called out first because a rewrite drops it):** this layer
**preserves** (a) the operator-declared-only reachability scope, (b) the **would-attempt (not
readiness)** keying of the build-time reachability check, and (c) the soft-miss / hard-miss halt
semantics. The only intended deltas are R9.6 (not-ready backend skips with a warning and falls
through), R9.9/R9.10/R9.11 (the `secret -> secret-backend` edge consequences), and reading the graph
instead of re-deriving.

## Two separate concerns, two separate times

1. **Build-time reachability** (eager, fail-fast, at `build_registry`): "every operator-declared
   secret is resolvable by some opted-in backend that would attempt it." Stays a post-finalize
   boundary check (today `validate_chain`, `resolve.py:108`). Keyed on **would-attempt, readiness-
   blind** (a secret whose only opted-in backend is not-ready is still reachable; it fails only at
   resolution, exactly as today). This introduces **no new build-time hard failure** from readiness.
2. **Resolution-time** (lazy, at use): walk the candidate backends and get values. This is where
   readiness gates (R9.6): a not-ready opted-in backend is skipped with a warning.

Keeping these distinct is the crux; conflating them (making build-time readiness-aware) would break
the "readiness introduces no new build-time failure" invariant.

## The `secret -> secret-backend` edges (recap from HLA component 2)

The secret's `dependencies(context)` emits a `secret -> secret-backend` edge for the union of:

- (a) **every present backend that would attempt it** (`would_attempt(secret, mapping)` true: has a
  mapping, or is mapping-optional), minus an explicit `false` opt-out; and
- (b) **every explicit non-`false` mapping key** the secret names, even a name with no present
  backend (this is what makes a typo'd key a dangling edge that the `"error"` miss policy
  hard-errors, R9.11).

`would_attempt` is a **pure function of `(secret, mapping)`** with no host probing, so freezing it
into edges at finalize is safe (the `SecretBackend` contract constraint). `edges_of(secret)` is
therefore the full candidate set (plus any dangling typo edge); an auto-declared secret's edges
correctly include the default backends (env-var, prompt). The builder hands the secret the
**available-backend list** via the uniform build `context` (LLD a/b; guard-exempt builder input).

## `active_backends`, reading the graph

`active_backends(config, registry)` (`resolve.py`) stops probing `SECRET_BACKEND_REGISTRY`. It now:

1. Reads the opt-in chain from `secret_config.backends` (the one resolution-layer config input,
   order and selection).
2. For each opted-in name, finds its `secret-backend` graph node and reads the **impl off the node**
   (LLD a; secret-backend impls are instances) and the **stored readiness** (`readiness_of`).
3. Filters to **present ∧ enabled** (a node exists and is enabled). The result is the ordered active
   backend list, each entry carrying its impl and its stored `Readiness`.

Readiness is **read, never re-checked** (R11). An opted-in name with no present backend node is a
config error surfaced here with config vocabulary (as today a chain naming an unknown backend is).

## The resolution walk (`resolve_secrets`)

The existing loop shape (`resolve.py:211`) is preserved: outer loop over backends in chain order,
each backend resolves the still-missing would-attempt subset in one `batch_get`, the next backend
sees only what remains. Batching is preserved (one `batch_get` per backend). The candidate set per
secret is the graph edge set, which equals `would_attempt` by construction, so the existing
`would_attempt`-keyed `attemptable` filter is unchanged in spirit; it may read the frozen edge
instead of recomputing, but the two agree.

**The one new gate (R9.6):** before a backend resolves, check its **stored readiness**:

```python
for index, backend in enumerate(backends):
    if not missing: break
    if not backend.readiness.is_ready:
        # skip with a warning, per still-missing secret this backend would attempt; fall through
        for s in missing:
            if backend.would_attempt(s):
                output.warn(f"secret {s.name}: skipping {backend.name}, not ready: {backend.readiness.reason}")
        continue
    # ... issue #202 early-doom check (readiness-aware, see below) ...
    # ... existing attemptable / batch_get / soft-miss / hard-miss logic, unchanged ...
```

- A **not-ready** opted-in backend is **skipped with a warning** and the chain falls through to the
  next candidate (possibly `prompt`). Never silent. **Delta vs today:** a mapped-but-unavailable
  store (e.g. `onepassword` with no `op`) raised `ConnectivityError` and **halted**; now it warns
  and falls through (R9.6).
- A **soft miss** (present, ready backend returns without the value) falls through, **unchanged**.
- A **hard miss** (`SecretMappingError`: a present, ready store definitively has no value) or a
  transport/auth failure still **halts** the chain, **unchanged**, preserving the anti-masking
  property (a usable store that says "no" must not be masked by a later prompt). The halt is kept
  precisely because the store is **ready**; readiness is what distinguishes "skip, it can't run
  here" from "halt, it ran and said no."

### The issue #202 early-doom check goes readiness-aware

The "fail before an interactive backend prompts" check (`resolve.py:274-278`) computes doomed
secrets as those **no remaining backend would attempt**. Because resolution now **skips** not-ready
backends, a secret whose only remaining would-attempt backend is not-ready is likewise doomed. So
the early-doom check treats a **not-ready** backend as **non-attempting**: `remaining` is filtered
to ready backends before the `would_attempt` test. This keeps the "spare a wasted prompt"
optimization honest under skipping, and it is the same readiness-aware predicate the external
`preview_resolution` predictor adopts (LLD e / HLA component 6), so prediction and the walk never
disagree. This is a **resolution-time** refinement only; it does **not** touch the build-time
reachability keying (still would-attempt, readiness-blind).

## The build-time reachability check (the split of `validate_chain`)

`validate_chain` splits:

- **Per-mapping spec validation** moves **out** of `validate_chain` into the finalize `validate`
  pass (via the secret's own `validate`, HLA component 2): every declared mapping addressed to a
  **present, enabled** backend is validated via that backend's `validate(mapping)` (R9.9, now every
  declared mapping, not just opted-in ones). A `false` opt-out is loop-owned and never validated; a
  mapping to an **absent** backend is the dangling edge the `"error"` policy hard-errors (R9.11,
  never validated); a mapping to a **disabled** backend is inert (not validated until enabled).
- **The reachability check stays** an eager post-finalize boundary check at `build_registry`, now
  reading the graph: for each **operator-declared** secret, `edges_of(secret) ∩ opted-in` must be
  non-empty (some opted-in backend would attempt it). Scope and keying preserved: operator-declared
  only (an auto-declared secret cannot invalidate a deliberate `backends = []` opt-out; it surfaces
  at use-time as `SecretUnavailableError`), keyed on would-attempt, readiness-blind. The error
  message and hint are preserved (`resolve.py:155-167`).

## Op-time held-capability secret refs (the decision the HLA deferred)

`Harness.secret_refs` (`harness/base.py:180`) and `GitCredentialProvider.secret_name`
(`git_credential/base.py:131`) read the construct-time `_secret_refs` cache (`base.py:306`).

**Decision: single derivation, not graph-threading.** The cache's source becomes
`dependencies(config)` (the same total function the graph is built from), so build-time edges and
op-time refs agree **by construction** (one function, not two divergent ones). This is the single
sanctioned op-time derivation and is guard-exempt (LLD b): it derives **one node's** refs from **its
own** config at construct, it does not re-walk the graph. Threading the frozen graph to op time is
possible but not required (the requirement is one truth, not two); it is rejected as unnecessary
coupling (op-time code would need the registry/graph handle threaded through every call site, for no
correctness gain over the shared function). A test pins that `secret_refs`/`secret_name` equal
`edges_of`'s secret edges for the same resource.

## What is deleted

- `validate_chain`'s chain re-derivation (the reachability half is rewritten to read the graph; the
  per-mapping half moves to finalize `validate`).
- The resolver's `SECRET_BACKEND_REGISTRY` probe (`active_backends` reads impls off the graph).

Not deleted: the construct-time `_secret_refs` cache (repurposed to `dependencies(config)`, above);
`collect_secrets_for` (rewritten as a `reachable_from` filter, LLD a); `vm_sites.validate_sites` (a
pure `defaults.site` lookup, name resolved against the graph).

## Acceptance

- R9.6: an opted-in `onepassword` with no `op` on PATH is skipped **with a warning** and the chain
  falls through to `prompt`; a ready store's hard miss still **halts** (both pinned by tests).
- R9.9: a stale/malformed mapping for a configured-but-**not-opted-in** backend now fails at
  `build_registry` (finalize `validate`), where today it lay dormant.
- R9.11: a `backend_mappings.<typo>` naming an unknown backend is a **hard error** at build (the
  dangling edge); the doctor granularity regression (a whole "Resource registry: FAIL" row instead
  of a pinpointed secret) is acknowledged and pinned.
- R9.10: a backend's `dependents_of` lists every secret that could resolve through it.
- Preservation: operator-declared-only reachability scope, would-attempt (readiness-blind)
  build-time keying, and soft/hard miss semantics are each pinned by a test that would fail if a
  rewrite dropped them.
- `secret` nodes remain always-ready (LLD c); resolvability is decided **here**, not in the fold.
