# LLD (a): the plugin framework (descriptor, atomic registration, adapters, collisions)

Implements HLA [components 2, 3](./hla.md) and part of [7](./hla.md). Governs plan
[Phase 2](./plan.md); FRD R2, R3, R5 (registration half), R6, R7. Owns the `Plugin` descriptor and
its immutability, the atomic, validating `register_plugin`, the per-capability-kind
`CapabilityAdapter` table, the inverted installed index, the seat/unseat test helper, and the
`_check_collision` `system-plugin` matrix. It builds no publish step and no enablement (LLDs b, c);
it is provable in isolation against a fixture, with the shipped index empty.

## Where it lives

A new package `agentworks/plugins/`:

- `base.py`: the `Plugin` descriptor, the reserved `PluginCommand` frame, `PluginError`.
- `adapters.py`: `CapabilityAdapter`, the four adapters, `CAPABILITY_ADAPTERS`.
- `registration.py`: `register_plugin`, the `seated_plugin` context manager, the collision precheck.
- `__init__.py`: the installed index `SYSTEM_PLUGINS` and its inverted build.

The `_check_collision` change is the only edit to an existing file
(`resources/registry.py:129-165`).

There is a stale, untracked `agentworks/plugins/__pycache__/` (leftover from the reset); the phase
starts by removing it so the fresh package is clean.

## The `Plugin` descriptor (R2, R10)

A frozen dataclass. All fields optional except `name`.

```python
@dataclass(frozen=True)
class Plugin:
    name: str
    description: str = ""
    capabilities: Mapping[str, tuple[type, ...]] = field(default_factory=dict)
    manifests: str | None = None
    required_scopes: tuple[ScopeLevel, ...] = ()   # reserved, inert (R10)
    commands: tuple[PluginCommand, ...] = ()        # reserved, inert (R10)
```

- **`capabilities`** is keyed by capability kind (`"vm-platform"`, `"harness"`,
  `"git-credential-provider"`, `"secret-backend"`); each value is a tuple of impl **classes**,
  **uniformly**, even for `secret-backend` (whose registry holds instances,
  `secrets/backends.py:185`). The class-vs-instance reconciliation is the adapter's job, not the
  descriptor's: every one of the four impls exposes `name`/`description` as **class attributes**
  (`VMPlatform.name` is a `ClassVar`, `vm_platform/lima.py:94`; `ShellHarness.name`;
  `AzDOCredentialProvider.name`, `git_credential/azdo.py:33`; `EnvVarBackend.name = "env-var"`,
  `secrets/env_var.py:42`, which satisfies the `SecretBackend` name/description properties at the
  class level), so identity is read off the class uniformly and only **seating** differs per kind.
- **`manifests`** is the importlib-resources package anchor whose `manifests/` subdirectory holds
  the plugin's YAML (or `None`). LLD (c) resolves and loads it; this LLD only stores it.
- **`required_scopes`** is the reserved least-privilege declaration (R10), typed to the existing
  `ScopeLevel` (`capabilities/base.py:64`), recorded and displayable (doctor, LLD c) but unenforced.
- **`commands`** is a **real, typed placeholder frame**, not `tuple[Any, ...]`:

  ```python
  @dataclass(frozen=True)
  class PluginCommand:
      """Reserved frame for a plugin-owned CLI command (R10). Inert in v1:
      nothing constructs or dispatches one. Typed so the field is a real
      shape a later effort populates, not an untyped hole."""
      name: str
  ```

- **`__post_init__`** normalizes `capabilities` to immutable ONLY: it rewrites the mapping to a
  `MappingProxyType` whose values are `tuple`s. It performs **no semantic validation** (name shape,
  adapter existence, impl typing): that is `register_plugin`'s whole-descriptor pass, which needs
  the adapter table and must not be imported into `base.py` (layering: `base.py` depends on nothing
  in `agentworks` beyond `ScopeLevel`). A `Plugin` is therefore constructible in a test without a
  registry; it becomes valid or rejected only when registered. This split is deliberate and pinned:
  immutability is a data invariant (enforced at construction, principle 6); descriptor **validity**
  is a registration-time contract (enforced in `register_plugin`, below).

