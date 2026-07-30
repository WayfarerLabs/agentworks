# LLD (b): the `_node_enablement` producer (reason-carrying, composed over sources)

Implements HLA [component 8](./hla.md), the load-bearing piece. Governs plan [Phase 4](./plan.md);
FRD R9 (capability side), R13. Owns the additive extension of the landed registry model so a
not-opted-in plugin's contributions become **present-but-disabled** with a reason that reaches the
dependent's hint, and so `_node_enablement` becomes a **composition over enablement sources** into
which a future operator-explicit-disable source slots with no re-shaping. It builds exactly one
source (plugin opt-in). It changes no fold, gate, or consumer **logic**; the whole point is that the
refactor already distributes and gates enablement, and this LLD only produces it and threads a
reason alongside it.

The single hardest requirement here is **additive-ness**: get it wrong and the refactor's
fold/materialization/validation/secret-resolution code has to change. Below, every touch point is
pinned as either "additive parameter / field, defaulted so existing callers are unaffected" or
"unchanged", with exactly one behavioral edit (the vm-site hint string).

## Where the reason must arrive, and why that bounds the design

The only place a disabled dependency's reason is **consumed** today is a dependent's `not_ready`
hook: `VMSiteDecl.not_ready` reads `platform.enablement is Enablement.disabled` and returns a
blocked `Readiness` with the hardcoded tail `"enable its unit"` (`vms/sites.py:122-123`). The reason
travels to that hook inside the `DependencyState` the fold hands in (`graph.py:87-104`,
`graph.py:384-389`). Nothing else needs the reason: the disabled node's own readiness is a ready
placeholder (`graph.py:368`), `has_ready_referrer` and `_validate_resources` gate on the **binary**
axis only (`registry.py:486`, `registry.py:328`), and `active_backends` / secret-mapping validation
exclude disabled backends via the binary `enablement_of` (HLA "Current state"). So the reason has to
reach exactly one struct (`DependencyState`) and be read at exactly one call site
(`VMSiteDecl.not_ready`). That bounds the change to: one new optional field, one new optional fold
parameter, one edited hint line. Everything else stays binary and untouched.

## The additive pieces (pinned, exhaustively)

### 1. `DisabledMark` and the source signature (new, in `graph.py`)

```python
@dataclass(frozen=True)
class DisabledMark:
    reason: str    # the remediation clause a dependent hint appends, e.g. "enable plugin `azure`"
    source: str    # source identity, e.g. "plugin-opt-in" (for precedence + future surfaces)

EnablementSource = Callable[
    [Mapping[str, Mapping[str, object]]],       # the present rows (self._resources)
    Mapping[tuple[str, str], DisabledMark],     # the (kind, name) it disables, and why
]
```

A source is a pure function from the present rows to the subset of nodes it disables, each with a
mark. A node **absent** from a source's output is (as far as that source is concerned) enabled. This
is the R13 multi-source seam: sources compose, and each disabled verdict carries which source fired
and its own reason.

### 2. `compose_enablement` (new, in `graph.py`)

```python
def compose_enablement(
    sources: Sequence[EnablementSource],
    resources: Mapping[str, Mapping[str, object]],
) -> dict[tuple[str, str], DisabledMark]:
    marks: dict[tuple[str, str], DisabledMark] = {}
    for source in sources:
        for key, mark in source(resources).items():
            marks.setdefault(key, mark)   # first source in the list wins
    return marks
```

**Precedence rule (pinned):** when more than one source disables the same node, the **first source
in the list wins** its reason (`setdefault`). The list order is controlled by the assembly point
(`build_registry`, LLD c), so the eventual operator-explicit-disable source can be ordered ahead of
the plugin source if operator intent should own the reason; that is a one-line ordering choice at
wiring time, not a re-shaping of the axis. This SDD ships one source, so precedence is never
exercised in production; a Phase 4 test injects a stub second source to pin the composition and the
rule (R13 seam).

### 3. `DependencyState.disabled_reason` (additive field)

`DependencyState` (`graph.py:87-104`) gains one field, defaulted:

```python
disabled_reason: str | None = None   # the mark's reason when this dep is disabled; None otherwise
```

Defaulted, so the field is additive: the struct's only constructor is in `node_readiness`
(`graph.py:384-389`), updated below; any other construction (tests) that omits it still type-checks.

### 4. `fold_readiness` / `node_readiness` gain one optional parameter

