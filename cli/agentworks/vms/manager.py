"""VM lifecycle management: create, list, start, stop, delete."""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, NamedTuple

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.config import validate_admin_username, validate_name
from agentworks.db import (
    SYSTEM_SLUG_KEY,
    InitStatus,
    ProvisioningStatus,
    VMStatus,
)
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    ProvisioningError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms.initializer import (
    announce_git_credentials,
    initialize_vm,
    rejoin_tailscale,
    run_initialization,
    verify_tailscale_available,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from agentworks.capabilities.base import OperationScope
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import LiveVMNode


class _VmAdminEnvScopes(NamedTuple):
    """Per-scope env dicts for vm-level commands (shell, exec).

    The ``workspace`` field is ``None`` for vm-level commands without a
    workspace pin (``vm shell`` / ``vm exec`` without ``--workspace``).
    When set, workspace-template env enters the scope precedence ladder
    between vm and admin.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None
    admin: dict[str, EnvEntry]


def _resolve_vm_admin_env_scopes(
    registry: Registry,
    vm: VMRow,
    *,
    ws: WorkspaceRow | None = None,
) -> _VmAdminEnvScopes:
    """Resolve per-scope env dicts for vm-level commands.

    When ``vm`` is provided (reinit / shell / exec), the vm-scope env
    comes from the VM's actual template (the ``vm.template`` DB row),
    NOT the config-time default, which may not match.

    When ``vm`` is None, the default template resolved from the registry
    is used.

    When ``ws`` is supplied (``vm shell --workspace`` / ``vm exec
    --workspace``), the workspace template's env enters the chain.
    """
    from agentworks.vms.templates import resolve_template as _resolve_vm_template

    vm_env = _resolve_vm_template(registry, vm.template).env

    ws_env: dict[str, EnvEntry] | None = None
    if ws is not None:
        from agentworks.workspaces.templates import resolve_template as _resolve_ws_template
        ws_env = _resolve_ws_template(registry, ws.template).env

    from agentworks.resources.access import admin_template

    return _VmAdminEnvScopes(vm=vm_env, workspace=ws_env, admin=admin_template(registry).env)


def _vm_secret_target(
    scopes: _VmAdminEnvScopes, *, label: str,
) -> SecretTarget:
    """Build the SecretTarget for VM-level commands from pre-resolved scopes.

    Callers resolve scopes via ``_resolve_vm_admin_env_scopes`` once and
    feed the result to BOTH this builder (for eager-resolve) and
    ``compose_env`` (for render) so the two consumers can't drift.
    """
    from agentworks.secrets import SecretTarget

    return SecretTarget(
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
        label=label,
    )


def _resolve_workspace_for_vm(
    db: Database, vm: VMRow, workspace_name: str | None,
) -> WorkspaceRow | None:
    """Resolve a ``--workspace`` flag against a target VM.

    Returns ``None`` when ``workspace_name`` is ``None``. Otherwise loads
    the workspace and validates that it belongs to ``vm``; cross-VM
    mismatch raises ``ValidationError`` upfront so the caller fails
    before any SSH work. Shared by ``shell_vm`` and ``exec_vm``; the
    agent variants do their own (authz-bearing) resolution in
    ``agents.manager``.
    """
    if workspace_name is None:
        return None
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if ws.vm_name != vm.name:
        raise ValidationError(
            f"workspace '{workspace_name}' belongs to VM '{ws.vm_name}', not '{vm.name}'",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    return ws


# -- System slug --------------------------------------------------------

_SLUG_PROMPT = (
    "A system slug uniquely identifies this agentworks installation. It "
    "is used to namespace VMs and other resources so this install does "
    "not collide with others that share the same cloud account, Proxmox "
    "cluster, or Windows/Mac user. Leave blank if this install is the "
    "only one using its sites' backends. [system slug]"
)


def validate_slug(slug: str) -> None:
    """Slug format: 3-20 chars, lowercase alphanumeric plus dash, no
    leading/trailing dash. Passes Azure's naming rules (the strictest
    we target), therefore passes all of them.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,18}[a-z0-9]", slug):
        raise ValidationError(
            f"invalid system slug '{slug}'. Slugs are 3-20 characters, "
            "lowercase alphanumeric plus dash, with no leading or "
            "trailing dash."
        )


def _resolve_system_slug(db: Database) -> str | None:
    """The install's slug, prompting once at first interactive
    ``vm create``.

    The settings row distinguishes never-asked (absent) from declined
    (present, empty): a BLANK answer is a perfectly valid one ("no
    slug") and records the declined row, so the prompt fires once
    regardless of the answer and never again: no nudges, no
    reminders (an earlier shared-backend nudge that re-asked decliners
    was removed by maintainer ruling: the blank answer is final).
    Non-interactive runs never prompt and never write, so a later
    interactive create still asks.
    """
    stored = db.get_setting(SYSTEM_SLUG_KEY)
    if stored is not None:
        return stored or None
    if not output.is_interactive():
        return None
    answer = output.prompt(_SLUG_PROMPT, default="").strip()
    if not answer:
        db.set_setting(SYSTEM_SLUG_KEY, "")
        return None
    # Invalid input aborts the create before any state mutation; the
    # settings row stays absent, so the next create asks again.
    validate_slug(answer)
    db.set_setting(SYSTEM_SLUG_KEY, answer)
    return answer


def ensure_active(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
) -> None:
    """Respect a manual stop; otherwise start on demand.

    With :func:`keep_active` this is the imperative activation-gate
    pair. Every command root has migrated onto the orchestrated gate
    (``orchestration.activation``); :func:`keep_active` is the pair's
    only remaining caller (see its docstring for the recorded interim
    holds), and both retire with those holds.

    Fast path: a Tailscale reachability probe (cheap, no cloud API)
    short-circuits the common case, keeping backend round trips off the
    per-op hot path, EXCEPT when the row already says manually
    stopped: pinging a stopped VM burns the probe's full timeout just
    to reach the refusal, so the likely-stopped case asks the backend
    directly (an out-of-band start still proceeds via the observed
    RUNNING). ``platform`` is the BOUND platform from the caller's
    already-run composition root, and ``ctx`` that composition's
    op-start context (the platform's power ops read any op secret via
    ``ctx.secret``, scoped delivery, the same as everywhere else).
    """
    if (
        not vm.operator_stopped
        and vm.tailscale_host
        and _is_tailscale_reachable(vm.tailscale_host)
    ):
        return
    observed = platform.status(vm, ctx)
    if observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        # Re-read the intent flag: the caller-loaded row may predate a
        # concurrent `vm stop`/`vm start` in another terminal, and
        # auto-restarting a VM the operator just stopped is the one
        # mistake this flag exists to prevent. The slow path already
        # paid a backend status() round trip; one DB read is cheap
        # next to it.
        current = db.get_vm(vm.name)
        manually_stopped = (
            current.operator_stopped if current else vm.operator_stopped
        )
        if manually_stopped:
            raise StateError(
                f"VM '{vm.name}' was manually stopped so it will not be "
                f"auto-started",
                entity_kind="vm",
                entity_name=vm.name,
                hint=f"start it with: agw vm start {vm.name}",
            )
        output.info(f"VM '{vm.name}' is {observed.value}. Starting...")
        platform.start(vm, ctx)
        # Hold while tailscaled reattaches: a freshly booted WSL2
        # distro must not idle out during the handshake wait.
        with platform.vm_active(vm, config=config):
            _ensure_tailscale(db, config, vm, platform)
    # RUNNING or UNKNOWN: proceed. A transient status failure must not
    # trigger a spurious start; the op will surface the real error.


