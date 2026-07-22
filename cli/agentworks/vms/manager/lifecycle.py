"""The two full-initialization VM flows: create and reinit.

Both call into ``agentworks.vms.initializer`` (``bootstrap_vm`` /
``run_initialization`` / ``announce_git_credentials`` /
``verify_tailscale_available``); see the module-level note near the
imports below for why those calls are routed through the package object
rather than a plain top-level import.

``create_vm`` splits the initializer's two phases across its output
sections: Phase A (``bootstrap_vm``: bootstrap + Tailscale connectivity +
SSH-config sync) is the tail of the ``Provisioning`` section, and Phase B
(``run_initialization``) runs after it as the ``VM Initialization`` /
``Admin Initialization`` sections. A single keepalive hold (an
``ExitStack`` entered before Phase A, released after Phase B) spans both,
and ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure in
either phase to the same operator-facing outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.config import validate_admin_username, validate_name
from agentworks.db import SYSTEM_SLUG_KEY, InitStatus, ProvisioningStatus
from agentworks.errors import (
    AlreadyExistsError,
    ExternalError,
    ProvisioningError,
    StateError,
    UserAbort,
    unknown_template_error,
)

from ._helpers import _require_vm

if TYPE_CHECKING:
    from typing import NoReturn

    from agentworks.config import Config
    from agentworks.db import Database

# NOTE on the initializer imports (``verify_tailscale_available``,
# ``announce_git_credentials``, ``bootstrap_vm``, ``run_initialization``):
# tests monkeypatch these as attributes of the PACKAGE
# (``agentworks.vms.manager.verify_tailscale_available`` etc, set by
# ``manager/__init__.py``'s top-level import from
# ``agentworks.vms.initializer``). A bare call to e.g.
# ``verify_tailscale_available()`` inside this module would resolve
# against THIS module's own globals, not the package's, so a test's
# ``monkeypatch.setattr(vm_manager, "verify_tailscale_available", ...)``
# would silently fail to take effect. Every call to one of these four
# names below therefore goes through ``import agentworks.vms.manager as
# _mgr`` at call time. ``tailscale.py``'s ``_ensure_tailscale`` needs the
# same treatment for ``verify_tailscale_available`` / ``rejoin_tailscale``
# (it is not only ``lifecycle.py`` that consumes these names, despite
# there being one canonical import site in ``manager/__init__.py``).


def _warn_init_cancel(vm_name: str) -> None:
    """Warn that a create was cancelled mid-initialization.

    Shared by ``create_vm``'s Phase A and Phase B cancellation handlers so
    an operator abort (``KeyboardInterrupt`` / ``UserAbort``) in either
    phase surfaces the same recovery guidance before the exception
    propagates unchanged (never downgraded to a Provisioning/External
    error, matching ``delete_vm``'s best-effort discipline).
    """
    output.warn(
        f"Cancelling vm create '{vm_name}' during initialization. "
        f"The VM exists but is partially initialized. "
        f"Use 'vm reinit {vm_name}' to retry, or 'vm delete {vm_name} --force' to remove it."
    )


def _raise_init_failure(db: Database, vm_name: str, cause: Exception) -> NoReturn:
    """Map a non-cancellation failure in either init phase to its outcome.

    A VM whose provisioning is ``failed`` (Phase A marked it so) raises a
    ``ProvisioningError`` with delete guidance; otherwise the VM provisioned
    but a later step failed, so it raises an ``ExternalError`` (the VM may
    still be usable, reinit guidance). Both carry the ``Details:`` pointer to
    the most recent ``vm-create`` log when one exists. Shared by Phase A and
    Phase B so both fail identically.
    """
    from agentworks.ssh import LOG_DIR

    log_hint = ""
    logs = sorted(LOG_DIR.glob(f"{vm_name}-*-vm-create.log"), reverse=True)
    if logs:
        log_hint = f"\nDetails: {logs[0]}"

    vm = db.get_vm(vm_name)
    if vm is not None and vm.provisioning_status == ProvisioningStatus.FAILED.value:
        raise ProvisioningError(
            f"provisioning failed: {cause}{log_hint}",
            entity_kind="vm",
            entity_name=vm_name,
            hint=f"VM '{vm_name}' is in a failed state. Use 'vm delete {vm_name}' to clean up.",
        ) from cause
    raise ExternalError(
        f"initialization failed: {cause}{log_hint}",
        entity_kind="vm",
        entity_name=vm_name,
        hint=f"VM '{vm_name}' may still be usable. Use 'vm reinit {vm_name}' to retry.",
    ) from cause


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str,
    template: str | None = None,
    admin_template: str | None = None,
    site: str | None = None,
) -> None:
    """Create a new VM: provision + initialize.

    Hardware and the admin username are template-owned: the vm-template
    supplies cpus/memory/disk/swap and the admin-template the username.
    There are no per-create overrides; deviations are new templates.

    ``admin_template`` selects which admin-template provisions the admin
    user (None = the reserved ``default``). A non-default name must be a
    declared admin-template resource; an unknown name fails here, before
    any DB or backend work.
    """
    import agentworks.vms.manager as _mgr
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
    from agentworks.resources.access import admin_template as access_admin_template
    from agentworks.resources.access import kind_dict

    # Resolve the selected admin-template (None = reserved ``default``,
    # which always materializes). A non-default name that the operator
    # never declared raises here, before any DB or backend work, with the
    # framework's uniform unknown-template error naming the bad selector.
    selected_admin_template = admin_template or "default"
    try:
        admin = access_admin_template(registry, selected_admin_template)
    except KeyError:
        raise unknown_template_error(
            kind="admin-template",
            label="admin template",
            name=selected_admin_template,
            available=kind_dict(registry, "admin-template"),
        ) from None
    resolved_admin_username = admin.username
    validate_admin_username(resolved_admin_username)

    _mgr.verify_tailscale_available()
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
    cred_nodes = tuple(git_credential_node(registry, cred_name) for cred_name in admin.git_credentials)
    providers = {node.provider.owner_name: node.provider for node in cred_nodes}

    # System slug: first interactive create prompts once (a blank
    # answer is final; see _resolve_system_slug). Runs before any
    # secret prompting or state mutation so an aborted slug entry
    # leaves nothing behind.
    slug = _mgr._resolve_system_slug(db)

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
    with output.section("Preflight"):
        output.info(f"Checking vm-site/{site}...")
        output.info(f"Checking vm-template/{vm_tmpl.name}...")
        _mgr.announce_git_credentials(providers)
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))

    with output.section("Resolving Secrets"):
        resolver.resolve()

    # Polymorphic post-Tailscale-ready hook. Azure overrides to detach
    # the cloud-init public IP (closing the public-exposure window the
    # instant Tailscale becomes reachable); other platforms are no-op.
    def _on_tailscale_ready() -> None:
        refreshed = db.get_vm(vm_name)
        assert refreshed is not None
        platform_obj.post_tailscale_ready(refreshed)

    # The keepalive hold spans BOTH init phases: WSL2 anchors its distro
    # against idle shutdown between Phase A (wsl.exe transport) and Phase B
    # (Tailscale SSH); the other platforms' hold is a no-op. It is entered
    # (into ``init_stack``) just before Phase A and released when the stack
    # exits, after Phase B, on every path. Provisioning (platform create +
    # Phase A bootstrap/connectivity) and Initialization (Phase B) are
    # sibling output sections either side of the stack's Phase-A/Phase-B
    # split; ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure
    # in either phase to the same operator-facing outcome.
    from contextlib import ExitStack

    with ExitStack() as init_stack:
        with output.section("Provisioning"):
            # Provisioning-phase runup: authenticate the platform's own
            # credential (proxmox API token) before create() mutates anything. A
            # definitive rejection aborts here, before the DB row or any backend
            # resource exists (the FATAL policy: nothing realized, nothing to
            # unwind). Runup is deferred and announced inline (no phase of its
            # own); lima/wsl2/azure have no token, so this is a silent no-op for
            # them. The credentials' write-step runup stays deferred into
            # initialization, under the skip-and-degrade policy.
            site_node.runup(scoped_ctx(site_node.secret_refs()))
            tailscale_auth_key = scoped_ctx(template_node.secret_refs()).secret(vm_tmpl.tailscale_auth_key)
            # Each credential's token, read through its node's SCOPED delivery.
            git_tokens = {
                node.provider.owner_name: scoped_ctx(node.secret_refs()).secret(node.provider.secret_name)
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
                # Store the canonical NULL for the reserved default (whether the
                # operator omitted the flag or passed it explicitly), so the
                # column has one encoding per semantic state.
                admin_template=None if selected_admin_template == "default" else selected_admin_template,
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

            # The op-start context for the platform's create op: secrets scoped
            # to the site's declared names.
            platform_ctx = scoped_ctx(site_node.secret_refs())

            # The primary provisioning step: promoted to info so it sits at
            # the section body level (the platform's own sub-steps render as
            # detail one notch deeper).
            output.info(f"Creating VM '{vm_name}' on vm-site '{site}'...")
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

            # -- Phase A: bootstrap + connectivity (the tail of Provisioning) --
            # Past the unwind window: if anything below fails, the VM exists on
            # the remote host and is kept (debuggable, reinit-able). The hold is
            # entered here so it spans Phase A and Phase B; the row exists (the
            # insert above), so no power-state convergence is threaded, only the
            # hold-span. Phase A closes the Provisioning section with the
            # announced "SSH config synced" line.
            init_row = db.get_vm(vm_name)
            assert init_row is not None, "create_vm inserted the row before init"
            try:
                # Enter the hold inside the mapped span (first, before Phase A)
                # so a failure to open it maps like any other init failure, as
                # it did when the hold was entered inside the old initialize_vm.
                init_stack.enter_context(platform_obj.vm_active(init_row, config=config))
                ts_target, logger, home = _mgr.bootstrap_vm(
                    db,
                    config,
                    vm_tmpl,
                    vm_name,
                    result.native_transport,
                    platform_obj,
                    admin_username=resolved_admin_username,
                    tailscale_auth_key=tailscale_auth_key,
                    git_tokens=git_tokens,
                    bootstrap_complete=result.bootstrap_complete,
                    tailscale_ip=result.tailscale_ip,
                    on_tailscale_ready=_on_tailscale_ready,
                )
            except (KeyboardInterrupt, UserAbort):
                # An operator abort must never downgrade to a
                # Provisioning/External error; re-raise as itself after the
                # recovery-guidance warning.
                _warn_init_cancel(vm_name)
                raise
            except Exception as e:
                _raise_init_failure(db, vm_name, e)

        # -- Initialization (Phase B) --
        # Sibling of Provisioning: runs after the section closes, over
        # Tailscale SSH, with the same failure mapping as Phase A.
        try:
            _mgr.run_initialization(
                db,
                config,
                registry,
                vm_tmpl,
                admin,
                vm_name,
                ts_target,
                providers,
                home,
                resolved_admin_username,
                logger,
                git_tokens=git_tokens,
                is_first_init=True,
            )
        except (KeyboardInterrupt, UserAbort):
            _warn_init_cancel(vm_name)
            raise
        except Exception as e:
            _raise_init_failure(db, vm_name, e)

    # -- Post-init: SSH config re-sync --
    # Phase A already synced and announced "SSH config synced" as the last
    # line of Provisioning; this re-sync captures any state Phase B changed
    # (nothing today) and stays silent (announce=False) to avoid a duplicate
    # line.
    try:
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db, announce=False)
    except Exception as e:
        output.warn(f"SSH config sync failed: {e}")
        output.info("VM is likely still usable.")

    # Final status is set by run_initialization (COMPLETE or PARTIAL). The
    # terminal outcome line renders at column 0 via result().
    vm = db.get_vm(vm_name)
    assert vm is not None
    if vm.init_status == InitStatus.PARTIAL.value:
        output.result(f"VM '{vm_name}' is ready (with warnings, see above)")
    else:
        output.result(f"VM '{vm_name}' is ready!")


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
    import agentworks.vms.manager as _mgr
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
            f"VM '{name}' provisioning is '{vm.provisioning_status}', not 'complete'. Cannot reinitialize.",
            entity_kind="vm",
            entity_name=name,
        )

    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
        )

    # Resolve the admin-template the VM was created with (NULL column =
    # reserved ``default``), not always ``default``. Mirror create's
    # clean error if the operator has since removed the declaration, so a
    # dropped admin-template surfaces as a typed error naming the selector
    # rather than a raw KeyError traceback. This cheap row + registry
    # check bails before the Tailscale probe below.
    from agentworks.resources.access import kind_dict

    selected_admin_template = vm.admin_template or "default"
    try:
        admin = admin_template(registry, selected_admin_template)
    except KeyError:
        raise unknown_template_error(
            kind="admin-template",
            label="admin template",
            name=selected_admin_template,
            available=kind_dict(registry, "admin-template"),
        ) from None

    _mgr.verify_tailscale_available()
    cred_nodes = tuple(git_credential_node(registry, cred_name) for cred_name in admin.git_credentials)
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
        with output.section("Preflight"):
            output.info(f"Checking vm-site/{vm.site}...")
            _mgr.announce_git_credentials(providers)
            preflight_all(nodes, RunContext(config=config, operation_scope=scope))

        with output.section("Resolving Secrets"):
            resolver.resolve()

        # No command-root runup at reinit: reinit reaches the VM over
        # Tailscale SSH and never calls the platform API in its planned
        # ops, and the git-credential runup is deferred into the
        # Initialization phase (the skip-and-degrade policy at the
        # write step). So the next banner the operator sees is
        # Initialization.
        git_tokens = {
            node.provider.owner_name: scoped_ctx(node.secret_refs()).secret(node.provider.secret_name)
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
                _mgr.run_initialization(
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
    # Terminal outcome line at column 0 via result().
    if refreshed_vm.init_status == InitStatus.PARTIAL.value:
        output.result(f"VM '{name}' reinitialized (with warnings, see above)")
        output.info(f"Log: {logger.path}")
    else:
        output.result(f"VM '{name}' reinitialized successfully!")
