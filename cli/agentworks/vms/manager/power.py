"""VM inspection and power-state commands: list, describe, start, stop,
delete, rekey. None of these call into ``agentworks.vms.initializer``
(that's ``lifecycle.py``'s job); they drive the platform's power ops and
the Tailscale rejoin/logout helpers in ``tailscale.py``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import SYSTEM_SLUG_KEY, VMStatus
from agentworks.errors import (
    AgentworksError,
    StateError,
    UserAbort,
)

from ._helpers import (
    _guard_failed_vm,
    _mask_env_var_backend_for,
    _require_vm,
    _vm_scope,
)
from .boundary import _live_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.vms.nodes import LiveVMNode

# NOTE on ``_ensure_tailscale`` (start_vm), ``_tailscale_logout``
# (delete_vm), and ``_query_live_resources`` (describe_vm): all three are
# defined elsewhere (``tailscale.py``, ``_helpers.py``), and tests
# monkeypatch them as attributes of the PACKAGE
# (``agentworks.vms.manager._ensure_tailscale`` / ``._tailscale_logout`` /
# ``._query_live_resources``). A plain ``from .tailscale import
# _ensure_tailscale`` (or ``from ._helpers import
# _query_live_resources``) here would bind a local name in THIS module's
# namespace, invisible to a monkeypatch of the package attribute. All
# three calls below go through ``import agentworks.vms.manager as _mgr``
# at call time instead.


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
    import agentworks.vms.manager as _mgr

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
        live = _mgr._query_live_resources(vm, config)

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
            # Headerless sections carry the per-workspace session listing's
            # indentation (was detail(indent=2)/detail(indent=3)): the
            # "Sessions" line sits one level under the workspace, each
            # session one level under that.
            with output.section():
                if sessions:
                    output.detail(f"Sessions ({len(sessions)}):")
                    with output.section():
                        for s in sessions:
                            mode_label = f"agent:{s.agent_name}" if s.agent_name else "admin"
                            output.detail(f"{s.name}  [{s.template}]  {mode_label}")
                else:
                    output.detail("(no sessions)")
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


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM. Clears the operator-stopped flag so the
    activation gate resumes auto-starting on demand.

    Orchestrated, composition only: the graph derives from the VM's
    row and the power ops drive through the node's held platform
    (:func:`_live_vm_boundary`). No activation gate opens here: the
    start IS this command's operation, and the operator-stopped flag
    is CLEARED by it, never consulted.
    """
    import agentworks.vms.manager as _mgr

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
        _mgr._ensure_tailscale(db, config, vm, platform)
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
    output.result(f"VM '{name}' stopped")


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
    import agentworks.vms.manager as _mgr

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
        output.warn(f"platform binding failed, skipping backend cleanup: {e}" + (f"\n{hint}" if hint else ""))

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
                    _mgr._tailscale_logout(vm, config, platform)
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
    output.result(f"VM '{name}' deleted")


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

    with output.section(f"Rekeying '{name}'"), contextlib.ExitStack() as _stack:
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

        output.info("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.info("Logging out of current tailnet...")
        exec_target.run("tailscale logout", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.info("Joining new tailnet...")
        quoted_key = shlex.quote(ts_auth_key)
        exec_target.run(f"tailscale up --auth-key {quoted_key}", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.info("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.info("Reading new Tailscale IP...")
        result = exec_target.run("tailscale ip -4", sudo=True, timeout=15)
        raw_ip = result.stdout.strip()
        new_ip = raw_ip.splitlines()[0].strip() if raw_ip else ""
        try:
            ipaddress.IPv4Address(new_ip)
        except ValueError:
            raise SSHError(f"tailscale ip -4 returned invalid address: {new_ip!r}\nfull output: {raw_ip}") from None
        output.detail(f"Tailscale IP: {new_ip}")

        # Update DB and SSH config with the new IP (correct regardless of
        # reachability: the old IP is definitely dead after logout)
        db.update_vm_tailscale(name, new_ip)
        sync_ssh_config(config, db)
        db.insert_vm_event(name, "rekey", f"new_ip={new_ip}")

        # If the operator needs to share the VM back, pause before connectivity check
        if wait_for_share:
            output.pause("Share the VM back to your tailnet, then press Enter to verify connectivity...")

        # Always verify Tailscale SSH connectivity to the new IP
        output.info(f"Verifying SSH to {new_ip}...")
        from agentworks.transports import SSHTransport

        ts_target = transport(vm, config)
        # ``transport()`` returns an SSHTransport for Tailscale-backed VMs.
        # Retarget the host in place instead of rebuilding the whole
        # transport; the other fields (user, identity, etc.) are unchanged.
        assert isinstance(ts_target, SSHTransport)
        ts_target.host = new_ip
        if wait_for_reconnect(ts_target):
            output.result(f"VM '{name}' rekeyed successfully. Tailscale IP: {new_ip}")
        else:
            output.warn(
                f"VM '{name}' rekeyed but {new_ip} is not reachable via SSH. "
                "Check tailnet sharing/ACLs. Run 'vm rekey' again to retry."
            )
