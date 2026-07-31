"""Shared low-level helpers for the ``vms.manager`` package.

Leaf module: everything here is self-contained (no imports from sibling
``manager`` submodules), so it carries zero risk of import cycles and is
safe for every other submodule to depend on.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, NamedTuple

from agentworks import output
from agentworks.db import SYSTEM_SLUG_KEY
from agentworks.errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.base import SecretDecl


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

    return _VmAdminEnvScopes(
        vm=vm_env,
        workspace=ws_env,
        admin=admin_template(registry, vm.admin_template or "default").env,
    )


def _vm_secret_target(
    scopes: _VmAdminEnvScopes,
    *,
    label: str,
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
    db: Database,
    vm: VMRow,
    workspace_name: str | None,
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
    from agentworks.db import InitStatus, ProvisioningStatus
    from agentworks.errors import StateError

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
