"""Keep graph consumers on the retained ``DependencyGraph`` query surface.

The graph is the single access path for structural and derived resource facts.
These guards prevent four bypass patterns from returning and reintroducing
competing derivations of edges, readiness, capability availability, or inbound
references. Each exemption below names the module and the architectural reason
the otherwise-banned operation is legitimate there, making this file the
self-contained enforcement contract.

THE FOUR BANNED PATTERNS (each a way to re-derive the graph outside the build):

1. Re-walking a resource's ``dependencies()`` to reconstruct the edge set
   OUTSIDE the graph build (was: cycle detection at ``registry.py``, the
   ``collect_secrets_for`` DFS in ``walk.py``, the ``secret_refs`` recompute in
   the two node factories). The honest path: cycle detection three-colors over
   the built edge map, and every other consumer reads ``edges_of`` /
   ``reachable_from``.
2. A ``*_REGISTRY.get(...)`` availability probe in edge production or readiness
   (was: ``VM_PLATFORM_REGISTRY`` in ``vms/kinds.py``'s ``disabled_reason``,
   ``SECRET_BACKEND_REGISTRY`` in ``secrets/resolve.py``'s resolver). The honest
   path: readiness comes off the graph node, and a consumer that needs a
   capability's code reads ``impl_of``.
   The pattern has TWO spellings and the guard watches both. Naming a registry
   is the original one. The second is
   ``descriptor_for(kind).registry().get(name)``: the capability-kind descriptor
   carries a lazy accessor returning the very same live dict, so reaching it
   that way is the same probe with the registry's name filed off. Watching only
   the names would mean the allow-list tightened (three modules came off it in
   declarative-schema step 2.0, having stopped naming registries) while the
   spelling they moved to went unwatched.
3. A lazy readiness recompute instead of reading ``readiness_of`` (was:
   ``inspect.disabled_reason_for``, the ``site_disabled_reason`` callers,
   ``doctor``). The honest path: ``not_ready`` is called only by the fold; every
   projection surface reads ``readiness_of`` / ``not_ready_reason_for``.
4. Reading inbound edges/usage off a resource dataclass ``references`` field
   (was: the section-E readers). The honest path: the field is gone from the
   resource dataclasses and every reader uses ``dependents_of``.

THE EXEMPTIONS (whitelisted by module, or the honest path and the banned pattern
are the same call). "Non-exempt module" is the guard's unit: a banned pattern
reintroduced in any module NOT on the relevant allow-list fails the build. The
allow-listed modules are trusted; each is justified inline on its allow-list.

An exemption is not a permanent grant. ``test_every_exemption_is_load_bearing``
fails an entry whose module has stopped performing the banned operation, because
such an entry is a HOLE rather than a leftover: the guard goes on trusting a
module that has nothing left to trust it for. Declarative-schema step 2.3 left
eight of them behind in one change, which is why the check exists.

- ``resources/graph.py`` + ``resources/registry.py``: the graph BUILDER. It
  walks each resource's ``dependencies(context)`` (handing it the build
  context), reaches the capability code registries to stamp each capability
  node's impl (``_impl_for`` / ``build_context``), and calls ``not_ready`` in
  the fold. This is the sanctioned builder-reads-registry path. It reaches
  them through the descriptors' accessors now, so it NAMES no registry and is
  no longer exempt from pattern 2's first spelling: that exempt spelling moved
  to the four ``kinds.py`` modules below. It is absent from the SECOND
  spelling's allow-list too, and that absence is real rather than an oversight:
  ``_impl_for`` calls a loader it took out of its own private
  ``_capability_registry_loaders`` map, never ``<descriptor>.registry()``, so
  reintroducing the descriptor spelling here would (correctly) have to be
  argued for on the allow-list.
- ``capabilities/publish.py`` / ``plugins/adapters.py`` /
  ``plugins/registration.py``: pattern 2's second spelling only. These are the
  three capability-registry WRITE-and-mirror paths: the generic built-in
  publisher mirrors a kind's registry into resource rows, the generic adapter
  seats plugin impls into it (the plugin analog of the publishers) and builds
  their rows, and the snapshot/restore helper saves and restores all four
  around a test-seated plugin. None of them probes availability; each is the
  registry's own machinery, reaching it through the descriptor because that is
  where the accessor lives.
- the four capability ``kinds.py`` modules (``capabilities/vm_platform``,
  ``capabilities/harness_integration``, ``capabilities/git_credential``,
  ``secrets``): each kind's ``CapabilityKindDescriptor`` carries the lazy
  accessor for its own code registry, and the secret-backend record carries the
  readiness callable that asks the backend instance. Both are the graph
  BUILDER's own exempt code relocated beside the kind it serves (the builder's
  per-kind loaders and its readiness fold derive from the descriptor table), so
  this is the same exemption at a new address, not a new bypass.
- ``vms/sites.py`` / ``git_credentials/credential.py`` / ``sessions/template.py``:
  edge production. A resource's own ``dependencies(context)`` fetches its
  capability CLASS from the code registry (a host-agnostic type lookup, not an
  availability probe; there is no graph node to read during the build that
  produces the graph) and asks it for the config-implied edges; the finalize
  ``validate`` pass fetches the same class to validate the owned blob. This is
  the builder's edge-production primitive, invoked during the build.
- ``secrets/base.py``: the secret's finalize ``validate`` fetches each present
  backend to validate its mapping (R9.9).
- ``git_credentials/__init__.py`` / ``vms/initializer/credentials.py``: op-time
  CONSTRUCTION of a capability instance to run an operation, not a graph query.
- ``migrate/planning.py``: the migrate dry-run, not a finalized-registry path
  (it keeps its explicit validation, caller inventory A).
- ``manifests/decode.py``: a decode-time shadow check (a code-registry
  membership test), before the graph exists.
- ``config/loaders_secrets.py``: load-time validation of the deprecated
  ``[secret_backends]`` TOML section, before the graph exists (a config-shape
  check, not an edge/readiness probe).
- ``resources/inspect.py`` / ``secrets/inspect.py``: the describe-VIEW
  projections carry a ``references`` field, populated FROM ``dependents_of`` (the
  honest reader), distinct from the retired resource-dataclass field.

Each detection function (``find_*``) is unit-proven non-vacuous by
``test_detectors_are_not_vacuous``: fed a synthetic banned snippet it flags it,
and fed an exempt/benign snippet it stays silent. So a guard that passes is
actually watching, not trivially green.

WHAT THE DETECTORS CATCH, AND THE ACCEPTED RESIDUALS:

- Pattern 2's first spelling catches the bare (``VM_PLATFORM_REGISTRY``),
  qualified (``vm_platform.VM_PLATFORM_REGISTRY``), and aliased-import
  (``... import VM_PLATFORM_REGISTRY as R``) read idioms. Its second spelling
  catches a ``<expr>.registry(...)`` CALL, which is the only way the descriptor
  accessor yields the live dict; referencing the field without calling it
  (``{d.kind: d.registry for d in ...}``, the graph's loader map) hands on a
  callable rather than reaching a registry, so it is deliberately not flagged.
- Pattern 4's declaration check catches a ``references`` member declared as an
  annotated field, a plain class attribute, or a method/property; this is the
  real defense, because the read side can only catch the literal
  ``getattr(x, "references")`` form (a plain ``.references`` read is
  indistinguishable from the exempt describe views without type inference).
- Allow-list scoping is whole-FILE, not whole-function: a banned pattern
  reintroduced INSIDE an exempt module is not caught by the module scan. The
  softest such module is ``vms/sites.py`` (exempt for patterns 1, 2, and 3 at
  once); ``test_vms_sites_exempt_reads_are_function_scoped`` pins its sanctioned
  reads to the functions that own them to close that one gap.
- Patterns 1, 2's second spelling, and 3 match direct attribute calls, not deep
  indirection (``fn = decl.dependencies; fn(ctx)``,
  ``get = d.registry; get().get(name)``). That is a deliberate two-line form no
  accidental regression takes and is not currently exploitable, so it is an
  ACCEPTED RESIDUAL, not a hole to chase. The one place the codebase does hold a
  registry accessor in a local and call it is ``_impl_for``, inside the graph
  builder that owns the loader map.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

import pytest

import agentworks

_AGENTWORKS_ROOT = Path(agentworks.__file__).parent

_CAPABILITY_REGISTRIES = frozenset(
    {
        "VM_PLATFORM_REGISTRY",
        "HARNESS_INTEGRATION_REGISTRY",
        "GIT_CREDENTIAL_PROVIDER_REGISTRY",
        "SECRET_BACKEND_REGISTRY",
    }
)


# -- Detection functions (pure over source text; proven non-vacuous below) ----


def find_dependencies_calls(source: str) -> list[int]:
    """Line numbers of ``<expr>.dependencies(...)`` attribute calls.

    Catches a consumer re-walking a resource's ``dependencies()`` to rebuild
    edges (banned pattern 1). ``def dependencies`` is a definition, not a call,
    and the module-function ``_dependencies(resource, ctx)`` is a bare-name call,
    so neither matches: only genuine attribute calls do.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dependencies"
    )


