"""VM lifecycle management: create, list, start, stop, delete.

This package preserves the flat ``agentworks.vms.manager`` import surface
that predates the split into submodules (``_helpers``, ``boundary``,
``tailscale``, ``lifecycle``, ``power``, ``exec``): every name below is
re-exported here so ``from agentworks.vms.manager import create_vm`` (and
the many ``agentworks.vms.manager.<name>`` attribute / monkeypatch
references across the codebase and test suite) keep working unchanged.

The five names imported from ``agentworks.vms.initializer`` are the
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
    announce_git_credentials,
    bootstrap_vm,
    rejoin_tailscale,
    run_initialization,
    verify_tailscale_available,
)

from ._helpers import (
    _SLUG_PROMPT,
    _credential_line_key,
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
from .boundary import _live_vm_boundary, gated_vm_boundary
from .exec import add_git_credential, exec_vm, shell_vm
from .lifecycle import create_vm, reinit_vm
from .power import delete_vm, describe_vm, list_vms, rekey_vm, start_vm, stop_vm
from .tailscale import (
    _ensure_tailscale,
    _is_tailscale_reachable,
    _tailscale_logout,
    _warned_tailscale_missing,
    port_forward_vm,
)

__all__ = [
    "_SLUG_PROMPT",
    "_VmAdminEnvScopes",
    "_credential_line_key",
    "_ensure_tailscale",
    "_guard_failed_vm",
    "_human_bytes",
    "_init_log_hint",
    "_is_tailscale_reachable",
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
    "add_git_credential",
    "announce_git_credentials",
    "bootstrap_vm",
    "create_vm",
    "delete_vm",
    "describe_vm",
    "exec_vm",
    "gated_vm_boundary",
    "list_vms",
    "port_forward_vm",
    "reinit_vm",
    "rejoin_tailscale",
    "rekey_vm",
    "run_initialization",
    "shell_vm",
    "start_vm",
    "stop_vm",
    "validate_slug",
    "verify_tailscale_available",
]
