"""Tests for the retained ``DependencyGraph`` (phase 2 of the registry
readiness refactor, LLD a).

The graph is a pure structural retention of the reference edge set the
finalize walk already computes: ``edges_of`` (outbound), ``dependents_of``
(inbound, the replacement for the removed per-resource ``references`` field),
and ``runtime_reachable_from`` (transitive closure). This phase computes no real
readiness, so every node is ready/enabled; that is asserted here too.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import (
    Enablement,
    Origin,
    Readiness,
    Registry,
    ResourceReference,
    collect_secrets_for,
)
from agentworks.resources.reference import ReferenceEntry
from tests.conftest import ManifestDoc, write_manifests


@dataclass(frozen=True)
class _Node:
    """A test-only resource that emits a fixed edge list. Published under an
    arbitrary kind; every target is published up front so no miss policy
    fires and the kind never needs to be in ``KIND_REGISTRY``."""

    reqs: tuple[ResourceReference, ...] = ()
    origin: Origin | None = None

    def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
        return self.reqs


def _edge(src: str, dst: str) -> ResourceReference:
    """An edge from ``("node", src)`` to ``("node", dst)``. Source is the
    emitting node itself, as real producers always set it."""
    return ResourceReference(
        name=dst,
        kind="node",
        usage=f"{src} needs {dst}",
        source=("node", src),
    )


def _graph_from(edges: dict[str, list[str]]) -> Registry:
    """Build and finalize a registry whose ``node`` rows have exactly the
    given outbound edges. Every name mentioned (source or target) is
    published, so finalize dispatches no miss policy."""
    names = set(edges) | {dst for dsts in edges.values() for dst in dsts}
    r = Registry.empty()
    origin = Origin.built_in(source="tests.graph")
    for name in sorted(names):
        reqs = tuple(_edge(name, dst) for dst in edges.get(name, []))
        r.add("node", name, _Node(reqs=reqs), origin)
    r.finalize()
    return r


# -- edges_of / dependents_of / the closure on a multi-hop chain -------------


def test_chain_edges_dependents_and_reachability() -> None:
    """A -> B -> C -> D. Each node points at the next; reachability from the
    head is the whole tail in order."""
    graph = _graph_from({"a": ["b"], "b": ["c"], "c": ["d"]}).graph

    assert [(r.kind, r.name) for r in graph.edges_of("node", "a")] == [("node", "b")]
    assert [(r.kind, r.name) for r in graph.edges_of("node", "d")] == []

    # Inbound: d is pointed at only by c.
    assert graph.dependents_of("node", "d") == (ReferenceEntry(source=("node", "c"), usage="c needs d"),)
    assert graph.dependents_of("node", "a") == ()

    assert graph.runtime_reachable_from("node", "a") == [("node", "b"), ("node", "c"), ("node", "d")]
    assert graph.runtime_reachable_from("node", "c") == [("node", "d")]
    assert graph.runtime_reachable_from("node", "d") == []


# -- the diamond: dedupe + two inbound edges ---------------------------------


def test_diamond_reachability_dedupes_and_records_both_inbound_edges() -> None:
    """A -> B, A -> C, B -> D, C -> D. D is reachable by two paths but appears
    once in the closure, and carries two inbound references."""
    graph = _graph_from({"a": ["b", "c"], "b": ["d"], "c": ["d"]}).graph

    # Outbound order preserves first-encountered edge order.
    assert [(r.kind, r.name) for r in graph.edges_of("node", "a")] == [("node", "b"), ("node", "c")]

    # D reached once, deduped, first-encountered order (b before c, d via b).
    assert graph.runtime_reachable_from("node", "a") == [("node", "b"), ("node", "c"), ("node", "d")]

    # Both b and c point at d.
    inbound_sources = {entry.source for entry in graph.dependents_of("node", "d")}
    assert inbound_sources == {("node", "b"), ("node", "c")}
    assert len(graph.dependents_of("node", "d")) == 2


def test_edges_of_preserves_source_emission_order_across_sources() -> None:
    """A source's ``outbound`` is ordered by its own emission order, even when
    an earlier source already inserted a later target first. Regression against
    re-deriving outbound from the target-keyed reference map (which would
    reorder a source's edges to match global target-first-encounter order).

    ``early`` emits ``-> m_a`` and is walked first (sorted publish order), so
    target ``m_a`` is inserted before ``m_b``. ``src`` then emits
    ``[-> m_b, -> m_a]``. A target-keyed re-key would yield ``[src->m_a,
    src->m_b]``; the source-keyed walk preserves ``src``'s emission order.
    """
    graph = _graph_from({"early": ["m_a"], "src": ["m_b", "m_a"]}).graph
    assert [(r.kind, r.name) for r in graph.edges_of("node", "src")] == [
        ("node", "m_b"),
        ("node", "m_a"),
    ]


def test_the_closure_is_cycle_safe() -> None:
    """The closure tolerates a cycle via its visited set (it may be
    called on a graph built before finalize's cycle pass in tests). The
    registry itself rejects cycles at finalize, so build the cyclic graph
    directly from the graph builder to exercise the query in isolation."""
    from agentworks.resources.graph import build_graph

    # a -> b, b -> a (the cycle), b -> c.
    edges = [_edge("a", "b"), _edge("b", "a"), _edge("b", "c")]
    all_refs: dict[tuple[str, str], list[ResourceReference]] = {}
    all_outbound: dict[tuple[str, str], list[ResourceReference]] = {}
    for e in edges:
        all_refs.setdefault((e.kind, e.name), []).append(e)
        all_outbound.setdefault(e.source, []).append(e)
    resources = {"node": {"a": object(), "b": object(), "c": object()}}
    graph = build_graph(resources, all_refs, all_outbound)

    # Does not loop; visits every other node once.
    assert set(graph.runtime_reachable_from("node", "a")) == {("node", "b"), ("node", "c")}


# -- query error semantics + this-phase readiness defaults -------------------


def test_edges_and_dependents_raise_on_unknown_key() -> None:
    graph = _graph_from({"a": ["b"]}).graph
    with pytest.raises(KeyError):
        graph.edges_of("node", "missing")
    with pytest.raises(KeyError):
        graph.dependents_of("node", "missing")


def test_every_node_is_ready_and_enabled_this_phase() -> None:
    """Phase 2 computes no real readiness: every node is ready/enabled, and
    ``readiness_of`` tolerates a missing node with a default-ready verdict."""
    reg = _graph_from({"a": ["b"]})
    graph = reg.graph
    for name in ("a", "b"):
        assert graph.readiness_of("node", name) == Readiness.ready()
        assert graph.is_ready("node", name) is True
        assert graph._nodes[("node", name)].enablement is Enablement.enabled
    # Missing node: default-ready, no raise (the projection-surface tolerance).
    assert graph.readiness_of("node", "missing").is_ready is True
    assert graph.is_ready("node", "missing") is True


# -- golden: dependents_of reproduces exactly the old references field -------


def _write_cfg(tmp_path: Path, settings: str = "", *manifests: ManifestDoc | str) -> Path:
    """Write a settings-only config.toml plus its resources/ manifests and
    return the config path. ``settings`` carries settings-only TOML (operator
    block plus any ``[secret_config]`` / ``[plugins]``); the resources under
    test are authored as ``manifests`` beside it (ADR 0022)."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(settings)
    )
    if manifests:
        write_manifests(tmp_path, *manifests)
    return p


def _recompute_inbound(registry: Registry) -> dict[tuple[str, str], list[ReferenceEntry]]:
    """Independently rebuild the inbound map ``dependents_of`` exposes: walk
    every resource's ``dependencies(context)`` and project each edge onto its
    target. An independent recomputation (not a read of the graph's own
    inbound), threaded the same build context the finalize walk uses, so a
    secret's ``secret -> secret-backend`` edges (R9.10) are reproduced too."""
    from agentworks.resources.graph import build_context

    context = build_context(registry._resources)
    inbound: dict[tuple[str, str], list[ReferenceEntry]] = {}
    for kind in registry.iter_kinds():
        for _name, resource in registry.iter_kind_items(kind):
            method = getattr(resource, "dependencies", None)
            if method is None:
                continue
            for ref in method(context):
                inbound.setdefault((ref.kind, ref.name), []).append(ReferenceEntry(source=ref.source, usage=ref.usage))
    return inbound


def test_dependents_of_reproduces_old_references_field(tmp_path: Path) -> None:
    """Golden: for every node in a representative real registry,
    ``dependents_of`` holds exactly the entries the removed ``references``
    field held (same set of (source, usage) pairs)."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc("secret", "shared-key", description="Shared"),
        ManifestDoc(
            "admin-template",
            "default",
            {"env": {"ADMIN_KEY": {"secret": "shared-key"}, "API_KEY": {"secret": "auto-key"}}},
        ),
        ManifestDoc(
            "vm-template",
            "azure-prod",
            {"cpus": 2, "env": {"TEMPLATE_KEY": {"secret": "shared-key"}}},
        ),
    )
    registry = build_registry(load_config(cfg, warn_issues=False))
    graph = registry.graph
    expected = _recompute_inbound(registry)

    # Cross-check several concrete nodes carry non-empty inbound sets.
    assert {e.source for e in graph.dependents_of("secret", "shared-key")} == {
        ("admin-template", "default"),
        ("vm-template", "azure-prod"),
    }
    assert {e.source for e in graph.dependents_of("secret", "auto-key")} == {("admin-template", "default")}

    # Inbound ORDER (not just the set) reproduces the walk order the removed
    # field held: a multi-source target lists its referrers first-encountered.
    # The first-source is what the description-polish rule reads, so order is
    # load-bearing. ``shared-key`` is reached by two sources.
    assert [(e.source, e.usage) for e in graph.dependents_of("secret", "shared-key")] == [
        (e.source, e.usage) for e in expected[("secret", "shared-key")]
    ]

    # Exhaustive golden across every published node.
    for kind in registry.iter_kinds():
        for name, _resource in registry.iter_kind_items(kind):
            got = sorted((e.source, e.usage) for e in graph.dependents_of(kind, name))
            want = sorted((e.source, e.usage) for e in expected.get((kind, name), []))
            assert got == want, f"dependents_of({kind!r}, {name!r}) diverged from the old references field"


def test_capability_impls_are_stamped_on_nodes(tmp_path: Path) -> None:
    """The builder populates capability nodes' impl off the code registry
    (heterogeneous: classes for platform/harness-integration/provider, an instance for
    secret-backend); declarable nodes carry ``None``."""
    from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY
    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    cfg = _write_cfg(tmp_path, "")
    registry = build_registry(load_config(cfg, warn_issues=False))
    graph = registry.graph

    for name, cls in HARNESS_INTEGRATION_REGISTRY.items():
        assert graph._nodes[("harness-integration", name)].impl is cls
    for name, backend in SECRET_BACKEND_REGISTRY.items():
        assert graph._nodes[("secret-backend", name)].impl is backend
    # A declarable node carries no impl.
    assert graph._nodes[("vm-template", "default")].impl is None


# -- the runtime closure reproduces collect_secrets_for ----------------------


def test_runtime_closure_matches_collect_secrets_for(tmp_path: Path) -> None:
    """The runtime closure (filtered to secrets) reproduces the set
    ``collect_secrets_for`` returns for representative roots, including a
    VM template whose bootstrap reaches the tailscale auth key."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc("admin-template", "default", {"env": {"ADMIN_KEY": {"secret": "admin-secret"}}}),
        ManifestDoc("vm-template", "default", {"env": {"VM_KEY": {"secret": "vm-secret"}}}),
    )
    registry = build_registry(load_config(cfg, warn_issues=False))
    graph = registry.graph

    for root in (("vm-template", "default"), ("admin-template", "default")):
        walk_names = {decl.name for decl in collect_secrets_for(registry, root)}
        reachable_secret_names = {name for (kind, name) in graph.runtime_reachable_from(*root) if kind == "secret"}
        assert reachable_secret_names == walk_names, f"the runtime closure diverged from collect_secrets_for for {root}"

    # The VM template reaches the auto-declared tailscale key (bootstrap edge).
    vm_secrets = {name for (kind, name) in graph.runtime_reachable_from("vm-template", "default") if kind == "secret"}
    assert "tailscale-auth-key" in vm_secrets
    assert "vm-secret" in vm_secrets


