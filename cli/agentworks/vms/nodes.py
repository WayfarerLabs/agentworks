"""VM-domain node implementations (the orchestration-layer model).

Nodes are the runtime objects an orchestrator constructs and walks:
``Readiness`` plus graph identity (see ``orchestration/node.py``).
Domains implement their own nodes; this module holds the VM domain's,
each built by a factory that applies the reference-graph-to-node-graph
TRANSLATION RULE to real declared resources and DB rows, so a
command's graph is DERIVED, never hand-wired:

- a registry reference to a CAPABILITY with config at the reference
  site (the platform behind a ``vm-site``, the provider behind a
  ``git-credential``) means the referencing node CONSTRUCTS and HOLDS
  the instance: no node, no edge, and the holder's ``preflight`` /
  ``runup`` compose the instance's;
- ``secret``-kind references become ``secret_refs()`` entries (secrets
  are inputs the orchestrator resolves, never nodes);
- a live node's row fields become live edges: a VM row's ``site``
  field is an edge to the ``vm-site`` node;
- a PENDING node is constructed with its edges by the orchestrator,
  from the resolved templates and selections it planned with (names
  chosen up front, so identity is complete while still pending).

The held-instance composition here is the thin case: a one-line
per-kind fan-in (``git-credential`` and ``vm-site`` each hold exactly
one instance). Whether richer node kinds (an agent template over its
feature map) want a shared held-instances hook instead of per-kind
boilerplate is an explicit design decision deferred until they land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import VMStatus
from agentworks.errors import StateError

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.capabilities.base import RunContext, SecretReader
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.git_credentials.nodes import GitCredentialNode
    from agentworks.orchestration.node import Node
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.vms.templates import ResolvedVMTemplate


class VMSiteNode:
    """The ``vm-site`` consuming-resource node: holds the bound
    platform instance and composes its readiness. Built by
    :func:`vm_site_node`.
    """

    def __init__(
        self,
        name: str,
        platform: VMPlatform,
        secret_refs: tuple[ResourceReference, ...],
        registry: Registry,
    ) -> None:
        self._name = name
        self._platform = platform
        self._secret_refs = secret_refs
        self._registry = registry

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
        return tuple(ref.name for ref in self._secret_refs)

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        # The platform's API credential, as declared. The preflight
        # sweep predicts resolvability over these; the site itself does
        # not, because how a secret gets a value is the operation's
        # concern and not the site's.
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None:
        # The site checks that its own declarations are INTACT (the
        # names its config named reach real registry rows), which is
        # registry consistency and legitimately its concern; then the
        # held instance's own world checks.
        from agentworks.orchestration.secrets import require_declared_refs

        require_declared_refs(self.key, self._secret_refs, self._registry)
        self._platform.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        self._platform.runup(ctx)


class LiveVMNode:
    """A live VM, constructed from its DB row: a ``Node`` (its ``site``
    row field is its edge to the ``vm-site`` node) and the activation
    gate's ``GateTarget`` (the power-state ops the gate drives, exactly
    the semantics the now-retired imperative ``vms.manager.ensure_active``
    once carried, the migration's parity oracle). Built by
    :func:`live_vm_node`.

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
        self._repair_refs: tuple[str, ...] | None = None

    @property
    def key(self) -> str:
        return f"vm/{self._row.name}"

    @property
    def row(self) -> VMRow:
        """The backing DB row (a live node's data is its row)."""
        return self._row

    @property
    def site(self) -> VMSiteNode:
        """The vm-site node this VM's row points at; dependents reach
        the held platform instance through it (the site HOLDS the
        platform, the VM's edge points at the site)."""
        return self._site

    def deps(self) -> tuple[Node, ...]:
        return (self._site,)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...

    # -- GateTarget: the power-state surface the activation gate drives.
    # The gate's scoped reader is the ops' direct source: each op call
    # wraps ``gate_secrets`` in a RunContext, so the platform reads its
    # API token via ctx.secret (scoped delivery), never from held
    # state.

    def gate_secret_refs(self) -> tuple[str, ...]:
        # The observe/start credentials are the site's declared config
        # secrets (the platform API credential), already folded into
        # the site node's secret_refs by the translation rule.
        return self._site.secret_refs()

    def _gate_ops_ctx(self, gate_secrets: SecretReader) -> RunContext:
        """The op-start context for gate-driven power ops: the gate's
        scoped reader (eager gate values, lazy repair values, anything
        else refused) behind the standard ``ctx.secret`` surface."""
        from agentworks.capabilities.base import RunContext

        return RunContext(config=self._config, secrets=gate_secrets)

    def repair_secret_refs(self) -> tuple[str, ...]:
        # The rejoin auth key comes from the VM's template row field.
        # Resolved on FIRST call, not construction: the gate consults
        # this only when the repair path actually reads a name, which
        # keeps the healthy path free of template resolution, exactly
        # like the imperative repair path (_ensure_tailscale resolves
        # the template only after a failed reconnect). Memoized so the
        # gate reader's authorization check and the node's own read
        # share one template resolution.
        if self._repair_refs is None:
            from agentworks.vms.templates import resolve_live_template

            # One ref, always: the resolved template's auth-key name is
            # non-optional (the kind's default is "tailscale-auth-key"),
            # so there is no absent case to fold to an empty tuple.
            tmpl = resolve_live_template(self._db, self._registry, self._row.name, self._row.template)
            self._repair_refs = (tmpl.tailscale_auth_key,)
        return self._repair_refs

    def confirmed_active(self) -> bool:
        from agentworks.vms.manager import _is_tailscale_reachable

        row = self._row
        # A row already marked manually stopped skips the reachability
        # probe: pinging a stopped VM burns the probe's full timeout
        # just to reach the refusal (the backend answers directly).
        # Truthiness on the host, matching the oracle: an empty string
        # takes the slow path, never a probe of "".
        host = row.tailscale_host
        return bool(not row.operator_stopped and host and _is_tailscale_reachable(host))

    def observed_stopped(self, gate_secrets: SecretReader) -> bool:
        observed = self._site.platform.status(self._row, self._gate_ops_ctx(gate_secrets))
        self._observed = observed
        # RUNNING or UNKNOWN proceeds: a transient status failure must
        # not trigger a spurious start; the real op surfaces the error.
        # That degrade covers the BACKEND read only. A failure in the
        # credential layer beneath it does not reach here as UNKNOWN: a
        # platform with explicitly configured credentials (an azure site
        # on the service-principal arm) raises a typed error instead, and
        # deliberately so, since its identity layer cannot distinguish a
        # rejected credential from an unreachable one and reporting
        # UNKNOWN would hide a misconfiguration behind a plausible
        # answer. Same stance as that platform's fatal runup.
        return observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED)

    def auto_start(self, gate_secrets: SecretReader) -> None:
        from agentworks import output
        from agentworks.vms.manager import _ensure_tailscale, _tailscale_rejoin_required

        # Re-read the intent flag: the row this node was built from may
        # predate a concurrent `vm stop` / `vm start` in another
        # terminal, and auto-starting a VM the operator just stopped is
        # the one mistake the flag exists to prevent.
        current = self._db.get_vm(self._row.name)
        manually_stopped = current.operator_stopped if current else self._row.operator_stopped
        if manually_stopped:
            raise StateError(
                f"VM '{self._row.name}' was manually stopped so it will not be auto-started",
                entity_kind="vm",
                entity_name=self._row.name,
                hint=f"start it with: agw vm start {self._row.name}",
            )
        observed = self._observed.value if self._observed else "stopped"
        output.info(f"VM '{self._row.name}' is {observed}. Starting...")
        platform = self._site.platform
        platform.start(self._row, self._gate_ops_ctx(gate_secrets))

        with platform.vm_active(self._row, config=self._config):
            if _tailscale_rejoin_required(
                self._db,
                self._config,
                self._row,
                already_running=False,
            ):
                auth_key_name = self.repair_secret_refs()[0]
                _ensure_tailscale(
                    self._db,
                    self._config,
                    self._row,
                    platform,
                    # The unchanged gate reader remains the only auth source.
                    self._gate_ops_ctx(gate_secrets),
                    auth_keys=gate_secrets,
                    auth_key_name=auth_key_name,
                )

    def hold_active(self) -> AbstractContextManager[None]:
        return self._site.platform.vm_active(self._row, config=self._config)