`PluginError(StateError)` is the typed error for every malformed-descriptor / duplicate-plugin /
cross-plugin-collision case. It is a `StateError` family (`errors.py:66`), not `ConfigError`: a
shipped plugin is curated in-repo code, so a bad descriptor is a **framework/curation bug**, not
operator data. (Operator-facing plugin errors, an unknown enabled name or an unknown `[plugins]`
key, are `ConfigError`; those live in LLD c.) Every `PluginError` message names the offending
plugin.

## The `CapabilityAdapter` table (R5, R6)

One adapter per core capability kind, reconciling the heterogeneous registries behind a uniform seat
and build-row contract:

```python
class CapabilityAdapter(Protocol):
    kind: str
    def seat(self, impl_cls: type) -> None: ...            # write impl into the kind's registry under impl_cls.name
    def peek(self, name: str) -> object | None: ...        # the current occupant of that name, for the collision precheck
    def build_row(self, name: str, origin: Origin) -> Any: ...  # the kind's Entry dataclass, stamped with origin

CAPABILITY_ADAPTERS: Mapping[str, CapabilityAdapter]
```

Per-kind seat/build behavior (all keyed by `impl_cls.name`):

| kind                      | seat                                           | build_row (Entry, `resources/...`)                                       |
| ------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------ |
| `vm-platform`             | `VM_PLATFORM_REGISTRY[name] = impl_cls`        | `VMPlatformEntry(name, description=<seated>.description, origin=...)`    |
| `harness`                 | `HARNESS_REGISTRY[name] = impl_cls`            | `HarnessEntry(name, origin=...)`                                         |
| `git-credential-provider` | `GIT_CREDENTIAL_PROVIDER_REGISTRY[name] = cls` | `GitCredentialProviderEntry(name, origin=...)`                           |
| `secret-backend`          | `SECRET_BACKEND_REGISTRY[name] = impl_cls()`   | `SecretBackendEntry(name, description=<seated>.description, origin=...)` |

- **The instance trap is confined to `seat`.** `secret-backend` is the one kind whose registry holds
  a constructed **instance** (`secrets/backends.py:185`), so its adapter alone calls `impl_cls()`
  (once, at seat). The other three seat the class. This mirrors exactly how the graph's `impl_of`
  documents the same asymmetry (`graph.py:176-183`) and how the built-in publishers seat
  (`vm_platform/__init__.py:44` classes; `secrets/backends.py:185` instances).