def _registry_aliases(tree: ast.AST) -> frozenset[str]:
    """Local names bound to a capability registry by an aliased import
    (``from ... import VM_PLATFORM_REGISTRY as R``). A plain
    ``from ... import VM_PLATFORM_REGISTRY`` binds the registry's own name, which
    the read matcher already catches, so only ``asname`` aliases need tracking.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _CAPABILITY_REGISTRIES and alias.asname:
                    aliases.add(alias.asname)
    return frozenset(aliases)


def _is_registry_read(node: ast.AST, aliases: frozenset[str]) -> TypeGuard[ast.expr]:
    """Whether ``node`` reads one of the four capability registries in any of the
    three idioms an accidental reintroduction would use: a bare name
    (``VM_PLATFORM_REGISTRY``), a qualified attribute
    (``vm_platform.VM_PLATFORM_REGISTRY``), or an aliased import's local name.
    """
    if isinstance(node, ast.Name):
        return node.id in _CAPABILITY_REGISTRIES or node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in _CAPABILITY_REGISTRIES
    return False


def find_registry_reads(source: str) -> list[int]:
    """Line numbers where one of the four capability registries is read (banned
    pattern 2), in bare, qualified, or aliased-import form (:func:`_is_registry_read`).

    An ``import`` statement binds a name via an ``alias`` node (not an
    ``ast.Name`` read) and ``__all__`` holds string literals, so neither the
    import line nor the export list matches; docstring mentions are strings. Only
    real reads match.
    """
    tree = ast.parse(source)
    aliases = _registry_aliases(tree)
    return sorted({node.lineno for node in ast.walk(tree) if _is_registry_read(node, aliases)})


def find_descriptor_registry_calls(source: str) -> list[int]:
    """Line numbers of ``<expr>.registry(...)`` attribute calls: banned pattern
    2 reached through a capability-kind descriptor rather than by naming the
    registry.

    ``CapabilityKindDescriptor.registry`` is a lazy accessor returning the LIVE
    dict, so ``descriptor_for("vm-platform").registry().get(name)`` is the same
    availability probe ``find_registry_reads`` was written about, spelled so that
    scan cannot see it.

    Only CALLS match. The four kinds modules' ``def _registry()`` accessor
    definitions are not calls; ``registry.add(...)`` is a call on a resource
    Registry, not of a ``registry`` member; and the loader-map comprehension
    (``{d.kind: d.registry for d in ...}``) passes the callable on without
    reaching a registry.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "registry"
    )


