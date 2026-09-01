"""The two full-initialization VM flows: create and reinit.

Both call into ``agentworks.vms.initializer`` (``bootstrap_vm`` /
``run_initialization`` /
``verify_tailscale_available``); see the module-level note near the
imports below for why those calls are routed through the package object
rather than a plain top-level import.

``create_vm`` splits the initializer's two phases across its output
sections: Phase A (``bootstrap_vm``: record the platform-owned bootstrap,
verify Tailscale connectivity, and sync SSH config) is the tail of the
``Provisioning`` section, and Phase B
(``run_initialization``) runs after it as the ``VM Initialization`` /
``Admin Initialization`` sections. A single keepalive hold (an
``ExitStack`` entered before Phase A, released after Phase B) spans both,
and ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure in
either phase to the same operator-facing outcome.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.config import validate_admin_username
from agentworks.db import SYSTEM_SLUG_KEY, InitStatus, ProvisioningStatus
from agentworks.debian import CURRENT_DEBIAN_RELEASE, DebianRelease, probe_debian_release
from agentworks.errors import (
    AlreadyExistsError,
    ConfigError,
    ExternalError,
    ProvisioningError,
    StateError,
    UserAbort,
)
from agentworks.naming import MAX_VM_NAME_LENGTH, validate_name

from ._helpers import _require_vm
from .boundary import _warn_legacy_release

if TYPE_CHECKING:
    from typing import NoReturn

    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.transports import Transport

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

    scope = OperationScope(level=ScopeLevel.VM, system_slug=slug, vm=vm_name)

    def scoped_ctx(
        secret_names: tuple[str, ...],
        *,
        admin_target: Transport | None = None,
        agent_target: Transport | None = None,
    ) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=scope,
            admin_target=admin_target,
            agent_target=agent_target,
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

    # Polymorphic post-Tailscale-ready hook. Azure overrides to delete
    # its ephemeral bootstrap SSH allow rule (closing provisioning
    # access the instant Tailscale becomes reachable); other platforms
    # are no-op. It fires deep inside Phase A, so it closes over the same
    # ``platform_obj`` / ``platform_ctx`` the create op ran with (both
    # bound below, well before bootstrap_vm invokes this), which is what
    # delivers the SP credential to the NSG call with no ambient
    # fallback.
    def _on_tailscale_ready() -> None:
        refreshed = db.get_vm(vm_name)
        assert refreshed is not None
        platform_obj.post_tailscale_ready(refreshed, platform_ctx)

    # The keepalive hold spans BOTH init phases: WSL2 anchors its distro
    # against idle shutdown between Phase A (wsl.exe transport) and Phase B
    # (Tailscale SSH); the other platforms' hold is a no-op. It is entered
    # (into ``init_stack``) just before Phase A and released when the stack
    # exits, after Phase B, on every path. Provisioning (platform create +
    # Phase A state recording/connectivity) and Initialization (Phase B) are
    # sibling output sections either side of the stack's Phase-A/Phase-B
    # split; ``_warn_init_cancel`` / ``_raise_init_failure`` map a failure
    # in either phase to the same operator-facing outcome.
    from contextlib import ExitStack

    with ExitStack() as init_stack:
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
            from agentworks.secrets.line_safety import (
                LineOrientedSecretUse,
                require_line_safe_secret,
            )

            tailscale_auth_key = require_line_safe_secret(
                scoped_ctx(template_node.secret_refs()).secret(vm_tmpl.tailscale_auth_key),
                use=LineOrientedSecretUse.TAILSCALE,
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
                    redactions=(tailscale_auth_key, *git_redactions),
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

            try:
                request = ProvisionRequest(
                    vm_name=vm_name,
                    debian_release=creation_release,
                    hostname=hostname,
                    system_slug=slug,
                    admin_username=resolved_admin_username,
                    ssh_public_key=prepared_ssh.public_text,
                    ssh_private_key=config.operator.ssh_private_key,
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
                log.unwind()
                raise
            try:
                result = platform_obj.create(request, platform_ctx)
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
            except (ConfigError, StateError):
                # Release-map misses and live-release verification failures
                # are already typed with their owning platform's remediation.
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
                ) from e
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

            # A platform's success return is not evidence about the guest. It
            # may come from third-party code, so core independently observes
            # the live release over the returned transport. Metadata is
            # already durable: if attestation fails after the platform closed
            # its rollback window, the failed row still addresses the backend
            # for an explicit delete.
            try:
                output.info(f"Confirming Debian release {creation_release.value}...")
                observed_release = probe_debian_release(
                    result.native_transport,
                    expected=creation_release,
                )
            except (KeyboardInterrupt, UserAbort):
                db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
                output.warn(
                    f"VM '{vm_name}' exists but its Debian release was not verified; "
                    f"run 'agw vm delete {vm_name}' to remove it."
                )
                raise
            except Exception as e:
                db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
                raise StateError(
                    f"Agentworks could not verify VM '{vm_name}' as Debian {creation_release.value} "
                    f"after vm-platform/{platform_obj.name} returned success",
                    entity_kind="vm",
                    entity_name=vm_name,
                    hint=f"Run 'agw vm delete {vm_name}' to remove the unverified VM, then update the platform.",
                ) from e

            db.update_vm_debian_release(vm_name, observed_release)

            # -- Phase A: record + verify connectivity (the tail of Provisioning) --
            # Past the unwind window: if anything below fails, the VM exists on
            # the remote host and is kept (debuggable, reinit-able). The hold is
            # entered here so it spans Phase A and Phase B; the row exists (the
            # insert above), so no power-state convergence is threaded, only the
            # hold-span. The manager closes the Provisioning section explicitly
            # after Phase A returns.
            init_row = db.get_vm(vm_name)
            assert init_row is not None, "create_vm inserted the row before init"
            try:
                # Enter the hold inside the mapped span (first, before Phase A)
                # so a failure to open it maps like any other init failure, as
                # it did when the hold was entered inside the old initialize_vm.
                init_stack.enter_context(platform_obj.vm_active(init_row, config=config))
                ts_target, home = _mgr.bootstrap_vm(
                    db,
                    config,
                    vm_name,
                    result.native_transport,
                    platform_obj,
                    platform_ctx,
                    logger,
                    admin_username=resolved_admin_username,
                    tailscale_ip=result.tailscale_ip,
                    on_tailscale_ready=_on_tailscale_ready,
                )
                output.info("Provisioning complete.")
            except (KeyboardInterrupt, UserAbort):
                # An operator abort must never downgrade to a
                # Provisioning/External error; re-raise as itself after the
                # recovery-guidance warning.
                _warn_init_cancel(vm_name)
                render_overlay_outcome(overlay_outcome)
                raise
            except Exception as e:
                render_overlay_outcome(overlay_outcome)
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
                credential_ops,
                home,
                resolved_admin_username,
                logger,
                debian_release=observed_release,
                operation=_mgr.VMInitializationOperation.VM_CREATE,
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
    render_overlay_outcome(overlay_outcome)
    if vm.init_status == InitStatus.PARTIAL.value:
        output.result(f"VM '{vm_name}' is ready (with warnings, see above)")
    else:
        output.result(f"VM '{vm_name}' is ready!")


def reinit_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
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
    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    # build_registry runs first so framework miss-policies surface
    # before any template / DB / VM business logic.
    registry = load_request_registry(config, live_database=db)

    vm = _require_vm(db, name)
    if vm.debian_release is not None:
        _warn_legacy_release(vm)

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

    resolver = Resolver(config, registry, interaction=interaction)

    # BUILD before any secret collection: a stranded site fails here
    # (inside the live node's site edge) with the manifest hint instead
    # of after git-token prompts. Construction is cheap; the walk union
    # below is the boundary's source, nothing resolves yet.
    vm_node = live_vm_node(db, config, registry, vm)

    # Resolve the VM's template so init uses the right values
    from agentworks.instance_specs import ensure_effective_references_enabled, get_vm_instance_overlays
    from agentworks.vms.template import effective_references
    from agentworks.vms.templates import resolve_template_with_provenance

    stored_overlays = get_vm_instance_overlays(db, vm.name)
    stored_vm_overlay = None if stored_overlays is None else stored_overlays.vm
    layered_reinit_vm_tmpl = resolve_template_with_provenance(
        registry,
        vm.template,
        overlay=None if stored_vm_overlay is None else stored_vm_overlay.declaration,
        instance_name=vm.name,
    )
    reinit_vm_tmpl = layered_reinit_vm_tmpl.value
    ensure_effective_references_enabled(
        registry,
        effective_references(reinit_vm_tmpl, ("vm", vm.name), layered_reinit_vm_tmpl.provenance),
    )

    # Refuse a recipe drawing on a disabled plugin's declarable resource before
    # the reinit realize (Phase 7, LLD b). Drift guard:
    # tests/agents/test_recipe_gate_drift.py. The admin-template recipe is gated
    # below, once it is resolved.
    from agentworks.resources.access import ensure_recipe_enabled

    ensure_recipe_enabled(registry, "vm-template", reinit_vm_tmpl.name)

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
    selected_admin_template = "default" if vm.admin_template is None else vm.admin_template
    from agentworks.vms.admin import effective_references as admin_effective_references
    from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin_template

    layered_admin = resolve_admin_template(
        registry,
        selected_admin_template,
        overlay=None if stored_overlays is None else stored_overlays.admin,
        instance_name=vm.name,
    )
    admin = layered_admin.value
    ensure_effective_references_enabled(
        registry,
        admin_effective_references(admin, ("vm", vm.name), layered_admin.provenance),
    )

    # Gate the selected admin-template's recipe too, before backend work
    # (Phase 7, LLD b).
    ensure_recipe_enabled(registry, "admin-template", selected_admin_template)

    # Validate the configured public/private pair after all cheap declaration
    # and recipe checks, but before the applied-state boundary, activation,
    # secret resolution, or transport construction. Authorized-key
    # reconciliation repeats this read immediately before its remote write so
    # a path replacement during the operation still fails safely.
    from agentworks.vms.applied_state import prepare_configured_ssh_identity

    prepare_configured_ssh_identity(
        config.operator.ssh_public_key,
        config.operator.ssh_private_key,
    )

    # Reinit is the one establishment path that may operate on a historic VM
    # with no SSH evidence. Known drift still refuses before activation or
    # transport construction.
    _mgr.require_vm_ssh_boundary(db, config, vm, allow_not_recorded=True)

    _mgr.verify_tailscale_available()
    cred_nodes = tuple(git_credential_node(registry, cred_name) for cred_name in admin.git_credentials)

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

    def scoped_ctx(
        secret_names: tuple[str, ...],
        *,
        admin_target: Transport | None = None,
        agent_target: Transport | None = None,
    ) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=scope,
            admin_target=admin_target,
            agent_target=agent_target,
            secrets=ScopedSecrets(resolver.values, secret_names),
        )

    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        # The preflight boundary: git tokens and any site config secret
        # (proxmox's API token) resolve in one prompt session.
        # Provisioning is hermetic: no operator-env secrets are
        # prompted at reinit; they get prompted at the use site (vm
        # shell, session create, etc.).
        with output.section("Preflight"):
            from agentworks.git_credentials import announce_git_credentials

            output.info(f"Checking vm-site/{vm.site}...")
            announce_git_credentials(cred_nodes)
            preflight_all(
                nodes,
                RunContext(config=config, operation_scope=scope),
                registry=registry,
                interaction=interaction,
            )

        with output.section("Resolving Secrets"):
            resolver.resolve()

        # No command-root runup at reinit: reinit reaches the VM over
        # Tailscale SSH and never calls the platform API in its planned
        # ops, and the git-credential runup is deferred into the
        # Initialization phase (the skip-and-degrade policy at the
        # write step). So the next banner the operator sees is
        # Initialization.
        from agentworks.git_credentials import (
            credential_redactions,
            credential_requests,
        )

        credential_ops = credential_requests(cred_nodes, scoped_ctx)
        git_redactions = credential_redactions(cred_nodes, resolver.values)

        # Build Tailscale SSH target with logging
        from agentworks.ssh import SSHLogger

        # The activation gate and any conditional Tailscale rejoin finish
        # before this logger exists. The rejoin path separately enforces that
        # its auth-key-bearing transport has no logger, so this operation log's
        # complete secret set is exactly the secret-backed credential inputs
        # used by initialization.
        logger = SSHLogger(name, "vm-reinit", redactions=git_redactions)
        ts_target = transport(vm, config, default_timeout=60, logger=logger)

        home = f"/home/{vm.admin_username}"

        # try/finally ensures the SSH logger is closed exactly once,
        # AFTER any warning output. Matches the pattern used by agent
        # create / reinit and workspace create / rehome.
        try:
            try:
                verified_release = _mgr.verified_vm_release(db, vm, ts_target)
                _warn_newly_observed_legacy(vm, verified_release)
                _mgr.run_initialization(
                    db,
                    config,
                    registry,
                    reinit_vm_tmpl,
                    admin,
                    name,
                    ts_target,
                    credential_ops,
                    home,
                    vm.admin_username,
                    logger,
                    debian_release=verified_release,
                    operation=_mgr.VMInitializationOperation.VM_REINIT,
                )
            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling vm reinit '{name}'. The VM may be in a partial state. "
                    f"Re-run 'vm reinit {name}' to retry. Log: {logger.display_path}"
                )
                raise
            except Exception:
                output.warn(f"Log: {logger.display_path}")
                raise
        finally:
            logger.close()

    refreshed_vm = db.get_vm(name)
    assert refreshed_vm is not None
    # Terminal outcome line at column 0 via result().
    if refreshed_vm.init_status == InitStatus.PARTIAL.value:
        output.result(f"VM '{name}' reinitialized (with warnings, see above)")
        output.info(f"Log: {logger.display_path}")
    else:
        output.result(f"VM '{name}' reinitialized successfully!")


def _warn_newly_observed_legacy(vm: VMRow, observed: DebianRelease) -> None:
    """Warn after observation only when the command entered with an unknown row."""
    if vm.debian_release is None:
        _warn_legacy_release(replace(vm, debian_release=observed))