class VMTemplateNode:
    """The resolved ``vm-template`` node: the template's graph identity
    and its declared Tailscale auth key. Built by
    :func:`vm_template_node`.

    Holds no registry. It used to, solely so its ``preflight`` could
    predict the auth key's resolvability over that key's declaration;
    with prediction moved to the operation's preflight sweep, the node
    declares the reference and nothing reads a registry through it.
    """

    def __init__(self, tmpl: ResolvedVMTemplate) -> None:
        self._tmpl = tmpl

    @property
    def key(self) -> str:
        return f"vm-template/{self._tmpl.name}"

    @property
    def tmpl(self) -> ResolvedVMTemplate:
        """The resolved template, for the orchestrator's domain ops
        (hardware values, the init recipe)."""
        return self._tmpl

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        # ONLY the Tailscale auth key: provisioning is hermetic. The
        # template's env-block secret references are runtime inputs,
        # resolved at their own use sites (shell / session composition
        # roots), never in a provisioning command's boundary pass.
        return (self._tmpl.tailscale_auth_key,)

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        """The Tailscale auth key, as the reference the vm-template kind
        itself publishes (:func:`~agentworks.vms.template
        .tailscale_secret_reference`, single-sourced with what
        ``dependencies`` emits at finalize).

        The preflight sweep predicts resolvability over this; the node
        does not, on the same rule the vm-site and git-credential nodes
        follow. The key is still the TEMPLATE's responsibility rather
        than the site's, and it still stays out of a reinit's boundary
        (that command's graph deliberately excludes this node); what
        moved is only who reports whether a declared name has an attemptable source.
        Conditionality is unchanged either way, because it was never
        expressed in the check: the node either participates in the
        command's graph or it does not.
        """
        from agentworks.vms.template import tailscale_secret_reference

        return (tailscale_secret_reference(self._tmpl.tailscale_auth_key, self._tmpl.name),)

    def preflight(self, ctx: RunContext) -> None:
        """No-op. The template's one readiness concern was its auth key's
        resolvability, which is now the preflight sweep's
        (:meth:`config_secret_refs` is what it predicts over).

        Deliberately no intactness check either, unlike the vm-site and
        git-credential nodes: those verify their declared names reach
        real registry rows, but the auth key rides
        ``secret_declarations``'s lookup-or-synthesize fallback on
        purpose, so a well-known name with no manifest reference still
        gets a synthesized declaration and a callable source chain.
        Requiring a row here would retire that fallback as a side effect.
        """

    def runup(self, ctx: RunContext) -> None: ...