@contextlib.contextmanager
def keep_active(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
) -> Iterator[None]:
    """Gate (:func:`ensure_active`), then hold (``vm_active``) for the
    context's duration.

    Takes the BOUND platform from the caller's already-run composition
    root (binding may need resolved config secrets, which only that
    root's single resolve pass has) plus that composition's op-start
    ``ctx`` for the gate's power ops. WSL2's ``vm_active`` spawns a
    keepalive subprocess anchoring the distro against
    ``vmIdleTimeout``; the other platforms' default hold is a no-op.

    The recorded INTERIM callers, each a handed-in-platform hold
    inside an orchestrated caller's composition (rebuilding a boundary
    there would re-resolve mid-command), and the reason this pair
    outlives the resolver retirement's caller drain:

    - the nested-teardown paths (``agents.manager.delete_agent`` /
      ``workspaces.manager.delete_workspace`` with ``platform=`` from
      a pending node's rollback), closing when the session-create
      unwind hands a node instead of a platform;
    - ``vms.initializer.initialize_vm``'s whole-init hold (the
      platform handed in from ``create_vm``'s composition root; the
      initializer internals are still imperative and hold no node).
    """
    ensure_active(db, config, vm, platform, ctx)
    with platform.vm_active(vm, config=config):
        yield


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str,
    template: str | None = None,
    site: str | None = None,
) -> None:
    """Create a new VM: provision + initialize.

    Hardware and the admin username are template-owned: the vm-template
    supplies cpus/memory/disk/swap and the admin-template the username.
    There are no per-create overrides; deviations are new templates.
    """

    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    # build_registry runs first so framework miss-policies (typo'd git
    # credential, future TemplateReference typos on inherits, etc.)
    # surface before any template / DB / VM business logic.
    registry = build_registry(config)

    vm_tmpl = resolve_template(registry, template)

    # Resolve the target site and its declaration. An undeclared site
    # fails here with the stranded-site ConfigError + manifest hint,
    # and a DISABLED one with its reason chain, both before any DB or
    # backend work, and critically before the Tailscale check and the
    # interactive system-slug prompt below: the operator must never
    # answer a prompt for an op the site already sank.
    from agentworks.vms.sites import ensure_site_enabled, lookup_site, select_site

    site = select_site(site, config.defaults.site, registry)
    site_decl = lookup_site(site, registry)
    ensure_site_enabled(site_decl)

    vm_name = name
    validate_name(vm_name)

    if db.get_vm(vm_name) is not None:
        raise AlreadyExistsError(
            f"VM '{vm_name}' already exists",
            entity_kind="vm",
            entity_name=vm_name,
        )

    # Resource settings are template-owned (no per-create overrides): the
    # vm-template carries hardware, the admin-template the username.
    resolved_cpus = vm_tmpl.cpus
    resolved_memory = vm_tmpl.memory
    resolved_disk = vm_tmpl.disk
    from agentworks.resources.access import admin_template

    admin = admin_template(registry)
    resolved_admin_username = admin.username
    validate_admin_username(resolved_admin_username)

    verify_tailscale_available()
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.unwind import RealizationLog
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import (
        pending_vm_node,
        vm_site_node,
        vm_template_node,
    )

    resolver = Resolver(config, registry)

    # BUILD: the command names its direct resources (the resolved
    # template, the chosen site, the admin template's declared
    # credentials) and constructs the PENDING VM node up front with
    # those edges attached; the walk assembles the graph. Provider
    # and platform construction is cheap and touches no secret
    # machinery; the walk union below is the boundary's source.
    # Nothing resolves yet.
    cred_nodes = tuple(
        git_credential_node(registry, cred_name)
        for cred_name in admin.git_credentials
    )
    providers = {node.provider.owner_name: node.provider for node in cred_nodes}

    # System slug: first interactive create prompts once (a blank
    # answer is final; see _resolve_system_slug). Runs before any
    # secret prompting or state mutation so an aborted slug entry
    # leaves nothing behind.
    slug = _resolve_system_slug(db)

    template_node = vm_template_node(vm_tmpl, registry)
    site_node = vm_site_node(registry, site)
    pending_vm = pending_vm_node(db, vm_name, template_node, site_node, cred_nodes)
    nodes = walk(pending_vm)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = OperationScope(level=ScopeLevel.VM, system_slug=slug, vm=vm_name)

    def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=scope,
            secrets=ScopedSecrets(resolver.values, secret_names),
        )

    # PREFLIGHT-ALL, then the one boundary resolve: tailscale auth,
    # git-credential tokens, and the site's config secrets (proxmox's
    # API token) in a single prompt session. Provisioning is hermetic:
    # operator [admin.env] / [vm_templates.*.env] secrets are NOT
    # prompted here (they are runtime inputs, resolved at the shells'
    # own composition roots), which is why the template node's
    # secret_refs carry only the Tailscale key.
    output.phase("Preflight")
    output.detail(f"Checking vm-site/{site}...")
    output.detail(f"Checking vm-template/{vm_tmpl.name}...")
    announce_git_credentials(providers)
    preflight_all(nodes, RunContext(config=config, operation_scope=scope))

    output.phase("Resolving Secrets")
    resolver.resolve()

    output.phase("Provisioning")
    # Provisioning-phase runup: authenticate the platform's own
    # credential (proxmox API token) before create() mutates anything. A
    # definitive rejection aborts here, before the DB row or any backend
    # resource exists (the FATAL policy: nothing realized, nothing to
    # unwind). Runup is deferred and announced inline (no phase of its
    # own); lima/wsl2/azure have no token, so this is a silent no-op for
    # them. The credentials' write-step runup stays deferred into
    # initialization, under the skip-and-degrade policy.
    site_node.runup(scoped_ctx(site_node.secret_refs()))
    tailscale_auth_key = scoped_ctx(template_node.secret_refs()).secret(
        vm_tmpl.tailscale_auth_key
    )
    # Each credential's token, read through its node's SCOPED delivery.
    git_tokens = {
        node.provider.owner_name: scoped_ctx(node.secret_refs()).secret(
            node.provider.secret_name
        )
        for node in cred_nodes
    }

    # The VM's OS hostname, computed once at create time and recorded on the
    # row: {slug}-{name} with a slug, the bare name without. Bounded by
    # construction: slug max 20 + dash + name max 30 = 51 characters,
    # inside the 63-char hostname-label and Azure 64-char limits.
    hostname = f"{slug}-{vm_name}" if slug else vm_name

    # Create DB record with as-provisioned resource values. This is the
    # pending VM's realization artifact (what teardown deletes), so the
    # log records it the moment the row exists: a provisioning failure
    # below unwinds exactly the row (today's rollback, relocated onto
    # the node), and nothing past provisioning is rollback-tracked (an
    # initialized-but-partial VM is kept, debuggable, reinit-able).
    db.insert_vm(
        vm_name,
        site=site,
        hostname=hostname,
        template=vm_tmpl.name,
        cpus=resolved_cpus,
        memory_gib=resolved_memory,
        disk_gib=resolved_disk,
        swap_gib=vm_tmpl.swap,
        admin_username=resolved_admin_username,
    )
    log = RealizationLog()
    log.mark_realized(pending_vm)

    # The platform instance was bound (and preflighted, and its secrets
    # resolved) at the composition root above; dispatch is just ops now.
    platform_obj = site_node.platform
    from agentworks.capabilities.vm_platform import ProvisionRequest

    request = ProvisionRequest(
        vm_name=vm_name,
        hostname=hostname,
        system_slug=slug,
        admin_username=resolved_admin_username,
        ssh_public_key=config.operator.ssh_public_key.read_text().strip(),
        ssh_private_key=config.operator.ssh_private_key,
        tailscale_auth_key=tailscale_auth_key,
        cpus=resolved_cpus,
        memory_gib=resolved_memory,
        disk_gib=resolved_disk,
        swap_gib=vm_tmpl.swap,
    )

    # The op-start context for the platform's ops (create here; the
    # initializer's keep_active hold below): secrets scoped to the
    # site's declared names.
    platform_ctx = scoped_ctx(site_node.secret_refs())

    output.detail(f"Creating VM '{vm_name}' on vm-site '{site}'...")
    try:
        result = platform_obj.create(request, platform_ctx)
    except KeyboardInterrupt:
        output.warn(f"Cancelling vm create '{vm_name}'... rolling back.")
        log.unwind()
        raise
    except UserAbort:
        # No prompt lives in this span today (the boundary resolve ran
        # at the composition root above), but an operator abort must
        # never downgrade to a ProvisioningError; roll back like the
        # KeyboardInterrupt twin above.
        log.unwind()
        raise
    except Exception as e:
        log.unwind()
        raise ProvisioningError(
            f"provisioning failed: {e}",
            entity_kind="vm",
            entity_name=vm_name,
        ) from e
    # The unwind window closes here: provisioning succeeded, the VM
    # exists, and initialization failures keep it (with recovery
    # guidance), exactly as before.

    # Persist the platform's opaque identifiers verbatim; the owning
    # platform is the column's only reader.
    db.update_vm_platform_metadata(vm_name, result.platform_metadata)

    # -- Initialization --
    # If this fails, the VM exists on the remote host and may be debuggable.
    # Keep the DB record so the user can reinit or delete.
    # Polymorphic post-Tailscale-ready hook. Azure overrides to detach
    # the cloud-init public IP (closing the public-exposure window the
    # instant Tailscale becomes reachable); other platforms are no-op.
    def _on_tailscale_ready() -> None:
        refreshed = db.get_vm(vm_name)
        assert refreshed is not None
        platform_obj.post_tailscale_ready(refreshed)

    try:
        initialize_vm(
            db,
            config,
            registry,
            vm_tmpl,
            admin,
            vm_name,
            exec_target=result.native_transport,
            providers=providers,
            platform=platform_obj,
            platform_ctx=platform_ctx,
            admin_username=resolved_admin_username,
            tailscale_auth_key=tailscale_auth_key,
            git_tokens=git_tokens,
            bootstrap_complete=result.bootstrap_complete,
            tailscale_ip=result.tailscale_ip,
            on_tailscale_ready=_on_tailscale_ready,
        )
    except KeyboardInterrupt:
        output.warn(
            f"Cancelling vm create '{vm_name}' during initialization. "
            f"The VM exists but is partially initialized. "
            f"Use 'vm reinit {vm_name}' to retry, or 'vm delete {vm_name} --force' to remove it."
        )
        raise
    except UserAbort:
        # No prompt lives in this span today (the boundary resolve ran
        # at the composition root above), but the catch-all below must
        # never downgrade an operator abort to a Provisioning/External
        # error (same discipline as delete_vm's best-effort spans).
        # Same recovery guidance as the KeyboardInterrupt twin: the VM
        # exists and must not be stranded silently.
        output.warn(
            f"Cancelling vm create '{vm_name}' during initialization. "
            f"The VM exists but is partially initialized. "
            f"Use 'vm reinit {vm_name}' to retry, or 'vm delete {vm_name} --force' to remove it."
        )
        raise
    except Exception as e:
        from agentworks.ssh import LOG_DIR

        log_hint = ""
        logs = sorted(LOG_DIR.glob(f"{vm_name}-*-vm-create.log"), reverse=True)
        if logs:
            log_hint = f"\nDetails: {logs[0]}"

        vm = db.get_vm(vm_name)
        if vm is not None and vm.provisioning_status == ProvisioningStatus.FAILED.value:
            raise ProvisioningError(
                f"provisioning failed: {e}{log_hint}",
                entity_kind="vm",
                entity_name=vm_name,
                hint=f"VM '{vm_name}' is in a failed state. Use 'vm delete {vm_name}' to clean up.",
            ) from e
        else:
            raise ExternalError(
                f"initialization failed: {e}{log_hint}",
                entity_kind="vm",
                entity_name=vm_name,
                hint=f"VM '{vm_name}' may still be usable. Use 'vm reinit {vm_name}' to retry.",
            ) from e

    # -- Post-init: SSH config --
    try:
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)
    except Exception as e:
        output.warn(f"SSH config sync failed: {e}")
        output.detail("VM is likely still usable.")

    # Final status is set by initialize_vm (COMPLETE or PARTIAL)
    vm = db.get_vm(vm_name)
    assert vm is not None
    if vm.init_status == InitStatus.PARTIAL.value:
        output.info(f"VM '{vm_name}' is ready (with warnings, see above)")
    else:
        output.info(f"VM '{vm_name}' is ready!")


