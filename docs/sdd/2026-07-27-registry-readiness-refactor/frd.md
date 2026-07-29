# FRD: registry readiness refactor

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Start date: 2026-07-27.

## Summary

The resource registry fuses two concerns that should be separate: **constructing the resource
dependency graph** and **validating each resource's config**. A resource declares its graph edges in
`referenced_resources()`, which calls the capability's `validate_config()`, a method that does
double duty: it both validates the config block (and raises on bad input) and extracts the
config-implied edges (the secret a credential needs, and so on). Because the two are welded
together, the graph cannot be built without validating, config validation is forced across several
phases (two of them before a registry even exists), and a resource's place in the graph depends on
its config being valid.

Separately, the registry's vocabulary for "a resource that is present but cannot serve", the
`disabled_reason` hook, has become overloaded. It answers "why can't this run on this host" (a
readiness signal), but the word "disabled" is about to collide with a genuine enable/disable concept
arriving elsewhere in the system. One word carries two orthogonal signals: **"can't run"**
(readiness, a present resource blocked by its host or a dependency) and **"not enabled"**
(enablement, a unit the operator has not opted into).

This refactor **decouples graph construction from config validation**, makes the registry **produce
and retain a first-class dependency graph** as its product, computes readiness as a **dependency-
ordered fold stored on the graph**, and **renames the readiness hook** so "enabled/disabled" means
exactly one thing. It is behavior-preserving for a valid, enabled, ready configuration except for a
small set of intended changes (the readiness-vocabulary rename plus a few error-timing and
typo-handling deltas), which R9 enumerates.

## Motivation

1. **The fusion is the root cause of a class of defects.** Because building the graph runs the
   throwing `validate_config`, config validation is scattered across several phases (manifest decode
   and TOML load, both before a registry exists, plus the finalize walk and again at capability
   construction), a single malformed config block can prevent graph analysis entirely, and code that
   should ask "is this dependency available" instead asks "is its code present." These are not
   isolated bugs; they are the same weld showing through in different places. Untangling it once
   removes the whole class.

2. **The vocabulary overload will get worse, not better.** As soon as a real enable/disable concept
   exists in the system, `disabled` meaning "not ready" is a genuine collision that makes every
   reader disambiguate from context. Reserving "enabled/disabled" for opt-in, and giving readiness
   its own name, is cheap now and expensive after more code and docs accrete around the ambiguity.

3. **The timing is right.** The registry is a load-bearing subsystem that many parts of the CLI
   depend on. A downstream effort (the system-plugin work) is explicitly blocked on getting this
   model right; that PR (#237) is parked as design-only, with no registry-touching commits to
   rebase, so there is nothing pending that a registry-core change would conflict with. Doing it
   now, completely, is cheaper than piecemeal later under more constraints.

## The two problems, in depth

### Problem 1: graph construction is fused with config validation

A resource's `referenced_resources()` produces its outgoing edges. For a capability-config resource
(a `vm-site` naming a `vm-platform`, a `git-credential` naming a `git-credential-provider`, a
`session-template` naming a `harness`) it does two things in one call: it emits a bare edge to the
named capability, and it asks that capability to `validate_config(...)` so the config block's
_implied_ references (secrets and the like) can be attributed to the resource. `validate_config`
therefore serves two masters: it is the throwing correctness check for the config block, and it is
the extractor of config-implied edges.

Three consequences follow, all observed:

- **Validation is scattered.** Because edge-extraction (and thus `validate_config`) has to happen
  wherever a resource is first interpreted, capability config blocks are validated at
  manifest-decode time, at TOML-load time, at the finalize walk, and once more at capability
  construction (a documented construct-time invariant). The two earliest run before a registry
  exists; only the finalize walk has the graph in hand. The same block can raise at any of them, and
  which one wins is phase- and import-order-dependent.
- **A malformed block blocks graph analysis.** Since building the edge set runs the throwing
  validator, one bad config block aborts construction of the whole graph, so nothing downstream (the
  readiness walk, cycle detection, the rest of finalize) can run until every block is well-formed.
- **Resolution reads code-presence, not the graph.** Because the extractor consults the capability's
  live registry rather than the resolved graph, "is this dependency available" is answered by "is
  its implementation seated in the process," a different question with a different answer. The
  current code papers over the gap with a load-bearing workaround (below) rather than a model.

### Problem 2: "disabled" conflates readiness with enablement

The registry already has a clean, general notion of a resource that is present but cannot serve: the
per-kind `disabled_reason(registry, resource) -> str | None` hook. Its contract is host readiness:
"why can this resource not run on this host, or `None` when it can (or the kind has no such
concept)." A `vm-site`, for example, reports it cannot run when its platform is missing,
host-unsupported, or its bound instance lacks a local tool. A disabled resource still registers,
lists, describes, and holds references; only _using_ it is an error. This is a good model, poorly
named.

