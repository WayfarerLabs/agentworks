# LLD (c): the readiness fold and the `Readiness` verdict

Implements HLA [component 4](./hla.md). Owns the fold algorithm, the `Readiness` and
`DependencyState` types, the platform-node-vs-site-node check split, the non-constructing tool-check
seam off the graph impl (avoiding B1), and the minimal-`RunContext` fallback. Governs FRD R4, R5,
R6, R10, R13 (readiness side).

## The shared verdict types

```python
class Enablement(Enum):
    enabled = "enabled"
    disabled = "disabled"

@dataclass(frozen=True)
class Readiness:
    reason: str | None            # None => ready; a string => why not ready
    @property
    def is_ready(self) -> bool: return self.reason is None
    @classmethod
    def ready(cls) -> "Readiness": return cls(None)
    @classmethod
    def blocked(cls, reason: str) -> "Readiness": return cls(reason)

@dataclass(frozen=True)
class DependencyState:
    enablement: Enablement
    readiness: Readiness | None   # None iff disabled (readiness is computed only for enabled nodes)
    impl: object | None           # the dependency's capability impl, for a config-dependent check
```

`Readiness` is what `readiness_of` returns (R10, no `str | None` double negative). `DependencyState`
is what the fold **hands each node about each of its dependencies**: the dep's enablement, its
readiness when enabled, and its impl so the node can run a config-dependent capability check
**without reaching into a live registry** (the impl came from the graph via the fold, so this stays
guard-clean, R11).

## The two readiness hooks (the rename, reshaped)

Today there are two `disabled_reason`s. Both are renamed and reshaped:

1. **`Capability.disabled_reason(self) -> str | None`** (`base.py:340`), an **instance** method
   reading `self.config` (the config-dependent tool check, e.g. lima's local-`limactl`,
   `lima.py:99`). It becomes a **non-constructing classmethod** `not_ready(config) -> Readiness`. It
   reads the config fields it needs (`config.get("vm_host")`), tolerates malformed ones, does
   **not** construct an instance, and does **not** validate. Default (base): `Readiness.ready()`.
2. **`_VMSiteKind.disabled_reason(registry, resource)`** (`vms/kinds.py:197`, which reached into
   `VM_PLATFORM_REGISTRY`) becomes the resource-level `not_ready(config, deps) -> Readiness`, pure
   over its own best-effort config and its dependencies' `DependencyState`s, never querying a live
   registry.

**`unsupported_reason()` keeps its name** (it is host-support, not "disabled" vocabulary) and stays
a config-independent classmethod on `VMPlatform`. It is the source for the **platform node's own**
readiness (below), not a "disabled" surface, so R6's rename does not touch it.

## The platform-node vs site-node check split (the central question)

The `site_disabled_reason` three-step chain (`sites.py:169-194`) splits by **owner**, and this is
the crux of avoiding B1:

- **platform-missing** (`VM_PLATFORM_REGISTRY.get(...) is None`): collapses to the pass-2
  resolve-time **hard error** (absent = typo, R7). Under R13 a supported-or-not platform is always
  **present**, so the only miss is a genuinely unknown name.
- **platform-unsupported** (`unsupported_reason()`, config-**independent**): the **platform node's
  own** readiness. The fold computes a `vm-platform` node's `Readiness` from `unsupported_reason()`
  (ready when `None`, else `Readiness.blocked(f"platform '<name>' is unsupported here: {reason}")`).
  This is handed to the site as the platform's `DependencyState.readiness`.
- **the config-dependent tool check** (local-Lima needs local `limactl`, keyed on the **site's**
  `platform_config`): the **site's own** `not_ready`, which calls the platform's
  `not_ready(site.platform_config)` classmethod **off the graph node's impl**
  (`DependencyState.impl`), non-constructing.

This split is why a config-independent + config-dependent **pair** is the right shape, not one
method: the two checks are owned by two different nodes. `unsupported_reason` cannot be folded into
`not_ready(config)` because calling `not_ready({})` on lima would read an empty config as "local"
and wrongly block the lima **platform node** on a missing `limactl`, even though a **remote** lima
site needs none. The platform node owns host-support (config-free); the site owns the tool check
(with its own config). Self-determination (R4) is exactly this: the site does not blindly inherit
the platform's verdict, it re-asks with its own config.

### The vm-site `not_ready`

```python
def not_ready(self, config, deps) -> Readiness:
    platform = deps[("vm-platform", self.platform)]
    if platform.enablement is Enablement.disabled:
        return Readiness.blocked(f"depends on vm-platform '{self.platform}', which is disabled; enable its unit")
    if not platform.readiness.is_ready:
        return Readiness.blocked(f"platform '{self.platform}' is not ready: {platform.readiness.reason}")
    # config-dependent tool check, off the graph impl, NON-constructing (no validate re-run):
    return platform.impl.not_ready(self.platform_config)
```

The "enable its unit" hint is read off the disabled dependency's own `DependencyState` (R7), no
diagnosis at a miss point. In this effort no node is ever disabled, so the first branch is exercised
only by the test fixture; it ships for the plugin rebuild.

