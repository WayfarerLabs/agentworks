"""VM lifecycle management -- create, list, start, stop, delete."""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, NamedTuple

from agentworks import output
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
    resolve_git_credential_providers,
    run_initialization,
    verify_tailscale_available,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.git_credentials.base import GitCredentialProvider
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.resolver import Resolver


class _VmAdminEnvScopes(NamedTuple):
    """Per-scope env dicts for vm-level commands (shell, exec).

    The ``workspace`` field is ``None`` for vm-level commands without a
    workspace pin (``vm shell`` / ``vm exec`` without ``--workspace``).
    When set, workspace-template env enters the FRD R2 precedence ladder
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


def bind_platform(
    config: Config,
    vm: VMRow,
    *,
    registry: Registry | None = None,
    resolver: Resolver | None = None,
    prepare: bool = True,
    targets: Sequence[SecretTarget] = (),
) -> VMPlatform:
    """Composition-root helper: bind a VM's platform through its site.

    Runs the capability lifecycle's composition-root ordering once:
    registry (built here unless the caller already has one) -> construct
    (cheap; the site's declared config secrets register on the
    operation's resolver, nothing resolves) -> preflight -> the
    operation's one resolve pass at the preflight boundary (one prompt
    session covering the union of everything registered). Call ONCE at
    a VM-touching command's entry and thread the bound platform down;
    the gates (:func:`ensure_active` / :func:`keep_active`) take it as
    a parameter and never resolve or bind anything themselves.

    ``targets`` folds the command's runtime env chain into the same
    pass: every secret the targets' merged env references (the shell /
    exec / session-create roots) registers before the boundary, so the
    workload's env secrets and the site's config secrets are ONE prompt
    session. Callers read the mapping back via the resolver they passed
    in (``resolver.values`` feeds ``compose_env``).

    ``prepare=False`` returns the constructed instance without the
    preflight + resolve boundary, for the roots that interleave other
    participating resources' preflights first (``create_vm`` and
    ``rekey_vm`` add the vm-template's Tailscale-key prediction before
    the one resolve pass). Those callers own running the boundary.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.sites import platform_for

    if registry is None:
        registry = build_registry(config)
    if resolver is None:
        resolver = Resolver(config, registry)
    platform = platform_for(vm, registry, resolver=resolver)
    if targets:
        resolver.register_targets(targets)
    if prepare:
        platform.preflight()
        resolver.resolve()
    return platform