The problem is the name and the conflation. "Disabled" reads as "switched off," which is about to
mean a specific, different thing (an operator opting a unit in or out). Two orthogonal signals are
funneled through one word:

- **"can't run" (readiness):** a **present, enabled** resource blocked by its host, a missing tool,
  or a dependency that is itself unavailable. Graph-following. The resource exists and the operator
  wants it; circumstances block it.
- **"not enabled" (enablement):** a unit the operator has not opted into. This is a distinct axis
  from readiness, and it is _not_ the same as absence: a shipped unit the operator has not turned on
  is present-but-disabled (the system knows exactly what it provides), whereas only a typo or an
  uninstalled third-party unit is genuinely absent.

Collapsing readiness and enablement is what generated the confusion this refactor exists to end. The
target model separates them into three availability tiers (R7).

## Background (verified against current code)

- **`referenced_resources()` fuses edges and validation, asymmetrically.** `VMSite`
  (`vms/sites.py`), `GitCredentialConfig` (`git_credentials/credential.py`), and `SessionTemplate`
  (`sessions/template.py`) each emit a bare capability edge and call the seated capability's
  `validate_config(...)` to expand config-implied edges (which raises on a malformed block). But
  they differ in the miss case: `VMSite` **suppresses all its edges** when its platform is absent or
  host-unsupported (`sites.py`), while `GitCredentialConfig` and `SessionTemplate` emit the selector
  edge unconditionally, so an unknown provider or harness is a hard finalize-time miss error today.
- **The vm-site edge-suppression is a load-bearing workaround, with two jobs.** It exists to avoid
  the vm-platform kind's `"error"` miss policy when the platform is absent or host-unsupported, and
  (per its own comment) to stop a can't-run site's config-implied secrets from auto-declaring and
  predict-resolving. Both jobs must be replaced deliberately when the graph becomes total (R7, R12).
- **Finalize's cycle pass re-derives edges rather than reusing the built map.** `Registry.finalize`
  (`resources/registry.py`) runs a worklist loop that walks `referenced_resources()` into an
  accumulated reference map, dispatches each kind's miss policy (`"error"` raises "references
  unknown `<kind>` `<name>`"; `"auto-declare"` synthesizes), attaches incoming references, then
  detects cycles. Cycle detection (`_edges_from`) does **not** reuse the accumulated map: it
  re-calls `referenced_resources()` per node, a second ad-hoc recomputation that also re-runs
  `validate_config` a second time. This strengthens the case for a retained graph.
- **The readiness hook is general and opt-in.** `kind.py` documents the optional
  `disabled_reason(registry, resource) -> str | None`; absent-on-class means "never disabled." It is
  offline and cheap (no network, secrets, or prompting; deeper readiness is the capability
  lifecycle's preflight). Surfaces reach it through `resources/inspect.disabled_reason_for` (the
  projection, whose docstring carries the "why can this run on this host" wording), which
  `resource list` renders as `(disabled)` and `describe` as `Disabled: <reason>`.