Both (`graph.py:317`, `graph.py:348`) gain
`disabled_marks: Mapping[tuple[str, str], DisabledMark] | None = None`, defaulted to `None` (treated
as empty). The **only** use is when `node_readiness` builds a **disabled** dependency's
`DependencyState`: it sets `disabled_reason=(disabled_marks or {}).get(target)` -> `.reason`. The
enabled branch is unchanged. Concretely the construction at `graph.py:384-389` becomes:

```python
dep_enabled = opt_in.get(target, Enablement.enabled) is Enablement.enabled
mark = None if dep_enabled else (disabled_marks or {}).get(target)
deps[target] = DependencyState(
    enablement=Enablement.enabled if dep_enabled else Enablement.disabled,
    readiness=readiness[target] if dep_enabled else None,
    impl=_impl_for(*target),
    disabled_reason=mark.reason if mark is not None else None,
)
```

The binary `enablement` parameter is **unchanged** and remains the authority for the
enabled/disabled branch; `disabled_marks` only supplies the reason on the branch that is already
disabled. A default of `None` means every existing `fold_readiness` / `node_readiness` call
(including `_materialize_deferred`'s single-node fold, `registry.py:514`) keeps working with no
reason (the disabled hint falls back to "enable its unit", below), so this is purely additive.

### 5. `build_graph` and `_Node`: unchanged

The reason is never stored on a node: a disabled node's own readiness is a placeholder, and no
surface reads a per-node reason (the doctor roster and `--include-disabled` view derive their
strings from the origin and config, LLD c). So `build_graph` (`graph.py:245`) and `_Node`
(`graph.py:106-124`) take no new field and no new argument. This is deliberate: keeping the reason
off the frozen node is what lets `build_graph` stay untouched.

### 6. `VMSiteDecl.not_ready`: the one behavioral edit

The disabled branch (`vms/sites.py:122-123`) changes its hardcoded tail to the carried reason, with
the old string as the fallback:

```python
if platform.enablement is Enablement.disabled:
    tail = platform.disabled_reason or "enable its unit"
    return Readiness.blocked(f"depends on vm-platform '{self.platform}', which is disabled; {tail}")
```

So a not-opted-in plugin platform yields the hint
`depends on vm-platform 'azure-vm', which is disabled; enable plugin azure`, while a disabled node
with no mark (a test that disables without a source) still reads the generic tail. This is the sole
edit to a fold consumer, and it is a string, not a control-flow, change.

## The finalize wiring inside `registry.py` (additive)

`finalize` (`registry.py:169`) gains one optional parameter:

```python
def finalize(self, enablement_sources: Sequence[EnablementSource] = ()) -> None:
```

Defaulted to empty, so every existing `finalize()` call (tests, other roots, `bootstrap` until LLD c
wires the source) is unaffected and yields all-enabled, exactly as today. Inside, the current line
`enablement = self._node_enablement()` (`registry.py:265`) is replaced by:

```python
marks = compose_enablement(enablement_sources, self._resources)
enablement = {
    key: (Enablement.disabled if key in marks else Enablement.enabled)
    for kind, kind_dict in self._resources.items()
    for key in ((kind, name) for name in kind_dict)
}
readiness = fold_readiness(self._resources, all_outbound, enablement, marks)
```

`marks` is the single source of truth; the binary `enablement` map (the exact type the fold, the
materialization gate, `build_graph`, and `_validate_resources` already consume) is a pure projection
of it (`disabled` iff a mark exists), so there is no drift between "which nodes are disabled" and
"why". `marks` also flows to `fold_readiness` as the new `disabled_marks` argument.
`_materialize_deferred` (`registry.py:449`) passes `marks` through to its `node_readiness` call
(additive), so a late-materialized dependent of a disabled node gets the reason too.

The refactor's `_node_enablement()` **method** (`registry.py:349-361`) is **removed**: its
all-enabled behavior is subsumed (no sources -> no marks -> all enabled), and "composition over
sources" is where the FRD/HLA explicitly said it goes. This retires the refactor's scaffold seam.
The refactor's `test_readiness_fold.py` currently injects a disabled node by monkeypatching
`registry._node_enablement` (`tests/resources/test_readiness_fold.py:102,134,175,348`); those tests
**migrate to the source-injection path** (`registry.finalize(enablement_sources=[stub_source])`),
exercising the identical fold behavior through the real seam. This test migration is the only churn
to landed-refactor tests, and it is mechanism, not behavior: the fold code they test is unchanged
except the additive parameter.

**Layering holds.** `finalize` receives opaque `EnablementSource` callables; the `Registry` imports
neither `Config` nor `agentworks.plugins`. The config-bound plugin source is constructed by
`build_registry` (the app glue that already knows both, LLD c) and passed in, exactly as publishers
are handed to `add`. The `Registry` stays publisher- and config-agnostic.

## The plugin source (the one source built)

Lives in the plugin package (`plugins/enablement.py`), consumed by `build_registry` (LLD c). Shape:

```python
def plugin_enablement_source(config: Config) -> EnablementSource:
    enabled = frozenset(config.plugins_enabled)
    def _source(resources: Mapping[str, Mapping[str, object]]) -> dict[tuple[str, str], DisabledMark]:
        marks: dict[tuple[str, str], DisabledMark] = {}
        for kind, kind_dict in resources.items():
            for name, row in kind_dict.items():
                origin = getattr(row, "origin", None)
                if origin is not None and origin.variant == "system-plugin" and origin.plugin not in enabled:
                    marks[(kind, name)] = DisabledMark(
                        reason=f"enable plugin `{origin.plugin}`",
                        source="plugin-opt-in",
                    )
        return marks
    return _source
```

It reads only the **frozen rows' origins** (each `system-plugin` row carries `plugin=<name>`, LLD c)
and `config.plugins_enabled`; it does **no new registry probe** and constructs no impl. It is a
builder/finalize input assembled by `build_registry` and threaded into `finalize` (the whitelisted
builder path, the same shape the fold's `build_context` uses at `graph.py:228-242`), **not** a
consumer probing a live registry at op time, so it sits cleanly inside the R11 guard, not against
it. A shipped-but-not-opted-in plugin's capability rows (published unconditionally, LLD c) are
therefore marked `disabled`; an operator `vm-site` naming one is not-ready with the enable hint (via
the existing fold), never an unknown-name hard error.

## Ambiguity raised (phrasing of the disabled reason)

The upstream docs describe the mark's content two ways: HLA component 8 and plan Phase 4 say the row
is "disabled with reason `plugin <name> not enabled`" (a **state** phrasing), while FRD R13 and the
HLA risk mitigation require the dependent's hint to read `enable plugin <name>` (a **remediation**
phrasing). A single string cannot be both verbatim. **Decision (pinned):** `DisabledMark.reason` is
the **remediation** clause `enable plugin <name>`, because the one load-bearing consumer is the
vm-site hint, which appends the reason verbatim and must read `... enable plugin azure` (FRD R13 and
the risk mitigation are the more specific, testable statements). The **state** phrasing is produced
independently where it is wanted: the doctor roster renders `disabled (not enabled in [plugins])`
from `SYSTEM_PLUGINS` vs config (LLD c), needing no mark. This reconciles the two without
contradicting either intent; it is flagged here rather than papered over because it is a genuine
spec inconsistency the LLD had to resolve.

## Acceptance (Phase 4 tests must pin)

- A not-opted-in plugin capability node reads
  `graph.enablement_of(kind, name) is Enablement.disabled`; an opted-in one reads `enabled`.
- A `vm-site` referencing a disabled plugin platform is **not-ready** with the reason
  `depends on vm-platform '<platform>', which is disabled; enable plugin <name>`, produced by the
  existing fold (no new code in the fold beyond the additive parameter and the one hint line),
  **not** an unknown-name error.
- The refactor's consumers, pinned **under this real producer** (not re-implemented):
  materialization withholds a disabled plugin platform's config-implied deps (`has_ready_referrer`
  excludes it), and `active_backends` / secret-mapping validation exclude a disabled plugin
  `secret-backend`.
- **Additive-ness**: `DependencyState.disabled_reason` defaults to `None`; `fold_readiness` /
  `node_readiness` / `finalize` all accept their new argument omitted and behave exactly as the
  landed refactor (all-enabled) when no source fires; a disabled node with no mark still reads the
  fallback `enable its unit` tail.
- **R13 seam**: a stub second `EnablementSource` disabling a node composes through
  `finalize(enablement_sources=[plugin_source, stub])`; the composition disables the union and the
  precedence rule (first source wins the reason) holds; nothing in the plugin source assumes it is
  the only source.
- The migrated `test_readiness_fold.py` disabled-node cases pass through the source-injection seam
  with identical verdicts.
