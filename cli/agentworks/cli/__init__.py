"""Typer CLI entrypoint for Agentworks.

The package is split into:

* ``_app``           -- root Typer instance, global flags, interactivity gate
* ``_helpers``       -- shared resolver helpers (get_db, prompt_vm, ...)
* ``_typer_output``  -- TyperHandler implementing the output.OutputHandler protocol
* ``_errors``        -- error-rendering helpers used by the entrypoint
* ``_entry``         -- ``main()`` -- installs the handler and routes exceptions
* ``commands/``      -- one module per Typer subapp (vm, workspace, agent, ...)

Importing the package wires every subapp onto the root ``app`` via the
``commands`` package's side-effect imports. The names below are re-exported
for backward compatibility with existing callers (tests, scripts entry).
"""

from __future__ import annotations

# Trigger registration of every command group.
from agentworks.cli import commands as _commands  # noqa: F401
from agentworks.cli._app import app
from agentworks.cli._entry import main
from agentworks.cli._errors import record_unhandled_error as _record_unhandled_error
from agentworks.cli.commands.completion import _resolve_shell

__all__ = [
    "app",
    "main",
    "_record_unhandled_error",
    "_resolve_shell",
]
