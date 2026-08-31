"""VM lifecycle management: create, list, start, stop, delete.

This package preserves the flat ``agentworks.vms.manager`` import surface
that predates the split into submodules (``_helpers``, ``boundary``,
``tailscale``, ``lifecycle``, ``inspect``, ``power``, ``exec``): every name below is
re-exported here so ``from agentworks.vms.manager import create_vm`` (and
the many ``agentworks.vms.manager.<name>`` attribute / monkeypatch
references across the codebase and test suite) keep working unchanged.

The four names imported from ``agentworks.vms.initializer`` are the
canonical entry point into that package from ``manager``: submodules that
call them (``lifecycle.py``'s ``create_vm`` / ``reinit_vm``,
``tailscale.py``'s ``_ensure_tailscale``) do so via
``import agentworks.vms.manager as _mgr`` at call time rather than
importing these names directly, so that tests which monkeypatch e.g.
``agentworks.vms.manager.verify_tailscale_available`` affect every
internal caller, not just whichever submodule happened to hold its own
copy of the import.
"""

from __future__ import annotations

from agentworks.vms.initializer import (
    VMInitializationOperation,
    bootstrap_vm,
    rejoin_tailscale,
    run_initialization,
    verify_tailscale_available,
)

from ._helpers import (
    _SLUG_PROMPT,
    _guard_failed_vm,
    _human_bytes,
    _init_log_hint,
    _lookup_or_synthesize_secret,
    _mask_env_var_backend_for,
    _query_live_resources,
    _require_vm,
    _resolve_system_slug,
    _resolve_vm_admin_env_scopes,
    _resolve_workspace_for_vm,
    _vm_scope,
    _vm_secret_target,
    _VmAdminEnvScopes,
    validate_slug,
)
from .boundary import (
    _live_vm_boundary,
    gated_vm_boundary,
    gated_vm_platform_recovery_boundary,
    require_vm_ssh_boundary,
)
from .checkpoints import (
    checkpoint_listing_data,
    create_checkpoint,
    create_upgrade_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    render_checkpoint_listing,
    require_upgrade_checkpoint,
    restore_checkpoint,
)
from .exec import exec_vm, shell_vm
from .inspect import (
    describe_vm,
    list_vms,
    render_vm_listing,
    vm_description,
    vm_listing,
)
from .lifecycle import create_vm, reinit_vm
from .power import delete_vm, rekey_vm, start_vm, stop_vm
from .release import verified_vm_release
from .tailscale import (
    _ensure_tailscale,
    _is_tailscale_reachable,
    _tailscale_logout,
    _tailscale_rejoin_required,
    _warned_tailscale_missing,
    port_forward_vm,
)
from .upgrade import upgrade_vm
from .verification import VMConnectionVerification, verify_vm_connection

__all__ = [
    "_SLUG_PROMPT",
    "_VmAdminEnvScopes",
    "_ensure_tailscale",
    "_guard_failed_vm",
    "_human_bytes",
    "_init_log_hint",
    "_is_tailscale_reachable",
    "_tailscale_rejoin_required",
    "_live_vm_boundary",
    "_lookup_or_synthesize_secret",
    "_mask_env_var_backend_for",
    "_query_live_resources",
    "_require_vm",
    "_resolve_system_slug",
    "_resolve_vm_admin_env_scopes",
    "_resolve_workspace_for_vm",
    "_tailscale_logout",
    "_vm_scope",
    "_vm_secret_target",
    "_warned_tailscale_missing",
    "bootstrap_vm",
    "checkpoint_listing_data",
    "create_checkpoint",
    "create_vm",
    "create_upgrade_checkpoint",
    "delete_checkpoint",
    "delete_vm",
    "describe_vm",
    "exec_vm",
    "gated_vm_boundary",
    "gated_vm_platform_recovery_boundary",
    "list_vms",
    "list_checkpoints",
    "port_forward_vm",
    "reinit_vm",
    "rejoin_tailscale",
    "rekey_vm",
    "render_vm_listing",
    "render_checkpoint_listing",
    "require_upgrade_checkpoint",
    "require_vm_ssh_boundary",
    "run_initialization",
    "restore_checkpoint",
    "shell_vm",
    "start_vm",
    "stop_vm",
    "validate_slug",
    "verify_vm_connection",
    "verify_tailscale_available",
    "verified_vm_release",
    "VMConnectionVerification",
    "upgrade_vm",
    "VMInitializationOperation",
    "vm_description",
    "vm_listing",
]