- **The vm-site readiness chain reaches into a live registry.** `vms/sites.site_disabled_reason`
  answers by looking the platform up in `VM_PLATFORM_REGISTRY` itself (platform missing, then
  `unsupported_reason`, then the bound instance's own hook), i.e. a node queries global state to
  answer its own readiness.
- **The registry retains no graph.** The `Registry` holds only `_resources` (nodes) and `_frozen`;
  the aggregate edge map is a local in `finalize`, discarded on return; each node gets its incoming
  references attached; readiness is not stored at all (it is recomputed on every projection).
- **`validate_config` is also a construct-time invariant.** `capabilities/base.py` re-runs
  `validate_config` when a capability is constructed, and implementations rely on it (e.g. a
  provider that reasons "validate_config ran at construct, so `org` is a valid str"). Any split must
  keep that guarantee for the resources that actually run.

## Terminology

- **Structural graph:** the resource dependency graph built from declared references, independent of
  whether any resource's config block is _valid_. Under this refactor it is the registry's retained,
  first-class product.
- **`dependencies`:** a node's out-edges in the dependency graph, derived from its config, total and
  non-throwing, "what this declares as dependencies, as far as they are derivable without assuming
  the config is valid." This is the graph itself. It is the extraction half of today's
  `validate_config`, and (renamed from `referenced_resources`) the resource-level method too, so
  resource and capability share one word for their graph edges.
- **`validate`:** the throwing correctness check for a config block. The other half of today's
  `validate_config`.
- **Enablement (enabled / disabled):** whether a unit the system knows about is turned on. An
  orthogonal axis to readiness, reserved in this refactor's vocabulary for opt-in.
- **Readiness / `not_ready`:** whether a **present, enabled** resource can serve on this host right
  now. The hook (renamed from `disabled_reason`) returns `str | None`: `None` means ready, a string
  is the reason it is not ready.
- **Readiness fold:** the dependency-ordered pass that computes each node's readiness, handing each
  node its dependencies' verdicts rather than letting the node query global state.
- **Availability tiers:** absent (no node) / present-but-disabled (a node, not enabled) / present
  and enabled (then ready or not-ready). See R7.
- **Materialization:** auto-declaring / synthesizing a referenced resource (e.g. a secret) into the
  registry because something references it.

## Functional requirements

Requirement clauses that name a specific mechanism (the pass ordering in R8, "stored on the graph
node" in R4) encode settled design intent; the HLA may refine the mechanism as long as the stated
outcome holds.

- **R1 The registry produces and retains a first-class dependency graph.** Construction is total and
  config-validity-independent: the pass that builds the graph never raises on a malformed
  **capability config block**, and a resource emits every edge it can derive best-effort, omitting
  only edges whose identity depends on a field that is itself malformed. (This is scoped to
  capability config blocks; kind-owned structural decode errors, e.g. "spec.harness_config must be a
  mapping," still raise at load before a node exists, unchanged.) The graph it produces is
  **retained as the registry's product** (frozen with it): its nodes are the resources, its edges
  are their dependencies (queryable in both directions and transitively, per R11), and the derived
  facts (readiness per R4, incoming references) live on it. Today the registry keeps only a resource
  map and discards the aggregate edge map when finalize returns; this makes the graph a real,
  retained, inspectable object instead.

- **R2 The capability contract splits into `dependencies` and `validate`.** Each capability kind's
  contract replaces the single `validate_config` with two methods: `dependencies(config)` (total,
  never throws, returns the node's graph out-edges as far as they are structurally derivable) and
  `validate(config)` (the throwing correctness check). This lands for **all four** capability kinds
  (`vm-platform`, `harness`, `secret-backend`, `git-credential-provider`) in this effort; the split
  is not staged. The resource-level `referenced_resources()` is renamed to `dependencies()` in the
  same pass, so resource and capability share one graph vocabulary, with `validate()` as its
  correctness counterpart.

- **R3 Config validation is a separate, eager pass, and stays a construct-time invariant.** Config
  blocks are validated in a dedicated pass distinct from graph construction. It runs **eagerly** for
  every present, enabled, ready resource on each registry build (not lazily, not scoped to the
  current command), preserving today's behavior that a malformed block on a resource that will run
  is a hard, command-aborting error with a precise source-located (`file:line`) message. A
  not-ready, disabled, or absent resource's block is not validated in this pass (there is nothing to
  run, and its capability may be unavailable), a deliberate delta (R9.4). Validation also remains a
  **capability construct-time invariant**; consequently the total, non-throwing `dependencies` and
  `not_ready` methods must not assume config validity, while `validate` and construction guarantee
  it for the resources that run.

- **R4 Disablement becomes a dependency-ordered readiness fold, computed once and stored on the
  graph.** Readiness is computed during finalize by walking the graph in reverse-topological
  (dependencies-first) order. The framework **hands each node its dependencies' verdicts**; the
  node's own `not_ready` then decides its verdict from its (best-effort-parsed) config and those
  verdicts. A node never reaches into global registry state to learn a dependency's readiness.
  **Readiness is self-determined per resource: the fold distributes verdicts and imposes no
  propagation rule.** A resource may propagate (a `vm-site` is not-ready if its single platform is
  not-ready or disabled), combine its dependencies however it likes, or **opt out entirely** (a
  `secret` implements no readiness, so it is always ready; whether it can actually be resolved from
  one of its many backends is a resolution-time question, not a readiness one, so **ready does not
  mean resolvable**). Each node's verdict is **recorded on the graph as first-class state**; nothing
  recomputes it lazily. A leaf's readiness is self-contained (host and tool checks); a non-leaf's is
  whatever its `not_ready` makes of its dependencies' verdicts.

- **R5 Readiness is not validity.** These are independent verdicts and both surface. A resource may
  be ready (dependencies present and ready, host supports it) and still have an invalid config
  block; conversely a resource with a valid block may be not-ready because a dependency is
  unavailable. "Ready" must never be read as "valid," and the readiness fold is deliberately blind
  to config validity.

- **R6 The readiness hook is renamed; "enabled/disabled" is freed.** The `disabled_reason` hook and
  its projection are renamed to **`not_ready`** (returning `str | None`, `None` = ready). The rename
  reaches every surface that renders it (`resource list`'s dormant marker, `describe`'s reason line,
  `doctor`) and the operator-facing readiness strings and helper/function names that today say
  "disabled" for host readiness. After this, "enabled" and "disabled" are reserved in the codebase
  for opt-in enablement and never used for host readiness.

- **R7 Three availability tiers, with enablement distinct from readiness.** A referenced capability
  or resource sits in exactly one tier:
  - **Absent** (no node): a typo, or (later) an uninstalled third-party unit. Absent is the
    registry's structural miss and is a **hard error** (the existing `"error"` miss policy), loud
    because it is a genuine mistake. Delta (R9.2): today a `vm-site` with a typo'd platform silently
    self-disables via the edge-suppression workaround; under this model it is a hard error like
    every other kind's unknown-reference.
  - **Present-but-disabled** (a node, marked not-enabled): a unit the system ships and knows about
    but the operator has not turned on. Enablement is a **distinct axis from readiness**: a disabled
    capability is not "unable to run," it is switched off, and it could run if enabled. A resource
    that depends on a disabled capability **may** be not-ready as a result (readiness is
    self-determined per R4): a propagating kind like `vm-site` reports "depends on X, which is
    disabled; enable its unit," with the "enable it" reason read off the disabled node itself, so no
    diagnosis at the miss point is needed; a kind that opts out (a `secret`, whose many-backend
    resolvability is a resolution-time question) stays ready and lets resolution skip the disabled
    path.
  - **Present and enabled**: a normal node, then ready or not-ready per the fold (R4).
    Host-unsupported (an installed platform whose `unsupported_reason` is non-None on this host,
    e.g. `wsl2` off Windows) is a not-ready case of this tier, **not** an absent node; making that
    true requires unconditional capability publication (R13).

  This SDD builds on `main` with the plugin system parked, so nothing yet **produces** disabled
  nodes (today the only absent case is a typo; host-unsupported becomes readiness rather than
  absence via R13). The refactor therefore **designs the node-state model to carry the enablement
  axis** and proves it with a test fixture that produces a disabled node (exercising the axis and
  the not-ready propagation), but ships **no producer**. The system-plugin rebuild is the first real
  producer and slots onto this model without re-touching the core. This supersedes an earlier plan's
  absent-plus-diagnosis seam: because a disabled unit is a present node rather than an absent one,
  there is nothing to diagnose at a miss.

- **R8 One graph, walked several ways, in a fixed order.** There is no separate "disablement graph."
  Missing-reference detection, cycle detection, the readiness fold, and (R12) readiness-gated
  materialization are passes over the single structural graph. The readiness fold requires an
  acyclic graph and therefore runs after cycle detection. The ordering constraint that
  readiness-gated materialization (R12) imposes (materialization must know readiness) is the one
  place the naive "build fully, then fold" order does not suffice; the HLA resolves it (an
  interleave or a second synthesis pass), but the passes still operate over the one retained graph.

- **R9 Behavior is preserved for a valid, enabled, ready config, except for an enumerated set of
  intended changes.** Auto-declare/synthesize, reserved-default materialization, incoming-reference
  attachment, description polish, and freeze keep their current behavior for such configs. The
  intended, acceptance-pinned deltas are:
  1. **Rendered vocabulary.** The readiness rename (R6) changes surface strings (`resource list`'s
     `(disabled)` marker, `describe`'s `Disabled:` line, doctor's disabled wording) to the readiness
     vocabulary.
  2. **A typo'd capability reference is now a hard error, not a silent self-disable** (R7).
  3. **Which-error-wins can reorder.** With capability-block validation moved out of decode/load
     into a post-graph pass (R3, R8) (construction still validates, per R3), a config with both a
     malformed block and (say) a cycle now reports the cycle first, where today the malformed block
     failed at decode before finalize ran.
  4. **A not-ready resource's malformed block is deferred, not silenced.** It is validated when the
     resource becomes ready, not at every build (R3); today decode/load validate it eagerly
     regardless of readiness.
  5. **An installed-but-host-unsupported capability now appears as a not-ready row.** With
     unconditional publication (R13), an installed platform whose `unsupported_reason` is non-None
     on this host (e.g. `wsl2` on macOS/Linux) publishes a present, not-ready `vm-platform` row
     visible in `resource list` and `doctor`, where today `publish_to` skips it and it appears
     nowhere.
  6. **Secret backends now report offline readiness, and a not-ready mapped backend warns and falls
     through instead of halting.** A configured backend whose host tool is missing (e.g.
     `onepassword` with no `op` on `PATH`) now shows as **not-ready** in `secret list` /
     `secret describe` / `doctor`, and during resolution it is **skipped with a warning** and the
     chain falls through to the next candidate. Today that same case raises `ConnectivityError` and
     **halts** the chain; the change is deliberate (the warning keeps it from being silent). The
     anti-masking **halt is kept** for a ready store's hard miss (available but definitively no
     value). The readiness check stays offline (no store probe, no biometric); interactivity is
     still previewed optimistically.
  7. **Doctor gains a secret-backends group** (one readiness row per backend, parallel to
     vm-platforms), and the `secret list` grid replaces its overloaded "disabled" cell with the
     states an opted-in backend can be in: would-attempt (the identifier), not-ready (with reason),
     or won't-attempt (a `false` opt-out or a mapping-required backend with no mapping). Enablement
     and not-opted-in are shown in `secret describe` and the doctor backend group, not the grid.
  8. **`agw env show --reveal-secrets` is renamed `--resolve`**, aligning the flag with the defined
     resolution vocabulary.
  9. **Every declared backend mapping is validated at build, not just the opted-in ones.** A
     secret's `validate()` checks its mapping for every **present, enabled** backend it addresses
     (via that backend's `validate(mapping)`), where today `validate_chain` validates only mappings
     for backends in the active chain. So a stale or malformed mapping for a
     configured-but-not-opted-in backend now fails at build instead of lying dormant. (A mapping to
     an **absent** backend is the hard-error edge per R7; a mapping to a present-but-**disabled**
     backend is not validated, staying inert until enabled.)
  10. **Secret-backend rows gain inbound references.** Because a secret now emits
      `secret -> secret-backend` edges (to every candidate backend), each backend row is referenced
      by the secrets that could resolve through it, so `resource list`'s REFS count and
      `resource describe secret-backend/<name>`'s "Referenced by" gain entries, where those rows are
      referenced by nothing today. (Analogously, previously-suppressed vm-sites now reference their
      platforms.)
  11. **A well-formed mapping to an unknown backend name is now a hard error.** Today
      `validate_chain` only iterates active-chain backends, so a `backend_mappings.<typo>` for a
      name not in the registry lies dormant; under the `secret -> secret-backend` edge it is an
      absent target and a hard error at build (the R9.9 classification, restated here because it is
      a behavior change, not just a rule).

  Reachability itself is **preserved, not moved to lazy time**: the "every operator-declared secret
  is resolvable" check stays an eager post-finalize boundary check (fail-fast at `build_registry`),
  now reading the graph rather than re-deriving, still scoped to operator-declared secrets only (an
  auto-declared secret cannot invalidate a deliberate `backends = []` opt-out; it surfaces at
  use-time), and still keyed on would-attempt (not readiness), so a secret whose only opted-in
  backend is not-ready is still reachable and fails only at resolution, exactly as today. So no
  reachability behavior changes; only its implementation (graph read) does.

  Secret-chain validation is otherwise not preserved as a single unit: `secrets.validate_chain`
  splits (per-mapping spec validation into the finalize `validate` pass; reachability stays an eager
  post-finalize boundary check, now graph-reading). The test suite pins both the preservation and
  this delta list; the list is the HLA's acceptance contract.

- **R10 Readiness stays projectable and cheap.** `not_ready` remains offline and cheap (no network,
  secrets, or prompting; deeper checks stay in the capability lifecycle's preflight) and remains
  projectable to `resource list`, `describe`, and `doctor` exactly as `disabled_reason` is today,
  now by **reading the stored verdict off the graph** rather than recomputing, and including the
  dependency-following reasons the fold produces. (The stored verdict may be a small ready/not-ready
  object rather than a bare `str | None`, to spare consumers double negatives; an HLA choice.)

- **R11 The graph is the single access path for structural and derived facts, enforced by a guard.**
  The refactor forces the rest of the system onto the graph and removes the bypass paths. The
  **banned patterns** the guard checks: recomputing a resource's edges by calling `dependencies()`
  outside the graph build; querying a capability's live implementation registry to decide
  availability; computing readiness lazily instead of reading the stored verdict; and reading
  incoming references off a resource dataclass field instead of the graph. The **inventory to
  migrate** is larger than the capability-config resources: `resources/walk.py`
  (`collect_secrets_for`, an ad-hoc reachability DFS), the `secret_refs` recomputation in
  `vms/nodes.py` and `git_credentials/nodes.py`, capability construct-time `secret_refs` extraction
  (`capabilities/base.py`), and cycle detection itself (`registry.py` `_edges_from`). Because
  `collect_secrets_for` needs a **transitive / reachable-set** query, the retained graph's API must
  answer reachability ("what is transitively reachable from X"), not only immediate neighbors, or
  that caller hand-rolls a walk again on day one. The effort produces a dated caller inventory (of
  `validate_config`, `disabled_reason`, and `referenced_resources` call sites) that doubles as the
  guard's baseline, and a guard test confirms the banned patterns are gone.

- **R12 A not-ready or disabled node contributes no materialized dependencies.** The config-implied
  edges of a resource that is not-ready or disabled do not drive auto-declaration or
  reserved-default materialization: the registry does not predict-resolve the secrets (or other
  auto-declared resources) of something that cannot run on this host or is switched off. This
  preserves today's behavior, where a host-disabled `vm-site`'s secrets do not appear in
  `agw secret list`, doctor, or resolution prediction (achieved today by the edge-suppression
  workaround R7 removes). Because readiness is known only after the fold while synthesis happens
  during construction, this requires readiness to gate materialization; the ordering is the HLA's to
  resolve (R8), but the required behavior is fixed here.

- **R13 Installed capability rows publish unconditionally; host-support is readiness, not absence.**
  Today `vm_platform.publish_to` skips any platform whose `unsupported_reason()` is non-None, so an
  installed-but-host-unsupported platform publishes **no** node, i.e. it is an _absent_ node. The
  bundled `wsl2` vm-site references `wsl2`, which is absent on every non-Windows host; today only
  the vm-site edge-suppression (which R7/R12 remove) keeps that from being a dangling edge. With the
  graph total and absent = hard error (R7), leaving publication as-is would make the bundled `wsl2`
  site abort every command on every non-Windows host. So installed capability rows **publish
  unconditionally**: `unsupported_reason` stops gating publication and instead becomes an input to
  the capability node's `not_ready` verdict, so a host-unsupported capability is a
  **present-but-not-ready node** and its dependents are not-ready rather than hard-erroring. This is
  the deliberate replacement for "job 1" of the vm-site suppression (R12 is job 2), and it is what
  makes "host-unsupported is readiness, not absence" (R7) actually true rather than asserted. The
  surface change is the R9.5 delta. (Kinds with no host-support concept already publish
  unconditionally, so this is a no-op for them.)

## Scope

In scope: making the registry produce and retain a first-class dependency graph, with a reachability
query, as its product (R1); decoupling graph construction from config validation (R1, R3); the
`dependencies` / `validate` split across all four capability kinds, plus the resource-level
`referenced_resources` rename (R2); the eager, construct-invariant-preserving validation pass (R3);
the dependency-ordered readiness fold, computed once and stored on the graph (R4); readiness-is-not-
validity (R5); the `not_ready` rename and freeing "enabled/disabled," including operator-facing
strings and function names (R6); the three availability tiers with the enablement axis modeled and
fixture-tested but not produced (R7); the single-graph pass ordering including readiness-gated
materialization (R8, R12); unconditional capability publication so host-support is readiness, not
absence (R13); the enumerated behavior deltas (R9); readiness projected off the graph (R10); forcing
all consumers onto the graph, the caller inventory, and the anti-bypass guard (R11); and the
migration of every current caller of `validate_config`, `disabled_reason`, and
`referenced_resources` to the new shapes. `secret-backend` is a full participant in the split like
the other three capability kinds (its per-secret `backend_mappings` is capability config owned by
the `secret`, validated and dependency-extracted at finalize); secret **resolution** is refactored
into a distinct operation that consumes the graph (edges, stored backend readiness, backend impls)
and applies the operator's opt-in resolution chain, kept out of the core finalize passes.

Out of scope: the system-plugin work itself (its rebuild consumes this refactor and is a separate,
already-parked SDD), including any real **producer** of present-but-disabled nodes; any new
capability kind or declarable kind; resource-name namespacing; a general operator-facing
enable/disable for individual resources (the freed vocabulary and the enablement axis leave the door
open, but the operator surface is not built here).

## Future direction

- **The system-plugin rebuild consumes this.** With graph construction decoupled from validation,
  readiness stored on a retained graph, and the enablement axis modeled, a shipped unit the operator
  has not enabled contributes **present-but-disabled** nodes, and a resource that references one is
  **not-ready** with an "enable it" reason read off the disabled node. That is the model the parked
  plugin SDD was blocked on.
- **A general resource enable/disable could reuse the freed vocabulary and the enablement axis.**
  Nothing in this effort builds an operator-facing on/off for individual resources, but by reserving
  "enabled/disabled" for opt-in and modeling the enablement axis it leaves that door cleanly open.

## Artifacts

Per the `sdd` skill, `prior-art-research.md` is skipped (this is a pure internal refactor of an
existing subsystem, the skill's named skip case). A **dated caller inventory** (of
`validate_config`, `disabled_reason`, and `referenced_resources` call sites) is produced before the
plan; it is both the R11 guard's baseline and the migration's current-state snapshot, standing in
for a full `migration-strategy.md` (the change is atomic and in-repo, with no data movement or
rollout).