def _is_not_ready_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """Whether ``node`` is a ``<expr>.not_ready(...)`` attribute call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "not_ready"


def find_not_ready_calls(source: str) -> list[int]:
    """Line numbers of ``<expr>.not_ready(...)`` attribute calls.

    Catches a lazy readiness recompute (banned pattern 3). A local dict named
    ``not_ready`` accessed by subscript / ``in`` is not an attribute call, so
    ``doctor``'s ``not_ready`` bookkeeping map does not match.
    """
    return sorted(node.lineno for node in ast.walk(ast.parse(source)) if _is_not_ready_call(node))


def find_getattr_references(source: str) -> list[int]:
    """Line numbers of ``getattr(<expr>, "references"...)`` calls (banned
    pattern 4's dynamic form: reaching for the retired resource field by name).
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "references"
        ):
            found.append(node.lineno)
    return sorted(found)


def find_references_fields(source: str) -> list[int]:
    """Line numbers of a class-scope ``references`` DECLARATION (banned pattern
    4's structural form: a resource carrying its own inbound-reference member
    instead of the graph holding it).

    The read side can only catch the literal ``getattr(x, "references")`` form (a
    plain ``.references`` attribute read is indistinguishable from the exempt
    describe views without type inference), so the declaration check is the real
    defense and must cover every way the member is reintroduced: an annotated
    field (``references: T``), a plain class attribute (``references = ()``), or a
    method / property named ``references``.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            annotated = (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "references"
            )
            assigned = isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "references" for target in stmt.targets
            )
            defined = isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and stmt.name == "references"
            if annotated or assigned or defined:
                found.append(stmt.lineno)
    return sorted(found)


# -- Allow-lists (relative POSIX paths under agentworks/); see module docstring
#    for the justification of every entry.


_DEPENDENCIES_ALLOWLIST = frozenset(
    {
        "resources/graph.py",  # graph builder
        "resources/registry.py",  # graph builder (walks dependencies(context))
        # Deliberately ABSENT since declarative-schema step 2.3, and each
        # absence is the point of that step rather than an oversight. The
        # three consuming resources and the capability base all used to call
        # a CAPABILITY's ``dependencies(owner, config)``; that classmethod is
        # gone, and the core reads a capability's references off its declared
        # model instead. An attribute call to ``dependencies`` reappearing in
        # any of them would be the invoked contract coming back, which is
        # exactly what this guard should refuse:
        #   capabilities/base.py, vms/sites.py,
        #   git_credentials/credential.py, sessions/template.py
    }
)

_REGISTRY_READ_ALLOWLIST = frozenset(
    {
        # Owners (each declares its kind's registry).
        "capabilities/vm_platform/__init__.py",
        "capabilities/harness_integration/__init__.py",
        "capabilities/git_credential/__init__.py",
        "secrets/backends.py",
        # The whole plugin framework is deliberately ABSENT. Its adapters
        # still seat plugin impls into the four registries (the plugin analog
        # of the built-in publishers), but the one generic adapter reaches
        # them through the descriptor's registry accessor
        # (declarative-schema step 2.0), exactly as the graph builder does,
        # so ``plugins/adapters.py`` and ``plugins/registration.py`` name no
        # registry at all and exempting either would only excuse a future
        # probe.
        # The four capability-kind descriptors (declarative-schema step 2.0).
        # Each carries the lazy accessor for its own registry, which IS the
        # builder's per-kind loader relocated beside the kind it belongs to:
        # the graph builder's loader map derives from these, so the sanctioned
        # builder-reads-registry path moved here, it did not multiply. Same
        # exemption, new address, and ``resources/graph.py`` gave its own up
        # (it names no registry now).
        "capabilities/vm_platform/kinds.py",
        "capabilities/harness_integration/kinds.py",
        "capabilities/git_credential/kinds.py",
        "secrets/kinds.py",
        # Op-time construction of a capability instance.
        "vms/sites.py",  # resolve_site: the one chokepoint every VM op passes
        "git_credentials/__init__.py",
        "vms/initializer/credentials.py",
        # Deliberately ABSENT since declarative-schema step 2.3: edge
        # production and finalize validate no longer fetch a capability
        # class at all. Each of the four asks the core instead
        # (``capability_config_references`` / ``validate_capability_config``),
        # which reaches the registry once, in ``capabilities/config.py``, on
        # the descriptor allow-list below. A registry read reappearing in any
        # of them would be the probe this pattern bans:
        #   git_credentials/credential.py, sessions/template.py,
        #   secrets/base.py, migrate/planning.py
        # Decode-time shadow check (code-registry membership).
        "manifests/decode.py",
        # Load-time validation of the deprecated [secret_backends] section.
        "config/loaders_secrets.py",
    }
)

_DESCRIPTOR_REGISTRY_ALLOWLIST = frozenset(
    {
        # The three capability-registry write-and-mirror paths, and only
        # those. Each is the registry's own machinery reaching it through the
        # descriptor, which is where the accessor now lives; none of them
        # probes availability.
        "capabilities/publish.py",  # generic built-in publisher (registry -> rows)
        "plugins/adapters.py",  # generic adapter: peek / seat / build_row
        "plugins/registration.py",  # snapshot + restore around a seated plugin
        # Core-driven capability config (declarative-schema step 2.3). It
        # fetches the seated implementation CLASS to read the config model it
        # declares, which is the edge-production-and-validate read the four
        # consuming resources used to do by naming their kind's registry.
        # Same sanctioned read, relocated: all four gave up their own
        # exemptions above when they moved onto this module, so the exempted
        # surface for that read is one call site rather than four.
        # Availability is never what it asks: an absent name yields no model,
        # and the dangling capability edge is what reports it.
        "capabilities/config.py",
        # Deliberately ABSENT, and each absence is load-bearing rather than an
        # oversight:
        #   resources/graph.py     -- the builder reaches registries through
        #                             loaders taken from its OWN private
        #                             _capability_registry_loaders map, never
        #                             through <descriptor>.registry(), so it
        #                             needs no exemption from this spelling.
        #   the four kinds.py      -- they DEFINE their kind's accessor
        #                             (``def _registry()``); defining it is not
        #                             calling it, and their reads inside it are
        #                             already pinned function-scoped by
        #                             test_descriptor_exempt_reads_are_function_scoped.
    }
)

_NOT_READY_ALLOWLIST = frozenset(
    {
        "resources/graph.py",  # the readiness fold
        "vms/sites.py",  # VMSite.not_ready hook -> platform impl off the graph
        # The secret-backend descriptor's readiness callable: the fold's own
        # secret-backend branch, relocated onto the descriptor so the fold
        # stops enumerating kinds (declarative-schema step 2.0). It is fold
        # code living beside its kind, not a projection surface recomputing
        # a verdict, and it is now the SOLE implementation: the fold calls it
        # rather than duplicating it.
        "secrets/kinds.py",
    }
)

_REFERENCES_FIELD_ALLOWLIST = frozenset(
    {
        "resources/inspect.py",  # describe view, populated from dependents_of
        "secrets/inspect.py",  # describe view, populated from dependents_of
    }
)


def _iter_agentworks_modules() -> list[tuple[str, Path]]:
    return [(path.relative_to(_AGENTWORKS_ROOT).as_posix(), path) for path in sorted(_AGENTWORKS_ROOT.rglob("*.py"))]


def _scan(finder: object, allowlist: frozenset[str]) -> list[str]:
    """Run ``finder`` over every agentworks module not on ``allowlist`` and
    return ``"<rel>:<lineno>"`` offenders (blank when the guard holds)."""
    assert callable(finder)
    offenders: list[str] = []
    for rel, path in _iter_agentworks_modules():
        if rel in allowlist:
            continue
        for lineno in finder(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}:{lineno}")
    return offenders


# -- Banned-pattern guards -----------------------------------------------------


def test_pattern1_no_dependencies_rewalk_outside_the_builder() -> None:
    offenders = _scan(find_dependencies_calls, _DEPENDENCIES_ALLOWLIST)
    assert not offenders, (
        "Banned pattern 1 (re-walking dependencies() to reconstruct edges "
        "outside the graph build). Read edges off the retained graph "
        "(edges_of / reachable_from) instead, or add the module to the "
        "documented edge-production/builder allow-list if it is genuinely one:\n" + "\n".join(offenders)
    )


def test_pattern2_no_registry_probe_outside_builder_and_publishers() -> None:
    named_offenders = _scan(find_registry_reads, _REGISTRY_READ_ALLOWLIST)
    descriptor_offenders = _scan(find_descriptor_registry_calls, _DESCRIPTOR_REGISTRY_ALLOWLIST)
    assert not named_offenders, (
        "Banned pattern 2 (a *_REGISTRY availability probe outside the "
        "publishers, the graph builder, and the sanctioned "
        "construction/validation paths). Read readiness off the graph node "
        "(readiness_of) and a capability's code via impl_of:\n" + "\n".join(named_offenders)
    )
    assert not descriptor_offenders, (
        "Banned pattern 2, second spelling (reaching a live capability registry "
        "via <descriptor>.registry() rather than by naming it). The descriptor's "
        "accessor returns the same dict, so this is the same probe with the "
        "registry's name filed off. Read readiness off the graph node "
        "(readiness_of) and a capability's code via impl_of:\n" + "\n".join(descriptor_offenders)
    )


def test_pattern3_no_not_ready_recompute_outside_the_fold() -> None:
    offenders = _scan(find_not_ready_calls, _NOT_READY_ALLOWLIST)
    assert not offenders, (
        "Banned pattern 3 (calling not_ready outside the readiness fold). A "
        "projection surface must read the stored verdict off the graph "
        "(readiness_of / not_ready_reason_for), never recompute it:\n" + "\n".join(offenders)
    )


def test_pattern4_no_references_field_or_getattr_on_resources() -> None:
    field_offenders = _scan(find_references_fields, _REFERENCES_FIELD_ALLOWLIST)
    getattr_offenders = _scan(find_getattr_references, frozenset())
    assert not field_offenders, (
        "Banned pattern 4 (a resource dataclass carrying its own inbound "
        "`references` field). Inbound references live on the graph; read them "
        "via dependents_of:\n" + "\n".join(field_offenders)
    )
    assert not getattr_offenders, (
        'Banned pattern 4 (getattr(_, "references") reaching for the retired '
        "resource field). Read inbound references via dependents_of:\n" + "\n".join(getattr_offenders)
    )


# -- Positive assertions: the honest path is present (not just the banned path
#    absent). These confirm migrated consumers read the graph query API, so a
#    future refactor cannot quietly swap them back.


def _read(rel: str) -> str:
    return (_AGENTWORKS_ROOT / rel).read_text(encoding="utf-8")


def _function_source(rel: str, name: str) -> str:
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(_read(rel), node)
            assert segment is not None
            return segment
    raise AssertionError(f"{rel}: function {name!r} not found (guard baseline drifted from HEAD)")


def _enclosing_functions(source: str, predicate: Callable[[ast.AST], bool]) -> list[tuple[str | None, int]]:
    """For every node matching ``predicate``, the name of the innermost enclosing
    function (``None`` at module scope) and the node's line number. Used to pin an
    exempt module's sanctioned reads to the specific functions that own them."""
    hits: list[tuple[str | None, int]] = []

    def walk(node: ast.AST, stack: list[str]) -> None:
        if predicate(node):
            hits.append((stack[-1] if stack else None, getattr(node, "lineno", 0)))
        inner = [*stack, node.name] if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else stack
        for child in ast.iter_child_nodes(node):
            walk(child, inner)

    walk(ast.parse(source), [])
    return hits