def list_vms(db: Database, *, names_only: bool = False) -> None:
    """List all VMs with their init and runtime status.

    With ``names_only=True``, emit one VM name per line and skip the
    table render. Used by shell completion (see issue #147).
    """
    vms = db.list_vms()

    if names_only:
        # Names-only short-circuits BEFORE the empty check so an
        # empty db prints nothing (not the friendly "No VMs"
        # message), keeping the completion candidate set clean.
        for vm in vms:
            output.info(vm.name)
        return

    if not vms:
        output.info("No VMs registered.")
        return

    header = (
        f"{'NAME':<20} {'SITE':<12} {'TEMPLATE':<12} {'PROV':<12} {'INIT':<12} "
        f"{'WS/AG/SE':<10} {'TAILSCALE':<20} {'CREATED'}"
    )
    output.info(header)
    output.info("-" * len(header))
    for vm in vms:
        ws = db.count_workspaces_on_vm(vm.name)
        ag = db.count_agents_on_vm(vm.name)
        se = db.count_sessions_on_vm(vm.name)
        counts = f"{ws}/{ag}/{se}"
        output.info(
            f"{vm.name:<20} {vm.site:<12} {vm.template or '-':<12} "
            f"{vm.provisioning_status:<12} {vm.init_status:<12} "
            f"{counts:<10} {vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def describe_vm(db: Database, config: Config, name: str) -> None:
    """Show detailed information about a VM.

    Orchestrated, composition only (:func:`_live_vm_boundary`): the
    graph derives from the VM's row and the backend reads drive
    through the node's held platform. NO activation gate ever opens:
    describe reads state (one status probe is its op) and inspecting
    a stopped VM must render "(manual)" / "(idle)", never start it.
    """
    vm = _require_vm(db, name)

    # Compose through the site so the platform (the site's capability)
    # and the backend-side identity render polymorphically. Describe is
    # an inspection command and a stranded row is exactly the one an
    # operator wants to look at, so a stranded site degrades to a
    # warning (with the manifest hint) rather than erroring: the row's
    # own fields still render.
    from agentworks.bootstrap import build_registry
    from agentworks.vms.sites import lookup_site

    registry = build_registry(config)
    site_platform = "-"
    backend_label = "-"
    status_label = "-"
    observed: VMStatus | None = None
    try:
        site_decl = lookup_site(vm.site, registry)
        # Known as soon as the declaration resolves: keep it alive even
        # if the boundary below degrades.
        site_platform = site_decl.platform
        vm_node, ops_ctx = _live_vm_boundary(db, config, vm, registry=registry)
        platform = vm_node.site.platform
    except UserAbort:
        # Ctrl-C at the boundary's secret prompt aborts describe too;
        # a half-report would read as the command having succeeded.
        raise
    except AgentworksError as e:
        # Inspection degrades on ANY typed build/preflight/resolve
        # failure (a stranded site's ConfigError, a missing tool's
        # ConnectivityError, an unresolvable secret): describe is the
        # command an operator reaches for on exactly such a row, so the
        # row's own fields must still render.
        output.warn(f"{e}" + (f"\n{e.hint}" if e.hint else ""))
    else:
        # The backend reads degrade under the same discipline as the
        # boundary above: a live backend flake (API hiccup, SSH timeout)
        # must not crash the report: a flaky backend is exactly when
        # an operator reaches for describe, and the row's static fields
        # still render with '-' placeholders.
        try:
            backend_label = platform.display_backend_name(vm)
            # Live observed status, paired with operator intent: a
            # manual stop reads differently from an idle timeout.
            observed = platform.status(vm, ops_ctx)
            status_label = observed.value
            if observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
                status_label += " (manual)" if vm.operator_stopped else " (idle)"
        except UserAbort:
            raise
        except AgentworksError as e:
            output.warn(f"{e}" + (f"\n{e.hint}" if e.hint else ""))

    # VM details
    output.info(f"Name:           {vm.name}")
    output.info(f"Created:        {vm.created_at}")
    output.info(f"Site:           {vm.site}")
    output.info(f"Platform:       {site_platform}")
    output.info(f"Backend:        {backend_label}")
    output.info(f"Status:         {status_label}")
    output.info(f"Hostname:       {vm.hostname}")
    # The slug never shows in normal CLI output (vm list stays
    # name-only); describe and doctor are its surfaces. The slug is
    # install-level, so a VM created before it was set gets a marker:
    # its hostname and backend names carry no prefix. A blank answer is
    # a VALID one: declined ("(none)") renders distinctly from
    # never-asked ("-").
    stored_slug = db.get_setting(SYSTEM_SLUG_KEY)
    slug = stored_slug or None
    slug_label = slug or ("(none)" if stored_slug == "" else "-")
    # Exact hostname comparison (the slug is immutable and the hostname is
    # recorded as {slug}-{name}); a prefix test could false-negative on
    # a pre-slug VM whose name happens to start with the slug.
    if slug and vm.hostname != f"{slug}-{vm.name}":
        slug_label += " (not applied to this VM)"
    output.info(f"System Slug:    {slug_label}")
    output.info(f"Template:       {vm.template or '-'}")
    output.info(f"Admin User:     {vm.admin_username}")
    output.info(f"Provisioning:   {vm.provisioning_status}")
    output.info(f"Initialization: {vm.init_status}")
    output.info(f"Tailscale IP:   {vm.tailscale_host or '-'}")

    # Resources table: Initial / Current / Used (Used%). The live read
    # SSHes to the VM, so skip it when the status probe above OBSERVED
    # the VM stopped: connecting to a dead host would burn the
    # transport's connect timeout (times its retries) just to print the
    # '-' placeholders. A degraded/UNKNOWN status still tries: the VM
    # may well be up, and the read has its own error handling.
    live = None
    if vm.tailscale_host is not None and observed not in (
        VMStatus.STOPPED,
        VMStatus.DEALLOCATED,
    ):
        live = _query_live_resources(vm, config)

    if vm.cpus is not None or live is not None:
        output.info(f"\n{'Resources':<16}{'Provisioned':<14}{'Current':<14}{'Used'}")
        output.detail(
            f"{'CPU':<16}"
            f"{str(vm.cpus) if vm.cpus else '-':<14}"
            f"{live['cpus'] if live else '-':<14}"
            f"{'load ' + live['load_avg'] if live else '-'}"
        )
        output.detail(
            f"{'Memory':<16}"
            f"{str(vm.memory_gib) + 'G' if vm.memory_gib else '-':<14}"
            f"{live['mem_total'] if live else '-':<14}"
            f"{live['mem_used'] + ' (' + live['mem_pct'] + ')' if live else '-'}"
        )
        output.detail(
            f"{'Swap':<16}"
            f"{str(vm.swap_gib) + 'G' if vm.swap_gib else '-':<14}"
            f"{live['swap_total'] if live else '-':<14}"
            f"{live['swap_used'] + ' (' + live['swap_pct'] + ')' if live else '-'}"
        )
        output.detail(
            f"{'Disk':<16}"
            f"{str(vm.disk_gib) + 'G' if vm.disk_gib else '-':<14}"
            f"{live['disk_total'] if live else '-':<14}"
            f"{live['disk_used'] + ' (' + live['disk_pct'] + ')' if live else '-'}"
        )

    if vm.last_seen_at:
        output.info(f"Last Seen:      {vm.last_seen_at}")

    # Agents on this VM
    agents = db.list_agents(vm_name=name)
    output.info(f"\nAgents ({len(agents)}):")
    if agents:
        for agent in agents:
            grant_count = db.count_agent_grants(agent.name)
            grant_label = "all" if agent.grant_all else str(grant_count)
            output.detail(f"{agent.name}  (user: {agent.linux_user}, grants: {grant_label})")
    else:
        output.detail("(none)")

    # Workspaces with sessions
    workspaces = db.list_workspaces(vm_name=name)
    output.info(f"\nWorkspaces ({len(workspaces)}):")
    if workspaces:
        for ws in workspaces:
            output.detail(f"{ws.name}  ({ws.workspace_path})")

            sessions = db.list_sessions(workspace_name=ws.name)
            if sessions:
                output.detail(f"Sessions ({len(sessions)}):", indent=2)
                for s in sessions:
                    mode_label = f"agent:{s.agent_name}" if s.agent_name else "admin"
                    output.detail(f"{s.name}  [{s.template}]  {mode_label}", indent=3)
            else:
                output.detail("(no sessions)", indent=2)
    else:
        output.detail("(none)")

    # Events
    events = db.list_vm_events(name)
    output.info(f"\nEvents ({len(events)}):")
    if events:
        for event in events:
            evt_detail = f"  {event.detail}" if event.detail else ""
            output.detail(f"{event.created_at}  {event.event}{evt_detail}")
    else:
        output.detail("(none)")


def shell_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    platform_transport: bool = False,
    workspace_name: str | None = None,
) -> int:
    """Open a shell on a VM as the admin user.

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`exec_vm`.

    By default uses the Tailscale SSH transport. Pass
    ``platform_transport=True`` (the ``vm shell --platform`` flag) to
    use the platform-native transport instead (``limactl shell`` for
    Lima, ``wsl.exe`` for WSL2, SSH via public IP for Azure). That is
    the right choice when Tailscale connectivity is the thing you need
    to fix (e.g. healing the issue #117 latched DNS state, which
    involves restarting tailscaled itself).

    When ``workspace_name`` is set, the shell ``cd``s into the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`): the graph derives from
    the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the whole interactive session.
    """
    import shlex

    from agentworks.env import ResourceContext, compose_env
    from agentworks.transports import native_transport, transport

    vm = _require_vm(db, name)
    # Init failure warns instead of blocks: shelling into a partially-
    # initialized VM is exactly the kind of operation that lets the
    # operator diagnose what failed or apply a manual fix (e.g. healing
    # the issue #117 latched DNS state) before re-running reinit. Same
    # rationale applies to `vm exec` (see exec_vm below).
    _guard_failed_vm(vm, allow_failed_init=True)

    # Resolve workspace before the transport-state guard: a cross-VM
    # mismatch is more diagnostic than "no Tailscale", so it should
    # surface first. The scope chain also needs the workspace before
    # secret resolution.
    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    if not platform_transport and vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint=(
                "VM init may not be complete; check 'vm describe' for status. "
                "If Tailscale itself is the problem you're trying to reach the "
                "VM to fix, run with --platform to use the platform-native "
                "transport instead."
            ),
        )

    # The orchestrated composition root (gated_vm_boundary): the admin
    # shell's env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), and every node's
    # preflight (missing tool, stranded site, unresolvable secret)
    # fails before any prompt. The same scope dicts feed both the
    # SecretTarget (via _vm_secret_target) and compose_env so the two
    # consumers can't drift. Crucially the vm scope comes from
    # vm.template (DB row), not the config-default template, which may
    # not match and would silently route the wrong env into a shell on
    # a non-default-template VM.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_vm_admin_env_scopes(registry, vm, ws=ws)

    with contextlib.ExitStack() as stack:
        vm_node, resolver = stack.enter_context(
            gated_vm_boundary(
                db, config, registry, vm,
                targets=[_vm_secret_target(scopes, label=f"vm-shell={vm.name}")],
            )
        )

        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=vm.admin_username,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            admin=scopes.admin,
        )

        target = (
            native_transport(vm, vm_node.site.platform, config, stack=stack)
            if platform_transport
            else transport(vm, config)
        )
        if ws is not None:
            cmd = f"cd {shlex.quote(ws.workspace_path)} && exec $SHELL -l"
            return target.interactive(cmd, env=env)
        return target.interactive("", env=env)