# -- Phase 4 head step: secret -> secret-backend edges -----------------------


def test_auto_declared_secret_has_resolving_backend_edges(tmp_path: Path) -> None:
    """Phase 4 head step (and LLD b subtlety 3, the now-load-bearing
    materialize loop): the default VM template auto-declares
    ``tailscale-auth-key``, and the materialized secret's
    ``dependencies(context)`` emits its ``secret -> secret-backend`` edges to
    every present would-attempt backend. env-var and prompt are
    mapping-optional (always attempt), so both are candidates; onepassword is
    mapping-required and unmapped, so it is not. That the build COMPLETES pins
    the no-loop regression: a materialized secret walks its backend edges, and
    those point only back into the already-present backend nodes, so the
    fixpoint terminates."""
    cfg = _write_cfg(tmp_path, "", ManifestDoc("vm-template", "default"))
    registry = build_registry(load_config(cfg, warn_issues=False))
    graph = registry.graph

    assert any(name == "tailscale-auth-key" for name, _ in registry.iter_kind_items("secret"))
    targets = {(ref.kind, ref.name) for ref in graph.edges_of("secret", "tailscale-auth-key")}
    assert ("secret-backend", "env-var") in targets
    assert ("secret-backend", "prompt") in targets
    assert ("secret-backend", "onepassword") not in targets