def test_collect_secrets_for_reads_reachable_from() -> None:
    source = _function_source("resources/walk.py", "collect_secrets_for")
    assert "reachable_from" in source
    assert find_dependencies_calls(source) == []


def test_node_factories_read_edges_of() -> None:
    for rel, factory in (("vms/nodes.py", "vm_site_node"), ("git_credentials/nodes.py", "git_credential_node")):
        source = _function_source(rel, factory)
        assert "edges_of" in source, f"{rel}:{factory} must read edges_of"
        assert find_dependencies_calls(source) == [], f"{rel}:{factory} must not re-walk dependencies()"


def test_cycle_detection_reads_the_built_edge_map() -> None:
    source = _function_source("resources/registry.py", "_detect_cycles")
    assert "all_outbound" in source, "cycle detection must three-color over the built edge map"
    assert find_dependencies_calls(source) == [], "cycle detection must not re-walk dependencies()"


def test_readiness_projections_read_readiness_of() -> None:
    # The was-banned readiness recompute sites now read the stored verdict.
    assert "readiness_of" in _read("resources/inspect.py")
    assert "readiness_of" in _read("doctor.py")
    # The two named banned-pattern-2 sites carry zero registry reads.
    assert find_registry_reads(_read("vms/kinds.py")) == []
    assert find_registry_reads(_read("secrets/resolve.py")) == []
    # Secret resolution reaches a backend's code off the graph, not the registry.
    assert "impl_of" in _read("secrets/resolve.py")


