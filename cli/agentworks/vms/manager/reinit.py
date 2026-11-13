"""``vm reinit``: re-run initialization on a VM that has completed provisioning.

It starts with the VM contract's base checks over ``agentworks.execution``,
then drives ``run_initialization`` inside the activation gate. Calls into
``agentworks.vms.initializer`` (``run_initialization``,
``verify_tailscale_available``) and the manager's ``reconcile_release`` go
through ``import agentworks.vms.manager as _mgr`` at call time, so a test that
patches the package attribute reaches this module too (see the note in
``lifecycle.py``).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import SYSTEM_SLUG_KEY, InitStatus, ProvisioningStatus
from agentworks.errors import StateError
from agentworks.execution.access import Admin, Agent
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.linux_users import managed_home
from agentworks.vms import contract

from ._helpers import _require_vm
from .boundary import _warn_legacy_release

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.debian import DebianRelease
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy


def reinit_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    admin_template: str | None = None,
    admin_spec: str | None = None,
    interaction: TtyInteractionPolicy,
) -> None:
    """Re-run initialization on a VM that has already been provisioned.

    ``admin_template`` re-points the VM to another declared admin-template,
    and ``admin_spec`` replaces its stored admin layer (an empty value, or
    ``{}``, clears it); without them both are retained. A replacement
    supersedes a stored admin layer this release can no longer use
    (``ExplicitReplacementRequired``), which a retain refuses. Every input is
    validated and the effective declaration resolved before either is
    persisted, and both persist in one transaction under the VM's native
    mutation guard, which the whole reinit holds; as with ``agent reinit``,
    they remain if initialization then fails, so a re-run retries them.

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
    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, name) as guard:
        import agentworks.vms.manager as _mgr
        from agentworks.bootstrap import load_request_registry

        # The declarations come first so framework miss-policies surface before
        # any template / DB / VM business logic. A supplied admin spec must be able
        # to supersede a stored admin layer this release cannot use, so the live
        # resources are published only once this VM's candidate is known (below).
        registry = load_request_registry(config, include_live_resources=False)

        vm = _require_vm(db, name)
        if vm.debian_release is not None:
            _warn_legacy_release(vm)

        from agentworks.capabilities.base import OperationScope, ScopeLevel
        from agentworks.git_credentials.nodes import git_credential_node
        from agentworks.instance_specs import (
            decode_stored_vm_overlays,
            ensure_effective_references_enabled,
            parse_admin_instance_spec,
            vm_layer_kept_by_admin_spec,
        )
        from agentworks.legacy_claude import legacy_component
        from agentworks.orchestration.activation import (
            activation_gate,
            gate_secret_resolver,
        )
        from agentworks.orchestration.readiness import preflight_all
        from agentworks.orchestration.secrets import ScopedSecrets, secret_union
        from agentworks.orchestration.walk import walk
        from agentworks.resources.access import ensure_recipe_enabled
        from agentworks.secrets.resolver import Resolver
        from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin
        from agentworks.vms.nodes import live_vm_node
        from agentworks.vms.template import effective_references
        from agentworks.vms.templates import resolve_template_with_provenance as resolve_vm

        # Resolve the VM's templates and layers so init uses the right values.
        candidate_admin_template = vm.admin_template if admin_template is None else admin_template
        original_overlay = db.instance_state.get_desired_overlay("vm", vm.name)
        supplied_admin = None if admin_spec is None else parse_admin_instance_spec(admin_spec or "{}")
        legacy_conversion = None
        if admin_spec is not None:
            # The VM layer is kept as stored; the stored admin layer may be superseded.
            stored_vm_overlay = (
                None
                if original_overlay is None
                else vm_layer_kept_by_admin_spec(original_overlay, clearing=supplied_admin is None)
            )
            candidate_admin_overlay = supplied_admin
        else:
            base = resolve_admin(registry, candidate_admin_template).value.active_harness_integrations
            stored_overlays = (
                None if original_overlay is None else decode_stored_vm_overlays(original_overlay, legacy_user_base=base)
            )
            if (
                original_overlay is not None
                and stored_overlays is not None
                and legacy_component(original_overlay) is not None
            ):
                legacy_conversion = (original_overlay, stored_overlays.payload)
            stored_vm_overlay = None if stored_overlays is None else stored_overlays.vm
            candidate_admin_overlay = None if stored_overlays is None else stored_overlays.admin
        layered_reinit_vm_tmpl = resolve_vm(
            registry,
            vm.template,
            overlay=None if stored_vm_overlay is None else stored_vm_overlay.declaration,
            instance_name=vm.name,
        )
        reinit_vm_tmpl = layered_reinit_vm_tmpl.value

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

        # Resolve the VM's admin-template (the re-point when given; a NULL column
        # is the reserved ``default``), not always ``default``. Mirror create's
        # clean error if the operator has since removed the declaration, so a
        # dropped admin-template surfaces as a typed error naming the selector
        # rather than a raw KeyError traceback. This cheap row + registry
        # check bails before the Tailscale probe below.
        selected_admin_template = "default" if candidate_admin_template is None else candidate_admin_template
        previous_admin_template = "default" if vm.admin_template is None else vm.admin_template
        vm = replace(vm, admin_template=candidate_admin_template)
        from agentworks.resources.live_publish import project_vm_live_resource
        from agentworks.vms.admin import effective_references as admin_effective_references

        layered_admin = resolve_admin(
            registry,
            selected_admin_template,
            overlay=candidate_admin_overlay,
            instance_name=vm.name,
        )
        admin = layered_admin.value

        # Publish this VM's candidate before the database's live resources, so the
        # stored layer a supplied admin spec supersedes is never read for it.
        pending = project_vm_live_resource(
            name=vm.name,
            site=vm.site,
            vm_template_name="default" if vm.template is None else vm.template,
            admin_template_name=selected_admin_template,
            layered_vm=layered_reinit_vm_tmpl,
            layered_admin=layered_admin,
        )
        registry = load_request_registry(
            config,
            live_database=db,
            pending_publishers=(lambda target: target.add_live(pending),),
        )
        resolver = Resolver(config, registry, interaction=interaction)

        # BUILD before any secret collection: a stranded site fails here
        # (inside the live node's site edge) with the manifest hint instead
        # of after git-token prompts. Construction is cheap; the walk union
        # below is the boundary's source, nothing resolves yet.
        vm_node = live_vm_node(db, config, registry, vm, held_native_guard=guard)

        ensure_effective_references_enabled(
            registry,
            effective_references(reinit_vm_tmpl, ("vm", vm.name), layered_reinit_vm_tmpl.provenance),
        )
        # Refuse a recipe drawing on a disabled plugin's declarable resource before
        # the reinit realize (Phase 7, LLD b). Drift guard:
        # tests/agents/test_recipe_gate_drift.py.
        ensure_recipe_enabled(registry, "vm-template", reinit_vm_tmpl.name)
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
        # The base checks run through agentworks.execution: an unsupported client
        # refuses before the gate, a secret or the platform is touched.
        execution.resolve_ssh()
        cred_nodes = tuple(git_credential_node(registry, cred_name) for cred_name in admin.git_credentials)

        # The reinit graph: the live VM (whose row's site field is its edge
        # to the vm-site node) plus each declared credential as its own
        # root. The vm-template is deliberately NOT a node here: its
        # Tailscale key is not part of reinit's planned ops (repairing a broken
        # node is 'agw vm recover'), so it must not join the boundary union.
        nodes = walk(vm_node, *cred_nodes)
        for secret_name in secret_union(nodes):
            resolver.register_name(secret_name)

        from agentworks.harness_setup.lifecycle import prepare_vm_setup

        setup_inputs = prepare_vm_setup(db, registry, name=name, template=reinit_vm_tmpl, admin=admin)
        for setup_input in setup_inputs:
            setup_input.register(resolver, registry)

        scope = OperationScope(
            level=ScopeLevel.VM,
            system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
            vm=name,
        )

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

        setup_failures: list[SetupFailure] = []

        from agentworks.instance_specs import (
            OverlayDisposition,
            OverlayOutcome,
            admin_overlay_field_names,
            render_overlay_outcome,
            replace_vm_admin_overlay,
        )

        overlay_outcome = None
        if admin_spec is None and candidate_admin_overlay is not None:
            overlay_outcome = OverlayOutcome(
                OverlayDisposition.RETAINED, admin_overlay_field_names(candidate_admin_overlay)
            )

        def persist_declarations() -> None:
            """Persist the re-point and the admin layer together, as agent reinit does.

            Called once every check before initialization has passed (the
            declarations, the SSH boundary, the activation gate, preflight,
            secrets and the VM contract's base checks), none of which reads
            what this stores. Both remain if initialization then fails, so a
            re-run retries them.
            """
            nonlocal overlay_outcome
            if admin_template is None and admin_spec is None:
                return
            with db.transaction():
                if admin_template is not None:
                    db.update_vm_admin_template(vm.name, admin_template)
                if admin_spec is not None:
                    overlay_outcome = replace_vm_admin_overlay(db, vm.name, supplied_admin)
            if selected_admin_template != previous_admin_template:
                output.info(
                    f"VM '{name}' admin template binding saved as {selected_admin_template!r} (previously "
                    f"{previous_admin_template!r}). The binding remains if initialization fails; use "
                    "--admin-template to choose a different one."
                )

        try:
            with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
                # Credentials and active setup env/config secrets share one eager
                # boundary. Install-command runners keep their existing environment.
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

                # The operation log every request of this reinit records into.
                from agentworks.ssh import SSHLogger

                # This operation log's redaction set includes credentials and every
                # resolved setup input before any incremental initialization log write.
                logger = SSHLogger(
                    name,
                    "vm-reinit",
                    redactions=tuple(
                        dict.fromkeys((*git_redactions, *(resolver.values.values() if setup_inputs else ())))
                    ),
                )
                home = managed_home(vm.admin_username)

                # try/finally ensures the SSH logger is closed exactly once,
                # AFTER any warning output. Matches the pattern used by agent
                # create / reinit and workspace create / rehome.
                try:
                    try:
                        # The base checks come first, over agentworks.execution: release
                        # drift refuses before any other finding is shown, and a failure
                        # stops before Phase B touches the VM.
                        logger.step("Check the VM contract")
                        access = execution.vm_access(vm, config, Admin(), log=logger.record)
                        agents = [Agent(agent.name, agent.linux_user) for agent in db.list_agents(vm_name=name)]
                        report = contract.check_base(
                            access, expected_release=None, admin=vm.admin_username, agents=agents
                        )
                        verified_release = _mgr.reconcile_release(db, vm, report.release)
                        _warn_newly_observed_legacy(vm, verified_release)
                        contract.enforce(report, name)
                        persist_declarations()
                        _mgr.run_initialization(
                            db,
                            config,
                            registry,
                            reinit_vm_tmpl,
                            admin,
                            name,
                            access,
                            credential_ops,
                            home,
                            vm.admin_username,
                            logger,
                            debian_release=verified_release,
                            operation=_mgr.VMInitializationOperation.VM_REINIT,
                            setup_failures=setup_failures,
                            legacy_conversion=legacy_conversion,
                            setup_inputs=setup_inputs,
                            setup_values=resolver.values,
                            setup_guard=guard,
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

            # The aliases name the configured key, which the initialization just installed and may be a new one.
            from agentworks.ssh_config import sync_ssh_config_or_warn

            sync_ssh_config_or_warn(config, db)
        finally:
            # The outcome renders once the command settles, before any collected setup failure.
            render_overlay_outcome(overlay_outcome)

        refreshed_vm = db.get_vm(name)
        assert refreshed_vm is not None
        # Terminal outcome line at column 0 via result().
        if setup_failures:
            output.info(f"Log: {logger.display_path}")
        raise_setup_failures(setup_failures)
        if refreshed_vm.init_status == InitStatus.PARTIAL.value:
            output.result(f"VM '{name}' reinitialized (with warnings, see above)")
            output.info(f"Log: {logger.display_path}")
        else:
            from agentworks.harness_setup.outcomes import setup_completion_suffix
            from agentworks.harness_setup.state import read_native_setup

            suffix = setup_completion_suffix(read_native_setup(db, "vm", name))
            output.result(f"VM '{name}' reinitialized{suffix}.")


def _warn_newly_observed_legacy(vm: VMRow, observed: DebianRelease) -> None:
    """Warn after observation only when the command entered with an unknown row."""
    if vm.debian_release is None:
        _warn_legacy_release(replace(vm, debian_release=observed))
