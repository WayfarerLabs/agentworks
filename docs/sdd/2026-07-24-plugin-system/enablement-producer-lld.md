# LLD (b): the `_node_enablement` producer (reason-carrying, composed over sources)

Implements HLA [components 8, 8b](./hla.md), the load-bearing piece. Governs plan
[Phase 4, and Phase 7's reference-side half](./plan.md); FRD R9 (capability side and, per the Phase
7 extension section below, the manifest side's reference gate), R13, R14. Owns the additive
extension of the landed registry model so a not-opted-in plugin's contributions become
**present-but-disabled** with a reason that reaches the dependent's hint, so `_node_enablement`
becomes a **composition over enablement sources** into which a future operator-explicit-disable
source slots with no re-shaping, and so the two capability kinds the refactor left un-wired
(`harness`, `git-credential-provider`) actually honor enablement at their consumers (R14). It builds
exactly one enablement source (plugin opt-in). It changes no fold or producer **logic**; the whole
point is that the refactor already distributes and gates enablement, and this LLD produces it,
threads a reason alongside it, and wires the last two consumers.

The single hardest requirement here is **additive-ness**: get it wrong and the refactor's
fold/materialization/validation/secret-resolution code has to change. Below, every touch point is
pinned as either "additive parameter / field, defaulted so existing callers are unaffected" or
"unchanged". Two classes of edit exist, both additive against the produced enablement: the
reason-carrying change touches **one existing fold consumer** (the vm-site hint string); the R14
consumer-gating (section "Closing the consumer-gating gap") adds **new** wiring to the
git-credential and harness consumers. Neither touches the fold or the producer.

## Where the reason must arrive, and why that bounds the design

The reason is **consumed** only by a **propagating** `not_ready` hook: `VMSiteDecl.not_ready` reads
`platform.enablement is Enablement.disabled` and returns a blocked `Readiness` with the hardcoded
tail `"enable its unit"` (`vms/sites.py:122-123`), and the R14 git-credential hook (added below)
does the same for its provider. The reason travels to those hooks inside the `DependencyState` the
fold hands in (`graph.py:88-104`, `graph.py:384-389`). Nothing else needs the reason on the graph:
the disabled node's own readiness is a ready placeholder (`graph.py:368`), `has_ready_referrer` and
`_validate_resources` gate on the **binary** axis only (`registry.py:486`, `registry.py:328`),
`active_backends` / secret-mapping validation exclude disabled backends via the binary
`enablement_of` (HLA "Current state"), and the R14 use-time gates (git-credential, harness) read the
binary axis and craft their own message from the row's `system-plugin` origin (they do not read the
mark reason). So the reason has to reach exactly one struct (`DependencyState`), read only by
propagating hooks. That bounds the reason-carrying change to: one new optional field, one new
optional fold parameter, one edited hint line (vm-site). Everything else stays binary and untouched.

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

`DependencyState` (`graph.py:88-104`) gains one field, defaulted:

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
disabled. A default of `None` means every existing `fold_readiness` / `node_readiness` call keeps
working with no reason, so this is purely additive.

The `"enable its unit"` fallback (section 6) is a **defensive default for the additive parameter**,
not a live runtime state: in production, `finalize` derives the binary map as a pure projection of
`marks` (below), so a node the fold sees as `disabled` **always** has a mark, hence always a reason.
The fallback is reachable only by a direct `node_readiness` / `fold_readiness` test call that passes
an `enablement` map with a disabled node but omits `disabled_marks`; it exists so that call still
returns a coherent verdict, never as a code path a real build takes.

### 4b. `_materialize_deferred` threads `disabled_marks` (additive)

`_materialize_deferred` (`registry.py:449`) calls `node_readiness` for each late-materialized node
(`registry.py:514`); it gains the `disabled_marks` argument and forwards `marks` (the same map
`finalize` composed), so a late-materialized dependent of a disabled node gets the reason on the
same additive path. Its own signature gains `disabled_marks` alongside its existing `enablement`
argument, both threaded from `finalize`; the `node_readiness` boundary defaults the argument, so
this is additive. This is the fourth and final additive touch point (with sections 3, 4, and the
finalize wiring); no other fold or materialization call changes.

