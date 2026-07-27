# FRD: registry readiness refactor

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Start date: 2026-07-27.

## Summary

The resource registry fuses two concerns that should be separate: **constructing the resource
dependency graph** and **validating each resource's config**. A resource declares its graph edges in
`referenced_resources()`, which calls the capability's `validate_config()`, a method that does
double duty: it both validates the config block (and raises on bad input) and extracts the
config-implied edges (the secret a credential needs, and so on). Because the two are welded
together, the graph cannot be built without validating, config validation is forced to run at three
registry-unaware phases, and a resource's place in the graph depends on its config being valid.

Separately, the registry's vocabulary for "a resource that is present but cannot serve", the
`disabled_reason` hook, has become overloaded. It answers "why can't this run on this host" (a
readiness signal), but the word "disabled" is about to collide with a genuine enable/disable concept
arriving elsewhere in the system. One word is being asked to carry two orthogonal signals: **"can't
run"** (a present resource blocked by its host or a dependency) and **"not enabled"** (something the
operator never opted into).

This refactor **decouples graph construction from config validation**, reshapes disablement as a
**readiness** signal computed by a dependency-ordered fold, and **renames the readiness hook** so
"enabled/disabled" can mean exactly one thing. It changes the registry's internal decomposition; for
any valid configuration the externally observable outcome is unchanged.

## Motivation

1. **The fusion is the root cause of a class of defects.** Because building the graph runs the
   throwing `validate_config`, config validation is scattered across three registry-unaware phases
   (manifest decode, TOML load, and the finalize walk), a single malformed config block can prevent
   graph analysis entirely, and code that should ask "is this dependency available" instead asks "is
   its code present." These are not isolated bugs; they are the same weld showing through in
   different places. Untangling it once removes the whole class.

2. **The vocabulary overload will get worse, not better.** As soon as a real enable/disable concept
   exists in the system, `disabled` meaning "not ready" is a genuine collision that makes every
   reader disambiguate from context. Reserving "enabled/disabled" for opt-in, and giving readiness
   its own name, is cheap now and expensive after more code and docs accrete around the ambiguity.

3. **The timing is right.** The registry is a load-bearing subsystem that many parts of the CLI
   depend on. There are no major pending PRs to conflict with a core change, and a downstream effort
   (the parked system-plugin work) is explicitly blocked on getting this model right. Doing it now,
   completely, is cheaper than doing it piecemeal later under more constraints.

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

- **Validation is scattered and phase-inconsistent.** Because edge-extraction (and thus
  `validate_config`) has to happen wherever a resource is first interpreted, capability config
  blocks are validated at manifest-decode time, at TOML-load time, and again during the finalize
  walk. Only the last of these has the finalized registry in hand. The same block can raise at any
  of the three, and which one wins is phase- and import-order-dependent.
- **A malformed block blocks graph analysis.** Since building the edge set runs the throwing
  validator, one bad config block aborts construction of the whole graph, so nothing downstream (the
  readiness walk, cycle detection, the rest of finalize) can run until every block is well-formed.
- **Resolution reads code-presence, not the graph.** Because the extractor consults the capability's
  live registry rather than the resolved graph, "is this dependency available" is answered by "is
  its implementation seated in the process," which is a different question with a different answer.

### Problem 2: "disabled" conflates readiness with enablement

The registry already has a clean, general notion of a resource that is present but cannot serve: the
per-kind `disabled_reason(registry, resource) -> str | None` hook. Its documented contract is host
readiness: "why can this resource not run on this host, or `None` when it can (or the kind has no
such concept)." A `vm-site`, for example, reports it cannot run when its platform is missing,
host-unsupported, or its bound instance lacks a local tool. A disabled resource still registers,
lists, describes, and holds references; only _using_ it is an error. This is a good model.

The problem is only the name and the conflation. "Disabled" reads as "switched off," which is about
to mean a specific, different thing (an operator opting a unit in or out). Two orthogonal signals
are being funneled through one word:

- **"can't run" (readiness):** a **present** resource, blocked by its host, a missing tool, or a
  dependency that is itself unavailable. Graph-following. The resource exists and the operator wants
  it; circumstances block it.
- **"not enabled" (absence):** a unit the operator never opted into. Its contributions are simply
  **not present**. This is not a readiness state at all.

Collapsing these is what generated the confusion this refactor exists to end. They compose but are
distinct: a resource can be _not ready_ **because** a dependency it names is _absent_. The dependent
is not ready; the dependency is not "disabled", it is simply not there.