def exec_vm(
    db: Database,
    config: Config,
    name: str,
    command: list[str],
    *,
    workspace_name: str | None = None,
) -> int:
    """Execute a command on a VM as the admin user via direct admin SSH.

    Uses inherited stdio for streaming output without buffering.
    Returns the remote exit code.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to this VM.

    Orchestrated (:func:`gated_vm_boundary`), mirroring
    :func:`shell_vm`: the gate opens before the preflight sweep and
    the held-active span covers the streamed remote command.
    """
    import shlex

    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import reject_dash_prefixed_command
    from agentworks.transports import transport

    reject_dash_prefixed_command(command, kind="vm", name=name)

    vm = _require_vm(db, name)
    # Init failure warns instead of blocks. exec is the non-interactive
    # twin of shell: both are diagnostic primitives, and running
    # `agw vm exec failed-vm cat /var/log/cloud-init.log` is precisely
    # the kind of investigation an operator does on a failed-init VM.
    _guard_failed_vm(vm, allow_failed_init=True)

    ws = _resolve_workspace_for_vm(db, vm, workspace_name)

    # transport() raises StateError when tailscale_host is None; guard first so
    # the operator gets an actionable StateError instead of an AssertionError
    # (which also disappears under python -O).
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    # The orchestrated composition root (gated_vm_boundary): the exec
    # env-chain secrets join the ONE boundary resolve (site secrets +
    # env secrets, one prompt session), after every node's preflight.
    # The same scope dicts feed both the SecretTarget and compose_env
    # so the two consumers can't drift. The vm scope comes from
    # vm.template (DB row), not the config-default template.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_vm_admin_env_scopes(registry, vm, ws=ws)

    with gated_vm_boundary(
        db, config, registry, vm,
        targets=[_vm_secret_target(scopes, label=f"vm-exec={vm.name}")],
    ) as (_vm_node, resolver):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=vm.admin_username,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            admin=scopes.admin,
        )

        target = transport(vm, config)
        remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
        if ws is not None:
            remote_cmd = f"cd {shlex.quote(ws.workspace_path)} && {remote_cmd}"
        return target.call_streaming(remote_cmd, env=env)