### 5. `build_graph` and `_Node`: unchanged

The reason is never stored on a node: a disabled node's own readiness is a placeholder, and no
surface reads a per-node reason (the doctor roster and `--include-disabled` view derive their
strings from the origin and config, LLD c). So `build_graph` (`graph.py:245`) and `_Node`
(`graph.py:106-124`) take no new field and no new argument. This is deliberate: keeping the reason
off the frozen node is what lets `build_graph` stay untouched.

**Reconciling "untouched" with R13's "carries which source".** R13 says a disabled verdict "carries
which source disabled it and why". That is carried on the transient `DisabledMark` (and, for the
reason, on the transient `DependencyState`), **not** persisted on the frozen graph. The intended
reading, pinned here: a future operator-disable **display** surface (or any "why is this disabled"
query) **recomputes** the sources, since a source is a pure function of `(rows, bound config)` and
`build_registry` holds both. Source identity is never persisted on `_Node`, so the "untouched" claim
and R13's "carries which source" clause do not conflict: the composition is cheap and repeatable,
and re-running it is how a later surface answers "which source", exactly as this SDD's producer runs
it at finalize. If a future effort finds recomputation too costly, adding a
`disabled_by: DisabledMark | None` field to `_Node` is a clean additive follow-on; this SDD does not
need it and does not add it.

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

## The disabled-reason phrasing (docs reconciled)

An earlier draft of this LLD flagged a conflict between two phrasings of the mark's content: a
**state** phrasing (`plugin <name> not enabled`) and a **remediation** phrasing
(`enable plugin <name>`). The upstream docs have since been **reconciled** to agree with this LLD's
decision, so this is now a record, not an open ambiguity:

- `DisabledMark.reason` carries the **remediation** clause `enable plugin <name>`. HLA component 8
  now states the source carries "the disabling remediation reason (the clause a dependent's hint
  renders, e.g. `enable plugin <name>`, NOT the state phrasing)", matching FRD R13's requirement
  that the dependent hint read `enable plugin <name>`. The one load-bearing consumer, the
  propagating `not_ready` hook, appends the reason verbatim, so the remediation clause is the
  correct content.
- The **state** phrasing is produced independently where it is wanted: the doctor roster renders
  `disabled (not enabled in [plugins])` from `SYSTEM_PLUGINS` vs config (LLD c), needing no mark.

The FRD, HLA, plan, and this LLD now use the same phrasing; nothing here reinterprets the upstream
docs.

## Closing the consumer-gating gap (R14)

