"""The two readiness contracts: ``Readiness`` and ``Node``.

The lifecycle splits into two complementary protocols, and keeping them
distinct is what keeps the graph's identity clean:

- ``Readiness`` is the shared readiness verbs (``preflight`` before the
  secret-resolve boundary, ``runup`` after it) and nothing else.
  Capability instances satisfy it; they are NOT graph participants.
- ``Node`` is ``Readiness`` plus graph identity (``key``, declared
  ``deps``, declared ``secret_refs``). Only consuming resources and
  live resources are nodes; the orchestrator walks these and nothing
  else.

The verbs are shared deliberately: the readiness semantics are
identical. What differs between an instance and a node is
walked-vs-composed, a graph-participation difference that lives in the
TYPE (the presence of ``key`` / ``deps``), not in a renamed verb. A
capability instance is HELD by a node (the platform a ``vm-site``
holds, the provider a ``git-credential`` holds) and its readiness is
COMPOSED by that node's ``preflight`` / ``runup``, never invoked by
the orchestrator. Its declared secrets surface through the holding
node's ``secret_refs``.

Key convention: nodes key as ``<node-kind>/<name>``, plain, matching
the registry's ``(kind, name)``. Because only consuming and live
resources are nodes, every key is a natural, globally-unique name; no
owner-qualification exists anywhere (held instances have no key, so
they never need one).

    live/pending VM         vm/<name>                vm/box
    live/pending workspace  workspace/<name>         workspace/ws1
    live/pending agent      agent/<name>             agent/dev
    live/pending session    session/<name>           session/s1
    consuming resource      <kind>/<name>            git-credential/gh
    resolved template       <template-kind>/<name>   vm-template/default

Memoization, cycle reporting, and the unwind log all key off these
strings, so the one-object-per-key construction contract matters: an
orchestrator constructs each node once per command and shares the
object (``walk`` enforces this loudly).

Ops stay domain-specific and deliberately un-unified, on the instances
and the node kinds, never on these protocols.

The split is the model's fixed surface: helpers and node kinds may
evolve, but walked-vs-composed stays a TYPE distinction (the presence
of ``key`` / ``deps``), never a renamed verb.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext


@runtime_checkable
class Readiness(Protocol):
    """A thing that can be checked for readiness: ``preflight``
    (pre-resolve, dependency-blind, read-only) and ``runup``
    (post-resolve, authenticated, read-only, deferred to just before
    first use). Both nodes and the capability instances a node holds
    satisfy it. NOT a graph participant: no key, no deps, so it cannot
    be walked; a bare ``Readiness`` is always composed by its holder.

    Either stage may be a no-op. The semantics are the capability
    model's, unchanged (``capabilities/README.md``); what this protocol
    adds is only a shared shape for them.
    """

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


@runtime_checkable
class Node(Readiness, Protocol):
    """A readiness-bearing thing WITH graph identity: what the
    orchestrator walks. Only consuming resources and live resources
    are nodes.

    Nodes DECLARE, never walk: ``deps`` returns this node's own
    declared dependency edges (assembly is :func:`~agentworks
    .orchestration.walk.walk`'s job), and ``secret_refs`` the secret
    names this node's readiness and ops consume, including those of any
    held capability instances (secrets are inputs the orchestrator
    resolves, never nodes). A node never calls another node's lifecycle
    stages and never resolves a secret.
    """

    @property
    def key(self) -> str:
        """Graph identity, ``<node-kind>/<name>`` (see the module
        docstring's convention table)."""
        ...

    def deps(self) -> tuple[Node, ...]: ...

    def secret_refs(self) -> tuple[str, ...]: ...


@runtime_checkable
class CreatableNode(Node, Protocol):
    """A node kind a command can create: ``Node`` plus the node-scope
    half of unwind.

    Declared here so the creatable surface has one home; the first
    implementations (and the ``RealizationLog`` that sequences
    teardowns, plus the pending-to-realized bookkeeping) land with
    ``vm create``'s pending nodes. Teardown bodies are today's rollback
    code relocated onto the nodes; SEQUENCING them (reverse realization
    order, best-effort, never masking the original error) is the
    orchestrator's job, not the node's.
    """

    def teardown(self) -> None:
        """Delete what this node's realizing mutation made. Invoked
        only by the orchestrator's unwind, only after this node
        realized."""
        ...
