"""The R11 anti-bypass guard (registry-readiness-refactor, plan phase 6).

The refactor forced the whole system onto the retained ``DependencyGraph`` as
the single access path for structural and derived facts, and removed the bypass
paths. This guard pins that the four banned patterns cannot silently return: it
is the enforcement mechanism that keeps the migration from eroding one careless
commit at a time. Its baseline is the caller inventory's "Guard baseline"
section (``docs/sdd/2026-07-27-registry-readiness-refactor/caller-inventory.md``);
the precise banned-pattern definitions and exemptions are LLD (b),
``finalize-ordering-lld.md`` -> "The anti-bypass guard (R11)".

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

- ``capabilities/base.py``: the construct-time ``_secret_refs`` derivation, a
  capability computing its OWN config-implied refs from its OWN config via
  ``dependencies(config)`` (not a graph re-walk).
- ``resources/graph.py`` + ``resources/registry.py``: the graph BUILDER. It
  walks each resource's ``dependencies(context)`` (handing it the build
  context), reads the four capability code registries to stamp each capability
  node's impl (``_impl_for`` / ``build_context``), and calls ``not_ready`` in
  the fold. The sanctioned builder-reads-registry path (LLD b exemptions).
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

- Pattern 2 catches the bare (``VM_PLATFORM_REGISTRY``), qualified
  (``vm_platform.VM_PLATFORM_REGISTRY``), and aliased-import
  (``... import VM_PLATFORM_REGISTRY as R``) read idioms.
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
- Patterns 1 and 3 match direct attribute calls, not deep indirection
  (``fn = decl.dependencies; fn(ctx)``). That is a deliberate two-line form no
  accidental regression takes and is not currently exploitable, so it is an
  ACCEPTED RESIDUAL, not a hole to chase.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

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
        "capabilities/base.py",  # construct-time _secret_refs (own config)
        "vms/sites.py",  # edge production (capability.dependencies)
        "git_credentials/credential.py",  # edge production
        "sessions/template.py",  # edge production
    }
)

_REGISTRY_READ_ALLOWLIST = frozenset(
    {
        # Publishers (own the registry).
        "capabilities/vm_platform/__init__.py",
        "capabilities/harness_integration/__init__.py",
        "capabilities/git_credential/__init__.py",
        "secrets/backends.py",
        # Plugin framework: the per-kind adapters SEAT plugin impls into the
        # four registries (the plugin analog of the built-in publishers), and
        # register_plugin snapshots/restores them for the seat/unseat helper.
        "plugins/adapters.py",
        "plugins/registration.py",
        # Graph builder (stamps impls, assembles the build context, folds).
        "resources/graph.py",
        # Edge production + finalize validate (fetch the capability class).
        "vms/sites.py",
        "git_credentials/credential.py",
        "sessions/template.py",
        "secrets/base.py",
        # Op-time construction of a capability instance.
        "git_credentials/__init__.py",
        "vms/initializer/credentials.py",
        # Migrate dry-run (not a finalized-registry path).
        "migrate/planning.py",
        # Decode-time shadow check (code-registry membership).
        "manifests/decode.py",
        # Load-time validation of the deprecated [secret_backends] section.
        "config/loaders_secrets.py",
    }
)

_NOT_READY_ALLOWLIST = frozenset(
    {
        "resources/graph.py",  # the readiness fold
        "vms/sites.py",  # VMSite.not_ready hook -> platform impl off the graph
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
    offenders = _scan(find_registry_reads, _REGISTRY_READ_ALLOWLIST)
    assert not offenders, (
        "Banned pattern 2 (a *_REGISTRY availability probe outside the "
        "publishers, the graph builder, and the sanctioned "
        "construction/validation paths). Read readiness off the graph node "
        "(readiness_of) and a capability's code via impl_of:\n" + "\n".join(offenders)
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
#    absent). LLD (b) asks the guard to confirm the migrated consumers read the
#    graph query API, so a future refactor cannot quietly swap them back.


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