def add_git_credential(db: Database, config: Config, name: str, credential_name: str) -> None:
    """Add or update a git credential on a VM.

    This is the first ORCHESTRATED command: its graph is DERIVED from
    the DB row and the declared references by the ``vms/nodes.py``
    factories (zero hand-wired edges), the activation gate replaces
    this command's ``keep_active`` use (opening BEFORE the preflight
    sweep and seeding the boundary resolver with its just-in-time
    values), secrets are delivered scoped to each node's declared
    names, and a rejected token is FATAL (a plain uncaught raise: the
    operator asked to add exactly this one credential, unlike vm/agent
    provisioning's skip-and-degrade).

    The tracer's three documented interim seams are CLOSED with the
    resolver retirement: the walk union is the boundary's only source
    (construct-time registration is gone), prediction is central at
    the node preflights, and the platform's power ops read the
    context (``ctx.secret``, with the gate's scoped reader as the
    source for gate-driven ops).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.transports import transport
    from agentworks.vms.nodes import live_vm_node

    # build_registry runs first so framework miss-policies (e.g.
    # GitCredentialKind's error policy on a typo'd credential name)
    # surface before any DB / VM / config-key business logic.
    registry = build_registry(config)

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry)

    # BUILD: the command names its direct resources (this VM, this
    # credential); everything else enters through the derived graph
    # (the row's site field, the decl's references).
    cred_node = git_credential_node(registry, credential_name)
    provider = cred_node.provider

    entry = provider.helper_entry()
    if entry.repos or entry.owner:
        # Scoped credentials need the helper's embedded selection map
        # rebuilt: a single-line store merge can't provide that. The
        # full-rebuild path (reinit) can. Guarded before the VM node is
        # built and before the gate, preserving the imperative error
        # precedence (at HEAD the site bound after this guard, so a bad
        # site never preempted this error) and ensuring a scoped
        # credential never costs a prompt or a VM start.
        raise ValidationError(
            f"git credential '{credential_name}' is scoped (fine-grained "
            f"PAT); add it to the admin or agent template and run "
            f"'agw vm reinit {name}' instead of add-git-credential"
        )

    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node, cred_node)
    # The walk supplies the boundary union.
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = _vm_scope(db, name)

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # PREFLIGHT-ALL against the one command-start context, then the
        # boundary resolve: the walk-away point.
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()

        def scoped_ctx(node_secret_refs: tuple[str, ...]) -> RunContext:
            return RunContext(
                config=config,
                operation_scope=scope,
                secrets=ScopedSecrets(resolver.values, node_secret_refs),
            )

        # add-git-credential is a single explicit add, so a rejected
        # token is fatal here (unlike vm/agent provisioning, which
        # skips and continues to partial): the operator asked to add
        # exactly this one credential.
        if config.defaults.runup_git_credentials:
            output.detail(
                f"Performing runup test for git-credential/{credential_name}..."
            )
            cred_node.runup(scoped_ctx(cred_node.secret_refs()))
        # The materials-write op reads its token through the node's
        # SCOPED delivery: only the credential's declared secret names.
        token = scoped_ctx(cred_node.secret_refs()).secret(provider.secret_name)
        new_lines = provider.credential_lines(token)

        target = transport(vm, config)

        # Read existing credentials, filter out entries this credential
        # replaces. The key is (username, host/path): scoped github
        # lines are path-less and share the host, so a host-only key
        # would evict every github line including the scoped ones.
        result = target.run("cat ~/.git-credentials 2>/dev/null || true")
        existing = result.stdout.strip().splitlines() if result.stdout.strip() else []

        new_keys = {_credential_line_key(line) for line in new_lines} - {None}
        filtered = [e for e in existing if _credential_line_key(e) not in new_keys]

        # New (always unscoped, see the guard above) lines go FIRST:
        # a username-less query takes the first matching store line, so
        # the host-level fallback must precede username-tagged scoped
        # lines that may already be on the VM.
        all_lines = new_lines + filtered
        cred_content = "\n".join(all_lines) + "\n"
        target.write_file("~/.git-credentials", cred_content, mode="600")
        # This single-line merge does NOT regenerate the credential helper
        # script (it stays from the last full init/reinit). The scoped
        # guard above forces scoped credentials through reinit, so the
        # added line is always unscoped and selection needs no helper
        # change; its only gap is that a rejection of a credential added
        # post-init falls to the helper's generic (unnamed) diagnosis
        # until the next reinit rebuilds the script. Acceptable.
        # Never downgrade the helper slot: on a helper-provisioned VM
        # the agentworks helper stays registered (reverting to store
        # would reintroduce its erase-on-rejection self-destruct for
        # EVERY credential); on an old VM without the helper script,
        # keep store working until the next reinit installs the helper.
        from agentworks.git_credentials import GIT_CRED_HELPER_PATH

        target.run(
            f"if [ -x {GIT_CRED_HELPER_PATH} ]; then "
            f"git config --global --replace-all credential.helper '!{GIT_CRED_HELPER_PATH}'; "
            f"else git config --global credential.helper store; fi"
        )

    output.info(f"Git credential '{credential_name}' configured on VM '{name}'")


def _credential_line_key(line: str) -> tuple[str, str] | None:
    """Identity of a ``~/.git-credentials`` line: (username, host/path).

    Scoped github lines are path-less and share the host, so a
    host-only key would evict every github line at once; the username
    disambiguates. Non-URL lines get ``None`` (never matched).
    """
    if "@" not in line or "//" not in line:
        return None
    userinfo = line.split("//", 1)[1].split("@", 1)[0]
    return (userinfo.split(":", 1)[0], line.split("@", 1)[1])


def _vm_scope(db: Database, vm_name: str) -> OperationScope:
    """The VM commands' shared VM-level operation scope: the operation
    is about the VM itself (the ``_workspace_scope`` /
    ``_session_scope`` siblings' shape at this level). The VM level's
    field rules (required vm; forbidden workspace, agent, session) are
    enforced by the scope's own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel

    return OperationScope(
        level=ScopeLevel.VM,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm_name,
    )


@contextlib.contextmanager
def gated_vm_boundary(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    targets: Sequence[SecretTarget] = (),
    scope: OperationScope | None = None,
) -> Iterator[tuple[LiveVMNode, Resolver]]:
    """The gate-opening commands' shared composition root (vm/agent
    shell and exec, console attach, the workspace lifecycle ops):
    commands that operate interactively on one existing VM. Build the
    live VM node from its row (the site edge holds the bound
    platform), register the walk union AND the command's env-chain
    ``targets`` on the one resolver (site config secrets and runtime
    env secrets are ONE prompt session), then open the ACTIVATION GATE
    before the
    preflight sweep (its just-in-time values seed the boundary
    resolver) and run the one boundary resolve inside it. Yields
    ``(vm_node, resolver)`` within the held-active span: the body's
    interactive or streaming work stays anchored (WSL2's keepalive)
    for the command's duration, and callers read ``resolver.values``
    for env composition.

    ``scope`` is the command's :class:`OperationScope`; when None the
    default VM-level scope for this VM is built. THE RULE: pass the
    level of the entity the command is ABOUT, not of what it walks
    (the graph here is always the live VM alone; the scope names WHY
    the operation runs). The workspace lifecycle callers pass a
    WORKSPACE-level scope, the agent-op callers (agent shell / exec /
    delete / grant / revoke) an AGENT-level one, and the singular
    session ops a SESSION-level one accordingly; the VM default
    serves the commands that are about the VM itself.

    Deliberately NOT :func:`_live_vm_boundary` (the no-gate lifecycle
    trio): these commands converge power state first, and the gate
    ordering (gate, then preflight, then resolve, all inside the span)
    changes the composition's shape rather than adding a flag to it.

    """
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    resolver = Resolver(config, registry)
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    if targets:
        resolver.register_targets(targets)
    if scope is None:
        scope = _vm_scope(db, vm.name)
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()
        yield vm_node, resolver


def _live_vm_boundary(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    registry: Registry | None = None,
) -> tuple[LiveVMNode, RunContext]:
    """The no-gate commands' shared composition root (``start_vm`` /
    ``stop_vm`` / ``delete_vm`` / ``describe_vm``, whose graphs are
    identical): build the live VM node from its row (the site edge
    holds the bound platform), register the walk union on the
    resolver, sweep preflight at VM scope, and run the one boundary
    resolve. Returns the node plus the OP-START context (secrets
    scoped to the site's declared names); callers drive the power ops
    through the held platform (``node.site.platform``) with that
    context, the declare/receive contract's delivery surface.
    ``registry`` reuses a caller-built registry (describe builds one
    early for its degrade-friendly site lookup); ``None`` builds one
    here.

    Deliberately NO activation gate: for start and stop the power op IS
    the command's operation (a command whose op is the state change
    does not converge state first), delete must not gate at all (an
    operator-stopped VM would refuse; broken states are what delete
    exists to clean up), and describe only READS state (a status
    probe is its op; inspecting a stopped VM must never start it).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    if registry is None:
        registry = build_registry(config)
    resolver = Resolver(config, registry)
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    scope = _vm_scope(db, vm.name)
    preflight_all(nodes, RunContext(config=config, operation_scope=scope))
    resolver.resolve()
    ops_ctx = RunContext(
        config=config,
        operation_scope=scope,
        secrets=ScopedSecrets(resolver.values, vm_node.site.secret_refs()),
    )
    return vm_node, ops_ctx


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM. Clears the operator-stopped flag so the
    activation gate resumes auto-starting on demand.

    Orchestrated, composition only: the graph derives from the VM's
    row and the power ops drive through the node's held platform
    (:func:`_live_vm_boundary`). No activation gate opens here: the
    start IS this command's operation, and the operator-stopped flag
    is CLEARED by it, never consulted.
    """
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    vm_node, ops_ctx = _live_vm_boundary(db, config, vm)
    platform = vm_node.site.platform
    # An explicit start is operator intent, whatever the observed state:
    # clear the flag first so a crashed start doesn't leave the gate
    # refusing to auto-resume a VM the operator asked to run.
    db.set_operator_stopped(name, False)
    # Probe status and issue the start BEFORE entering the hold: the
    # WSL2 keepalive subprocess boots a stopped distro as a side effect,
    # which would make status() report RUNNING and mislabel the VM as
    # "already running". The keepalive then anchors the (now running) VM
    # through the Tailscale verification.
    status = platform.status(vm, ops_ctx)
    if status == VMStatus.RUNNING:
        output.info(f"VM '{name}' is already running")
    else:
        platform.start(vm, ops_ctx)

    # Tailscale verification runs inside the keepalive so a freshly booted
    # WSL2 distro doesn't idle-shut while we wait for tailscaled to come up.
    # The rejoin auth key, needed only on a failed reconnect, keeps its
    # internal late resolve (the documented conditional-need exception):
    # there is no gate here to hand a lazy reader through.
    with vm_node.hold_active():
        _ensure_tailscale(db, config, vm, platform)
    # Only emit "is ready" on the path that actually started the VM. When
    # status was already RUNNING we already said so above, and Tailscale
    # verification is usually a no-op (handshake already valid), so an
    # extra "is ready" line is just noise. On the real-work path it
    # confirms tailscaled finished its handshake.
    if status != VMStatus.RUNNING:
        output.info(f"VM '{name}' is ready")


def stop_vm(db: Database, config: Config, name: str) -> None:
    """Stop a running VM and record the operator's intent.

    Orchestrated, composition only, mirroring :func:`start_vm`: no
    activation gate (the stop IS the operation), power ops through the
    node's held platform.
    """
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    vm_node, ops_ctx = _live_vm_boundary(db, config, vm)
    platform = vm_node.site.platform
    # Record intent BEFORE the already-stopped short-circuit: an
    # operator stopping an already-stopped VM still means "keep it
    # stopped" (e.g. the VM idled out and they don't want the next op
    # to auto-resume it).
    db.set_operator_stopped(name, True)
    status = platform.status(vm, ops_ctx)
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        # Never conflate an auto-stop with an explicit one: when the VM
        # stopped on its own, this command still CHANGED something (the
        # intent flag above) and the message says what.
        if vm.operator_stopped:
            output.info(f"VM '{name}' is already manually stopped")
        else:
            output.info(
                f"VM '{name}' had already stopped on its own; it is now "
                f"marked manually stopped and will not be auto-started"
            )
        return
    # No hold here: stop is the inverse of what the keepalive is for.
    # The platform stop call doesn't need SSH to the VM, and holding a
    # wsl.exe sleep subprocess open would fight `wsl --terminate`.
    platform.stop(vm, ops_ctx)
    output.info(f"VM '{name}' stopped")


def rekey_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    wait_for_share: bool = False,
    ignore_env: bool = False,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin).

    Useful for rotating keys, switching tailnets, or recovering from
    expired ephemeral keys. Uses the platform's native transport
    (out-of-band) since Tailscale connectivity drops during
    the operation.

    Orchestrated: the walk roots the VM-TEMPLATE node beside the live
    VM node, because the new auth key IS this command's planned op
    (the contrast with reinit, whose graph deliberately excludes the
    template: there the key belongs only to the gate's conditional
    repair path). The template's readiness (predict the key
    resolvable) runs in the preflight sweep and the key joins the ONE
    boundary resolve, mirroring HEAD's interleaved
    preflight-then-single-resolve exactly; this migration is what
    retired the ``preflight_vm_template`` delegate. The running check
    stays past the boundary (a backend status read; on proxmox it
    needs the token), and the activation gate opens AFTER it, exactly
    where HEAD held ``keep_active``: a not-running VM errors before
    any gate, so rekey never auto-starts one outside the same race
    HEAD had.
    """
    import ipaddress
    import shlex
    import time

    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.activation import activation_gate
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.ssh import SSHError
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.transports import native_transport, transport, wait_for_reconnect
    from agentworks.vms.nodes import live_vm_node, vm_template_node
    from agentworks.vms.templates import resolve_template

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)

    # The composition root: construct (registers the site's config
    # secrets), preflight both participating resources (the vm-template
    # predicts the new auth key can resolve; the platform checks its
    # world), then the operation's one resolve pass: the new auth key
    # and any site secret (proxmox's API token) in a single prompt
    # session. The template node roots FIRST so the sweep keeps HEAD's
    # precedence (template readiness before the platform preflight).
    # ``ignore_env`` is honored by temporarily masking the env-var
    # backend for the auth-key secret (the env-var source reads
    # ``os.environ`` at ``would_attempt`` time, so removing the var
    # skips it cleanly across BOTH the preflight prediction and the
    # resolve, and the prompt backend takes over).
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    vm_node = live_vm_node(db, config, registry, vm)
    rekey_vm_tmpl = resolve_template(registry, vm.template)
    tmpl_node = vm_template_node(rekey_vm_tmpl, registry)
    nodes = walk(tmpl_node, vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    # Cache hit by design: the auth key is already in the union (the
    # template node's secret_refs); this call only fetches the DECL,
    # which the --ignore-env env-var mask below needs.
    ts_decl = resolver.register_name(rekey_vm_tmpl.tailscale_auth_key)
    scope = _vm_scope(db, name)
    with _mask_env_var_backend_for(ts_decl, masked=ignore_env):
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()
    ts_auth_key = resolver.get(rekey_vm_tmpl.tailscale_auth_key)

    # The running check is an op (a backend status read), so it sits
    # past the boundary: on proxmox it needs the API token, delivered
    # scoped to the site's declared names.
    from agentworks.orchestration.secrets import ScopedSecrets

    ops_ctx = RunContext(
        config=config,
        operation_scope=scope,
        secrets=ScopedSecrets(resolver.values, vm_node.site.secret_refs()),
    )
    platform = vm_node.site.platform
    status = platform.status(vm, ops_ctx)
    if status != VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' is not running (status: {status.value})",
            entity_kind="vm",
            entity_name=name,
        )

    output.info(f"Rekeying '{name}'...")

    with contextlib.ExitStack() as _stack:
        # The activation gate, opened AFTER the boundary at exactly the
        # point HEAD held ``keep_active``: converge power state (a race
        # from the running check above, as at HEAD), then hold for the
        # rekey's duration (no-op for Lima/Azure/Proxmox; WSL2 anchors
        # the distro against vmIdleTimeout so per-step `time.sleep`s
        # can't let it idle out). Boundary-then-gate means the gate
        # callback must SERVE the boundary's cached values, never
        # resolve or seed (the batch-ops precedent): the union already
        # covers the gate secrets AND the repair path's rejoin key (the
        # auth key is this command's op secret), and ``Resolver.get``
        # refuses anything outside it loudly.
        _stack.enter_context(activation_gate(vm_node, resolver.get))

        # native_transport() composes transient_route (Azure attach /
        # detach via the polymorphic hook) with the platform-native
        # transport builder and the 6-attempt reachability probe. The
        # caller-supplied ExitStack scopes the transient state to the
        # duration of the rekey.
        exec_target = native_transport(vm, platform, config, stack=_stack)

        # Restart, logout, login, restart. The initial restart clears any
        # stale daemon state (a previous interrupted rekey can leave the
        # daemon in a state where `tailscale logout` hangs waiting for a
        # control plane response that never comes). The final restart
        # fixes a Tailscale bug where the node registers but peers can't
        # reach it after rekeying to a different tailnet.
        # All platforms run systemd (WSL2 enables it via /etc/wsl.conf during
        # provisioning); tailscaled is always a systemd unit. Daemon-side
        # flags like --tun=userspace-networking live in /etc/default/tailscaled,
        # not on `tailscale up`.
        restart_cmd = "systemctl restart tailscaled"
        stabilize_secs = 15  # pause between steps for daemon/network stability

        output.detail("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.detail("Logging out of current tailnet...")
        exec_target.run("tailscale logout", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.detail("Joining new tailnet...")
        quoted_key = shlex.quote(ts_auth_key)
        exec_target.run(f"tailscale up --auth-key {quoted_key}", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.detail("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.detail("Reading new Tailscale IP...")
        result = exec_target.run("tailscale ip -4", sudo=True, timeout=15)
        raw_ip = result.stdout.strip()
        new_ip = raw_ip.splitlines()[0].strip() if raw_ip else ""
        try:
            ipaddress.IPv4Address(new_ip)
        except ValueError:
            raise SSHError(
                f"tailscale ip -4 returned invalid address: {new_ip!r}\nfull output: {raw_ip}"
            ) from None
        output.detail(f"Tailscale IP: {new_ip}")

        # Update DB and SSH config with the new IP (correct regardless of
        # reachability: the old IP is definitely dead after logout)
        db.update_vm_tailscale(name, new_ip)
        sync_ssh_config(config, db)
        db.insert_vm_event(name, "rekey", f"new_ip={new_ip}")

        # If the operator needs to share the VM back, pause before connectivity check
        if wait_for_share:
            output.pause(
                "Share the VM back to your tailnet, then press Enter to verify connectivity..."
            )

        # Always verify Tailscale SSH connectivity to the new IP
        output.detail(f"Verifying SSH to {new_ip}...")
        from agentworks.transports import SSHTransport

        ts_target = transport(vm, config)
        # ``transport()`` returns an SSHTransport for Tailscale-backed VMs.
        # Retarget the host in place instead of rebuilding the whole
        # transport; the other fields (user, identity, etc.) are unchanged.
        assert isinstance(ts_target, SSHTransport)
        ts_target.host = new_ip
        if wait_for_reconnect(ts_target):
            output.info(f"VM '{name}' rekeyed successfully. Tailscale IP: {new_ip}")
        else:
            output.warn(
                f"VM '{name}' rekeyed but {new_ip} is not reachable via SSH. "
                "Check tailnet sharing/ACLs. Run 'vm rekey' again to retry."
            )


def delete_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a VM, cleaning up all associated resources.

    Orchestrated, composition only: the child-count guard and the
    confirm gate stay pre-boundary (zero prompts and zero resolves on
    a refused or declined delete), then the whole build-and-boundary
    composition (:func:`_live_vm_boundary`) is BEST-EFFORT: a broken
    backend, a stranded site, or an unresolvable secret warns and
    skips backend cleanup, because broken states are exactly what
    delete exists to clean up. No activation gate ever opens (an
    operator-stopped VM would refuse; deletion never starts a stopped
    VM), and the Tailscale logout uses a hold-only span. ``UserAbort``
    is the one exception nothing here may downgrade: an abort at the
    boundary's secret prompt or inside an op span must keep the row.
    """
    vm = _require_vm(db, name)

    # Check for workspaces (which contain agents and sessions)
    ws_count = db.count_workspaces_on_vm(name)
    ag_count = db.count_agents_on_vm(name)
    ts_count = db.count_sessions_on_vm(name)
    has_children = ws_count > 0

    if has_children and not force:
        parts = [f"{ws_count} workspace(s)"]
        if ag_count > 0:
            parts.append(f"{ag_count} agent(s)")
        if ts_count > 0:
            parts.append(f"{ts_count} session(s)")
        raise StateError(
            f"VM '{name}' has {', '.join(parts)}.",
            entity_kind="vm",
            entity_name=name,
            hint="Delete them first, or pass --force to also delete the children.",
        )

    if not yes and not force:
        msg = f"Delete VM '{name}'?"
        if has_children:
            parts = [f"{ws_count} workspace(s)"]
            if ag_count > 0:
                parts.append(f"{ag_count} agent(s)")
            if ts_count > 0:
                parts.append(f"{ts_count} session(s)")
            msg += f" ({', '.join(parts)} will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    # Platform-specific cleanup (also handles Tailscale logout)
    vm_node: LiveVMNode | None
    ops_ctx: RunContext | None = None
    try:
        vm_node, ops_ctx = _live_vm_boundary(db, config, vm)
    except UserAbort:
        # Ctrl-C at the boundary's secret prompt must keep the SIGINT
        # contract: abort the whole delete rather than orphaning the
        # backend VM behind a warn. (The boundary helper runs the
        # preflight sweep and the resolve pass, so the prompt happens
        # inside it.)
        raise
    except Exception as e:
        # Preflight or build failure (unreachable API, missing tool,
        # stranded site, unresolvable secret): warn and skip backend
        # cleanup; broken backends are what delete exists to clean up.
        vm_node = None
        hint = getattr(e, "hint", None)
        output.warn(
            f"platform binding failed, skipping backend cleanup: {e}"
            + (f"\n{hint}" if hint else "")
        )

    if vm_node is not None:
        assert ops_ctx is not None  # set beside vm_node above
        platform = vm_node.site.platform
        # Tailscale logout (best-effort, hold-only): the logout wants
        # the VM alive if it happens to be, but delete must NOT gate:
        # an operator-stopped VM would raise. (The WSL2 hold does boot a
        # stopped distro; the logout genuinely needs the VM up.) The
        # whole hold+logout span is best-effort: broken states (e.g. a
        # manually unregistered WSL2 distro whose hold raises) are
        # exactly what `vm delete` exists to clean up, so nothing here
        # may skip the delete below. UserAbort is the one exception the
        # catch-alls must NOT downgrade: a swallowed abort would fall
        # through and delete the DB row the operator just declined.
        if vm.tailscale_host:
            try:
                with vm_node.hold_active():
                    _tailscale_logout(vm, config, platform)
            except UserAbort:
                raise
            except Exception as e:
                output.warn(f"tailscale logout skipped: {e}")

        try:
            platform.delete(vm, ops_ctx)
        except UserAbort:
            raise
        except Exception as e:
            output.warn(f"platform cleanup failed: {e}")

    # Clean up logs
    from agentworks.ssh import LOG_DIR

    vm_logs = list(LOG_DIR.glob(f"{name}-*.log")) if LOG_DIR.exists() else []
    for log in vm_logs:
        log.unlink(missing_ok=True)
    if vm_logs:
        output.info(f"Cleaned up {len(vm_logs)} log(s)")

    # Remove from DB (cascades workspaces and agents), then rebuild SSH config
    db.delete_vm(name)

    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)
    output.info(f"VM '{name}' deleted")