The producer above only makes strictly-opt-in **real** for a kind whose **consumer** honors a
disabled dependency. The refactor wired only two of the four: `vm-site` propagates (its `not_ready`
reads the platform's disabled state) and `secret` consults backend enablement in
resolution/validation. The other two consumers currently opt out of readiness entirely, so a
not-enabled plugin's `harness` or `git-credential-provider` would be **silently usable**, R9's
opt-in guarantee would hold for only two of the four kinds R6 allows. Per the registry's
self-determined-readiness principle, each consumer chooses its own model (not a blanket
propagation). Both wirings below are **additive against the already-produced enablement**: neither
changes the fold or the producer; each reads the enablement the producer already computed.

### `git-credential` propagates (the vm-site model)

A `git-credential` has a **single** provider (`GitCredentialConfig.provider`,
`git_credentials/credential.py:56`), so, like a vm-site, it is not-ready when that provider is
disabled.

- **`not_ready(deps)` hook on `GitCredentialConfig`** (new method, mirroring
  `VMSiteDecl.not_ready`): reads `deps[("git-credential-provider", self.provider)]` and, when its
  `enablement is Enablement.disabled`, returns a blocked verdict with the carried reason:

  ```python
  dep = deps[("git-credential-provider", self.provider)]
  tail = dep.disabled_reason or "enable its unit"
  return Readiness.blocked(f"depends on git-credential-provider '{self.provider}', which is disabled; {tail}")
  ```

  The `git-credential -> git-credential-provider` edge **already exists**
  (`GitCredentialConfig.dependencies`, `credential.py:81-88`), so the fold hands the provider's
  `DependencyState` for free; adding `not_ready` makes `GitCredentialConfig` a `_ReadinessResource`
  (`graph.py:307-314`) the fold dispatches. No fold change (the fold already dispatches on the
  structural `not_ready` shape).

- **Use-time refusals** (a disabled provider cannot be constructed even if a resource names it):
  - `resolve_git_credential_providers(registry, names)` (`vms/initializer/credentials.py:49-96`):
    before the `provider_cls(...)` construction at `credentials.py:91`, read the credential's stored
    propagated verdict `registry.graph.readiness_of("git-credential", name)`; if not ready, raise a
    typed error with that reason, mirroring `ensure_site_ready` (`vms/sites.py:278-297`). Reusing
    the credential's propagated readiness means the "enable plugin `<name>`" reason is already in
    hand; no separate origin lookup. `registry` is in scope.
  - `remote_advisories(registry, url)` (`git_credentials/__init__.py:49-79`): the loop at
    `__init__.py:71` iterates `registry.iter_kind_items("git-credential")`; **skip** a credential
    when `not registry.graph.is_ready("git-credential", name)` (this best-effort preflight already
    skips a `provider_cls is None`, `__init__.py:72-73`; a disabled-provider credential does not
    advise). `registry` is in scope.

### `harness` stays ready, gates at use (the secret model)

A `session-template` maps to one harness but does **not** propagate (it lists ready, mirroring how a
`secret` stays ready and its backends are gated at resolution). Instead the harness is gated at
**use**: constructing a session whose template names a disabled harness is a typed error.

**Seam refinement (scouted, flagged for confirmation).** The HLA names
`capabilities/harness/harness_for` (called from `sessions/nodes.py`) as the gate site, but those
sites have **no graph access**: `harness_for(name)` (`capabilities/harness/__init__.py:48`) takes
only a name, and
`_harness_for_template(template, *, session_name, target, admin, vm, workspace, state)`
(`sessions/nodes.py:226`) and its node-factory callers `pending_session_node` / `live_session_node`
(`sessions/nodes.py:267,302`) thread no `registry`. Gating **inside** them would require threading a
registry through the node factories, which is **not** additive. The additive gate lands one level
up, at the **two** session-build call sites that already hold `registry` and the resolved template
(with `.harness`):

- `_create_build.py:170` (session **create**, calls `pending_session_node`), `registry` in scope
  (`_create_build.py:49`), template from `_resolve_template(registry, ...)` (`_create_build.py:69`).
- `_lifecycle.py:305` (live session **use**: restart/reattach, calls `live_session_node`),
  `registry` in scope (`_lifecycle.py:257`), template from `_resolve_template(registry, ...)`
  (`_lifecycle.py:262`).

Both funnel through `_mgr._resolve_template(registry, ...)` (`sessions/manager/_env.py:27`), and
`_harness_for_template` is called **only** from those two factories (`sessions/nodes.py:290,337`),
so gating at the two call sites covers every harness construction. A shared helper
`ensure_harness_enabled(registry, harness_name)` (co-located with the capability, mirroring
`ensure_site_ready`) reads `registry.graph.enablement_of("harness", name)`; when `disabled`, it
raises a typed error naming the plugin to enable, deriving the plugin name from the harness row's
`system-plugin` origin (`registry.lookup("harness", name).origin.plugin`, the same origin-read the
doctor roster and describe use), since the mark reason is not on the frozen node. The **read-only**
display path (`_display_harness`, `_env.py:63`) is deliberately **not** gated: it must keep showing
an enabled session-template's harness name (a session-template that references a disabled harness is
itself enabled; only its harness is disabled, and it fails at create/use, not in a listing).

Both wirings are additive consumer code against the produced enablement; the fixture's tests
exercise a disabled plugin of **each of the four kinds** through its actual consumer (site
not-ready; secret backend excluded; git-credential not-ready; session harness use-error), so R9's
guarantee is proven kind-by-kind, not only for vm-platform.

