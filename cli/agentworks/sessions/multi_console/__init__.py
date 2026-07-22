"""Named consoles with explicit session lists.

A console is a named tmux session on a VM that aggregates a curated subset of
the VM's sessions as windows, with optional extra shell panes per session
window. Unlike the legacy vm-console (one per VM, holds all sessions), a
console is created explicitly with a chosen set of sessions and can be
attached, modified, or deleted independently.

This is a package rather than a single module because the implementation
outgrew a single 500-line file. The submodules are organized by concern:

- ``_helpers``: constants, spec parsing, and read-only DB lookups shared by
  everything else.
- ``crud``: DB-level create/add/remove/reorder/delete plus their live-tmux
  best-effort sync.
- ``secrets_env``: eager-prompting SecretTarget builders and pane env
  composition.
- ``tmux_build``: tmux-side mechanics (splitting a pane, adding a window,
  building a console from scratch).
- ``restore``: reconciling a session window's live tmux state against its
  configured shell list.
- ``attach``: live-tmux probing, the attach loop, and the high-level
  attach/delete/list/describe entrypoints.

Every name below is re-exported here so ``agentworks.sessions.multi_console``
keeps working as the single public import path this package replaces
(including the private names tests monkeypatch directly on this module --
see each submodule's docstring for why calls route back through this
package object rather than a direct cross-submodule import).
"""

from __future__ import annotations

from ._helpers import (
    ADMIN_SHELL_WINDOW,
    TMUX_PREFIX,
    SessionSpec,
    default_shells,
    infer_vm_from_session_specs,
    parse_session_spec,
    running_session_names,
    tmux_session_name,
)
from .attach import (
    _attach_loop_wrapper,
    _console_tmux_exists,
    _kill_console_tmux,
    _live_best_effort,
    _live_target,
    _prepare_vm_target_for_attach,
    _session_linux_user,
    attach_console,
    delete_console,
    describe_console,
    kill_session_windows,
    list_consoles,
)
from .crud import (
    _validate_cwd,
    add_sessions,
    add_shell,
    create_console,
    delete_console_record,
    remove_sessions,
    reorder_sessions,
)
from .restore import restore_session
from .secrets_env import (
    _SUDO_PRESERVE_PROBE_VAR,
    _admin_only_secret_target,
    _console_build_secret_targets,
    _pane_secret_target,
    _resolve_pane_env,
    _restore_session_secret_targets,
)
from .tmux_build import (
    PreserveEnvMemo,
    _add_session_window,
    _build_console_tmux,
    _resolve_workspace_path,
    _split_shell_pane,
    _sudo_can_preserve_env,
)

__all__ = [
    "ADMIN_SHELL_WINDOW",
    "PreserveEnvMemo",
    "SessionSpec",
    "TMUX_PREFIX",
    "_SUDO_PRESERVE_PROBE_VAR",
    "_add_session_window",
    "_admin_only_secret_target",
    "_attach_loop_wrapper",
    "_build_console_tmux",
    "_console_build_secret_targets",
    "_console_tmux_exists",
    "_kill_console_tmux",
    "_live_best_effort",
    "_live_target",
    "_pane_secret_target",
    "_prepare_vm_target_for_attach",
    "_resolve_pane_env",
    "_resolve_workspace_path",
    "_restore_session_secret_targets",
    "_session_linux_user",
    "_split_shell_pane",
    "_sudo_can_preserve_env",
    "_validate_cwd",
    "add_sessions",
    "add_shell",
    "attach_console",
    "create_console",
    "default_shells",
    "delete_console",
    "delete_console_record",
    "describe_console",
    "infer_vm_from_session_specs",
    "kill_session_windows",
    "list_consoles",
    "parse_session_spec",
    "remove_sessions",
    "reorder_sessions",
    "restore_session",
    "running_session_names",
    "tmux_session_name",
]