def reinit_vm(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Re-run initialization on a VM that has already been provisioned.

    Requires provisioning_status == complete and a valid Tailscale
    connection. Orchestrated: the graph derives from the VM's row and
    the admin template's declared credentials; the activation gate
    replaces this command's ``keep_active`` use, opening BEFORE the
    preflight sweep (its just-in-time values seed the boundary
    resolver); tokens are delivered scoped to each node's declared
    names. Nothing here is created, so there is no realization log and
    nothing to unwind; a failed init leaves the VM re-runnable, as
    before.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.transports import transport

    # build_registry runs first so framework miss-policies surface
    # before any template / DB / VM business logic.
    registry = build_registry(config)

    vm = _require_vm(db, name)

    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.git_credentials.nodes import git_credential_node
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    resolver = Resolver(config, registry)

    # BUILD before any secret collection: a stranded site fails here
    # (inside the live node's site edge) with the manifest hint instead
    # of after git-token prompts. Construction is cheap; the walk union
    # below is the boundary's source, nothing resolves yet.
    vm_node = live_vm_node(db, config, registry, vm)

    # Resolve the VM's template so init uses the right values
    from agentworks.resources.access import admin_template
    from agentworks.vms.templates import resolve_template

    reinit_vm_tmpl = resolve_template(registry, vm.template)

    if vm.provisioning_status != ProvisioningStatus.COMPLETE.value:
        raise StateError(
            f"VM '{name}' provisioning is '{vm.provisioning_status}', not 'complete'. "
            f"Cannot reinitialize.",
            entity_kind="vm",
            entity_name=name,
        )

    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
        )

    verify_tailscale_available()
    admin = admin_template(registry)
    cred_nodes = tuple(
        git_credential_node(registry, cred_name)
        for cred_name in admin.git_credentials
    )
    providers = {node.provider.owner_name: node.provider for node in cred_nodes}

    # The reinit graph: the live VM (whose row's site field is its edge
    # to the vm-site node) plus each declared credential as its own
    # root. The vm-template is deliberately NOT a node here: its
    # Tailscale key is not part of reinit's planned ops (a broken
    # node's rejoin resolves it on the gate's own conditional repair
    # path), so it must not join the boundary union.
    nodes = walk(vm_node, *cred_nodes)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    scope = OperationScope(
        level=ScopeLevel.VM,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=name,
    )

    def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=scope,
            secrets=ScopedSecrets(resolver.values, secret_names),
        )

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: git tokens and any site config secret
        # (proxmox's API token) resolve in one prompt session.
        # Provisioning is hermetic: no operator-env secrets are
        # prompted at reinit; they get prompted at the use site (vm
        # shell, session create, etc.).
        output.phase("Preflight")
        output.detail(f"Checking vm-site/{vm.site}...")
        announce_git_credentials(providers)
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        output.phase("Resolving Secrets")
        resolver.resolve()

        # No command-root runup at reinit: reinit reaches the VM over
        # Tailscale SSH and never calls the platform API in its planned
        # ops, and the git-credential runup is deferred into the
        # Initialization phase (the skip-and-degrade policy at the
        # write step). So the next banner the operator sees is
        # Initialization.
        git_tokens = {
            node.provider.owner_name: scoped_ctx(node.secret_refs()).secret(
                node.provider.secret_name
            )
            for node in cred_nodes
        }

        # Build Tailscale SSH target with logging
        from agentworks.ssh import SSHLogger

        logger = SSHLogger(name, "vm-reinit")
        for token in git_tokens.values():
            logger.add_redaction(token)
        ts_target = transport(vm, config, default_timeout=60, logger=logger)

        home = f"/home/{vm.admin_username}"

        # try/finally ensures the SSH logger is closed exactly once,
        # AFTER any warning output. Matches the pattern used by agent
        # create / reinit and workspace create / rehome.
        try:
            try:
                run_initialization(
                    db,
                    config,
                    registry,
                    reinit_vm_tmpl,
                    admin,
                    name,
                    ts_target,
                    providers,
                    home,
                    vm.admin_username,
                    logger,
                    git_tokens=git_tokens,
                )
            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling vm reinit '{name}'. The VM may be in a partial state. "
                    f"Re-run 'vm reinit {name}' to retry. Log: {logger.path}"
                )
                raise
            except Exception:
                output.warn(f"Log: {logger.path}")
                raise
        finally:
            logger.close()

    refreshed_vm = db.get_vm(name)
    assert refreshed_vm is not None
    if refreshed_vm.init_status == InitStatus.PARTIAL.value:
        output.info(f"VM '{name}' reinitialized (with warnings, see above)")
        output.detail(f"Log: {logger.path}")
    else:
        output.info(f"VM '{name}' reinitialized successfully!")