### Why non-constructing avoids B1

The old chain's last step constructed the platform (`platform_cls(decl.name, decl.platform_config)`,
`sites.py:194`), and construction re-runs the throwing `validate_config` (`base.py:288-308`). If the
fold constructed, then (a) the fold would no longer be total (a malformed block would throw
mid-fold, violating R1/R4), and (b) a malformed block would become a permanent readiness reason (the
R9.4 loop). The reshaped `not_ready(config)` is a plain classmethod that reads config fields
best-effort and never constructs or validates, so the fold stays total over unvalidated config while
`validate` and construction still guarantee validity for the resources that actually run (R3, R5).

## Readiness by node type (what the fold computes)

| Node                                 | Its own readiness                                                                                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm-platform`                        | `unsupported_reason()` wrapped (config-independent)                                                                                                       |
| `secret-backend`                     | the backend instance's config-independent host-tool check (`op` on PATH, etc.); see below                                                                 |
| `harness`, `git-credential-provider` | always ready (no host-support, no override)                                                                                                               |
| `vm-site`                            | the `not_ready(config, deps)` above                                                                                                                       |
| `secret`                             | **no `not_ready`**: always ready (opts out; resolvability is a resolution-time question, LLD d)                                                           |
| `git-credential`, `session-template` | no readiness propagation this effort; default ready (they may fold their provider/harness `DependencyState` later, but those deps are always ready today) |

### secret-backend node readiness

Secret-backend impls are **instances** (heterogeneous impl, LLD a), and a backend's readiness is
config-independent (its host tool is present or not, irrespective of any per-secret mapping). So the
`SecretBackend` protocol gains `not_ready(self) -> Readiness` (no config arg), an offline host
check: `onepassword` checks `op` on PATH (`shutil.which`, a pure presence test, no biometric, no
store probe); `env-var` and `prompt` are always ready. The fold calls it once per backend node and
stores the verdict (R9.6 gives backends offline readiness). This is orthogonal to
interactive-optimism (the prompt/biometric stays optimistically previewed; LLD e).

## The fold algorithm

Runs as finalize pass 4 (LLD b), after cycle detection (needs an acyclic graph):

1. Reverse-topologically order the nodes (dependencies before dependents). The order comes from the
   built edge map (a standard DFS post-order over `edges_of`); cycle detection has already
   guaranteed acyclicity.
2. For each node in that order:
   - Gather its dependencies' `DependencyState`s (already computed, since deps precede it), each
     carrying the dep's enablement, readiness, and impl (impl from the graph node, LLD a).
   - Compute the node's own readiness by calling its readiness hook: a capability node's
     config-independent source (table above); a consuming resource's `not_ready(config, deps)`.
   - Store the `Readiness` verdict on the graph node (LLD a). Only **enabled** nodes get a computed
     readiness; a **disabled** node's `DependencyState.readiness` is `None` (enablement is the axis
     that answers for it).
3. Materialization (pass 5) folds late-materialized nodes the same way (LLD b's loop); their deps
   (backend nodes) are already folded, so the reverse-topological invariant holds.

The fold **imposes no propagation rule** (R4): it only distributes `DependencyState`s. Whether a
node propagates (vm-site: single-platform AND), combines (a hypothetical multi-dep AND/OR), or opts
out (secret: no hook) is the node's own business. This is what corrects the FRD's earlier "not-ready
if any dependency is not-ready" overreach.

## The minimal `RunContext` fallback seam

The current offline checks (`limactl`, `op` via `shutil.which`) need **no** `RunContext`. The seam
the HLA flags is a fallback: if a future readiness check needs a minimal context, the fold
constructs a **fresh** `RunContext` (it cannot be `dataclasses.replace`d, `base.py:206-212`), never
a resolver or secrets. This LLD does not use it; it is documented so a later check does not reach
for a live context or a construct. The offline-and-cheap contract (R10) is affirmed: no network, no
secrets, no prompting, no construction.

## Acceptance

- R9.5: `wsl2` on a non-Windows host is a **not-ready `vm-platform` node**, and the bundled `wsl2`
  site is **not-ready, not a hard error**.
- A local-lima site with no `limactl` is not-ready with "limactl not installed"; a remote-lima site
  (`vm_host` set) is ready with no `limactl` (config-dependent, off the graph impl,
  non-constructing).
- B1: a vm-site with a **malformed** `platform_config` does not throw during the fold (the fold is
  total); it still fails the finalize `validate` pass **if it is ready+enabled** (R5), and a
  not-ready site's malformed block is deferred (R9.4).
- The fixture disabled `vm-platform` node yields a site not-ready with the "enable its unit" hint
  (the enablement branch), proving the axis without a real producer (R7).
- `secret` nodes are always ready regardless of backend readiness (R4 opt-out); a `secret` whose
  only opted-in backend is not-ready is still **ready** (resolvability is LLD d's concern).
- The guard (LLD b) confirms `not_ready` is called only by the fold, and no readiness path reads
  `VM_PLATFORM_REGISTRY` / `SECRET_BACKEND_REGISTRY`.