def test_vms_sites_exempt_reads_are_function_scoped() -> None:
    """``vms/sites.py`` is the one module exempt for patterns 1, 2, AND 3 at
    once, so whole-file scoping is at its softest here: a regression re-inlining
    the old ``site_disabled_reason`` readiness recompute would evade all three
    scans. Pin the sanctioned reads to the functions that own them (mirroring the
    ``vms/kinds.py`` zero-read pin) so a stray read in any new function trips.

    Registry reads belong to edge production (``dependencies``), the finalize
    ``validate`` pass, and op-time construction (``resolve_site``). The only
    ``not_ready`` call is ``VMSite.not_ready`` delegating to the graph-stamped
    platform impl.
    """
    source = _read("vms/sites.py")
    aliases = _registry_aliases(ast.parse(source))
    reg_offenders = [
        f"{func}:{lineno}"
        for func, lineno in _enclosing_functions(source, lambda node: _is_registry_read(node, aliases))
        if func not in {"dependencies", "validate", "resolve_site"}
    ]
    not_ready_offenders = [
        f"{func}:{lineno}"
        for func, lineno in _enclosing_functions(source, _is_not_ready_call)
        if func not in {"not_ready"}
    ]
    assert not reg_offenders, (
        "vms/sites.py reads a capability registry outside its edge-production / "
        "validate / construction functions (a readiness recompute would land "
        "here and evade the module-scoped scans):\n" + "\n".join(reg_offenders)
    )
    assert not not_ready_offenders, (
        "vms/sites.py calls not_ready outside VMSite.not_ready (read the stored "
        "verdict via readiness_of instead):\n" + "\n".join(not_ready_offenders)
    )


