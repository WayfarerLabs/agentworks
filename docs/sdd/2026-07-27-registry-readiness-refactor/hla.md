# HLA: registry readiness refactor

Implements the [FRD](./frd.md). The registry gains one new retained object (a first-class dependency
graph), the capability contract splits one method into two, `finalize` is re-expressed as ordered
passes over that graph, and every consumer that today recomputes edges or readiness is routed
through it. The kinds, the manifest loader, the config surfaces, and the collision policy are
untouched.

## Current state (verified against current code)

- **The `Registry` retains no graph.** It holds only `_resources: dict[kind][name] -> Resource` and
  `_frozen` (`resources/registry.py:49-53`). The outbound edge map (`all_refs`, keyed by target) is
  a local in `finalize` and is discarded on return (`registry.py:231-259`). Each row is left
  carrying an _inbound_ `references` tuple of `ReferenceEntry(source, usage)`
  (`registry.py:253-259`, `_references_tuple:462-478`), which drops the target `kind`/`name`, so it
  cannot answer outbound queries.
- **`finalize` (`registry.py:165-265`)**: (0) materialize reserved-default names
  (`_materialize_reserved_defaults`); (1) worklist loop walking each row's `referenced_resources()`
  into `all_refs`, dispatching each kind's `miss_policy` (`"error"` raises "references unknown", or
  `"auto-declare"` synthesizes); (2) attach inbound `references` + polish descriptions; (3) detect
  cycles; (4) freeze. **Cycle detection re-derives edges** (`_edges_from:529-543`) by re-calling
  `referenced_resources()` per node, which for capability-config resources re-runs
  `validate_config`.
- **`validate_config` is one method doing two jobs** (`capabilities/base.py:314-338`): a throwing
  correctness check that also returns config-implied `ConfigReference`s (sourceless: `kind`/`name`/
  `usage`). It is re-run at capability construction (`base.py:288-308`) to extract `_secret_refs`,
  an invariant impls rely on (`vms/sites.py:188-193`). It runs 4-6x per resource across decode/load,
  the finalize walk, cycle detection, and construct.
- **Edge production inlines `validate_config`** in three resources: `VMSite` (`vms/sites.py:52-93`),
  `GitCredentialConfig` (`git_credentials/credential.py:71-109`), `SessionTemplate`
  (`sessions/template.py:66-117`). `VMSite` **suppresses all its edges** when its platform is absent
  or `unsupported_reason()` is non-None (`sites.py:67-71`), so its edges are host-conditional; the
  other two emit the selector edge unconditionally (unknown provider/harness is a hard miss error).
- **Readiness is a lazy, three-layer recompute, only for vm-site.** The kind hook
  `disabled_reason(registry, resource)` exists only on `_VMSiteKind` (`vms/kinds.py:197`),
  delegating to `site_disabled_reason` (`sites.py:169-194`), a chain of platform-missing (via
  `VM_PLATFORM_REGISTRY.get`) / `unsupported_reason` / instance `disabled_reason` (lima/wsl2). It is
  projected on demand by `inspect.disabled_reason_for` (`inspect.py:217-236`), recomputed per
  render. `doctor` bypasses the projection and calls `site_disabled_reason` plus a live node
  `preflight` directly (`doctor.py:272,281-282`).
- **Only `vm-platform` publication is host-gated.** `vm_platform.publish_to` **skips** a platform
  whose `unsupported_reason()` is non-None (`vm_platform/__init__.py:99`), so an unsupported
  platform is an absent node; the other three kinds publish unconditionally.
  `bootstrap.build_registry` publishes bundled manifests (including the bundled `wsl2` vm-site) then
  the capability rows, then operator config/manifests, then `finalize`, then the post-finalize
  `secrets.validate_chain` / `vm_sites.validate_sites` (`bootstrap.py:89-106`).