## Background (verified against current code)

- **`referenced_resources()` fuses edges and validation.** `VMSite.referenced_resources`
  (`vms/sites.py`) and `GitCredentialConfig.referenced_resources` (`git_credentials/credential.py`)
  each emit a bare capability edge and then call the seated capability's `validate_config(...)` to
  expand config-implied edges. `validate_config` raises on a malformed block.
- **Finalize already separates the graph walk from cycle detection.** `Registry.finalize`
  (`resources/registry.py`) runs a worklist loop that walks `referenced_resources()` to accumulate
  the reference map, dispatches each kind's miss policy (`"error"` raises "references unknown
  `<kind>` `<name>`"; `"auto-declare"` synthesizes), then attaches usage, then detects cycles via
  DFS three-coloring, then freezes. The cycle pass is already a separate walk over the assembled
  graph; the missing separation is pulling config _validation_ out of edge _extraction_.
- **The readiness hook is general and opt-in.** `kind.py` documents the optional
  `disabled_reason(registry, resource) -> str | None`; absent-on-class means "never disabled." It is
  offline and cheap (no network, secrets, or prompting; deeper readiness is the capability
  lifecycle's preflight). Projected to surfaces via `resources/inspect.disabled_reason_for`, which
  `resource list` renders as `(disabled)` and `describe` as `Disabled: <reason>`.
- **The vm-site readiness chain reaches into a live registry.** `vms/sites.site_disabled_reason`
  answers by looking the platform up in `VM_PLATFORM_REGISTRY` itself (platform missing, then
  `unsupported_reason`, then the bound instance's own hook), i.e. a node queries global state to
  answer its own readiness.
- **The capability contract is a single `validate_config`.** Each capability kind's base
  (`capabilities/*/base.py`) defines `validate_config` as the one method that both checks a config
  block and returns the references it implies. There is no separate "extract references without
  validating" entry point.

## Terminology

- **Structural graph:** the resource dependency graph built from declared references, independent of
  whether any resource's config block is _valid_. The registry's primary product.
- **`dependencies`:** a node's out-edges in the dependency graph, derived from its config, total and
  non-throwing, "what this declares as dependencies, as far as they are derivable without assuming
  the config is valid." This is the graph itself. It is the extraction half of today's
  `validate_config`, and (renamed from `referenced_resources`) the resource-level method too, so
  resource and capability share one word for their graph edges.
- **`validate`:** the throwing correctness check for a config block. The other half of today's
  `validate_config`.
- **Readiness / `not_ready`:** the signal that a **present** resource can or cannot serve on this
  host right now. The hook (renamed from `disabled_reason`) returns `str | None`: `None` means
  ready, a string is the reason it is not ready. The concept is readiness; the hook says _why_ you
  are not ready.
- **Readiness fold:** the dependency-ordered pass that computes each node's readiness, handing each
  node its dependencies' readiness verdicts rather than having the node query global state.
- **Absent / not present:** a referenced name with no node in the registry. Distinct from "present
  but not ready." The registry's structural miss.

## Functional requirements

- **R1 Graph construction is total and config-validity-independent.** The pass that builds the
  structural graph never raises on a malformed config block. A resource emits every edge it can
  derive best-effort and omits only edges whose identity depends on a field that is itself
  malformed. After this pass the registry holds the complete dependency graph regardless of any
  resource's config health.

- **R2 The capability contract splits into `dependencies` and `validate`.** Each capability kind's
  contract replaces the single `validate_config` with two methods: `dependencies(config)` (total,
  never throws, returns the node's graph out-edges as far as they are structurally derivable) and
  `validate(config)` (the throwing correctness check). This lands for **all four** capability kinds
  (`vm-platform`, `harness`, `secret-backend`, `git-credential-provider`) in this effort; the split
  is not staged. The resource-level `referenced_resources()` (which composes a resource's bare edge
  with its capabilities' `dependencies`) is renamed to `dependencies()` in the same pass, so
  resource and capability share one graph vocabulary, with `validate()` as its correctness
  counterpart.

- **R3 Config validation is a separate pass over the ready set.** Config blocks are validated in a
  dedicated pass, distinct from graph construction, that runs only for resources that are actually
  going to be used (present and ready), and it preserves today's precise, source-located
  (`file:line` where the resource has a declared location) error messages. A not-ready or absent
  resource's config block is not validated (there is nothing to run, and its capability may be
  unavailable).

- **R4 Disablement becomes a dependency-ordered readiness fold.** Readiness is computed by walking
  the graph in reverse-topological (dependencies-first) order; the framework hands each node its
  dependencies' readiness verdicts, and a node's readiness answer is a pure function of its own
  (best-effort-parsed) config and those verdicts. A node does not reach into global registry state
  to learn a dependency's readiness. A leaf's readiness is self-contained (host and tool checks); a
  non-leaf's readiness folds in its dependencies'.

- **R5 Readiness is not validity.** These are independent verdicts and both surface. A resource may
  be ready (its dependencies are present and ready, its host supports it) and still have an invalid
  config block; conversely a resource with a perfectly valid block may be not-ready because a
  dependency is unavailable. "Ready" must never be read as "valid," and the readiness fold is
  deliberately blind to config validity.

- **R6 The readiness hook is renamed; "enabled/disabled" is freed.** The `disabled_reason` hook and
  its projection are renamed to **`not_ready`** (returning `str | None`, `None` = ready). All
  surfaces that render it (`resource list`'s dormant marker, `describe`'s reason line, `doctor`)
  follow the rename. After this, "enabled" and "disabled" are reserved in the codebase for opt-in
  concepts and never used for host readiness.

- **R7 "Absent" and "not ready" are distinct, and a dependent follows its dependencies.** A
  reference to a name with no node in the registry is the registry's structural miss (its primary
  job), a different thing from a present node that is not ready. A resource that names an
  unavailable dependency (absent, or present-but-not-ready) is itself not ready, with a reason that
  distinguishes the two cases. The refactor also leaves a seam at the point a reference resolves to
  no node, so a later effort can annotate _why_ a name is absent (for example "provided by a unit
  that is not enabled") without the registry core knowing about any particular such source.

- **R8 One graph, walked several ways, in a fixed order.** There is no separate "disablement graph."
  Missing-reference detection, cycle detection, and the readiness fold are passes over the single
  structural graph, ordered: build the graph, detect cycles, fold readiness, validate config. The
  readiness fold requires an acyclic graph and therefore runs after cycle detection.

- **R9 Existing registry outcomes are preserved for valid configs.** Auto-declare/synthesize,
  reserved-default materialization, usage attachment, description polish, freeze, and the
  post-finalize boundary checks (`secrets.validate_chain`, and the vm-site validation) all keep
  their current behavior. This refactor changes the registry's internal decomposition and the
  readiness vocabulary, not the externally observable result of building a registry from a valid
  config. The test suite pins this: the refactor is behavior-preserving except for the intended
  readiness/error message changes.

- **R10 Readiness stays projectable and cheap.** `not_ready` remains offline and cheap (no network,
  secrets, or prompting; deeper checks stay in the capability lifecycle's preflight) and remains
  projectable to `resource list`, `describe`, and `doctor` exactly as `disabled_reason` is today,
  now including the dependency-following reasons the fold produces.

## Scope

In scope: decoupling graph construction from config validation (R1, R3); the `implied_references` /
`validate` split across all four capability kinds (R2); the dependency-ordered readiness fold (R4);
readiness-is-not-validity (R5); renaming the readiness hook and freeing "enabled/disabled" (R6); the
absent-vs-not-ready distinction and the reference-resolution seam (R7); the single-graph pass
ordering (R8); preservation of all existing registry outcomes for valid configs (R9); keeping
readiness cheap and projectable (R10); the migration of every current caller of `validate_config`,
`disabled_reason`, and `referenced_resources` to the new shapes.

Out of scope: the system-plugin work itself (its rebuild consumes this refactor and is a separate,
already-parked SDD); the plugin-specific "not enabled, here is how to enable it" diagnosis _content_
(only the resolution seam is built here, R7); any new capability kind or declarable kind; resource
name namespacing; a general operator-facing enable/disable for individual resources (the freed
vocabulary is reserved for it, but it is not built here).

## Future direction

- **The system-plugin rebuild consumes this.** With graph construction decoupled from validation and
  readiness reshaped, a unit the operator has not enabled contributes _absent_ nodes (not present),
  and a resource that references one is _not ready_ with a reason the R7 seam can enrich into
  "enable the unit that provides it." That is the model the parked plugin SDD was blocked on.
- **A general resource enable/disable could reuse the freed vocabulary.** Nothing in this effort
  builds an operator-facing on/off for individual resources, but by reserving "enabled/disabled" for
  opt-in it leaves that door cleanly open.