class PendingVMNode:
    """The VM a create command will make: the first creatable node.

    Constructed up front with its name and its edges (the template,
    the chosen site, the admin template's git credentials), so its
    identity is complete while it is still pending; the orchestrator
    flips it through ``RealizationLog.mark_realized`` once its row
    exists, and ``teardown`` is today's rollback body (delete the row)
    relocated onto the node. The row is the only artifact this node
    unwinds: a provisioning failure means nothing usable was created
    remotely (or the remote was unreachable), today's stance, and
    initialization failures are deliberately NOT unwound (the VM
    exists and is debuggable; reinit retries).
    """

    def __init__(
        self,
        db: Database,
        name: str,
        template: VMTemplateNode,
        site: VMSiteNode,
        credentials: tuple[GitCredentialNode, ...],
    ) -> None:
        self._db = db
        self._name = name
        self._template = template
        self._site = site
        self._credentials = credentials
        self._realized = False

    @property
    def key(self) -> str:
        return f"vm/{self._name}"

    def deps(self) -> tuple[Node, ...]:
        return (self._template, self._site, *self._credentials)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def config_secret_refs(self) -> tuple[ResourceReference, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...

    @property
    def realized(self) -> bool:
        return self._realized

    def mark_realized(self) -> None:
        if self._realized:
            raise StateError(
                f"{self.key} was already marked realized; the pending-to-realized flip is one-way and once."
            )
        self._realized = True

    def teardown(self) -> None:
        try:
            self._db.delete_vm(self._name)
        except Exception as exc:
            # The teardown contract: a raised error names the artifact
            # left standing (the unwind warning surfaces it verbatim).
            raise StateError(
                f"the DB record for VM '{self._name}' could not be deleted and is left standing: {exc}",
                entity_kind="vm",
                entity_name=self._name,
            ) from exc


# -- Factories: the translation rule applied to real declared resources ----


def vm_site_node(registry: Registry, name: str) -> VMSiteNode:
    """Build the ``vm-site/<name>`` node from its DECLARED resource:
    the platform capability reference becomes the held bound instance
    (via ``resolve_site``, the not-ready-site chokepoint), and the
    config-implied ``secret`` references become the node's
    ``secret_refs``.
    """
    from agentworks.vms.sites import resolve_site

    platform = resolve_site(name, registry)
    # Read the site's config-implied secret edges off the retained graph (the
    # single access path, R11) rather than re-walking the decl's dependencies.
    # ``resolve_site`` already raises the stranded-site error on an unknown name.
    secret_refs = tuple(ref for ref in registry.graph.edges_of("vm-site", name) if ref.kind == "secret")
    return VMSiteNode(name, platform, secret_refs, registry)


def live_vm_node(
    db: Database,
    config: Config,
    registry: Registry,
    row: VMRow,
    *,
    site_nodes: dict[str, VMSiteNode] | None = None,
) -> LiveVMNode:
    """Build the ``vm/<name>`` node from its DB row. The row's ``site``
    field translates to the live edge (row fields become edges): the
    factory constructs the ``vm-site`` node the edge points at, so the
    caller wires nothing by hand.

    One-object-per-key: a command whose graph reaches the same site
    from several places must share ONE site-node object (the walk
    enforces this loudly). ``site_nodes`` is that sharing mechanism,
    the cross-node memo: a multi-VM command passes one dict across its
    ``live_vm_node`` calls and each site node is built on first
    encounter and reused after, which also shares the held platform
    instance per site (the by-site dedup the imperative batch bind
    performed). ``None`` builds a fresh site node, the single-VM
    composition's shape.
    """
    if site_nodes is None:
        site = vm_site_node(registry, row.site)
    else:
        memoized = site_nodes.get(row.site)
        if memoized is None:
            memoized = vm_site_node(registry, row.site)
            site_nodes[row.site] = memoized
        site = memoized
    return LiveVMNode(db, config, registry, row, site)


def vm_template_node(tmpl: ResolvedVMTemplate) -> VMTemplateNode:
    """Build the ``vm-template/<name>`` node from the RESOLVED template
    (inheritance already applied; the resolved object is the backing
    data, the way a row backs a live node)."""
    return VMTemplateNode(tmpl)


def pending_vm_node(
    db: Database,
    name: str,
    template: VMTemplateNode,
    site: VMSiteNode,
    credentials: tuple[GitCredentialNode, ...],
) -> PendingVMNode:
    """Build the pending ``vm/<name>`` node with its edges attached:
    the orchestrator hands in the nodes for the resources it planned
    with (the resolved template, the chosen site, the admin template's
    declared credentials), and every edge holder shares those same
    objects (one object per node)."""
    return PendingVMNode(db, name, template, site, credentials)
