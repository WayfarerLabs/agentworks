"""VM-domain node implementations (the orchestration-layer model).

Nodes are the runtime objects an orchestrator constructs and walks:
``Readiness`` plus graph identity (see ``orchestration/node.py``).
Domains implement their own nodes; this module holds the VM domain's,
each built by a factory that applies the reference-graph-to-node-graph
TRANSLATION RULE (HLA "Deriving the graph") to real declared resources
and DB rows, so a command's graph is DERIVED, never hand-wired:

- a registry reference to a CAPABILITY with config at the reference
  site (the platform behind a ``vm-site``, the provider behind a
  ``git-credential``) means the referencing node CONSTRUCTS and HOLDS
  the instance: no node, no edge, and the holder's ``preflight`` /
  ``runup`` compose the instance's;
- ``secret``-kind references become ``secret_refs()`` entries (secrets
  are inputs the orchestrator resolves, never nodes);
- a live node's row fields become live edges: a VM row's ``site``
  field is an edge to the ``vm-site`` node.

The held-instance composition here is the thin case: a one-line
per-kind fan-in (``git-credential`` and ``vm-site`` each hold exactly
one instance). Whether richer node kinds (an agent template over its
feature map) want a shared held-instances hook instead of per-kind
boilerplate is an explicit LLD decision deferred until they land (HLA
open question).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import VMStatus
from agentworks.errors import NotFoundError, StateError

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.capabilities.base import RunContext, SecretReader
    from agentworks.capabilities.git_credential.base import GitCredentialProvider
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.orchestration.node import Node
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolver import Resolver


class GitCredentialNode:
    """The ``git-credential`` consuming-resource node: holds its
    provider instance, composes its readiness (the thin one-line
    fan-in), and folds the instance's declared secrets into its own
    ``secret_refs``. Built by :func:`git_credential_node`.
    """

    def __init__(
        self,
        name: str,
        provider: GitCredentialProvider,
        secret_refs: tuple[str, ...],
    ) -> None:
        self._name = name
        self._provider = provider
        self._secret_refs = secret_refs

    @property
    def key(self) -> str:
        return f"git-credential/{self._name}"

    @property
    def provider(self) -> GitCredentialProvider:
        """The held instance, for the orchestrator's domain ops
        (``helper_entry`` / ``credential_lines``). Ops stay
        un-unified; holding is not hiding."""
        return self._provider

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None:
        self._provider.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._provider.runup(ctx)


class VMSiteNode:
    """The ``vm-site`` consuming-resource node: holds the bound
    platform instance and composes its readiness. Built by
    :func:`vm_site_node`.
    """

    def __init__(
        self,
        name: str,
        platform: VMPlatform,
        secret_refs: tuple[str, ...],
    ) -> None:
        self._name = name
        self._platform = platform
        self._secret_refs = secret_refs

    @property
    def key(self) -> str:
        return f"vm-site/{self._name}"

    @property
    def platform(self) -> VMPlatform:
        """The held platform instance. The live VM node reaches its
        power-state ops through this (the site HOLDS the platform; the
        VM's edge points at the site)."""
        return self._platform

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None:
        self._platform.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._platform.runup(ctx)


class LiveVMNode:
    """A live VM, constructed from its DB row: a ``Node`` (its ``site``
    row field is its edge to the ``vm-site`` node) and the activation
    gate's ``GateTarget`` (the power-state ops the gate drives, exactly
    the semantics of the imperative ``vms.manager.ensure_active``, the
    parity oracle). Built by :func:`live_vm_node`.

    An already-existing VM has no pre- or post-resolve readiness of its
    own, so both stages are no-ops; its participation is its identity,
    its edge, and its gate surface.
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        registry: Registry,
        row: VMRow,
        site: VMSiteNode,
    ) -> None:
        self._db = db
        self._config = config
        self._registry = registry
        self._row = row
        self._site = site
        self._observed: VMStatus | None = None

    @property
    def key(self) -> str:
        return f"vm/{self._row.name}"

    def deps(self) -> tuple[Node, ...]:
        return (self._site,)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...

    # -- GateTarget: the power-state surface the activation gate drives.
    # Interim seam (documented in the plan, Phase 1): the platform's
    # power ops still read their API token through the instance's BOUND
    # resolver (proxmox's op-client bridge), so the orchestrator's
    # gate resolve callback must seed that resolver (Resolver.seed)
    # before these ops run; the gate_secrets reader becomes the ops'
    # direct source when the bridge dies (plan, Phase 5).

    def gate_secret_refs(self) -> tuple[str, ...]:
        # The observe/start credentials are the site's declared config
        # secrets (the platform API credential), already folded into
        # the site node's secret_refs by the translation rule.
        return self._site.secret_refs()

    def repair_secret_refs(self) -> tuple[str, ...]:
        # The rejoin auth key comes from the VM's template row field.
        # Resolved at CALL time, not construction: the gate asks only
        # when the VM actually needs a start, which keeps the healthy
        # path free of template resolution, exactly like the imperative
        # repair path (_ensure_tailscale resolves the template only
        # after a failed reconnect).
        from agentworks.vms.templates import resolve_template

        tmpl = resolve_template(self._registry, self._row.template)
        if tmpl.tailscale_auth_key is None:
            return ()
        return (tmpl.tailscale_auth_key,)

    def confirmed_active(self) -> bool:
        from agentworks.vms.manager import _is_tailscale_reachable

        row = self._row
        # A row already marked manually stopped skips the reachability
        # probe: pinging a stopped VM burns the probe's full timeout
        # just to reach the refusal (the backend answers directly).
        return (
            not row.operator_stopped
            and row.tailscale_host is not None
            and _is_tailscale_reachable(row.tailscale_host)
        )

    def observed_stopped(self, gate_secrets: SecretReader) -> bool:
        observed = self._site.platform.status(self._row)
        self._observed = observed
        # RUNNING or UNKNOWN proceeds: a transient status failure must
        # not trigger a spurious start; the real op surfaces the error.
        return observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED)

    def auto_start(self, gate_secrets: SecretReader) -> None:
        from agentworks import output
        from agentworks.vms.manager import _ensure_tailscale

        # Re-read the intent flag: the row this node was built from may
        # predate a concurrent `vm stop` / `vm start` in another
        # terminal, and auto-starting a VM the operator just stopped is
        # the one mistake the flag exists to prevent.
        current = self._db.get_vm(self._row.name)
        manually_stopped = (
            current.operator_stopped if current else self._row.operator_stopped
        )
        if manually_stopped:
            raise StateError(
                f"VM '{self._row.name}' was manually stopped so it will "
                f"not be auto-started",
                entity_kind="vm",
                entity_name=self._row.name,
                hint=f"start it with: agw vm start {self._row.name}",
            )
        observed = self._observed.value if self._observed else "stopped"
        output.info(f"VM '{self._row.name}' is {observed}. Starting...")
        platform = self._site.platform
        platform.start(self._row)
        # Hold while tailscaled reattaches: a freshly booted WSL2
        # distro must not idle out during the handshake wait. The
        # rejoin auth key, needed only when the node fails to
        # reconnect, reads LAZILY through the gate's reader; even its
        # NAME (a template lookup) is deferred to that first need.
        def rejoin_auth_key() -> str:
            refs = self.repair_secret_refs()
            if not refs:
                raise StateError(
                    f"VM '{self._row.name}' must rejoin tailscale but its "
                    f"template declares no auth key secret",
                    entity_kind="vm",
                    entity_name=self._row.name,
                )
            return gate_secrets.get(refs[0])

        with platform.vm_active(self._row, config=self._config):
            _ensure_tailscale(
                self._db,
                self._config,
                self._row,
                platform,
                auth_key_source=rejoin_auth_key,
            )

    def hold_active(self) -> AbstractContextManager[None]:
        return self._site.platform.vm_active(self._row, config=self._config)


# -- Factories: the translation rule applied to real declared resources ----


def git_credential_node(
    registry: Registry, name: str, resolver: Resolver | None
) -> GitCredentialNode:
    """Build the ``git-credential/<name>`` node from its DECLARED
    resource: the decl's provider reference becomes the held instance
    (constructed, not edged), and its ``secret``-kind references become
    the node's ``secret_refs``.
    """
    from agentworks.resources.access import git_credential
    from agentworks.vms.initializer import resolve_git_credential_providers

    decl = git_credential(registry, name)
    if decl is None:
        raise NotFoundError(
            f"git credential '{name}' not found in config",
            entity_kind="git-credential",
            entity_name=name,
        )
    provider = resolve_git_credential_providers(registry, [name], resolver)[name]
    secret_names = tuple(
        ref.name for ref in decl.referenced_resources() if ref.kind == "secret"
    )
    return GitCredentialNode(name, provider, secret_names)


def vm_site_node(
    registry: Registry, name: str, resolver: Resolver | None
) -> VMSiteNode:
    """Build the ``vm-site/<name>`` node from its DECLARED resource:
    the platform capability reference becomes the held bound instance
    (via ``resolve_site``, the disabled-site chokepoint), and the
    config-implied ``secret`` references become the node's
    ``secret_refs``.
    """
    from agentworks.vms.sites import lookup_site, resolve_site

    decl = lookup_site(name, registry)
    platform = resolve_site(name, registry, resolver=resolver)
    secret_names = tuple(
        ref.name for ref in decl.referenced_resources() if ref.kind == "secret"
    )
    return VMSiteNode(name, platform, secret_names)


def live_vm_node(
    db: Database,
    config: Config,
    registry: Registry,
    row: VMRow,
    resolver: Resolver | None,
) -> LiveVMNode:
    """Build the ``vm/<name>`` node from its DB row. The row's ``site``
    field translates to the live edge (row fields become edges): the
    factory constructs the ``vm-site`` node the edge points at, so the
    caller wires nothing by hand.

    One-object-per-key: a command whose graph reaches the same site
    from several places must construct through one factory pass and
    share the returned objects (the walk enforces this loudly); a
    cross-node memo emerges when the first multi-consumer command
    migrates.
    """
    return LiveVMNode(
        db, config, registry, row, vm_site_node(registry, row.site, resolver)
    )
