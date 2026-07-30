# HLA: registry readiness refactor

Implements the [FRD](./frd.md). The registry gains one new retained object (a first-class dependency
graph), the capability contract splits one method into two, `finalize` is re-expressed as ordered
passes over that graph, and every consumer that today recomputes edges or readiness is routed
through it. The **kind vocabulary** and the collision policy are unchanged; but the vm-site's
readiness hook is renamed (R6), and the manifest decoder / TOML loaders stop validating capability
blocks (that moves to the finalize `validate` pass, R3), so "the loader" is touched at those call
sites.

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
- **impl (capability nodes only)**: a reference to the capability's implementation, so a dependent's
  readiness can run the capability's non-constructing `not_ready(config)` classmethod (component 4),
  and resolution can call a backend, by reading the impl off the graph node rather than the live
  capability registry. Note the registries are heterogeneous (`vm-platform` / `harness` /
  `git-credential-provider` hold impl _classes_; `secret-backend` holds a constructed _instance_),
  so the impl field carries whichever shape the kind uses.

Query API (the single access path, R11):

- `edges_of(kind, name) -> tuple[ResourceReference, ...]` (outbound).
- `dependents_of(kind, name) -> tuple[ReferenceEntry, ...]` (inbound / usage).
- `reachable_from(kind, name) -> Iterable[(kind, name)]` (transitive closure; replaces
  `collect_secrets_for`'s hand-rolled DFS).
- `readiness_of(kind, name) -> Readiness` and `is_ready(kind, name) -> bool`.

The `references` field is removed from the resource dataclasses (the `DeclaredResource` base and
each capability `Entry`, `harness/kinds.py`, `git_credential/kinds.py`, `vm_platform/__init__.py`);
every reader moves to `dependents_of`. That is more than one site: `resources/inspect.py` **and**
`secrets/inspect.py` (`agw secret describe` reads `getattr(decl, "references", ())`) both consume
it, so both are on the caller inventory. The graph is the one home for outbound edges (node
factories, cycle detection, reachability), inbound usage (both inspect surfaces), and readiness
(inspect, doctor).

### 2. The capability-contract split (R2)

Replace `validate_config(owner, config) -> tuple[ConfigReference, ...]` with two methods on **all
four** capability kinds:

- **`dependencies(config) -> tuple[ConfigReference, ...]`**: total, never raises, returns the
  config-implied references as far as they are structurally derivable (omitting only an edge whose
  identity depends on a malformed field).
- **`validate(config) -> None`**: the throwing correctness check.

**The consuming resource owns its config and orchestrates.** Config lives on the consuming resource
(a `vm-site` owns `platform_config`, a `secret` owns `backend_mappings`), never on the capability
(the capability model's core tenet). The resource's own `dependencies()` / `validate()` /
`not_ready()` are where the business logic lives: they pull the relevant config sub-block(s) and
call the capabilities to validate them and extract their implied refs. That the config "belongs to"
a capability is just that resource's own impl detail.

`secret-backend` is a **full participant**, not a special case. A `secret`'s `backend_mappings` is
capability config (one mapping per backend) owned by the secret. Today's `validate_mapping`
(`secrets/backends.py`) is exactly the backend's `validate(mapping)` and its future
reference-deriving counterpart is `dependencies(mapping)` (the `SecretBackend` docstring already
anticipates this), both now run at **finalize**.

**The graph shows every candidate backend, not just the mapped ones.** A `secret` can be resolved by
any backend that would attempt it, and whether a backend requires a mapping is a **per-backend
property** (onepassword requires one; env-var / prompt are mapping-optional and attempt any secret).
So the `secret`'s own `dependencies()` is handed the **available-backend list** by the graph builder
(a controlled builder input, not an ad-hoc registry probe, component 7) and emits a
`secret -> secret-backend` edge for the union of (a) **every present backend that would attempt it**
(has a mapping, or is mapping-optional), minus an explicit `false` opt-out, and (b) **every explicit
non-`false` mapping key the secret names**, even a name with no present backend. Clause (b) is what
makes a typo'd key (`backend_mappings.onepasword`) a `secret -> secret-backend` edge to an
**absent** target, which the `"error"` miss policy then hard-errors (R9.11); without it a typo'd key
would emit no edge and lie dormant. `edges_of(secret)` is therefore the full candidate set (plus any
dangling typo edge), and `edges_of` for an auto-declared secret (`tailscale-auth-key`) correctly
yields the default backends. Resolution (component 6a) filters the present candidates by
`enabled ∧ ready ∧ opted-in` and walks them in chain order; the would-attempt logic lives here in
edge-building, where the secret orchestrates it via the backends. The `secret` implements no
`not_ready` (opts out; always ready). (`would_attempt` must stay a **pure function of
`(secret, mapping)`** with no host probing, so that freezing it into edges at finalize is safe; this
is a constraint on the `SecretBackend` contract.)

**Mapping validation.** The `secret` is always-ready, so its `validate()` runs in the finalize
validate pass and validates each mapping addressed to a **present, enabled** backend via that
backend's `validate(mapping)` (a pure offline shape check, so it runs regardless of that backend's
readiness). This is **every declared mapping, not just the opted-in ones** (FRD R9.9). The
loop-owned `false` opt-out is never passed to `validate`. A mapping key naming an **absent** backend
emits the edge (clause (b) above) that the `"error"` miss policy hard-errors (a typo); it is never
validated, because there is no backend to validate it. A mapping to a present-but-**disabled**
backend is **not validated** either (inert until enabled, so a disabled plugin's validate is never
invoked). In this effort all backends are present, enabled, and ready, so `validate` runs over every
mapping; the absent case is a typo and the disabled case is latent until the plugin work.

The sourceless→sourced conversion (`ConfigReference` → `SecretReference` when `kind == "secret"`,
else `ResourceReference`, attaching `source`) that is triplicated across the `referenced_resources`
bodies today is centralized into one helper. The resource-level method `referenced_resources()` is
renamed to **`dependencies()`** and becomes: emit the bare capability edge(s), then append the
capability's `dependencies(config)` mapped through that helper. No validation, no throwing, and **no
host-conditional suppression** (component 5 makes the platform node always present, so the vm-site
always emits its platform edge). `EnvEntry.referenced_resources(source)` (the one arg-taking
variant, `env/entry.py:38`) is deliberately left un-renamed: it is not a graph-node method the
builder calls, only an internal aggregation each template's `dependencies()` composes, so renaming
it would be churn without meaning.

### 3. `finalize` as ordered passes producing the graph (R8, R12)

```text
build:        walk every declared row's dependencies() (total, non-throwing) -> outbound edge map
resolve:      for each edge whose target has no node:
                - miss_policy "error"                            -> hard error now (R7: absent = typo)
                - "auto-declare", name NOT in auto_declare_names  -> hard error now (ungated, as today)
                - "auto-declare", allowed name                   -> defer to materialize (below)
              (reserved-default names are always-materialized up front, as today)
cycle-detect: three-coloring over the BUILT edge map (no re-derivation; fixes today's re-walk)
readiness-fold: reverse-topological; hand each node its deps' verdicts + enablement; store per node.
                a deferred (allowed-name) auto-declare target counts as satisfied, not "absent"
materialize:  synthesize each deferred auto-declare target a READY, ENABLED node references (R12);
              a target referenced only by not-ready/disabled nodes does not materialize. THEN walk
              each newly-materialized node's dependencies() into the edge map, resolve those targets,
              and fold their readiness -- looping until no new node appears (the fixpoint).
attach:       inbound references + description polish, onto the graph
validate:     validate(config) over the ready+enabled set only (throwing, precise file:line)
freeze
```

Two subtleties this ordering pins:

- **The miss policies do not partition into "hard vs gated" cleanly; auto-declare is itself split.**
  Today an auto-declare miss for a name NOT in `auto_declare_names` is an eager hard error
  (`registry.py:337-343`, e.g. a typo'd `inherits = ["defualt"]`). That hard error must stay
  ungated, or a typo referenced only by a not-ready node would be silently dropped (a delta not in
  R9). Only an _allowed-name_ auto-declare miss is deferred and readiness-gated.
- **A materialized node is NOT a leaf, so materialize must walk its edges (load-bearing, not a
  hedge).** An auto-declared `secret` (`tailscale-auth-key`, the `git-token-*` family) has outbound
  `secret -> secret-backend` edges per component 2; if `materialize` synthesizes it without walking
  those edges, `edges_of` returns nothing and resolution (6a) finds zero candidate backends, so
  every VM create fails to resolve its tailscale key. So `materialize` **loops**: synthesize, then
  walk the new node's `dependencies()`, resolve its targets, and fold it, until stable, exactly the
  worklist behavior today's `finalize` has and an earlier draft wrongly dropped. It terminates
  because a materialized secret's edges target only already-present, already-folded backend nodes
  (the real invariant is "a late-materialized node's out-edges target only already-present,
  already-folded nodes," not the false "secrets have no edges"). The finalize-ordering LLD owns this
  loop, the ungated auto-declare sub-branch, and whether a not-ready referrer's edge still
  contributes to a (separately-materialized) secret's inbound `dependents_of` / `describe` output.

### 4. The readiness fold and the `Readiness` verdict (R4, R5, R6, R10)

The `disabled_reason` hook is renamed **`not_ready`** and re-shaped from `(registry, resource)`
(which reached into the live registry) into pure, offline, **non-constructing** functions the fold
feeds:

- capability: `not_ready(config) -> Readiness`, a classmethod taking **best-effort** config,
  offline, checking host support (`unsupported_reason`) and any config-dependent host/tool check (a
  local-Lima platform needs local `limactl`). It **does not construct** an instance and **does not
  validate**, so the fold never triggers the throwing construct-time validator on possibly-invalid
  config (B1); it reads only the fields it needs, tolerating malformed ones.
- consuming resource: `not_ready(config, {dependency -> DependencyState}) -> Readiness`, a pure
  function of its own best-effort config and its dependencies' states, never querying a live
  registry.

The fold hands each node its dependencies' **`DependencyState`**, not a bare `Readiness`: it carries
the dependency's **enablement** (enabled / disabled) and, when enabled, its readiness (ready /
not-ready + reason). That is what lets R7's propagating kinds distinguish "dependency disabled" from
"dependency not-ready" and read the "enable its unit" hint off the disabled node, without
re-conflating the two axes. `readiness_of(kind, name)` returns the node's stored state; a
present-but-**disabled** node reports **disabled** (the enablement axis), distinct from not-ready,
and readiness is computed only for enabled nodes.

`Readiness` is a small verdict object (ready, or not-ready with a reason), stored on the graph node;
consumers read it via `readiness_of` (R10), sparing them the `str | None` double negative.
**Readiness is self-determined (R4): the fold distributes verdicts and imposes no propagation
rule.** A `vm-site` chooses to propagate (not-ready if its single platform is not-ready or
disabled); a `secret` implements no `not_ready` at all, so it is always ready regardless of its
backends (resolvability is a resolution-time question, component 6a). A resource is free to combine
its dependencies' verdicts however its impl dictates (a vm-site's single-platform AND is not a
secret's many-backend OR).

**Where today's `site_disabled_reason` three-step chain lands** (the fold LLD's central question):
platform-missing collapses to the resolve-time hard error (a typo; a supported platform is always
present under R13); platform-**unsupported** (config-independent) is the platform node's own
`not_ready()` verdict, folded into the site; the **config-dependent tool check** (a local-Lima site
needs local `limactl`, keyed on the site's `platform_config`) is the site's own `not_ready`, which
calls the platform's `not_ready(site_config)` classmethod read **off the graph node's impl**
(component 1), not the live registry, so within the guard. Crucially this is a **non-constructing**
call: it does not build a platform instance and so does not re-run the throwing construct-time
validator (`base.py:288-308`), which the old `VM_PLATFORM_REGISTRY[name](site_config)` path did,
that is what avoids B1's collision between the fold's totality (R1/R4) and the construct-time
invariant (R3), and the R9.4 loop where a malformed block would otherwise become a permanent
readiness reason. The fold LLD pins whether the capability exposes one `not_ready(config)` or a
config-independent + config-dependent pair, and confirms the offline-and-cheap contract (no network,
secrets, prompting, or construction; where a check needs a minimal context, the fold builds a fresh
`RunContext`, which cannot be `dataclasses.replace`d, `base.py:206-212`).

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

### 6a. Secret resolution as a distinct layer over the graph (R11)

Secret resolution stops recomputing and becomes a pure consumer of the graph, sitting **on top of**
the registry (never folded into the finalize passes). At resolution time, for each secret:

1. Read its candidate backends off the graph. `edges_of(secret)` is the **full** candidate set
   (every would-attempt backend, per component 2), each with its node's **stored** readiness
   (computed once at finalize). No would-attempt recomputation and no default-derivation are needed
   here; the edges already encode it.
2. Apply the operator's **opt-in resolution chain** (`secret_config.backends`: which backends
   participate, in what order). This is the one resolution-layer config input; it never touches
   finalize.
3. Walk the candidates that are **opted-in**, in opt-in order, skipping any that are not **present ∧
   enabled ∧ ready**, calling each usable backend's resolution op (`batch_get`) with the mapping
   read from the secret's own `backend_mappings` (config on the resource). Batching is preserved:
   read candidates per secret, group by backend, one `batch_get` per backend. **Result and skip
   semantics** (E):
   - a **not-ready** opted-in backend is **skipped with a warning** ("secret X: skipping
     onepassword, not ready: `op` not installed"), so the fall-through to a later candidate
     (possibly `prompt`) is never silent. This is an R9 delta: today a mapped-but-unavailable store
     raises `ConnectivityError` and **halts**; now it warns and falls through.
   - a **soft miss** (a present, ready backend returns without the value) falls through to the next
     candidate, unchanged.
   - a **hard miss** (`SecretMappingError`: a present, ready store definitively has no value) or a
     transport/auth failure still **halts** the chain, unchanged, preserving the anti-masking
     property for a store that is usable but says no.

This deletes two current recompute / registry-probe paths: `validate_chain` re-deriving the chain,
and the resolver reaching into `SECRET_BACKEND_REGISTRY`. Readiness is read, never re-checked.
(**Not** the construct-time `_secret_refs` path: that is the `Capability`-base cache for
`Harness.secret_refs` / `GitCredentialProvider.secret_name`, unrelated to secret resolution; it is
kept and repurposed to `dependencies(config)` as the single sanctioned derivation, guard-exempt, per
components 6 and 7.)

**Reachability stays an eager, fail-fast, build-time check** (not moved to lazy resolution time).
The "every operator-declared secret is resolvable" check that `validate_chain` runs post-finalize at
`build_registry` stays there, preserving today's property that any resource-touching command
fails-fast with config vocabulary. What changes is only its implementation: it **reads the graph**
(candidate backends off `edges_of`) plus the opt-in chain, instead of re-deriving. Two invariants it
must keep, easy to drop in a rewrite: it is scoped to **operator-declared secrets only** (an
auto-declared secret cannot invalidate a deliberate `backends = []` opt-out; it surfaces at use-time
as `SecretUnavailableError`), and it is keyed on **would-attempt, not readiness** (a secret whose
only opted-in backend is not-ready is still reachable and fails only at resolution, as today, so
readiness introduces no new build-time hard failure).

**Vocabulary, kept distinct throughout.** A backend is **present** (a node exists: a built-in, or an
installed plugin whether or not enabled), **enabled** (turned on, the plugin / three-tier axis where
"enabled/disabled" lives; a present-but-disabled backend is a distinct tier), **ready**
(host-usable, its own `not_ready`), and **opted-in** (named in `secret_config.backends`, a
resolution-layer selection and order). Resolution uses **present ∧ enabled ∧ ready ∧ opted-in**;
"enabled/disabled" is never reused for opt-in. (In this effort no backend is ever disabled, so
present implies enabled; the distinction is latent until the plugin work.)

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
- **Site selection and the use-time gate**, `select_site` (`sites.py:146,150`), `resolve_site` /
  `ensure_site_enabled` (`sites.py:243,258`), doctor's `defaults.site` warning (`doctor.py:295`),
  and `resource.py:111`, all recompute readiness lazily today (an R11-banned pattern) and carry
  "disabled" operator strings; they read `readiness_of` and adopt the readiness vocabulary. Note
  `ensure_site_enabled(decl)`'s signature must gain graph access. The three `validate_config` calls
  in `migrate/planning.py:458,502,548` move to the split shapes.
- **The preflight resolvability predictor** (the `preview_resolution` caller that passes
  `interactive_available=is_interactive()`, issue #202) becomes readiness-aware in lockstep with
  resolution's new skip, or it would predict "would resolve via onepassword" for a backend
  resolution will skip.
- **Op-time held-capability secret refs** (`Harness.secret_refs`,
  `GitCredentialProvider.secret_name`, from construct-time `_secret_refs`): the single shared
  derivation becomes `dependencies(config)` (the same total function the graph is built from), so
  build-time and op-time agree by construction rather than via a second method. Threading the frozen
  graph itself to op time is possible but not required; the requirement is one derivation of the
  edge truth, not two divergent ones (a decision to confirm in the LLD).

### 7. The anti-bypass guard (R11)

A guard test pins the banned patterns so they cannot return. The banned pattern is **re-deriving the
graph's structure or readiness outside the build**: re-walking resources' `dependencies()` to
reconstruct the edge set (cycle detection, `walk`, node factories), a `*_REGISTRY.get(...)`
availability probe in edge production or readiness, a lazy readiness recompute, or reading
edges/usage off a resource dataclass. It is **not** a ban on the word `dependencies` everywhere: a
capability instance computing **its own** config-implied refs from **its own** config via
`dependencies(config)` at construct (for `_secret_refs`, component 6) is the single sanctioned
derivation and is explicitly exempt (it derives one node's refs, it does not re-walk the graph).
Likewise, the graph **builder** handing a resource's `dependencies()` a controlled context (the
available-backend list a `secret` needs to emit its candidate edges, component 2) is a builder
input, not the banned ad-hoc `*_REGISTRY.get(...)` probe, the ban is on a consumer reaching into the
live registry on its own, not on the builder supplying structured context during the build. The
guard test must encode both exemptions, or the honest path and the banned pattern are the same call.
The authoritative, complete migration list is the caller inventory below, not component 6's prose.
The dated **caller inventory** (already scouted: the `validate_config` / `disabled_reason` /
`referenced_resources` / `unsupported_reason` / `references`-field sites) is the guard's baseline
and the migration checklist; it lands as a feature artifact.

### 8. Operator surfaces: secret CLI and doctor (R6, R9, R10)

The secret/backend surfaces already show most of this picture (`secret describe` has "Backend
mappings" and a "Resolution preview"; `secret list` is a per-backend grid; `doctor` reports one row
per secret); the refactor makes them **readiness-aware** and disambiguates the overloaded "disabled"
vocabulary. Exact output formats are the operator-surfaces LLD; the architecture-level changes:

- **Secret backends gain an offline readiness check.** A backend's host tool check (is `op` on
  `PATH` for onepassword, analogous to `limactl` for lima) becomes its `not_ready` verdict, computed
  once and stored on its graph node. It is cheap and offline: it does **not** probe the store (no
  biometric). Interactivity (the prompt / biometric) stays previewed **optimistically** as today, so
  readiness (offline, honest) and interactivity (the interaction, optimistically assumed) are
  orthogonal dimensions.
- **`secret list` grid.** Columns stay the **opted-in** backends (`secret_config.backends`), so the
  grid disambiguates only the states an opted-in backend can be in: the lookup identifier when it
  **would attempt** (has a mapping / mapping-optional), an explicit **not-ready** cell with reason
  when it can't run here (`not ready: op not installed`), and a **won't-attempt** cell (a `false`
  opt-out or a mapping-required backend with no mapping) instead of today's overloaded literal
  "disabled". Not-opted-in and (future) not-enabled are not grid states, a not-opted-in backend has
  no column, and enablement surfaces in `secret describe` and the doctor backend group, not here.
  This is what FRD R9.7 promises, corrected to the grid's actual column policy.
- **`secret describe`.** "Backend mappings" and "Resolution preview" become readiness-aware: a
  not-ready backend is shown as such and does not count toward "would resolve via X"; the preview
  walks **present ∧ enabled ∧ ready ∧ opted-in** candidates. The interactive-optimism is preserved
  (describe reports configured capability, not this run's TTY); readiness is the new honest offline
  layer under it.
- **Resolution and `env show`.** A not-ready opted-in backend is **skipped with a warning** and the
  "no backend could resolve" error names readiness reasons (E); the anti-masking halt is kept for a
  ready store's hard miss (component 6a). The `preview_resolution` **preflight predictor** becomes
  readiness-aware in lockstep (component 6), so it never predicts "would resolve via onepassword"
  for a backend resolution will skip. `env show --reveal-secrets` is renamed **`--resolve`** (R9.8).
- **Doctor.** `_check_vm_platforms` / `_check_vm_sites` read the **stored** offline readiness off
  the graph instead of recomputing `unsupported_reason` / `site_disabled_reason` ad hoc, while the
  live `preflight` (network) stays the deeper op-boundary check, now cleanly separated from the
  offline verdict. A **new secret-backends group** parallel to `_check_vm_platforms` reports one
  readiness row per backend (`[ok]` / `[not ready]: <reason>`), backends are capabilities now.
  `_check_secrets` stays one row per secret but becomes readiness-aware. The rename retires
  "disabled" for host readiness in every doctor string (R6).

Vocabulary these surfaces must keep straight (the hotspot): a backend is **present** (a node
exists), **enabled** (turned on, where "enabled/disabled" lives), **ready** (offline host check,
"not ready" + reason), **opted-in** (`secret_config.backends`, the resolution chain), and
**would-attempt** (has a mapping, or is mapping-optional, for this secret). Each surface says
exactly which it means.

## Interfaces (summary)

- Graph: `edges_of`, `dependents_of`, `reachable_from`, `readiness_of`, `is_ready`.
- Capability: `dependencies(config) -> tuple[ConfigReference, ...]`; `validate(config) -> None`;
  `not_ready(config) -> Readiness` (offline, non-constructing).
- Resource: **`dependencies(context)`** (renamed from `referenced_resources`), where `context` is a
  small build-context object the builder passes to **every** resource uniformly; most resources
  ignore it, the `secret` reads the available-backend list off it. (As shipped, `BuildContext`
  carries only `available_backends`, the one load-bearing field: the sole consumer is the secret's
  edge emission, and `would_attempt` is pure over `(secret, mapping)`, so the earlier "read-only
  graph-in-progress" field was deliberately not built, no resource needs it. Add it only if a future
  consumer does.) The uniform signature (over special-casing `secret` in the builder loop) keeps
  R2's one-shape rule and keeps the builder walk (`registry.py:321`) uniform.
- Readiness hook (resource): `not_ready(config, {dependency -> DependencyState}) -> Readiness`,
  where `DependencyState` carries the dependency's enablement and (if enabled) readiness.

## Sequencing rationale

Ordered so each step ends green: (1) split the capability contract (`dependencies` + `validate`) and
centralize the sourceless→sourced mapping, with resources still calling both to preserve behavior;
(2) introduce the retained `DependencyGraph` and have `finalize` populate it from the built edge
map, with consumers still on their old paths; (2b) move config validation out of manifest-decode
(`manifests/decode.py:176,242,310`) and the TOML loaders (`config/loaders_resources.py:430`,
`config/loaders_sessions.py:163`) into the finalize `validate` pass, keeping construct-time
validation (the invariant) intact, the green-window care here is that decode/load must stop throwing
on a capability block in the same change that the finalize pass starts, so no window validates
neither; (3) land **together, atomically**: R13 unconditional publication, the readiness fold
(readiness onto the graph), readiness-gated materialization (R12), and removal of the vm-site
edge-suppression. These are one step by necessity: the suppression does two jobs (avoid the platform
error-miss; keep a can't-run site's secrets from materializing), R13 replaces job 1 and R12 replaces
job 2, and splitting them opens a non-green window where the suppression is gone but materialization
is still ungated, regressing R12. (4) migrate consumers onto the graph one at a time (cycle
detection, walk, node factories, inspect, doctor, op-time refs, and secret resolution per component
6a); (5) land the operator surfaces (component 8: the readiness-aware secret CLI + the new doctor
backend group + the `--resolve` rename) together with the docs and help-text updates that make them
true (`docs/guides/resources.md`'s "Secrets: backends and the chain", `sample-config.toml`,
`cli/README.md` (line ~787 still shows `--reveal-secrets`), the command/section help strings, and
the completion tree the project rule requires for a CLI-surface change — the Typer-extracted spec
likely flows the `--resolve` rename through, but it must be verified, and whether `--reveal-secrets`
survives as a deprecated alias decided); (6) land the guard once the bypasses are gone.

LLDs: (a) the graph data structure + query semantics (including capability nodes carrying their
impl); (b) the finalize pass ordering, which owns the auto-declare hard sub-branch, the
suppression-and-gating co-location, the materialization fixpoint premise, and the partial-readiness
secret-`describe` question; (c) the readiness-fold contract, which owns the
platform-node-vs-site-node check split, the limactl construct-for-tool-check seam off the graph
impl, and the minimal `RunContext`; (d) the secret-resolution layer (component 6a), owning the graph
read, the present/ready/opted-in walk, the eager fail-fast reachability check, and batching, and
carrying an explicit acceptance line that it **preserves the operator-declared-only reachability
scope, the would-attempt (not readiness) keying, and the soft/hard miss semantics**; (e) the
operator surfaces (component 8), owning the `secret list`/`describe` output, the new doctor
secret-backends group and the readiness-aware secret rows, and the exact operator strings + docs,
with an acceptance line that the interactive-optimism preview is unchanged. The guard's
banned-pattern definition + construct-time exemption is specified as a section of (b) or (c),
sharpened enough to be a real test.

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
  R13; if R13 regressed, absent platforms would hard-error. Mitigation: R13, the fold, R12 gating,
  and suppression removal land together in one atomic step (sequencing step 3), so there is no
  window where the suppression is gone but materialization is ungated (an R12 regression) or the
  platform edge dangles; a non-Windows test asserts the bundled `wsl2` site is not-ready, not an
  error, and a host-disabled site's secrets stay absent.

## What does not change

The capability _kinds_ and `KIND_REGISTRY`; the manifest **envelope** and loader mechanics (only the
capability-block validation calls inside decode/load move, per step 2b); operator TOML/YAML
surfaces; the collision policy (`_check_collision`); reserved-default always-materialization; and
the capability op lifecycle (`preflight`/`runup`), which stays the deeper, live readiness boundary
distinct from the offline `not_ready`.

Changing (called out so the list above stays honest): capability-block validation moves out of
manifest-decode and the TOML loaders into the finalize `validate` pass (R3, step 2b);
`secrets.validate_chain` splits, its per-mapping spec validation moving into that same pass
(component 2) while its chain reachability check stays an eager post-finalize boundary check
(fail-fast preserved), now reading the graph instead of re-deriving (component 6a);
`vm_sites.validate_sites` is today a pure `defaults.site` existence lookup (`sites.py:266-291`), not
a readiness re-derivation, so it is essentially unchanged except that the name it looks up is now
resolved against the graph.
