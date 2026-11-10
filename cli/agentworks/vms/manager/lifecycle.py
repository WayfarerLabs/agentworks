"""``vm create``: provision and initialize a new VM. Its sibling, ``vm reinit``, is ``reinit.py``.

It calls into ``agentworks.vms.initializer`` (``bootstrap_vm`` /
``run_initialization`` /
``verify_tailscale_available``); see the module-level note near the
imports below for why those calls are routed through the package object
rather than a plain top-level import.

``create_vm`` splits the initializer's two phases across its output
sections: Phase A (``bootstrap_vm``: record the platform's address, enroll
the host key on first contact, check the VM contract, and sync SSH config)
is the tail of the ``Provisioning`` section, and Phase B
(``run_initialization``) runs after it as the ``VM Initialization`` /
``Admin Initialization`` sections. A single keepalive hold (an
``ExitStack`` entered before Phase A, released after Phase B) spans both,
and ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure in
either phase to the same operator-facing outcome.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import RetainedProvisioningError
from agentworks.config import validate_admin_username
from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.debian import CURRENT_DEBIAN_RELEASE
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    AuthorizationError,
    ConfigError,
    ExternalError,
    ProvisioningError,
    StateError,
    TokenRejectedError,
    UserAbort,
)
from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing, VMNotReady
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name

if TYPE_CHECKING:
    from typing import NoReturn

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy

# NOTE on the initializer imports (``verify_tailscale_available``,
# ``bootstrap_vm``, ``run_initialization``):
# tests monkeypatch these as attributes of the PACKAGE
# (``agentworks.vms.manager.verify_tailscale_available`` etc, set by
# ``manager/__init__.py``'s top-level import from
# ``agentworks.vms.initializer``). A bare call to e.g.
# ``verify_tailscale_available()`` inside this module would resolve
# against THIS module's own globals, not the package's, so a test's
# ``monkeypatch.setattr(vm_manager, "verify_tailscale_available", ...)``
# would silently fail to take effect. Every call to one of these four
# names below therefore goes through ``import agentworks.vms.manager as
# _mgr`` at call time. ``reinit.py`` and ``recover.py`` give
# ``verify_tailscale_available`` the same treatment (it is not only
# ``lifecycle.py`` that consumes these names, despite there being one
# canonical import site in ``manager/__init__.py``).


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

    A VM whose provisioning is not ``complete`` (it never passed Phase A, so
    reinit and every ordinary command refuse it) raises a
    ``ProvisioningError`` with delete guidance; otherwise the VM provisioned
    but a later step failed, so it raises an ``ExternalError`` (the VM may
    still be usable, reinit guidance). Both carry the ``Details:`` pointer to
    the most recent ``vm-create`` log when one exists. Shared by Phase A and
    Phase B so both fail identically.

    A cause whose hint names ``agw vm recover`` (python3 missing, no host-key
    record) is not chained on a VM that never completed: recover refuses that
    VM, so the delete guidance is the only hint.
    """
    from agentworks.ssh import LOG_DIR

    log_hint = ""
    logs = sorted(LOG_DIR.glob(f"{vm_name}-*-vm-create.log"), reverse=True)
    if logs:
        log_hint = f"\nDetails: {logs[0]}"

    vm = db.get_vm(vm_name)
    if vm is not None and vm.provisioning_status != ProvisioningStatus.COMPLETE.value:
        failure = ProvisioningError(
            f"provisioning failed: {cause}{log_hint}",
            entity_kind="vm",
            entity_name=vm_name,
            hint=f"VM '{vm_name}' did not complete provisioning. Use 'vm delete {vm_name}' to clean up.",
        )
        if isinstance(cause, (TrustMissing, VMNotReady)):
            raise failure from None
        raise failure from cause
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
    spec: str | None = None,
    admin_template: str | None = None,
    admin_spec: str | None = None,
    site: str | None = None,
    interaction: TtyInteractionPolicy,
) -> None:
    """Create a new VM: provision + initialize.

    Hardware and the admin username are template-derived: the vm-template
    supplies cpus/memory/disk/swap and the admin-template the username. A
    typed final ``spec`` layer may override VM-template fields for this VM,
    and ``admin_spec`` may override admin-template fields. There are no
    individual per-hardware flags.

    ``admin_template`` selects which admin-template provisions the admin
    user (None = the reserved ``default``). A non-default name must be a
    declared admin-template resource; an unknown name fails here, before
    any DB or backend work.
    """
    import agentworks.vms.manager as _mgr
    from agentworks.bootstrap import load_request_registry
    from agentworks.vms.templates import resolve_template_with_provenance

    # A declaration-only registry runs first so framework miss-policies (typo'd git
    # credential, future TemplateReference typos on inherits, etc.)
    # surface before any template / DB / VM business logic. The
    # pending-plus-durable registry built below is authoritative for mutation.
    registry = load_request_registry(config, include_live_resources=False)

    from agentworks.instance_specs import parse_vm_instance_specs

    desired_overlays = parse_vm_instance_specs(spec, admin_spec)
    vm_overlay = None if desired_overlays is None else desired_overlays.vm
    layered_vm_tmpl = resolve_template_with_provenance(
        registry,
        template,
        overlay=None if vm_overlay is None else vm_overlay.declaration,
        instance_name=name,
    )
    vm_tmpl = layered_vm_tmpl.value
    # Refuse a vm-template recipe that draws on a disabled plugin's declarable
    # resource (a bundled install-command / apt entry / inherited template)
    # BEFORE any DB or backend work, with the enable-plugin hint (Phase 7,
    # LLD b). Drift guard: tests/agents/test_recipe_gate_drift.py. The
    # admin-template recipe is gated below, once it is resolved.
    from agentworks.resources.access import ensure_recipe_enabled

    ensure_recipe_enabled(registry, "vm-template", vm_tmpl.name)

    selected_admin_template = "default" if admin_template is None else admin_template
    from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin_template

    layered_admin = resolve_admin_template(
        registry,
        selected_admin_template,
        overlay=None if desired_overlays is None else desired_overlays.admin,
        instance_name=name,
    )
    admin = layered_admin.value
    ensure_recipe_enabled(registry, "admin-template", selected_admin_template)
    resolved_admin_username = admin.username
    validate_admin_username(resolved_admin_username)

    # Resolve the target site and its declaration. An undeclared site
    # fails here with the stranded-site ConfigError + manifest hint,
    # and a NOT-READY one with its reason chain, both before any DB or
    # backend work, and critically before the Tailscale check and the
    # interactive system-slug prompt below: the operator must never
    # answer a prompt for an op the site already sank.
    from agentworks.vms.sites import ensure_site_ready, lookup_site, select_site

    site = select_site(site, config.defaults.site, registry)
    from agentworks.resources.live_publish import project_vm_live_resource

    pending = project_vm_live_resource(
        name=name,
        site=site,
        vm_template_name="default" if template is None else template,
        admin_template_name=selected_admin_template,
        layered_vm=layered_vm_tmpl,
        layered_admin=layered_admin,
    )
    existing_vm = db.get_vm(name)
    if existing_vm is None:
        registry = load_request_registry(
            config,
            live_database=db,
            pending_publishers=(lambda target: target.add_live(pending),),
        )
        from agentworks.instance_specs import ensure_effective_references_enabled

        ensure_effective_references_enabled(registry, pending.outbound)
    site_decl = lookup_site(site, registry)
    ensure_site_ready(site_decl, registry)

    vm_name = name
    validate_name(vm_name, max_length=MAX_VM_NAME_LENGTH)

    if existing_vm is not None:
        raise AlreadyExistsError(
            f"VM '{vm_name}' already exists",
            entity_kind="vm",
            entity_name=vm_name,
        )
    from agentworks.instance_specs import refuse_orphan_creation_state

    refuse_orphan_creation_state(db, "vm", vm_name)

    # Validate and retain the exact public identity before any secret
    # resolution, local state mutation, logging, or platform work. The
    # private carrier may be deliberately unverifiable (for example a
    # supported encrypted legacy key), but a verifiable mismatch cannot be
    # allowed to provision an identity this client does not hold.
    from agentworks.vms.applied_state import prepare_configured_ssh_identity

    prepared_ssh = prepare_configured_ssh_identity(
        config.operator.ssh_public_key,
        config.operator.ssh_private_key,
    )

    # Resource settings come from the already-resolved template plus its
    # optional final instance layer; the admin-template owns the username.
    resolved_cpus = vm_tmpl.cpus
    resolved_memory = vm_tmpl.memory
    resolved_disk = vm_tmpl.disk
    _mgr.verify_tailscale_available()
    # Phase A reaches the new VM through agentworks.execution: an unsupported
    # client refuses here, before the platform builds a VM or the join spends
    # the auth key.
    execution.resolve_ssh()
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

    resolver = Resolver(config, registry, interaction=interaction)

    # BUILD: the command names its direct resources (the resolved
    # template, the chosen site, the admin template's declared
    # credentials) and constructs the PENDING VM node up front with
    # those edges attached; the walk assembles the graph. Provider
    # and platform construction is cheap and touches no secret
    # machinery; the walk union below is the boundary's source.
    # Nothing resolves yet.
    cred_nodes = tuple(git_credential_node(registry, cred_name) for cred_name in admin.git_credentials)

    # System slug: first interactive create prompts once (a blank
    # answer is final; see _resolve_system_slug). Runs before any
    # secret prompting or state mutation so an aborted slug entry
    # leaves nothing behind.
    slug = _mgr._resolve_system_slug(db)

    template_node = vm_template_node(vm_tmpl)
    site_node = vm_site_node(registry, site)
    creation_release = CURRENT_DEBIAN_RELEASE
    pending_vm = pending_vm_node(
        db,
        vm_name,
        creation_release,
        template_node,
        site_node,
        cred_nodes,
    )
    nodes = walk(pending_vm)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)

    from agentworks.harness_setup.lifecycle import prepare_vm_setup

    setup_inputs = prepare_vm_setup(db, registry, name=vm_name, template=vm_tmpl, admin=admin)
    for setup_input in setup_inputs:
        setup_input.register(resolver, registry)

    scope = OperationScope(level=ScopeLevel.VM, system_slug=slug, vm=vm_name)

    def scoped_ctx(
        secret_names: tuple[str, ...],
        *,
        admin_access: VMAccess | None = None,
        agent_access: VMAccess | None = None,
    ) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=scope,
            admin_access=admin_access,
            agent_access=agent_access,
            secrets=ScopedSecrets(resolver.values, secret_names),
        )

    # Provisioning credentials and explicitly active setup env/config secrets
    # join one boundary pass. Install-command runners remain hermetic; with
    # neither desired setup nor prior applied-state records, unused operator env stays out.
    with output.section("Preflight"):
        from agentworks.git_credentials import announce_git_credentials

        output.info(f"Checking vm-site/{site}...")
        output.info(f"Checking vm-template/{vm_tmpl.name}...")
        announce_git_credentials(cred_nodes)
        preflight_all(
            nodes,
            RunContext(config=config, operation_scope=scope),
            registry=registry,
            interaction=interaction,
        )

    with output.section("Resolving Secrets"):
        resolver.resolve()

    # The keepalive hold spans BOTH init phases: WSL2 anchors its distro
    # against idle shutdown through Phase A and Phase B, both over SSH at its
    # tailnet address; the other platforms' hold is a no-op. It is entered
    # (into ``init_stack``) just before Phase A and released when the stack
    # exits, after Phase B, on every path. Provisioning (platform create +
    # Phase A state recording/connectivity) and Initialization (Phase B) are
    # sibling output sections either side of the stack's Phase-A/Phase-B
    # split; ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure
    # in either phase to the same operator-facing outcome.
    from contextlib import ExitStack

    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.interrupts import termination_as_interrupt

    setup_failures: list[SetupFailure] = []
    with ExitStack() as init_stack:
        # First in, so last out: SIGTERM and SIGHUP unwind like Ctrl-C through
        # every interrupt arm below (a cloud's create rolls back its bootstrap
        # allow and its partial VM) and through the stack's own exits.
        init_stack.enter_context(termination_as_interrupt())
        setup_guard = init_stack.enter_context(native_mutation_guard(db.path, vm_name))
        from agentworks.harness_setup.lifecycle import require_prepared_setup

        require_prepared_setup(db, "vm", vm_name, ("vm", "admin"), setup_inputs)
        with output.section("Provisioning"):
            # Provisioning-phase runup: the platform's own authenticated
            # pre-check before create() mutates anything (proxmox authenticates
            # its API token; azure resolves its credential, which on a site with
            # a configured service principal means reading and probing the
            # client secret, and confirms the resource group exists). A
            # definitive rejection aborts here, before the DB row or any backend
            # resource exists (the FATAL policy: nothing realized, nothing to
            # unwind). Runup is deferred and announced inline (no phase of its
            # own); lima and wsl2 have nothing to authenticate, so it is a
            # silent no-op for them. The credentials' write-step runup stays
            # deferred into initialization, under the skip-and-degrade policy.
            from .boundary import require_tailscale_auth_key

            tailscale_auth_key = require_tailscale_auth_key(
                scoped_ctx(template_node.secret_refs()).secret(vm_tmpl.tailscale_auth_key),
                secret_name=vm_tmpl.tailscale_auth_key,
            )
            from agentworks.git_credentials import (
                credential_redactions,
                credential_requests,
            )

            credential_ops = credential_requests(cred_nodes, scoped_ctx)
            git_redactions = credential_redactions(cred_nodes, resolver.values)
            site_node.runup(scoped_ctx(site_node.secret_refs()))

            # The VM's OS hostname, computed once at create time and recorded on the
            # row: {slug}-{name} with a slug, the bare name without. Bounded by
            # construction: slug max 20 + dash + name max 38 (MAX_VM_NAME_LENGTH)
            # = 59 characters, inside the 63-char DNS-label limit. The cap is 38
            # (not 42) because the tighter sink is Azure's virtual-network name
            # {slug}-{name}-vnet, capped at 64: 20 + 1 + 38 + 5 = 64. See
            # config/validation.py MAX_VM_NAME_LENGTH for the MIN-over-sinks
            # derivation.
            hostname = f"{slug}-{vm_name}" if slug else vm_name

            # Create DB record with as-provisioned resource values. This is the
            # pending VM's realization artifact (what teardown deletes), so the
            # log records it the moment the row exists: a provisioning failure
            # below unwinds exactly the row (today's rollback, relocated onto
            # the node), and nothing past provisioning is rollback-tracked (an
            # initialized-but-partial VM is kept, debuggable, reinit-able).
            from agentworks.instance_specs import persist_vm_creation_overlays

            with db.transaction():
                refuse_orphan_creation_state(db, "vm", vm_name)
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
                overlay_outcome = persist_vm_creation_overlays(db, vm_name, desired_overlays)
            log = RealizationLog()
            log.mark_realized(pending_vm)

            # Reset the host-key store file now that the name is ours, so a
            # reused name never inherits a key: it is left empty and private,
            # and Phase A's first connection writes the VM's key into it.
            # Every unwind below removes it with the row (the pending node's
            # teardown); a retained or kept row keeps it, as delete removes it.
            try:
                trust.begin_enrollment(vm_name)
            except BaseException:
                log.unwind()
                raise

            # The platform instance was bound (and preflighted, and its secrets
            # resolved) at the composition root above; dispatch is just ops now.
            platform_obj = site_node.platform
            from agentworks.capabilities.vm_platform import ProvisionRequest
            from agentworks.ssh import SSHLogger

            # One manager-owned log spans platform bootstrap, Phase A, and
            # initialization. Construct it before dispatch so WSL2 can report
            # its platform-owned bootstrap through the same redacted sink.
            # Register the close on the surrounding stack exactly once; every
            # success, failure, and interrupt after construction exits it. A
            # close error warns but cannot replace the primary operation result.
            try:
                logger = SSHLogger(
                    vm_name,
                    "vm-create",
                    redactions=tuple(
                        dict.fromkeys(
                            (tailscale_auth_key, *git_redactions, *(resolver.values.values() if setup_inputs else ()))
                        )
                    ),
                )
            except (KeyboardInterrupt, UserAbort):
                log.unwind()
                raise
            except Exception as e:
                log.unwind()
                raise ProvisioningError(
                    f"could not create the provisioning log: {e}",
                    entity_kind="vm",
                    entity_name=vm_name,
                ) from e

            def _close_create_logger() -> None:
                active_primary = sys.exc_info()[1]
                try:
                    logger.close()
                except BaseException as close_error:
                    if active_primary is None:
                        from agentworks.instance_specs import render_retained_creation_overlay

                        render_retained_creation_overlay(db, "vm", vm_name)
                        raise
                    try:  # noqa: SIM105 - warning failure must not replace the active primary
                        output.warn(f"could not close provisioning log {logger.display_path}: {close_error}")
                    except BaseException:
                        pass

            init_stack.callback(_close_create_logger)

            # The provisioning trust file exists for the platform's create
            # alone: cloud provisioning reaches the VM at its public address
            # before it is in the store, and nothing after create does.
            provisioning_files = ExitStack()
            try:
                request = ProvisionRequest(
                    vm_name=vm_name,
                    debian_release=creation_release,
                    hostname=hostname,
                    system_slug=slug,
                    admin_username=resolved_admin_username,
                    ssh_public_key=prepared_ssh.public_text,
                    ssh_private_key=config.operator.ssh_private_key,
                    provisioning_trust=provisioning_files.enter_context(trust.provisioning_binding(vm_name)),
                    tailscale_auth_key=tailscale_auth_key,
                    progress=logger,
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
                output.info(f"Creating VM '{vm_name}' on vm-site '{site}' ({creation_release.value})...")
            except BaseException:
                provisioning_files.close()
                log.unwind()
                raise
            try:
                with provisioning_files:
                    result = platform_obj.create(request, platform_ctx)
            except RetainedProvisioningError as e:
                # Rollback could not prove the backend clean. Persist the
                # platform's exact identifiers and keep the provisional row so
                # an explicit delete can finish cleanup instead of orphaning
                # resources when this command exits.
                with db.transaction():
                    db.update_vm_platform_metadata(vm_name, e.platform_metadata)
                    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
                from agentworks.instance_specs import render_overlay_outcome

                render_overlay_outcome(overlay_outcome)
                output.warn(f"VM '{vm_name}' is retained in failed state because backend cleanup was not confirmed.")
                output.warn(f"Log: {logger.display_path}")
                raise
            except KeyboardInterrupt:
                # The platform's create owns rolling back its own partial
                # backend resources before this interrupt propagates (the
                # create contract; Azure cleans up or, on a second Ctrl-C,
                # abandons loudly). By the time it reaches here the row is
                # the only artifact left to unwind; deleting it for a VM
                # that still exists in a backend would orphan the backend
                # side (#338).
                output.warn(f"Cancelling vm create '{vm_name}'... rolling back.")
                log.unwind()
                output.warn(f"Log: {logger.display_path}")
                raise
            except UserAbort:
                # No prompt lives in this span today (the boundary resolve ran
                # at the composition root above), but an operator abort must
                # never downgrade to a ProvisioningError; roll back like the
                # KeyboardInterrupt twin above.
                log.unwind()
                output.warn(f"Log: {logger.display_path}")
                raise
            except (AuthorizationError, ConfigError, StateError, TokenRejectedError):
                # These failures are already typed with their owning
                # platform's remediation.
                # A compliant platform has not mutated, or has rolled back,
                # so only the provisional row remains to unwind.
                log.unwind()
                raise
            except Exception as e:
                log.unwind()
                raise ProvisioningError(
                    f"provisioning failed: {e}\nDetails: {logger.display_path}",
                    entity_kind="vm",
                    entity_name=vm_name,
                    # The wrapped error's own guidance (a rerun, the admin console) still applies.
                    hint=e.hint if isinstance(e, AgentworksError) else None,
                ) from e
            db.record_vm_started(vm_name)
            # The platform's rollback window closes here: its create returned
            # and the VM exists. Core acceptance is still pending below; a
            # failure there keeps an addressable row with recovery guidance.
            from agentworks.instance_specs import render_overlay_outcome

            # Persist the platform's opaque identifiers verbatim; the owning
            # platform is the column's only reader.
            try:
                db.update_vm_platform_metadata(vm_name, result.platform_metadata)
            except BaseException:
                render_overlay_outcome(overlay_outcome)
                raise

            # -- Phase A: first contact and the VM contract (the tail of Provisioning) --
            # Past the unwind window: the VM exists on the backend, so it is
            # kept, and only an attested one is ever marked complete. Metadata
            # is already durable, so a failed row still addresses the backend
            # for an explicit delete. The hold is entered here so it spans
            # Phase A and Phase B; the row exists (the insert above), so no
            # power-state convergence is threaded, only the hold-span. The
            # manager closes the Provisioning section explicitly after Phase A
            # returns.
            init_row = db.get_vm(vm_name)
            assert init_row is not None, "create_vm inserted the row before init"
            try:
                # Enter the hold inside the mapped span (first, before Phase A)
                # so a failure to open it maps like any other init failure, as
                # it did when the hold was entered inside the old initialize_vm.
                init_stack.enter_context(platform_obj.vm_active(init_row, config=config))
                access, home, observed_release = _mgr.bootstrap_vm(
                    db,
                    config,
                    vm_name,
                    platform_obj,
                    platform_ctx,
                    logger,
                    admin_username=resolved_admin_username,
                    tailnet_address=result.tailnet_address,
                    expected_release=creation_release,
                )
                output.info("Provisioning complete.")
            except (KeyboardInterrupt, UserAbort):
                # An operator abort must never downgrade to a
                # Provisioning/External error; re-raise as itself after the
                # recovery-guidance warning.
                # Phase A writes complete only once the VM contract passed, and every
                # ordinary command refuses a VM that is not complete, so the status
                # chooses the guidance and nothing needs marking here. A read that
                # fails must not replace the interrupt, and the delete wording is
                # the safe one when the status is unknown.
                try:
                    row = db.get_vm(vm_name)
                except Exception:
                    row = None
                if row is None or row.provisioning_status != ProvisioningStatus.COMPLETE.value:
                    output.warn(
                        f"VM '{vm_name}' exists but was not verified; run 'agw vm delete {vm_name}' to remove it."
                    )
                else:
                    _warn_init_cancel(vm_name)
                render_overlay_outcome(overlay_outcome)
                raise
            except Exception as e:
                render_overlay_outcome(overlay_outcome)
                _raise_init_failure(db, vm_name, e)

        # -- Initialization (Phase B) --
        # Sibling of Provisioning: runs after the section closes, on the
        # admin's access Phase A returned, with the same failure mapping.
        try:
            _mgr.run_initialization(
                db,
                config,
                registry,
                vm_tmpl,
                admin,
                vm_name,
                access,
                credential_ops,
                home,
                resolved_admin_username,
                logger,
                debian_release=observed_release,
                operation=_mgr.VMInitializationOperation.VM_CREATE,
                setup_failures=setup_failures,
                setup_inputs=setup_inputs,
                setup_guard=setup_guard,
                setup_values=resolver.values,
            )
        except (KeyboardInterrupt, UserAbort):
            _warn_init_cancel(vm_name)
            render_overlay_outcome(overlay_outcome)
            raise
        except Exception as e:
            render_overlay_outcome(overlay_outcome)
            _raise_init_failure(db, vm_name, e)

    # -- Post-init: SSH config re-sync --
    # Phase A already synced and announced the first SSH-config update before
    # Provisioning completed; this re-sync captures any state Phase B changed
    # (nothing today) and stays silent (announce=False) to avoid a duplicate
    # line.
    from agentworks.ssh_config import sync_ssh_config_or_warn

    sync_ssh_config_or_warn(config, db, announce=False)

    # Final status is set by run_initialization (COMPLETE or PARTIAL). The
    # terminal outcome line renders at column 0 via result().
    vm = db.get_vm(vm_name)
    assert vm is not None
    render_overlay_outcome(overlay_outcome)
    if setup_failures:
        output.info(f"Log: {logger.display_path}")
    raise_setup_failures(setup_failures)
    if vm.init_status == InitStatus.PARTIAL.value:
        output.result(f"VM '{vm_name}' is ready (with warnings, see above)")
    else:
        from agentworks.harness_setup.outcomes import setup_completion_suffix
        from agentworks.harness_setup.state import read_native_setup

        suffix = setup_completion_suffix(read_native_setup(db, "vm", vm_name))
        output.result(f"VM '{vm_name}' is ready{suffix}.")