def test_descriptor_exempt_reads_are_function_scoped() -> None:
    """The four capability ``kinds.py`` modules are exempt only for the
    descriptor accessors they carry, not wholesale.

    Their exemption rests on "this is the graph builder's own code relocated
    beside the kind it serves". Whole-file scoping does not say that: it would
    equally excuse a readiness recompute or an availability probe added to a
    kind strategy later, in the module whose PREDECESSOR
    (``_VMSiteKind.disabled_reason``, reaching into ``VM_PLATFORM_REGISTRY``)
    is one of the two sites banned pattern 2 was written about. So pin each
    read to the accessor that owns it, exactly as ``vms/sites.py`` is pinned.

    ``secrets/kinds.py`` needs it most: it is the only other module exempt for
    two patterns at once, and it is where a "just ask the backend" recompute
    would most naturally be written.
    """
    registry_owners = {
        "capabilities/vm_platform/kinds.py": {"_registry"},
        "capabilities/harness_integration/kinds.py": {"_registry"},
        "capabilities/git_credential/kinds.py": {"_registry"},
        "secrets/kinds.py": {"_backend_registry"},
    }
    offenders: list[str] = []
    for rel, owners in registry_owners.items():
        source = _read(rel)
        aliases = _registry_aliases(ast.parse(source))

        def _reads_registry(node: ast.AST, aliases: frozenset[str] = aliases) -> bool:
            return _is_registry_read(node, aliases)

        offenders += [
            f"{rel}:{func}:{lineno}"
            for func, lineno in _enclosing_functions(source, _reads_registry)
            if func not in owners
        ]
    assert not offenders, (
        "a capability kinds module reads a capability registry outside the "
        "descriptor's registry accessor (the exemption covers that accessor, "
        "not the module):\n" + "\n".join(offenders)
    )

    backend_source = _read("secrets/kinds.py")
    not_ready_offenders = [
        f"secrets/kinds.py:{func}:{lineno}"
        for func, lineno in _enclosing_functions(backend_source, _is_not_ready_call)
        if func != "_backend_readiness"
    ]
    assert not not_ready_offenders, (
        "secrets/kinds.py calls not_ready outside the descriptor's readiness "
        "callable (read the stored verdict via readiness_of instead):\n" + "\n".join(not_ready_offenders)
    )