- **Consumers recompute rather than read a graph.** Outbound edges are recomputed by re-calling
  `referenced_resources()` in cycle detection (`registry.py:542`), `walk.collect_secrets_for`
  (`walk.py:71`, a transitive-reachability DFS used by `secrets/kinds.py:188`), and the node
  factories `vm_site_node` (`vms/nodes.py:412`) and `git_credential_node`
  (`git_credentials/nodes.py:93`). A third secret-ref path is the held capability's `_secret_refs`
  (from construct-time `validate_config`), read at op time by `Harness.secret_refs` and
  `GitCredentialProvider.secret_name`.

## Target state

The `Registry` produces and retains a frozen **`DependencyGraph`** as the output of `finalize`. The
graph holds, per node, its outbound edges (full `ResourceReference`s), its readiness verdict, its
enablement, and its inbound references; it answers immediate and transitive structural queries and
readiness queries. `validate_config` splits into total `dependencies(config)` (edge extraction) and
throwing `validate(config)`. `finalize` becomes ordered passes over the graph: build (total) →
resolve error-misses → cycle-detect → readiness fold → readiness-gated materialization → validate →
freeze. Capability rows publish unconditionally, so edges are host-independent and readiness is the
host-specific overlay. Every recompute/registry-query consumer reads the graph, enforced by a guard.

## Components

### 1. The retained `DependencyGraph` (R1, R11)

A frozen structure the `Registry` holds after `finalize`, alongside `_resources` (which stays the
row store). For each node keyed `(kind, name)` it records:

- **outbound edges**: the `tuple[ResourceReference, ...]` the node declares (the `all_refs` that is
  discarded today, re-keyed by _source_ instead of target), each carrying target `kind`/`name`,
  `usage`, and `source`.
- **readiness**: a `Readiness` verdict (see component 4), computed once by the fold and stored.
- **enablement**: `enabled | disabled` (the reserved axis, R7; only a test fixture produces
  `disabled` in this effort).