- **`build_row` reads description off the seated impl** via `peek(name)` (the registry occupant),
  never re-instantiating and never trusting an unseated descriptor claim. `VMPlatformEntry` /
  `SecretBackendEntry` carry a `description` (`vm_platform/__init__.py:84`, `secrets/kinds.py:56`);
  `HarnessEntry` / `GitCredentialProviderEntry` do not. Because `build_row` consults the live
  registry, **a row can only be built for an actually-seated impl** (Fable hardening): if `peek`
  returns `None`, `build_row` raises a `StateError` (a publisher-invariant violation, the same
  failure class as `_impl_for`'s fail-fast, `graph.py:449-477`). This is the by-construction tie
  between publication and seating; publication (LLD c) never fabricates a row for a descriptor claim
  that did not seat.
- **The guard test** pins
  `set(CAPABILITY_ADAPTERS) == {kind for kind, h in KIND_REGISTRY.items() if h.category == "capability"}`.
  Today that set is exactly
  `{"vm-platform", "harness", "git-credential-provider", "secret-backend"}` (`vms/kinds.py:147`,
  `harness/kinds.py:57`, `git_credential/kinds.py:96`, `secrets/kinds.py:209`), matching the fold's
  `_CAPABILITY_KINDS` (`graph.py:302`). A future capability kind fails this test until its adapter
  exists, so "plugins contribute existing kinds only" (R6) cannot silently rot.

## `register_plugin(plugin)` (R2, R5, R6): validate-whole-then-seat-atomically

Runs at import time (invoked by the index, below), once per shipped plugin. Three ordered passes;
**no capability registry is mutated until every impl across the whole descriptor is known
seatable**, so seating is all-or-nothing by construction (no rollback path):

1. **Validate the whole descriptor** (before touching any registry). Each failure is a `PluginError`
   naming the plugin:
   - `plugin.name` is non-empty and `/`-free (it is the identity the origin taxonomy and future
     trust model hang off; `/` is reserved registry-wide, `registry.py:106`).
   - Every key of `plugin.capabilities` has an adapter in `CAPABILITY_ADAPTERS` (R6: a kind with no
     adapter is rejected here, so "existing kinds only" holds by construction, not convention).
   - Every impl is a **class** (`isinstance(impl, type)`) carrying a non-empty, `/`-free `name`
     class attribute. This catches the natural `secret-backend` trap (passing `EnvVarBackend()`
     instead of `EnvVarBackend`) as a typed error, not a later `AttributeError`, and catches a
     missing `name` as a typed error, not a raw `AttributeError` (both were Fable findings).
   - No **intra-descriptor** collision: no two impls in this descriptor (across all its kinds)
     resolve to the same `(kind, name)`.
2. **Cross-plugin collision precheck** (still no mutation). For every impl, `adapter.peek(name)`:
   - occupant is `None`: seatable.
   - occupant is the **same class** (or, for `secret-backend`, an instance of the same class):
     **idempotent** re-registration, seatable as a no-op.
   - occupant is a **different** impl: `PluginError` (typed cross-plugin impl-name collision, naming
     the plugin and the kind/name). This is the seam that was previously last-writer-wins. If any
     impl in the descriptor collides, the method raises here, **before seating any impl**.
3. **Seat all.** Only now, `adapter.seat(impl_cls)` for every impl. Because pass 2 proved every impl
   seatable, this loop cannot fail partway, so a mid-descriptor failure leaving orphaned impls (the
   reset code's bug) is unrepresentable.

`register_plugin` returns nothing and does **not** touch the index or publish rows; it only seats
impls into the four code registries, exactly as core impls populate them at import.

### The seat/unseat snapshot helper

```python
@contextmanager
def seated_plugin(plugin: Plugin) -> Iterator[None]: ...
```

Snapshots the four capability registries (shallow dict copies), calls `register_plugin(plugin)`,
yields, and restores the snapshots on exit (including on exception). Tests seat a fixture plugin
without hand-snapshotting global dicts and without polluting later tests. The fixture (LLD's Phase
6, HLA component 10) seats through this helper against a **test-local** index, never
`SYSTEM_PLUGINS`.

## The installed index with inverted registration (R3)

`plugins/__init__.py` exposes `SYSTEM_PLUGINS: dict[str, Plugin]`, built by the index **importing
each shipped plugin module and calling `register_plugin(module.PLUGIN)` itself**:

```python
_INSTALLED_MODULES: tuple[ModuleType, ...] = ()   # ships empty (R11); add a module here to ship a plugin

SYSTEM_PLUGINS: dict[str, Plugin] = {}
for _module in _INSTALLED_MODULES:
    _plugin = _module.PLUGIN
    try:
        register_plugin(_plugin)
    except PluginError as exc:
        raise PluginError(f"system plugin {_module.__name__!r} failed to register: {exc}") from exc
    if _plugin.name in SYSTEM_PLUGINS:
        raise PluginError(f"duplicate system plugin name {_plugin.name!r} (from {_module.__name__!r})")
    SYSTEM_PLUGINS[_plugin.name] = _plugin
```

Inverted control (the index drives registration, registration is **not** a plugin module import side
effect) buys three things the FRD calls out (R3): a registration failure is wrapped with plugin
attribution rather than an opaque traceback that kills the whole CLI; provenance is derived from the
real module (`_module.__name__`), not a self-declared name a descriptor could spoof; and external
loading later becomes "another way to obtain a `module.PLUGIN`", not a new authoring contract. A
**duplicate plugin name** is a typed `PluginError` (not last-writer-wins). The shipped
`_INSTALLED_MODULES` is empty, so `SYSTEM_PLUGINS == {}` and nothing seats (R11); the framework is
exercised only by the fixture.

## The `_check_collision` `system-plugin` matrix (R7)

Extend `Registry._check_collision` (`registry.py:129-165`) to decide by the **unordered**
`{existing.variant, incoming.variant}` pair, so the verdict is independent of publish order (the
plugin rows land between the built-in capability rows and `config.publish_to`, but the matrix does
not depend on that; LLD c). Existing `operator`/`built-in` pairings are untouched. The new pairings,
each with its **own** message (never the generic "publisher ordering conflict"):

| existing            | incoming            | outcome                                                                                                                                                      |
| ------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `system-plugin`     | `system-plugin`     | `PluginError`-worded `ConfigError`: `<kind> "<name>" is published by two system plugins` (curation bug)                                                      |
| `built-in`          | `system-plugin`     | `ConfigError`: `system-plugin <kind> "<name>" collides with a built-in of the same name` (peers)                                                             |
| `system-plugin`     | `built-in`          | `ConfigError`: same peer message, other direction                                                                                                            |
| `system-plugin`     | `operator-declared` | `builtin_override == "allow"` -> operator wins (return); else `ConfigError` reserved-name (mirrors the built-in-over-operator branch, `registry.py:143-150`) |
| `operator-declared` | `system-plugin`     | same as above, normalized on the pair                                                                                                                        |

Implementation note: normalize by inspecting both variants up front (a small helper that returns the
`{existing, incoming}` set), then dispatch, so the two directions of each pair share one branch and
one message. The collision message stays a `ConfigError` (it can be reached by operator data driving
a publish order, so it must not be an `assert`, matching the existing contract at
`registry.py:100`), even though the two-plugins case is a curation bug: the `ConfigError` rendering
is the right operator surface if it ever escapes, and the curation bug is caught in CI by the
fixture/collision tests.

**Curated-set collisions are legitimate build errors.** Because all shipped plugins publish
unconditionally (R5, LLD c), a `(kind, name)` clash between two shipped plugins is a build error
even when **both are not enabled** (both still publish their capability rows). That is the correct,
loud outcome for an in-repo curation bug; resource-name namespacing that would let independent
external plugins coexist is deferred (FRD Future direction).

## What does not change

The capability kinds and `KIND_REGISTRY`; the four capability registries' shapes; the manifest
loader; every finalize pass. This LLD adds a package and one `_check_collision` branch set; it
produces no enablement and publishes no row (LLDs b, c).

## Acceptance (Phase 2 tests must pin)

- **Descriptor validation** rejects, each as a typed `PluginError` naming the plugin: an empty or
  `/`-bearing plugin name; a capability kind with no adapter (unknown-kind); an impl passed as an
  **instance** instead of a class (the `secret-backend` trap); an impl class with a missing or
  `/`-bearing `name`; two impls colliding within one descriptor.
- **Atomicity**: a descriptor whose second impl collides cross-plugin seats **nothing** (assert all
  four registries are unchanged after the failed `register_plugin`).
- **Idempotency**: registering the same plugin twice is a no-op; registering a different impl under
  a taken name is a typed `PluginError`.
- **Adapters** seat and build a row for all four kinds; `build_row` on an unseated name raises
  `StateError` (publication-tied-to-seating); the `CAPABILITY_ADAPTERS.keys()` ==
  capability-category kinds guard holds.
- **Index**: `SYSTEM_PLUGINS` is empty in the shipped build; a duplicate plugin name across two test
  modules raises `PluginError` with module attribution; a registration failure is re-raised wrapped
  with the module name.
- **`seated_plugin`** round-trips: inside the context the impl is seated; after it, all four
  registries equal their pre-context snapshots (even on exception).
- **The R7 matrix**: one test per pairing asserting the exact message; operator-over-system-plugin
  respects `builtin_override`; two shipped fixtures colliding on `(kind, name)` fail the build even
  when neither is enabled; existing operator/built-in pairings still behave as before.