def bind_platforms(
    config: Config,
    vms: Iterable[VMRow],
    *,
    registry: Registry | None = None,
) -> list[tuple[VMRow, VMPlatform]]:
    """Multi-VM :func:`bind_platform`: one registry build (lazy, so an
    empty VM set stays a no-op), one bound platform per distinct SITE
    shared across its VMs (a platform instance is site-bound),
    deduplicated by VM name, preserving first-encounter order. ONE
    resolver spans the whole batch: every site's platform preflights,
    then a single resolve pass covers the union of their declared
    secrets; prompt-once holds across sites, not just within one.
    Feed the result to :func:`keep_actives`.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.secrets.resolver import Resolver

    seen: set[str] = set()
    by_site: dict[str, VMPlatform] = {}
    pairs: list[tuple[VMRow, VMPlatform]] = []
    resolver: Resolver | None = None
    for vm in vms:
        if vm.name in seen:
            continue
        seen.add(vm.name)
        if registry is None:
            registry = build_registry(config)
        if resolver is None:
            resolver = Resolver(config, registry)
        if vm.site not in by_site:
            by_site[vm.site] = bind_platform(
                config, vm, registry=registry, resolver=resolver, prepare=False
            )
        pairs.append((vm, by_site[vm.site]))
    # The batch's preflight boundary: every distinct site's platform
    # preflights, then the one resolve pass for the union.
    for platform in by_site.values():
        platform.preflight()
    if resolver is not None:
        resolver.resolve()
    return pairs


def ensure_active(
    db: Database, config: Config, vm: VMRow, platform: VMPlatform
) -> None:
    """Respect a manual stop; otherwise start on demand.

    Fast path: a Tailscale reachability probe (cheap, no cloud API)
    short-circuits the common case, keeping backend round trips off the
    per-op hot path, EXCEPT when the row already says manually
    stopped: pinging a stopped VM burns the probe's full timeout just
    to reach the refusal, so the likely-stopped case asks the backend
    directly (an out-of-band start still proceeds via the observed
    RUNNING). ``platform`` is the BOUND platform from the caller's
    composition root (:func:`bind_platform`).
    """
    if (
        not vm.operator_stopped
        and vm.tailscale_host
        and _is_tailscale_reachable(vm.tailscale_host)
    ):
        return
    observed = platform.status(vm)
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
        platform.start(vm)
        # Hold while tailscaled reattaches: a freshly booted WSL2
        # distro must not idle out during the handshake wait.
        with platform.vm_active(vm, config=config):
            _ensure_tailscale(db, config, vm, platform)
    # RUNNING or UNKNOWN: proceed. A transient status failure must not
    # trigger a spurious start; the op will surface the real error.


@contextlib.contextmanager
def keep_active(
    db: Database, config: Config, vm: VMRow, platform: VMPlatform
) -> Iterator[None]:
    """Gate (:func:`ensure_active`), then hold (``vm_active``) for the
    context's duration.

    Takes the BOUND platform from the composition root: binding may
    need resolved config secrets, which only the composition root's
    single resolve pass has. WSL2's ``vm_active`` spawns a keepalive
    subprocess anchoring the distro against ``vmIdleTimeout``; the
    other platforms' default hold is a no-op.
    """
    ensure_active(db, config, vm, platform)
    with platform.vm_active(vm, config=config):
        yield


@contextlib.contextmanager
def keep_actives(
    db: Database,
    config: Config,
    pairs: Iterable[tuple[VMRow, VMPlatform]],
) -> Iterator[None]:
    """Multi-VM :func:`keep_active` over ``(vm, bound platform)`` pairs
    (from :func:`bind_platforms`), entered via ``ExitStack`` so a
    command touching multiple VMs (``session list --status``,
    ``session stop --all``) keeps all of them anchored.
    """
    with contextlib.ExitStack() as stack:
        for vm, platform in pairs:
            stack.enter_context(keep_active(db, config, vm, platform))
        yield


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str,
    template: str | None = None,
    site: str | None = None,
    cpus: int | None = None,
    memory: int | None = None,
    disk: int | None = None,
    azure_vm_size: str | None = None,
    admin_username: str | None = None,
) -> None:
    """Create a new VM: provision + initialize."""

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

    # Resolve resource settings: CLI flag > template > built-in default
    resolved_cpus = cpus if cpus is not None else vm_tmpl.cpus
    resolved_memory = memory if memory is not None else vm_tmpl.memory
    resolved_disk = disk if disk is not None else vm_tmpl.disk
    resolved_azure_size = azure_vm_size or vm_tmpl.azure_vm_size
    from agentworks.resources.access import admin_template

    admin = admin_template(registry)
    resolved_admin_username = admin_username or admin.username
    validate_admin_username(resolved_admin_username)

    verify_tailscale_available()
    from agentworks.secrets.resolver import Resolver

    # Construct the git-credential providers against the operation's
    # resolver up front, so their token secrets join the one boundary
    # resolve below (and each can verify() its token afterward).
    resolver = Resolver(config, registry)
    providers = resolve_git_credential_providers(
        registry, admin.git_credentials, resolver
    )
    announce_git_credentials(providers)

    # System slug: first interactive create prompts once (a blank
    # answer is final; see _resolve_system_slug). Runs before any
    # secret prompting or state mutation so an aborted slug entry
    # leaves nothing behind.
    slug = _resolve_system_slug(db)

    # The capability composition root: construct the site's platform
    # against the operation's resolver (cheap; the site's config secrets
    # register, nothing resolves yet), preflight every participating
    # resource: the vm-template predicts its Tailscale key can resolve
    # (the key is the template's responsibility, not the site's) and the
    # platform checks its world; then run the operation's ONE resolve
    # pass at the preflight boundary: tailscale auth, git-credential
    # tokens, and the site's config secrets (proxmox's API token) in a
    # single prompt session. Provisioning is hermetic: operator
    # [admin.env] / [vm_templates.*.env] secrets are NOT prompted here:
    # they're not used until runtime shells, which perform their own
    # resolve at their composition root.
    from agentworks.vms.sites import resolve_site
    from agentworks.vms.templates import preflight_vm_template

    platform_obj = resolve_site(site, registry, resolver=resolver)
    preflight_vm_template(vm_tmpl, resolver)
    platform_obj.preflight()
    for provider in providers.values():
        provider.preflight()
    output.info("Collecting credentials...")
    resolver.resolve()
    tailscale_auth_key = resolver.get(vm_tmpl.tailscale_auth_key)
    git_tokens = _git_tokens_after_resolve(config, providers, resolver)

    # The VM's OS hostname, computed once at create time and recorded on the
    # row: {slug}-{name} with a slug, the bare name without. Bounded by
    # construction: slug max 20 + dash + name max 30 = 51 characters,
    # inside the 63-char hostname-label and Azure 64-char limits.
    hostname = f"{slug}-{vm_name}" if slug else vm_name

    # Create DB record with as-provisioned resource values.
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

    # -- Provisioning --
    # If this fails, nothing was created on the remote host (or the remote
    # couldn't be reached), so we clean up the DB record.
    def _safe_delete_vm_row() -> None:
        # Best-effort rollback: a DB error here (e.g. lock contention) must
        # not mask the KeyboardInterrupt / provisioning exception that
        # triggered the rollback.
        try:
            db.delete_vm(vm_name)
        except Exception as cleanup_err:
            output.warn(
                f"rollback: failed to delete DB record for vm '{vm_name}': {cleanup_err}"
            )

    # The platform instance was bound (and preflighted, and its secrets
    # resolved) at the composition root above; dispatch is just ops now.
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
        azure_vm_size=resolved_azure_size,
    )

    try:
        result = platform_obj.create(request)
    except KeyboardInterrupt:
        output.warn(f"Cancelling vm create '{vm_name}'... rolling back.")
        _safe_delete_vm_row()
        raise
    except UserAbort:
        # No prompt lives in this span today (the boundary resolve ran
        # at the composition root above), but an operator abort must
        # never downgrade to a ProvisioningError; roll back like the
        # KeyboardInterrupt twin above.
        _safe_delete_vm_row()
        raise
    except Exception as e:
        _safe_delete_vm_row()
        raise ProvisioningError(
            f"provisioning failed: {e}",
            entity_kind="vm",
            entity_name=vm_name,
        ) from e

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
        output.info(f"VM '{vm_name}' is ready (with warnings -- see above)")
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
    """Show detailed information about a VM."""
    vm = _require_vm(db, name)

    # Bind through the site so the platform (the site's capability) and
    # the backend-side identity render polymorphically. Describe is an
    # inspection command and a stranded row is exactly the one an
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
        # if the bind below degrades.
        site_platform = site_decl.platform
        platform = bind_platform(config, vm, registry=registry)
    except UserAbort:
        # Ctrl-C at the boundary's secret prompt aborts describe too;
        # a half-report would read as the command having succeeded.
        raise
    except AgentworksError as e:
        # Inspection degrades on ANY typed bind/preflight failure (a
        # stranded site's ConfigError, a missing tool's
        # ConnectivityError, an unresolvable secret): describe is the
        # command an operator reaches for on exactly such a row, so the
        # row's own fields must still render.
        output.warn(f"{e}" + (f"\n{e.hint}" if e.hint else ""))
    else:
        # The backend reads degrade under the same discipline as the
        # bind above: a live backend flake (API hiccup, SSH timeout)
        # must not crash the report: a flaky backend is exactly when
        # an operator reaches for describe, and the row's static fields
        # still render with '-' placeholders.
        try:
            backend_label = platform.display_backend_name(vm)
            # Live observed status, paired with operator intent: a
            # manual stop reads differently from an idle timeout.
            observed = platform.status(vm)
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
) -> None:
    """Open a shell on a VM as the admin user.

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
    """
    import shlex
    import sys

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

    # The composition root: the admin shell's env-chain secrets join
    # the bind's ONE boundary resolve (site secrets + env secrets, one
    # prompt session), and the platform's preflight (missing tool,
    # stranded site, unresolvable secret) fails before any prompt. The
    # same scope dicts feed both the SecretTarget (via
    # _vm_secret_target) and compose_env so the two consumers can't
    # drift. Crucially the vm scope comes from vm.template (DB row),
    # not the config-default template, which may not match and would
    # silently route the wrong env into a shell on a
    # non-default-template VM.
    from agentworks.bootstrap import build_registry
    from agentworks.secrets.resolver import Resolver

    registry = build_registry(config)
    scopes = _resolve_vm_admin_env_scopes(registry, vm, ws=ws)
    resolver = Resolver(config, registry)
    bound = bind_platform(
        config, vm, registry=registry, resolver=resolver,
        targets=[_vm_secret_target(scopes, label=f"vm-shell={vm.name}")],
    )
    values = resolver.values

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
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
    )

    with contextlib.ExitStack() as stack:
        stack.enter_context(keep_active(db, config, vm, bound))
        target = (
            native_transport(vm, bound, config, stack=stack)
            if platform_transport
            else transport(vm, config)
        )
        if ws is not None:
            cmd = f"cd {shlex.quote(ws.workspace_path)} && exec $SHELL -l"
            sys.exit(target.interactive(cmd, env=env))
        else:
            sys.exit(target.interactive("", env=env))


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

    # The composition root: the exec env-chain secrets join the bind's
    # ONE boundary resolve (site secrets + env secrets, one prompt
    # session), after the platform's preflight. The same scope dicts
    # feed both the SecretTarget and compose_env so the two consumers
    # can't drift. The vm scope comes from vm.template (DB row), not
    # the config-default template.
    from agentworks.bootstrap import build_registry
    from agentworks.secrets.resolver import Resolver

    registry = build_registry(config)
    scopes = _resolve_vm_admin_env_scopes(registry, vm, ws=ws)
    resolver = Resolver(config, registry)
    bound = bind_platform(
        config, vm, registry=registry, resolver=resolver,
        targets=[_vm_secret_target(scopes, label=f"vm-exec={vm.name}")],
    )
    values = resolver.values

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
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        admin=scopes.admin,
    )

    target = transport(vm, config)
    remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
    if ws is not None:
        remote_cmd = f"cd {shlex.quote(ws.workspace_path)} && {remote_cmd}"
    with keep_active(db, config, vm, bound):
        return target.call_streaming(remote_cmd, env=env)


def add_git_credential(db: Database, config: Config, name: str, credential_name: str) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.bootstrap import build_registry
    from agentworks.transports import transport

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

    from agentworks.resources.access import git_credential

    cred_config = git_credential(registry, credential_name)
    if cred_config is None:
        raise NotFoundError(
            f"git credential '{credential_name}' not found in config",
            entity_kind="git-credential",
            entity_name=credential_name,
        )

    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry)
    providers = resolve_git_credential_providers(
        registry, [credential_name], resolver
    )
    provider = providers[credential_name]

    entry = provider.helper_entry()
    if entry.repos or entry.owner:
        # Scoped credentials need the helper's embedded selection map
        # rebuilt -- a single-line store merge can't provide that. The
        # full-rebuild path (reinit) can.
        raise ValidationError(
            f"git credential '{credential_name}' is scoped (fine-grained "
            f"PAT); add it to the admin or agent template and run "
            f"'agw vm reinit {name}' instead of add-git-credential"
        )

    # The composition root: the credential's token secret (registered on
    # the resolver at construct) joins the bind's boundary resolve, so
    # the platform preflight runs before any prompt and the operation
    # stays one prompt session; verify() confirms the token afterward.
    bound = bind_platform(
        config, vm, registry=registry, resolver=resolver, prepare=False
    )
    bound.preflight()
    resolver.resolve()
    token = _git_tokens_after_resolve(config, providers, resolver)[credential_name]
    new_lines = provider.credential_lines(token)

    with keep_active(db, config, vm, bound):
        target = transport(vm, config)

        # Read existing credentials, filter out entries this credential
        # replaces. The key is (username, host/path): scoped github
        # lines are path-less and share the host, so a host-only key
        # would evict every github line including the scoped ones.
        result = target.run("cat ~/.git-credentials 2>/dev/null || true")
        existing = result.stdout.strip().splitlines() if result.stdout.strip() else []

        new_keys = {_credential_line_key(line) for line in new_lines} - {None}
        filtered = [e for e in existing if _credential_line_key(e) not in new_keys]

        # New (always unscoped -- see the guard above) lines go FIRST:
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


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM. Clears the operator-stopped flag so the
    ensure_active gate resumes auto-starting on demand."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    platform = bind_platform(config, vm)
    # An explicit start is operator intent, whatever the observed state:
    # clear the flag first so a crashed start doesn't leave the gate
    # refusing to auto-resume a VM the operator asked to run.
    db.set_operator_stopped(name, False)
    # Probe status and issue the start BEFORE entering the hold: the
    # WSL2 keepalive subprocess boots a stopped distro as a side effect,
    # which would make status() report RUNNING and mislabel the VM as
    # "already running". The keepalive then anchors the (now running) VM
    # through the Tailscale verification.
    status = platform.status(vm)
    if status == VMStatus.RUNNING:
        output.info(f"VM '{name}' is already running")
    else:
        platform.start(vm)

    # Tailscale verification runs inside the keepalive so a freshly booted
    # WSL2 distro doesn't idle-shut while we wait for tailscaled to come up.
    with platform.vm_active(vm, config=config):
        _ensure_tailscale(db, config, vm, platform)
    # Only emit "is ready" on the path that actually started the VM. When
    # status was already RUNNING we already said so above, and Tailscale
    # verification is usually a no-op (handshake already valid), so an
    # extra "is ready" line is just noise. On the real-work path it
    # confirms tailscaled finished its handshake.
    if status != VMStatus.RUNNING:
        output.info(f"VM '{name}' is ready")


def stop_vm(db: Database, config: Config, name: str) -> None:
    """Stop a running VM and record the operator's intent."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    platform = bind_platform(config, vm)
    # Record intent BEFORE the already-stopped short-circuit: an
    # operator stopping an already-stopped VM still means "keep it
    # stopped" (e.g. the VM idled out and they don't want the next op
    # to auto-resume it).
    db.set_operator_stopped(name, True)
    status = platform.status(vm)
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
    platform.stop(vm)
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
    """
    import ipaddress
    import shlex
    import time

    from agentworks.bootstrap import build_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.ssh import SSHError
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.transports import native_transport, transport, wait_for_reconnect
    from agentworks.vms.templates import preflight_vm_template, resolve_template

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)

    # The composition root: construct (registers the site's config
    # secrets), preflight both participating resources (the vm-template
    # predicts the new auth key can resolve; the platform checks its
    # world), then the operation's one resolve pass: the new auth key
    # and any site secret (proxmox's API token) in a single prompt
    # session. ``ignore_env`` is honored by temporarily masking the
    # env-var backend for the auth-key secret (the env-var source reads
    # ``os.environ`` at ``would_attempt`` time, so removing the var
    # skips it cleanly across BOTH the preflight prediction and the
    # resolve, and the prompt backend takes over).
    registry = build_registry(config)
    resolver = Resolver(config, registry)
    platform = bind_platform(
        config, vm, registry=registry, resolver=resolver, prepare=False
    )
    rekey_vm_tmpl = resolve_template(registry, vm.template)
    ts_decl = resolver.register_name(rekey_vm_tmpl.tailscale_auth_key)
    with _mask_env_var_backend_for(ts_decl, masked=ignore_env):
        preflight_vm_template(rekey_vm_tmpl, resolver)
        platform.preflight()
        resolver.resolve()
    ts_auth_key = resolver.get(rekey_vm_tmpl.tailscale_auth_key)

    # The running check is an op (a backend status read), so it sits
    # past the boundary: on proxmox it needs the API token.
    status = platform.status(vm)
    if status != VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' is not running (status: {status.value})",
            entity_kind="vm",
            entity_name=name,
        )

    output.info(f"Rekeying '{name}'...")

    with contextlib.ExitStack() as _stack:
        # Holds the VM in an active state for the duration of the rekey.
        # No-op for Lima/Azure/Proxmox; WSL2 anchors the distro against
        # vmIdleTimeout so per-step `time.sleep`s can't let it idle out.
        _stack.enter_context(keep_active(db, config, vm, platform))

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
        # reachability -- the old IP is definitely dead after logout)
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
    """Delete a VM, cleaning up all associated resources."""
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
    try:
        platform = bind_platform(config, vm)
    except UserAbort:
        # Ctrl-C at the boundary's secret prompt must keep the SIGINT
        # contract: abort the whole delete rather than orphaning the
        # backend VM behind a warn. (bind_platform runs preflight and
        # the resolve pass, so the prompt happens inside it.)
        raise
    except Exception as e:
        # Preflight or bind failure (unreachable API, missing tool,
        # unresolvable secret): warn and skip backend cleanup; broken
        # backends are what delete exists to clean up.
        platform = None
        hint = getattr(e, "hint", None)
        output.warn(
            f"platform binding failed, skipping backend cleanup: {e}"
            + (f"\n{hint}" if hint else "")
        )

    if platform is not None:
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
                with platform.vm_active(vm, config=config):
                    _tailscale_logout(vm, config, platform)
            except UserAbort:
                raise
            except Exception as e:
                output.warn(f"tailscale logout skipped: {e}")

        try:
            platform.delete(vm)
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

    Requires provisioning_status == complete and a valid Tailscale connection.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.transports import transport

    # build_registry runs first so framework miss-policies surface
    # before any template / DB / VM business logic.
    registry = build_registry(config)

    vm = _require_vm(db, name)

    # Construct before any secret collection: a stranded site fails
    # here with the manifest hint instead of after git-token prompts.
    # prepare=False: the boundary (preflight + the one resolve pass)
    # runs below, once the git-token declarations have joined the set.
    from agentworks.secrets.resolver import Resolver

    resolver = Resolver(config, registry)
    platform = bind_platform(
        config, vm, registry=registry, resolver=resolver, prepare=False
    )

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
    # Construct against the operation's resolver so the token secrets
    # join the one boundary resolve below.
    providers = resolve_git_credential_providers(
        registry, admin.git_credentials, resolver
    )
    announce_git_credentials(providers)

    # The preflight boundary: git tokens and any site config secret
    # (proxmox's API token) resolve in one prompt session. The
    # vm-template's Tailscale key is NOT part of reinit's planned ops
    # (a broken node's rejoin resolves it on its own conditional path),
    # so the template preflight doesn't run here.
    platform.preflight()
    for provider in providers.values():
        provider.preflight()
    resolver.resolve()
    git_tokens = _git_tokens_after_resolve(config, providers, resolver)

    # Provisioning is hermetic: no operator-env secrets are prompted at
    # reinit. They get prompted at the use site (vm shell, session
    # create, etc.) once provisioning completes.

    # Build Tailscale SSH target with logging
    from agentworks.ssh import SSHLogger

    logger = SSHLogger(name, "vm-reinit")
    for token in git_tokens.values():
        logger.add_redaction(token)
    ts_target = transport(vm, config, default_timeout=60, logger=logger)

    home = f"/home/{vm.admin_username}"

    # Outer try/finally ensures the SSH logger is closed exactly once, AFTER
    # any warning output. Matches the pattern used by agent create / reinit
    # and workspace create / rehome.
    try:
        with keep_active(db, config, vm, platform):
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


def _git_tokens_after_resolve(
    config: Config,
    providers: Mapping[str, GitCredentialProvider],
    resolver: Resolver,
) -> dict[str, str]:
    """Read each provider's resolved token from the operation's resolver
    cache, after running the provider's ``verify()`` (the authenticated
    readiness stage) unless the operator disabled it via ``[defaults]
    verify_git_tokens = false``.

    The providers must have been constructed against ``resolver`` (so
    their token secrets joined the boundary resolve), and the pass must
    already have run. A definitive rejection during ``verify`` raises
    ``TokenRejectedError``; this is safe at every call site because
    verification runs before any VM/user mutation.
    """
    if config.defaults.verify_git_tokens:
        for provider in providers.values():
            provider.verify()
    return {
        name: resolver.get(provider.secret_name)
        for name, provider in providers.items()
    }


def _collect_git_tokens(
    config: Config,
    registry: Registry,
    credential_names: Iterable[str],
) -> dict[str, str]:
    """Resolve (and verify) token values for the named git credentials on
    a self-contained resolver pass. Returns ``{credential_name:
    token_value}``.

    Used by agent create, whose git tokens resolve on their own boundary
    (the agent's other secrets resolve at their own use sites). The
    providers are constructed against a fresh resolver so their token
    secrets register, preflight predicts each is resolvable, the one
    resolve pass runs (single prompt session), and ``verify()`` confirms
    each token before it is written. Raises ``NotFoundError`` if any name
    isn't a declared credential (the framework's ``GitCredentialKind``
    normally catches this at config-load).
    """
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.initializer import resolve_git_credential_providers

    names = list(credential_names)
    if not names:
        return {}

    resolver = Resolver(config, registry)
    providers = resolve_git_credential_providers(registry, names, resolver)
    for provider in providers.values():
        provider.preflight()
    resolver.resolve()
    return _git_tokens_after_resolve(config, providers, resolver)


def _lookup_or_synthesize_secret(registry: Registry, name: str) -> SecretDecl:
    """Return the ``SecretDecl`` for ``name`` from the framework
    Registry, or synthesize a bare one matching the auto-declare shape
    if no Resource was published or auto-declared under that name.

    Used by the Tailscale eager-resolve sites (``_collect_secrets`` for
    ``vm create``, ``rekey_vm``, ``_ensure_tailscale``). All three need
    the same fallback semantics: an operator who omits every
    ``[vm_templates.*]`` section AND every ``[secrets.*]`` section
    leaves the registry empty under the ``secret`` kind, so a strict
    lookup raises ``KeyError``. Synthesizing a bare ``SecretDecl`` (the
    same shape ``_SecretKind.synthesize`` would produce, minus
    ``origin`` which resolution doesn't read) keeps the backend chain
    callable. The Phase 2a ``VMTemplateKind`` will publish the default
    template's references and make this fallback redundant for the
    common case.
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
) -> None:
    """Forward one or more local ports to a VM via SSH tunnels.

    Each port spec is either REMOTE_PORT (local defaults to same) or
    LOCAL_PORT:REMOTE_PORT, matching kubectl port-forward syntax.
    """
    import signal
    import subprocess
    import sys

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
    with keep_active(db, config, vm, bind_platform(config, vm)):
        try:
            proc = subprocess.Popen(ssh_cmd)

            # Forward SIGINT/SIGTERM to the SSH process for clean shutdown
            def _handle_signal(sig: int, _frame: object) -> None:
                proc.terminate()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

            rc = proc.wait()
            sys.exit(rc)
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
) -> None:
    """After starting a VM, verify Tailscale connectivity and rejoin if
    needed. ``platform`` is the caller's bound platform (the gates never
    bind, and a re-bind here would re-run the resolve pass).
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