def test_backend_rows_gain_inbound_secret_refs(tmp_path: Path) -> None:
    """R9.10: because every secret emits an edge to each mapping-optional
    backend, the ``env-var`` and ``prompt`` backend rows now list under
    ``dependents_of`` the secrets that could resolve through them (they were
    referenced by nothing before the ``secret -> secret-backend`` edges)."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc("secret", "api-key", description="an API key"),
    )
    registry = build_registry(load_config(cfg, warn_issues=False))
    graph = registry.graph
    for backend in ("env-var", "prompt"):
        sources = {entry.source for entry in graph.dependents_of("secret-backend", backend)}
        assert ("secret", "api-key") in sources
        assert ("secret", "tailscale-auth-key") in sources


def test_onepassword_mapped_secret_gets_onepassword_edge(tmp_path: Path) -> None:
    """A secret with an explicit ``backend_mappings.onepassword`` gains the
    onepassword edge (its ``would_attempt`` is mapping-required), on top of
    the default env-var / prompt edges."""
    cfg = _write_cfg(
        tmp_path,
        "",
        ManifestDoc(
            "secret",
            "vault-key",
            {"backend_mappings": {"onepassword": "op://vault/item/field"}},
            description="a vaulted key",
        ),
    )
    registry = build_registry(load_config(cfg, warn_issues=False))
    targets = {(ref.kind, ref.name) for ref in registry.graph.edges_of("secret", "vault-key")}
    assert ("secret-backend", "onepassword") in targets
    assert ("secret-backend", "env-var") in targets


# -- the graph is frozen -----------------------------------------------------


def test_graph_is_frozen_and_registry_rejects_refinalize() -> None:
    """The retained graph is immutable, and the registry refuses a second
    finalize."""
    reg = _graph_from({"a": ["b"]})
    graph = reg.graph

    # The node map is a read-only mapping.
    with pytest.raises(TypeError):
        graph._nodes[("node", "a")] = graph._nodes[("node", "b")]  # type: ignore[index]

    # The dataclass itself is frozen.
    with pytest.raises(FrozenInstanceError):
        graph._nodes = {}  # type: ignore[misc]

    # Outbound/inbound are tuples (immutable); no append surface.
    assert isinstance(graph.edges_of("node", "a"), tuple)
    assert isinstance(graph.dependents_of("node", "b"), tuple)

    # Re-finalize is refused.
    with pytest.raises(RuntimeError, match="already been finalized"):
        reg.finalize()


def test_graph_property_before_finalize_raises() -> None:
    reg = Registry.empty()
    with pytest.raises(RuntimeError, match="only after finalize"):
        _ = reg.graph