# -- Allow-list hygiene: an exemption that stops firing is a hole -------------


#: Allow-list entries kept even though nothing in them currently fires the
#: detector, with the reason each is worth keeping. Both are the graph
#: BUILDER, which walks every resource's ``dependencies(context)`` through
#: the module-level ``_dependencies`` helper (a bare-name call the detector
#: deliberately ignores). Spelling that walk as an attribute call again would
#: be an ordinary refactor of the sanctioned path, not a regression, so the
#: exemption stays ahead of it.
_DELIBERATELY_QUIET = {
    ("_DEPENDENCIES_ALLOWLIST", "resources/graph.py"),
    ("_DEPENDENCIES_ALLOWLIST", "resources/registry.py"),
}


@pytest.mark.parametrize(
    ("name", "finder", "allowlist"),
    [
        pytest.param("_DEPENDENCIES_ALLOWLIST", find_dependencies_calls, _DEPENDENCIES_ALLOWLIST, id="dependencies"),
        pytest.param("_REGISTRY_READ_ALLOWLIST", find_registry_reads, _REGISTRY_READ_ALLOWLIST, id="registry-read"),
        pytest.param(
            "_DESCRIPTOR_REGISTRY_ALLOWLIST",
            find_descriptor_registry_calls,
            _DESCRIPTOR_REGISTRY_ALLOWLIST,
            id="descriptor-registry",
        ),
        pytest.param("_NOT_READY_ALLOWLIST", find_not_ready_calls, _NOT_READY_ALLOWLIST, id="not-ready"),
        pytest.param(
            "_REFERENCES_FIELD_ALLOWLIST", find_references_fields, _REFERENCES_FIELD_ALLOWLIST, id="references-field"
        ),
    ],
)
def test_every_exemption_is_load_bearing(name: str, finder: object, allowlist: frozenset[str]) -> None:
    """An exemption whose module no longer performs the banned operation is a
    HOLE, not a harmless leftover: the guard goes on trusting a module that
    has nothing left to trust it for, so a regression reintroducing the
    pattern there passes silently.

    This is the rot that follows a migration, and it is invisible without a
    check like this one: declarative-schema step 2.3 moved four modules off
    the capability registries and four off the invoked ``dependencies``
    contract, and every one of those eight exemptions survived the change
    reading as if it were still justified.

    Deliberate exceptions are declared, not tolerated (:data:`_DELIBERATELY_QUIET`).
    """
    dead = sorted(
        entry
        for entry in allowlist
        if (name, entry) not in _DELIBERATELY_QUIET and not _scan(finder, frozenset(allowlist - {entry}))
    )

    assert not dead, (
        f"{name} exempts {dead}, but removing each changes nothing: those modules no longer "
        f"perform the banned operation, so the exemption only widens what a future regression "
        f"can slip through. Delete the entry, or add it to _DELIBERATELY_QUIET with the reason "
        f"it is worth keeping ahead of a refactor."
    )