def _tailscale_logout(vm: VMRow, config: Config, platform: VMPlatform) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses ``native_transport(vm, platform, config, stack=...)`` so the
    Azure attach/detach lifecycle and the reachability probe are
    composed polymorphically. Platforms whose factory raises (Proxmox)
    are surfaced as a typed StateError, which we catch and warn.
    """
    from agentworks.transports import native_transport

    output.info("Deregistering from Tailscale...")
    try:
        with contextlib.ExitStack() as stack:
            exec_target = native_transport(vm, platform, config, stack=stack)

            # Fire and forget: tailscale down + logout can disrupt
            # networking on the VM, killing SSH-based transports before
            # they get a response. Lima/WSL2 use local transports and
            # are unaffected, but the nohup approach works universally.
            exec_target.run(
                "nohup sh -c 'tailscale down && tailscale logout' >/dev/null 2>&1 &",
                sudo=True,
                timeout=10,
            )
        output.info("Tailscale node deregistered")
    except Exception as e:
        output.warn(f"Tailscale logout failed (node may remain in admin console): {e}")


def _init_log_hint(vm_name: str) -> str:
    """Return a log hint suffix like ' See log: <path>' or empty string."""
    from agentworks.ssh import LOG_DIR

    if not LOG_DIR.exists():
        return ""
    logs = sorted(LOG_DIR.glob(f"{vm_name}-*.log"), reverse=True)
    return f" See log: {logs[0]}" if logs else ""


def _guard_failed_vm(vm: VMRow, *, allow_failed_init: bool = False) -> None:
    """Block operations on VMs with failed provisioning or initialization.

    When ``allow_failed_init`` is True, an init-status failure becomes
    a non-fatal warning instead of a hard block. Used by operations
    that exist precisely so the operator can reach into the VM to
    diagnose or fix the cause of the init failure (e.g. ``vm shell``
    opening a session on a partially-initialized VM to apply a manual
    heal before re-running ``vm reinit``; ``vm exec`` running a one-shot
    diagnostic command). Provisioning failure is never softened: the
    VM may not even be reachable, and the project's stance there is
    "delete and recreate."
    """
    if vm.provisioning_status == ProvisioningStatus.FAILED.value:
        raise StateError(
            f"VM '{vm.name}' has failed provisioning.{_init_log_hint(vm.name)}",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Only 'vm delete' is supported on a failed-provisioning VM.",
        )
    if vm.init_status == InitStatus.FAILED.value:
        if allow_failed_init:
            output.warn(
                f"VM '{vm.name}' has failed initialization.{_init_log_hint(vm.name)} "
                f"Continuing. Use 'vm reinit' to retry once the cause is resolved.",
            )
            return
        raise StateError(
            f"VM '{vm.name}' has failed initialization.{_init_log_hint(vm.name)}",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Use 'vm reinit' to retry or 'vm delete' to remove.",
        )


@contextlib.contextmanager
def _mask_env_var_backend_for(
    decl: SecretDecl,
    *,
    masked: bool,
) -> Iterator[None]:
    """Mask the env-var backend's view of ``decl`` for the duration of
    the block when ``masked`` is True; pass-through otherwise.

    Used by ``vm rekey --ignore-env`` to force the backend chain to
    skip the env-var backend and fall through to the prompt backend.
    The env-var source reads ``os.environ`` at ``would_attempt`` time,
    so popping the matching env vars during the resolve call makes the
    backend silently skip; the next backend in the chain takes over.

    The masked names cover (a) the framework's default convention
    ``AW_SECRET_<UPPER_NAME>`` for ``decl.name``, plus (b) any
    operator-typed string override at ``decl.backend_mappings["env-var"]``.
    Both names are restored on exit, even on exception, so a
    ``KeyboardInterrupt`` during a prompt doesn't leave the operator's
    shell with the var missing.
    """
    import os

    if not masked:
        yield
        return

    from agentworks.secrets.env_var import env_var_name_for

    masked_names: list[str] = [env_var_name_for(decl.name)]
    mapping = decl.backend_mappings.get("env-var")
    if isinstance(mapping, str):
        masked_names.append(mapping)

    saved: dict[str, str] = {}
    for var in masked_names:
        if var in os.environ:
            saved[var] = os.environ.pop(var)
    try:
        yield
    finally:
        os.environ.update(saved)


def _lookup_or_synthesize_secret(registry: Registry, name: str) -> SecretDecl:
    """Return the ``SecretDecl`` for ``name`` from the framework
    Registry, or synthesize a bare one matching the auto-declare shape
    if no Resource was published or auto-declared under that name.

    Used by ``_ensure_tailscale``'s imperative-caller late resolve (the
    orchestrated callers moved onto ``Resolver.register_name``, which
    carries the same fallback). The semantics: an operator who omits
    every ``[vm_templates.*]`` section AND every ``[secrets.*]``
    section leaves the registry empty under the ``secret`` kind, so a
    strict lookup raises ``KeyError``. Synthesizing a bare
    ``SecretDecl`` (the same shape ``_SecretKind.synthesize`` would
    produce, minus ``origin`` which resolution doesn't read) keeps the
    backend chain callable.
    """
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.kinds import SECRET_KIND_NAME

    try:
        found: SecretDecl = registry.lookup(SECRET_KIND_NAME, name)
        return found
    except KeyError:
        return SecretDecl(name=name, description="")


def _query_live_resources(vm: VMRow, config: Config) -> dict[str, str] | None:
    """Query live resource usage from a VM over SSH."""
    from agentworks.transports import transport

    target = transport(vm, config)
    cmd = (
        "nproc && "
        "uptime | grep -oP 'load average: \\K[^,]+' && "
        "free -b | awk '/^Mem:/{print $2,$3} /^Swap:/{print $2,$3}' && "
        "df -h / | awk 'NR==2{print $2,$3,$5}'"
    )

    try:
        result = target.run(cmd, check=False, retries=3)
    except Exception:
        return None

    if not result.ok:
        return None

    lines = result.stdout.strip().splitlines()
    if len(lines) < 5:
        return None

    try:
        cpus = lines[0].strip()
        load_avg = lines[1].strip()
        mem_parts = lines[2].split()
        swap_parts = lines[3].split()
        disk_parts = lines[4].split()

        mem_total_b = int(mem_parts[0])
        mem_used_b = int(mem_parts[1])
        swap_total_b = int(swap_parts[0])
        swap_used_b = int(swap_parts[1])

        mem_pct = f"{mem_used_b * 100 // mem_total_b}%" if mem_total_b > 0 else "0%"
        swap_pct = f"{swap_used_b * 100 // swap_total_b}%" if swap_total_b > 0 else "0%"

        return {
            "cpus": cpus,
            "load_avg": load_avg,
            "mem_total": _human_bytes(mem_total_b),
            "mem_used": _human_bytes(mem_used_b),
            "mem_pct": mem_pct,
            "swap_total": _human_bytes(swap_total_b),
            "swap_used": _human_bytes(swap_used_b),
            "swap_pct": swap_pct,
            "disk_total": disk_parts[0],
            "disk_used": disk_parts[1],
            "disk_pct": disk_parts[2],
        }
    except (IndexError, ValueError):
        return None


def _human_bytes(b: int) -> str:
    """Format bytes as a human-readable string (e.g. 494M, 8.0G)."""
    if b < 1024:
        return f"{b}B"
    for unit in ("K", "M", "G", "T"):
        b_f = b / 1024
        if b_f < 1024 or unit == "T":
            return f"{b_f:.1f}{unit}" if b_f >= 10 else f"{b_f:.2f}{unit}"
        b = int(b_f)
    return f"{b}T"


def _require_vm(db: Database, name: str) -> VMRow:
    vm = db.get_vm(name)
    if vm is None:
        raise NotFoundError(
            f"VM '{name}' not found",
            entity_kind="vm",
            entity_name=name,
        )
    return vm


def _is_tailscale_reachable(tailscale_host: str) -> bool:
    """Quick check whether a Tailscale IP is still reachable."""
    import subprocess

    try:
        result = subprocess.run(
            ["tailscale", "ping", "--timeout=5s", "-c=1", tailscale_host],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def port_forward_vm(
    db: Database,
    config: Config,
    name: str,
    ports: list[str],
    address: str = "localhost",
    verbose: bool = False,
) -> int:
    """Forward one or more local ports to a VM via SSH tunnels.

    Returns the underlying SSH process's exit code; the CLI layer owns the
    translation to process exit (this service function never calls
    ``sys.exit``). Mirrors ``exec_vm``'s return-the-code contract.

    Each port spec is either REMOTE_PORT (local defaults to same) or
    LOCAL_PORT:REMOTE_PORT, matching kubectl port-forward syntax.

    Orchestrated (:func:`gated_vm_boundary`): the graph derives from
    the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the foreground SSH tunnel. The port-spec
    validation and the no-Tailscale guard stay pre-gate: a refused
    forward costs zero prompts, zero resolves, and zero gate events.
    """
    import signal
    import subprocess

    from agentworks.bootstrap import build_registry

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    # Parse port specs
    forwards: list[tuple[int, int]] = []  # (local_port, remote_port)
    for spec in ports:
        parts = spec.split(":")
        if len(parts) == 1:
            try:
                port = int(parts[0])
            except ValueError:
                raise ValidationError(
                    f"invalid port '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((port, port))
        elif len(parts) == 2:
            try:
                local_port = int(parts[0])
                remote_port = int(parts[1])
            except ValueError:
                raise ValidationError(
                    f"invalid port spec '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((local_port, remote_port))
        else:
            raise ValidationError(
                f"invalid port spec '{spec}' (expected [LOCAL:]REMOTE)",
                entity_kind="vm",
                entity_name=name,
            )

    # Validate port ranges
    for local_port, remote_port in forwards:
        for label, port in [("local", local_port), ("remote", remote_port)]:
            if port < 1 or port > 65535:
                raise ValidationError(
                    f"{label} port {port} out of range (1-65535)",
                    entity_kind="vm",
                    entity_name=name,
                )

    # Build SSH command with -L flags for each forward
    ssh_cmd = ["ssh", "-N", "-o", "StrictHostKeyChecking=accept-new"]
    if config.operator.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.operator.ssh_private_key)])
    for local_port, remote_port in forwards:
        ssh_cmd.extend(["-L", f"{address}:{local_port}:localhost:{remote_port}"])
    if verbose:
        ssh_cmd.append("-v")
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")

    # Print forwarding info
    for local_port, remote_port in forwards:
        output.info(f"Forwarding {address}:{local_port} -> {vm.tailscale_host}:{remote_port}")
    if not verbose:
        output.info("Use --verbose for detailed SSH output.")

    # Run in foreground until interrupted
    registry = build_registry(config)
    with gated_vm_boundary(db, config, registry, vm):
        try:
            proc = subprocess.Popen(ssh_cmd)

            # Forward SIGINT/SIGTERM to the SSH process for clean shutdown
            def _handle_signal(sig: int, _frame: object) -> None:
                proc.terminate()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

            return proc.wait()
        except OSError as e:
            raise ConnectivityError(
                f"failed to start SSH: {e}",
                entity_kind="vm",
                entity_name=name,
            ) from e


def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    *,
    auth_key_source: Callable[[], str] | None = None,
) -> None:
    """After starting a VM, verify Tailscale connectivity and rejoin if
    needed. ``platform`` is the caller's bound platform (the gates never
    bind, and a re-bind here would re-run the resolve pass).

    ``auth_key_source`` supplies the rejoin auth key when the caller
    owns its resolution: the orchestrated activation gate passes its
    lazy gate-secrets reader (nodes receive, never resolve), so the key
    resolves on this function's first need, with the same
    conditional-need timing as the internal resolve below. ``None``
    keeps today's behavior for the imperative callers: this function
    resolves the key itself, late.
    """
    from agentworks.transports import native_transport, transport, wait_for_reconnect

    # Refresh VM row in case tailscale_host was cleared on stop
    vm = _require_vm(db, vm.name)

    # If we have a known Tailscale host, wait for it to reconnect after boot.
    # This avoids unnecessarily attaching a public IP on Azure.
    if vm.tailscale_host:
        if wait_for_reconnect(transport(vm, config)):
            return

        # Tailscale didn't reconnect (ephemeral key expired, etc.)
        output.info(f"Tailscale node {vm.tailscale_host} did not reconnect, rejoining...")
        db.clear_vm_tailscale(vm.name)

    if auth_key_source is not None:
        auth_key = auth_key_source()
    else:
        # Resolve a fresh Tailscale auth key via the framework before
        # entering the native-transport block; the backend chain handles
        # env-var lookup with prompt fallback. This is the documented
        # conditional-need exception to the resolve-at-the-preflight-boundary
        # contract: whether a rejoin (and therefore a NEW key) is needed is
        # only knowable after starting the VM and watching the node fail to
        # reconnect, so it gets its own late resolve rather than prompting
        # every start for a key that is almost never used.
        from agentworks.bootstrap import build_registry
        from agentworks.secrets import resolve_for_command
        from agentworks.vms.templates import resolve_template

        registry = build_registry(config)
        rejoin_vm_tmpl = resolve_template(registry, vm.template)
        ts_decl = _lookup_or_synthesize_secret(
            registry, rejoin_vm_tmpl.tailscale_auth_key
        )
        resolved = resolve_for_command([], config, registry, extra_decls=[ts_decl])
        auth_key = resolved[rejoin_vm_tmpl.tailscale_auth_key]

    # native_transport() composes Azure's attach/detach via
    # transient_route polymorphism with the reachability probe. Other
    # platforms have a nullcontext transient_route and just build the
    # native transport.
    with contextlib.ExitStack() as _stack:
        verify_tailscale_available()
        exec_target = native_transport(vm, platform, config, stack=_stack)
        rejoin_tailscale(db, vm.name, exec_target, auth_key=auth_key)

    # After the stack unwinds (Azure detach has fired), wait for
    # Tailscale SSH on the new IP to be reachable. The probe is cheap
    # on platforms whose IP didn't change (succeeds on the first try).
    refreshed = db.get_vm(vm.name)
    if refreshed and refreshed.tailscale_host:
        wait_for_reconnect(transport(refreshed, config))

    # Update SSH config in case the Tailscale IP changed
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)