- **inbound references**: the `ReferenceEntry(source, usage)` set (today's per-row `references`),
  moved off the resource dataclass onto the graph.

Query API (the single access path, R11):

- `edges_of(kind, name) -> tuple[ResourceReference, ...]` (outbound).
- `dependents_of(kind, name) -> tuple[ReferenceEntry, ...]` (inbound / usage).
- `reachable_from(kind, name) -> Iterable[(kind, name)]` (transitive closure; replaces
  `collect_secrets_for`'s hand-rolled DFS).
- `readiness_of(kind, name) -> Readiness` and `is_ready(kind, name) -> bool`.

The `references` field is removed from the resource dataclasses; `inspect` and any usage reader move
to `dependents_of`. The graph is the one home for outbound edges (node factories, cycle detection,
reachability), inbound usage (inspect), and readiness (inspect, doctor).

### 2. The capability-contract split (R2)

Replace `validate_config(owner, config) -> tuple[ConfigReference, ...]` with two classmethods on
every capability base (`vm_platform`, `harness`, `git_credential`; and the `SecretBackend` protocol,
whose throwing `validate_mapping` becomes `validate` and whose `dependencies` returns `()` since a
backend mapping implies no resources):

- **`dependencies(config) -> tuple[ConfigReference, ...]`**: total, never raises, returns the
  config-implied references as far as they are structurally derivable (omitting only an edge whose
  identity depends on a malformed field).
- **`validate(config) -> None`**: the throwing correctness check.

The sourceless→sourced conversion (`ConfigReference` → `SecretReference` when `kind == "secret"`,
else `ResourceReference`, attaching `source`) that is triplicated across the three
`referenced_resources` bodies today is centralized into one helper. The resource-level method
`referenced_resources()` is renamed to **`dependencies()`** and becomes: emit the bare capability
edge, then append the capability's `dependencies(config)` mapped through that helper. No validation,
no throwing, and **no host-conditional suppression** (component 5 makes the platform node always
present, so the vm-site always emits its platform edge). `EnvEntry.referenced_resources(source)`
(the one arg-taking variant, `env/entry.py:38`) stays an internal aggregation and is not called by
the graph builder.

### 3. `finalize` as ordered passes producing the graph (R8, R12)

```text
build:        walk every row's dependencies() (total, non-throwing) -> outbound edge map
resolve:      for each edge whose target has no node:
                - miss_policy "error"      -> hard error now (R7: absent = loud typo)
                - miss_policy "auto-declare" -> defer to materialize (below)
              (reserved-default names are always-materialized up front, as today)
cycle-detect: three-coloring over the BUILT edge map (no re-derivation; fixes today's re-walk)
readiness-fold: reverse-topological; hand each node its deps' verdicts; store Readiness per node.
                auto-declarable-but-not-yet-materialized targets count as satisfied (not "absent")
materialize:  synthesize each auto-declare target that a READY, ENABLED node references (R12);
              a target referenced only by not-ready/disabled nodes does not materialize.
              (secrets are leaves, so no second fold is needed; a small fixpoint covers any
              synthesized node that itself references)
attach:       inbound references + description polish, onto the graph
validate:     validate(config) over the ready+enabled set only (throwing, precise file:line)
freeze
```

The one ordering subtlety the FRD flagged (R8): materialization must know readiness, so it follows
the fold rather than happening during build. This is why `dependencies` is separated from
materialization: the build produces edges (including to-be-materialized targets) without
synthesizing, the fold runs, then materialization is gated on the referrer's readiness. Error-policy
misses (R7 hard errors) are _not_ readiness-gated; only auto-declare materialization is.

### 4. The readiness fold and the `Readiness` verdict (R4, R5, R6, R10)

The kind/instance `disabled_reason` hook is renamed **`not_ready`** and re-shaped from
`(registry, resource)` to a pure local function the fold feeds:

- capability node (a leaf, e.g. `vm-platform/wsl2`): `not_ready(config) -> Readiness` (host/tool
  checks; `unsupported_reason` and the instance `disabled_reason` fold in here).
- consuming resource: `not_ready(config, {dependency -> Readiness}) -> Readiness`, a pure function
  of its own best-effort-parsed config and its dependencies' verdicts, never querying a live
  registry.

`Readiness` is a small verdict object (ready, or not-ready with a reason), stored on the graph node;
consumers read it via `readiness_of` (R10), sparing them the `str | None` double negative.
Propagation: a node is not-ready if any dependency is not-ready **or disabled** (R4). This absorbs
`site_disabled_reason`'s three-step chain: platform-missing collapses (a typo is now a hard error at
resolve; a supported platform is always present), platform-unsupported becomes the platform node's
own not-ready verdict folded into the site, and the instance requirement (limactl) becomes the
site's own `not_ready`. `not_ready` stays offline and cheap; where a capability's check needs a
minimal context, the fold constructs a fresh one (`RunContext` cannot be `dataclasses.replace`d,
`base.py:206-212`).

### 5. Unconditional capability publication (R13)

`vm_platform.publish_to` drops the `unsupported_reason() is not None: continue` skip; every
installed platform publishes a `vm-platform` row. `unsupported_reason` stops gating publication and
becomes an input to the platform node's `not_ready` (component 4). Consequences: the vm-platform row
is present on every host (not-ready when unsupported), so the vm-site's platform edge always
resolves to a present node, the suppression in component 2 is unnecessary, **the structural graph is
host-independent** (same edges on every host; only readiness verdicts differ), and the bundled
`wsl2` site becomes not-ready on non-Windows hosts instead of a hard error. The other three kinds
already publish unconditionally, so this is a vm-platform-only change. Surface delta: an unsupported
platform now appears as a not-ready row (FRD R9.5).

### 6. Consumer migration onto the graph (R11)

- **Cycle detection**: reads `edges_of` instead of re-calling `referenced_resources()` (removes the
  second `validate_config` pass).
- **`walk.collect_secrets_for`**: becomes a thin filter over `reachable_from` (the graph owns the
  transitive walk). Its caller `secrets/kinds.py:188` is unaffected.
- **Node factories** `vm_site_node` / `git_credential_node`: read secret edges off `edges_of`
  instead of re-calling `referenced_resources()`.
- **`inspect`**: reads readiness via `readiness_of` and usage via `dependents_of`; the `(disabled)`
  description-cell rendering (`inspect.py:470,504`) adopts the readiness vocabulary (R6).
- **`doctor`**: reads readiness off the graph for the offline check (replacing its direct
  `site_disabled_reason` and `unsupported_reason` calls); its separate live `preflight` (network)
  stays as the deeper op-boundary check, unchanged.
- **Op-time held-capability secret refs** (`Harness.secret_refs`,
  `GitCredentialProvider.secret_name`, from construct-time `_secret_refs`): the single shared
  derivation becomes `dependencies(config)` (the same total function the graph is built from), so
  build-time and op-time agree by construction rather than via a second method. Threading the frozen
  graph itself to op time is possible but not required; the requirement is one derivation of the
  edge truth, not two divergent ones (a decision to confirm in the LLD).

### 7. The anti-bypass guard (R11)

A guard test pins the banned patterns so they cannot return: no call of the renamed `dependencies()`
outside the graph build; no `*_REGISTRY.get(...)` availability probe in edge production or
readiness; no lazy readiness recompute; no reading edges/usage off resource dataclasses. The dated
**caller inventory** (already scouted: the `validate_config` / `disabled_reason` /
`referenced_resources` / `unsupported_reason` sites) is the guard's baseline and the migration
checklist; it lands as a feature artifact.

## Interfaces (summary)

- Graph: `edges_of`, `dependents_of`, `reachable_from`, `readiness_of`, `is_ready`.
- Capability: `dependencies(config) -> tuple[ConfigReference, ...]`; `validate(config) -> None`.
- Resource: `dependencies()` (renamed from `referenced_resources`).
- Readiness hook: `not_ready(config[, dep_verdicts]) -> Readiness`.

## Sequencing rationale

Ordered so each step ends green: (1) split the capability contract (`dependencies` + `validate`) and
centralize the sourceless→sourced mapping, with resources still calling both to preserve behavior;
(2) introduce the retained `DependencyGraph` and have `finalize` populate it from the built edge
map, with consumers still on their old paths; (3) unconditional publication (R13) and the readiness
fold, moving readiness onto the graph; (4) readiness-gated materialization (R12); (5) migrate
consumers onto the graph one at a time (cycle detection, walk, node factories, inspect, doctor,
op-time refs); (6) land the guard once the bypasses are gone. LLDs are warranted for the graph data
structure + query semantics, the finalize pass ordering + materialization fixpoint, and the
readiness-fold contract.

## Risks and mitigations

- **The materialize-after-readiness interleave** is the subtlest change (component 3). Mitigation:
  an LLD pins the exact pass ordering and the auto-declarable-counts-as-satisfied rule; tests assert
  a host-disabled site's secrets stay absent (R12) while a ready site's still materialize.
- **The construct-time validity invariant** (`base.py:288-308`) must survive:
  `dependencies`/`not_ready` tolerate unvalidated config, but `validate` and construction still
  guarantee validity for the resources that run. Mitigation: keep construct-time `validate`; a test
  pins that a ready resource's malformed block still fails.
- **Behavior deltas (FRD R9)** are real (typo now hard-errors, error reordering, deferred
  validation, unsupported-platform row). Mitigation: the R9 delta list is the acceptance contract,
  pinned by tests.
- **Op-time secret refs** (component 6) are the one path not naturally graph-fed; the LLD confirms
  single-derivation vs graph-threading before implementation.
- **Host-conditional edges today** (`VMSite` suppression) become host-independent only because of
  R13; if R13 regressed, absent platforms would hard-error. Mitigation: the fold + unconditional
  publication are landed together (sequencing step 3), with a non-Windows test asserting the bundled
  `wsl2` site is not-ready, not an error.

## What does not change

The capability _kinds_ and `KIND_REGISTRY`; the manifest loader and envelope; operator TOML/YAML
surfaces; the collision policy (`_check_collision`); reserved-default always-materialization; the
post-finalize boundary checks (`secrets.validate_chain`, `vm_sites.validate_sites`); and the
capability op lifecycle (`preflight`/`runup`), which stays the deeper, live readiness boundary
distinct from the offline `not_ready`.