# -- Non-vacuity self-check: each detector actually flags its banned shape and
#    stays quiet on the honest / benign shape.


def test_detectors_are_not_vacuous() -> None:
    # Pattern 1: a consumer re-walking a resource's dependencies is caught; the
    # module-level _dependencies helper and a def are not.
    assert find_dependencies_calls("edges = decl.dependencies(context)") == [1]
    assert find_dependencies_calls("edges = _dependencies(resource, context)") == []
    assert find_dependencies_calls("def dependencies(self, context):\n    return []") == []

    # Pattern 2: the bare, qualified, and aliased-import reads are all caught; an
    # import / __all__ / docstring mention is not.
    assert find_registry_reads("cap = VM_PLATFORM_REGISTRY.get(name)") == [1]
    assert find_registry_reads("cap = vm_platform.VM_PLATFORM_REGISTRY.get(x)") == [1]
    assert find_registry_reads("from x import VM_PLATFORM_REGISTRY as R\ncap = R.get(x)") == [2]
    assert find_registry_reads("from agentworks.x import SECRET_BACKEND_REGISTRY") == []
    assert find_registry_reads('__all__ = ["HARNESS_INTEGRATION_REGISTRY"]') == []
    assert find_registry_reads('"""mentions GIT_CREDENTIAL_PROVIDER_REGISTRY in prose."""') == []

    # Pattern 2's second spelling: reaching the same live dict through the
    # descriptor's accessor is caught, whether the descriptor is looked up
    # inline or already in hand. Defining the accessor, handing it on
    # uncalled, and calling a method ON a resource Registry are not.
    assert find_descriptor_registry_calls("impl = descriptor_for(kind).registry().get(name)") == [1]
    assert find_descriptor_registry_calls("seated = self.descriptor.registry()[name]") == [1]
    assert find_descriptor_registry_calls("def _registry():\n    return VM_PLATFORM_REGISTRY") == []
    assert find_descriptor_registry_calls("loaders = {d.kind: d.registry for d in table}") == []
    assert find_descriptor_registry_calls("registry.add(kind, name, row, origin)") == []

    # Pattern 3: a not_ready recompute call is caught; a not_ready dict is not.
    assert find_not_ready_calls("verdict = platform.not_ready(config)") == [1]
    assert find_not_ready_calls("not_ready = {}\nnot_ready[name] = reason\nif name in not_ready:\n    pass") == []

    # Pattern 4: the getattr form plus every declaration form (annotated field,
    # plain attribute, method/property) are caught; reading a describe view's own
    # field and a benign field are not.
    assert find_getattr_references('refs = getattr(decl, "references", ())') == [1]
    assert find_getattr_references("refs = desc.references") == []
    assert find_references_fields("class R:\n    references: tuple = ()") == [2]
    assert find_references_fields("class R:\n    references = ()") == [2]
    assert find_references_fields("class R:\n    @property\n    def references(self):\n        return ()") == [3]
    assert find_references_fields("class R:\n    inbound: tuple = ()") == []
