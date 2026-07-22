"""Session lifecycle orchestration.

This package was split from a single ``sessions/manager.py`` module (once the
largest module in the repo). The public import path
``agentworks.sessions.manager`` is preserved: the service-layer functions and
the handful of privates the test suite imports are re-exported here.

Internal submodules reach cross-module helpers through this package object
(``import agentworks.sessions.manager as _mgr`` then ``_mgr.NAME(...)``) rather
than binding them with ``from ... import NAME``. The reads are deferred to call
time, so the tests' monkeypatches on this namespace (for example
``agentworks.sessions.manager.transport`` and
``agentworks.sessions.manager._require_workspace``) intercept the internal call
paths, and the partially-initialized package during import is never a problem.
"""

from __future__ import annotations

from agentworks.transports import transport as transport

from ._create import (
    create_session as create_session,
)
from ._env import (
    _display_harness as _display_harness,
)
from ._env import (
    _display_registry as _display_registry,
)
from ._env import (
    _resolve_session_env as _resolve_session_env,
)
from ._env import (
    _resolve_session_env_scopes as _resolve_session_env_scopes,
)
from ._env import (
    _resolve_template as _resolve_template,
)
from ._env import (
    _session_secret_target as _session_secret_target,
)
from ._env import (
    _session_secret_target_pre_create as _session_secret_target_pre_create,
)
from ._env import (
    _substitute_template_vars as _substitute_template_vars,
)
from ._env import (
    _substitute_template_vars_in_env as _substitute_template_vars_in_env,
)
from ._lifecycle import (
    _execute_stop as _execute_stop,
)
from ._lifecycle import (
    restart_all_sessions as restart_all_sessions,
)
from ._lifecycle import (
    restart_session as restart_session,
)
from ._lifecycle import (
    stop_all_sessions as stop_all_sessions,
)
from ._lifecycle import (
    stop_session as stop_session,
)
from ._logs import (
    session_logs as session_logs,
)
from ._pids import (
    _build_session_target as _build_session_target,
)
from ._pids import (
    _ensure_pid as _ensure_pid,
)
from ._pids import (
    _kill_session as _kill_session,
)
from ._pids import (
    _needs_repair as _needs_repair,
)
from ._pids import (
    _repair_session_pid as _repair_session_pid,
)
from ._pids import (
    _resolve_session_linux_user as _resolve_session_linux_user,
)
from ._pids import (
    ensure_pids_batch as ensure_pids_batch,
)
from ._prompts import (
    _prompt_mode_choice as _prompt_mode_choice,
)
from ._prompts import (
    _prompt_vm as _prompt_vm,
)
from ._prompts import (
    _prompt_workspace_choice as _prompt_workspace_choice,
)
from ._queries import (
    attach_session as attach_session,
)
from ._queries import (
    delete_session as delete_session,
)
from ._queries import (
    describe_session as describe_session,
)
from ._queries import (
    list_sessions as list_sessions,
)
from ._scope import (
    _batch_vm_boundary as _batch_vm_boundary,
)
from ._scope import (
    _distinct_vms_for_sessions as _distinct_vms_for_sessions,
)
from ._scope import (
    _prepare_vm as _prepare_vm,
)
from ._scope import (
    _regenerate_tmuxinator as _regenerate_tmuxinator,
)
from ._scope import (
    _require_session as _require_session,
)
from ._scope import (
    _require_vm_for_workspace as _require_vm_for_workspace,
)
from ._scope import (
    _require_workspace as _require_workspace,
)
from ._scope import (
    _session_scope as _session_scope,
)
from ._scope import (
    filter_sessions as filter_sessions,
)
from ._status import (
    _check_dedicated_session as _check_dedicated_session,
)
from ._status import (
    _get_boot_id as _get_boot_id,
)
from ._status import (
    _pid_alive as _pid_alive,
)
from ._status import (
    batch_check_all_sessions as batch_check_all_sessions,
)
from ._status import (
    batch_check_status as batch_check_status,
)
from ._status import (
    check_session_status as check_session_status,
)

__all__ = [
    "create_session",
    "describe_session",
    "list_sessions",
    "stop_session",
    "stop_all_sessions",
    "restart_session",
    "restart_all_sessions",
    "delete_session",
    "attach_session",
    "session_logs",
    "check_session_status",
    "batch_check_status",
    "batch_check_all_sessions",
    "ensure_pids_batch",
    "filter_sessions",
    "_ensure_pid",
    "_resolve_template",
    "_session_secret_target",
    "_session_secret_target_pre_create",
    "_require_workspace",
    "transport",
]