## Closing the declarable-reference gap (R9 manifest parity, Phase 7)

> Added 2026-07-30 for the reopened SDD. LLD (c) 3b makes a disabled plugin's **bundled declarable
> rows present** (published weak, disabled by the same overlay). This section owns the consumption
> side: without it, a present-but-disabled row is silently usable, because a present target is not a
> miss at `_resolve_misses` (`resources/registry.py:512-513`) and every declarable consumer fetches
> rows by name with no enablement read. The scouted consumers at HEAD: the agent install runner
> reads `kind_dict(registry, "user-install-command")` and runs the entry
> (`agents/initializer.py:469`, loop at `:475-503`); VM init phase B does the same for
> `system-install-command` and the admin's `user-install-command` set
> (`vms/initializer/driver.py:488-489`, run calls at `:619-622` and `:757-760`, runner at
> `vms/initializer/packages.py:224`); and the template resolvers merge a parent row's entire recipe
> with no enablement read (`agents/templates.py:96-102`, `vms/templates.py:112-118`,
> `sessions/templates.py:128`), so a disabled plugin template named in `inherits` would leak its
> whole contribution into an operator's resolved template.

### The named-row rule (pinned)

Enablement gates existed only where the four capability kinds are consumed (R14). A declarable row
that is present but disabled generalizes the need: **any consumer that fetches a declarable row by
name and acts on it must consult `enablement_of` first, and a disabled row is a typed refusal
carrying the enable-plugin hint, never a silent use and never an unknown-name error.** Like R14,
_how_ each consuming flow honors the rule is its own choice; unlike R14's four bespoke wirings, the
declarable kinds share one shape (fetch-by-name then act), so they share one helper pair.

### The helpers (additive, in `resources/access.py`)

Co-located with `kind_dict` (`access.py:29`) and `admin_template` (`access.py:62`), the access layer
every scouted consumer already imports:

- `ensure_reference_enabled(registry, kind, name)`: the direct-name gate, mirroring
  `ensure_harness_enabled` (`capabilities/harness/__init__.py:68-101`) line for line: return unless
  `registry.graph.enablement_of(kind, name) is Enablement.disabled`; on disabled, raise a typed
  `StateError` (entity kind/name attached) whose tail derives the plugin from the row's
  `system-plugin` origin (`registry.lookup(kind, name).origin.plugin`), falling back to
  `enable its unit` for a non-plugin disabled row (a future operator-explicit-disable source, R13),
  with the doctor-roster hint line.
- `ensure_recipe_enabled(registry, kind, name)`: the closure gate for template recipes. Checks the
  named node itself, then every node in `registry.graph.reachable_from(kind, name)`
  (`resources/graph.py:212-237`) whose kind is **declarable**
  (`KIND_REGISTRY[kind].category == "declarable"`, the field pinned at e.g.
  `install_commands.py:190`), refusing on the first disabled one via the same error shape, naming
  the offending `(kind, name)`. **Capability nodes are deliberately excluded** from the closure
  check: each capability kind keeps its own R14 model (platform propagates via the site, backend
  excluded at resolution, provider propagates via the credential, harness gated by
  `ensure_harness_enabled`), so the closure gate neither duplicates nor contradicts them.

Why the closure is the right unit: the resolvers merge **everything reachable** into the recipe
(parent lists append-merge, `agents/templates.py:116-133`, `vms/templates.py:145-157`), and the raw
template's graph edges are exactly what the resolvers follow (`agents/template.py:59-77`,
`vms/template.py:101-131`, `vms/admin.py:68-81`, plus the `apt-package -> apt-source` hop at
`apt.py:79-105`), so "any disabled declarable in the closure" is "some disabled contribution the
recipe would consume". Both helpers are safe no-ops for implicit defaults: `enablement_of` tolerates
a missing node (`graph.py:262-263`) and `reachable_from` a missing start (`graph.py:228-230`).

### The gate sites (pinned, all additive)

Each gate sits at the mutation entry that already holds the registry and the resolved name, before
any remote work, exactly where `ensure_site_ready` sits (`vms/manager/lifecycle.py:151`):

| flow            | site                                                              | gate                                                                               |
| --------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| agent create    | `agents/manager/lifecycle.py:76` (after resolve)                  | `ensure_recipe_enabled(registry, "agent-template", agent_tmpl.name)`               |
| agent reinit    | `agents/manager/lifecycle.py:420`                                 | same, on `agent.template`                                                          |
| vm create       | `vms/manager/lifecycle.py:139` and `:177`                         | `ensure_recipe_enabled` on the `vm-template` **and** the selected `admin-template` |
| vm reinit       | `vms/manager/lifecycle.py:514` and `:540`                         | same pair                                                                          |
| session create  | `sessions/manager/_create_build.py:177` (beside the harness gate) | `ensure_recipe_enabled(registry, "session-template", template.name)`               |
| session restart | `sessions/manager/_lifecycle.py:312` (beside the harness gate)    | same                                                                               |

The install **runners** are not gated (single enforcement point): `_run_install_commands`
(`packages.py:224`) and `_run_agent_install_commands` (`agents/initializer.py:438`) each gain the
drift-guard comment pointing at the entry gate, plus a caller-gating test asserting every caller of
the runners passes through a gated entry, the same guard shape the harness node factories carry
(`sessions/nodes.py:288,328`). The read-only display paths stay ungated, matching
`_display_harness`: `env/show.py:265-269` resolves templates for display, and `describe` renders a
disabled row by design (LLD c section 6).

### Why a use-gate and not a fold edge (evaluated, rejected)

The fold-edge alternative (give the template kinds a `not_ready(deps)` hook so a disabled
install-command propagates not-ready onto the template, vm-site style) was evaluated against the
code and rejected:

1. **Readiness has registry-wide side effects the recipe kinds must not trigger.** A not-ready
   referrer stops readiness-gated materialization (`has_ready_referrer`, `registry.py:562-580`): one
   disabled `az-cli` would make the vm-template not-ready and thereby suppress materialization of
   its `tailscale-auth-key` secret (`vms/template.py:132-138`), silently changing `secret list`,
   doctor, and resolution for functionality unrelated to the plugin. `_validate_resources` likewise
   skips not-ready rows (`registry.py:425-427`), deferring validation of the template's other
   blocks. The use-gate keeps templates ready, so neither shifts.
2. **Precedent coherence.** The `session-template` maps to exactly **one** harness and still stays
   ready, gating at use (R14's secret model); a template with N optional recipe inputs propagating
   not-ready would be stricter than the single-dependency case. Propagation is reserved for "serves
   no purpose without it" resources (vm-site, git-credential).
3. **Fold-edge is not smaller.** Nothing consults template readiness at create today
   (`resolve_template` reads rows, not verdicts), so fold-edge still needs the same entry refusals;
   it adds three `not_ready` hooks (plus inheritance-cascade rules) on top of them.
4. **The tradeoff, acknowledged:** fold-edge would surface the hint in `resource list` before first
   use. Discoverability is instead carried by `describe`'s `Disabled:` line, the doctor roster, and
   the typed error at the first mutating use; the plan's "reference ... is not-ready with the enable
   hint" is satisfied as **not-consumable, with the hint rendered at the gate and on describe**, not
   as a fold verdict. (Flagged in the review notes for the lead to confirm the reading.)

### Coverage audit (every consumption path per bundleable kind)

- `user-install-command`: agent path gated at agent create/reinit; admin path gated at vm
  create/reinit (the admin-template closure carries its `user_install_commands` edges,
  `vms/admin.py:72-81`). Runner lookups drift-guarded. **Covered.**
- `system-install-command`: vm create/reinit via the vm-template closure
  (`vms/template.py:123-131`). **Covered.** (`az-cli` at HEAD is this kind:
  `manifests/builtin/install-commands.yaml:15-22`.)
- `apt-package` / `apt-source`: reachable through the same vm-template closure, including the
  package-to-source hop (`apt.py:79-105`). **Covered.**
- `vm-template` / `agent-template` / `admin-template` / `session-template` (named use and
  `inherits`): the named node and every parent are in the checked closure. **Covered.**
- Excluded kinds (`secret`, `git-credential`, `vm-site`, `workspace-template`,
  `named-console-template`): **not gated, therefore not bundleable**; LLD (c) 3b.2's allowlist
  raises at publish, so no ungated path is reachable. Expanding the allowlist requires wiring that
  kind's gate here first (e.g. `git-credential` would extend `resolve_git_credential_providers`'s
  readiness read with an `enablement_of` read on the credential row, whose own readiness is a
  placeholder when the row itself is disabled).
- Out of registry scope, noted for honesty: `install_claude_plugins` probes the `claude` CLI binary
  on the VM (`vms/initializer/driver.py:822-833`), a filesystem fact, not a registry row; no gate
  applies or is needed.

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
- **R14, all four kinds through their actual consumer** (the opt-in guarantee proven kind-by-kind):
  - **vm-platform**: a `vm-site` on a disabled plugin platform is not-ready with the enable hint
    (already covered above); `resolve_site` refuses it.
  - **secret-backend**: a disabled plugin backend is excluded from `active_backends` and
    secret-mapping validation (already covered above).
  - **git-credential-provider**: a `git-credential` whose provider is a disabled plugin reads
    `readiness_of("git-credential", name)` not-ready with `... enable plugin <name>` (via the new
    `GitCredentialConfig.not_ready` propagate hook); `resolve_git_credential_providers` refuses it
    with that reason; `remote_advisories` skips it.
  - **harness**: a `session-template` whose harness is a disabled plugin **lists ready** (does not
    propagate), but creating or restarting a session on it raises the typed `ensure_harness_enabled`
    error naming the plugin; the read-only `_display_harness` still shows the harness name.
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

## Acceptance (Phase 7, reference side; fixture-driven)

Against a fixture plugin bundling a `user-install-command`, a `system-install-command`, and an
`agent-template` (the publication/collision cases are LLD c's list):

- **No unknown-name error**: an operator `agent-template` with
  `user_install_commands = ["<fixture-cmd>"]` and the plugin **not enabled** finalizes cleanly (the
  present row resolves the `miss_policy="error"` reference); the same config with the row truly
  absent still hard-errors, pinning that present-but-disabled and absent stay distinct.
- **Agent path refusal**: `create_agent` / `reinit_agent` on that template raise the typed
  `StateError` naming `user-install-command '<fixture-cmd>'` and `enable plugin '<fixture>'`,
  **before any transport call** (asserted via a transport spy); enabling the plugin makes the same
  create run the command.
- **VM path refusal**: `create_vm` / `reinit_vm` on a vm-template naming the fixture's
  `system-install-command` (directly or via an inherited parent, pinning the closure) refuse the
  same way; likewise an admin-template naming the fixture's `user-install-command`.
- **Inherits refusal**: an operator `agent-template` with `inherits = ["<fixture-template>"]` while
  the plugin is disabled refuses at agent create with the hint naming the template row. (The
  resolver runs before the gate and does merge the parent into the in-memory resolved object; the
  pin is that the refusal discards it, so no disabled contribution is ever **acted on**. The
  read-only display resolve stays permitted by design.)
- **Session path**: a `session-template` bundled by a disabled fixture plugin refuses at session
  create/restart via `ensure_recipe_enabled` beside the existing harness gate; the harness gate's
  own behavior is unchanged (no double-error for a disabled harness, which the closure gate skips as
  a capability kind).
- **Display stays ungated**: `describe` and the env/show template rendering still work against the
  disabled fixture rows (annotated, not refused).
- **Helper tolerances**: both helpers are no-ops for an implicit `default` template (missing node)
  and for an all-enabled registry; a disabled row with a non-plugin origin (stub second source, R13)
  refuses with the `enable its unit` fallback tail.
- **Drift guard**: the caller-gating test enumerates the callers of `_run_install_commands` /
  `_run_agent_install_commands` and asserts each is reached only through a gated entry, mirroring
  the harness-factory guard.
