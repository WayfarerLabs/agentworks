"""Introspect the Typer/Click command tree and build a completion spec."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import click
import typer

# -- Data model ------------------------------------------------------------


@dataclass
class ParamSpec:
    """Specification for a single CLI parameter."""

    name: str
    opts: list[str]
    help: str
    is_flag: bool
    is_argument: bool
    multiple: bool
    required: bool
    choices: list[str] | None = None
    dynamic_completer: str | None = None


@dataclass
class CommandSpec:
    """Specification for a CLI command or group."""

    name: str
    help: str
    params: list[ParamSpec] = field(default_factory=list)
    subcommands: dict[str, CommandSpec] = field(default_factory=dict)


# -- Dynamic completions (the only hand-maintained piece) ------------------

# Maps (dotted_command_path, param_name) to a completer identifier.
# Completer identifiers are abstract labels that each shell generator
# knows how to render into shell-specific completion functions.
#
# The completer identifiers and their corresponding CLI commands:
#   "vms"             -> agentworks vm list
#   "vm_hosts"        -> agentworks vm-host list
#   "workspaces"      -> agentworks workspace list
#   "ws_templates"    -> [workspace_templates.*] sections in config.toml
#   "git_credentials" -> [git_credentials.*] sections in config.toml
#   "catalog_entries" -> all entry names from built-in + custom catalog
#   "sessions"        -> agentworks session list --no-status
#   "session_templates" -> [session_templates.*] sections in config.toml
#   "agents"          -> agentworks agent list
#   "vm_templates"    -> [vm_templates.*] sections in config.toml
#   "agent_templates" -> [agent_templates.*] sections in config.toml
#   "consoles"        -> agentworks console list

DYNAMIC_COMPLETIONS: dict[tuple[str, str], str] = {
    ("vm.start", "name"): "vms",
    ("vm.stop", "name"): "vms",
    ("vm.delete", "name"): "vms",
    ("vm.rekey", "name"): "vms",
    ("vm.backup", "name"): "vms",
    ("vm.describe", "name"): "vms",
    ("vm.reinit", "name"): "vms",
    ("vm.exec", "name"): "vms",
    ("vm.shell", "name"): "vms",
    ("vm.add-git-credential", "name"): "vms",
    ("vm.port-forward", "name"): "vms",
    ("vm.create", "template"): "vm_templates",
    ("vm.create", "vm_host"): "vm_hosts",
    ("vm-host.remove", "name"): "vm_hosts",
    ("workspace.shell", "name"): "workspaces",
    ("workspace.console", "name"): "workspaces",
    ("workspace.copy", "source"): "workspaces",
    ("workspace.copy", "vm"): "vms",
    ("workspace.describe", "name"): "workspaces",
    ("workspace.rehome", "name"): "workspaces",
    ("workspace.reinit", "name"): "workspaces",
    ("workspace.delete", "name"): "workspaces",
    ("workspace.create", "vm"): "vms",
    ("workspace.create", "template"): "ws_templates",
    ("workspace.list", "vm"): "vms",
    ("vm.logs", "name"): "vms",
    ("vm.add-git-credential", "credential"): "git_credentials",
    ("agent.create", "vm"): "vms",
    ("agent.create", "template"): "agent_templates",
    ("agent.describe", "name"): "agents",
    ("agent.reinit", "name"): "agents",
    ("agent.grant-workspace", "name"): "agents",
    ("agent.grant-workspace", "workspaces"): "workspaces",
    ("agent.revoke-workspace", "name"): "agents",
    ("agent.revoke-workspace", "workspaces"): "workspaces",
    ("agent.exec", "name"): "agents",
    ("agent.shell", "name"): "agents",
    ("agent.shell", "workspace"): "workspaces",
    ("agent.delete", "name"): "agents",
    ("agent.list", "vm"): "vms",
    ("catalog.describe", "name"): "catalog_entries",
    # Session commands
    ("session.create", "agent"): "agents",
    ("session.create", "workspace"): "workspaces",
    ("session.create", "template"): "session_templates",
    ("session.create", "workspace_template"): "ws_templates",
    ("session.create", "vm"): "vms",
    ("session.describe", "name"): "sessions",
    ("session.list", "workspace"): "workspaces",
    ("session.stop", "name"): "sessions",
    ("session.stop", "vm"): "vms",
    ("session.stop", "workspace"): "workspaces",
    ("session.restart", "name"): "sessions",
    ("session.restart", "vm"): "vms",
    ("session.restart", "workspace"): "workspaces",

    ("session.attach", "name"): "sessions",
    ("session.delete", "name"): "sessions",
    ("session.logs", "name"): "sessions",

    # VM console
    ("vm.console", "name"): "vms",

    # Named consoles
    ("console.create", "vm"): "vms",
    ("console.create", "sessions"): "sessions",
    ("console.list", "vm"): "vms",
    ("console.describe", "name"): "consoles",
    ("console.attach", "name"): "consoles",
    ("console.delete", "name"): "consoles",
    ("console.add-session", "name"): "consoles",
    ("console.add-session", "sessions"): "sessions",
    ("console.remove-session", "name"): "consoles",
    ("console.remove-session", "sessions"): "sessions",
    ("console.add-shell", "name"): "consoles",
    ("console.add-shell", "session"): "sessions",
    ("console.restore-session", "name"): "consoles",
    ("console.restore-session", "session"): "sessions",
}


# -- Introspection ---------------------------------------------------------


def build_spec(app: typer.Typer) -> CommandSpec:
    """Walk the Typer app and build a CommandSpec tree."""
    click_app = typer.main.get_command(app)
    return _build_command_spec(click_app, path="")


def _build_command_spec(cmd: click.Command, path: str) -> CommandSpec:
    """Recursively build a CommandSpec from a Click command."""
    help_text = (cmd.help or "").split("\n")[0].strip()
    name = cmd.name or ""

    spec = CommandSpec(name=name, help=help_text)

    # Build params
    current_path = f"{path}.{name}" if path else name
    for param in cmd.params:
        if param.name == "help" or getattr(param, "hidden", False):
            continue
        spec.params.append(_build_param_spec(param, current_path))

    # Build subcommands for groups
    if isinstance(cmd, click.Group):
        ctx = click.Context(cmd, info_name=name)
        for sub_name in cmd.list_commands(ctx):
            sub_cmd = cmd.get_command(ctx, sub_name)
            if sub_cmd is not None:
                spec.subcommands[sub_name] = _build_command_spec(sub_cmd, path=current_path)

    return spec


def _build_param_spec(param: click.Parameter, command_path: str) -> ParamSpec:
    """Build a ParamSpec from a Click parameter."""
    is_argument = isinstance(param, click.Argument)

    choices = None
    if isinstance(param.type, click.Choice):
        choices = list(param.type.choices)

    opts: list[str] = []
    if isinstance(param, click.Option):
        opts = list(param.opts)

    # DYNAMIC_COMPLETIONS keys use paths without the root app name
    # (e.g. "vm.shell" not "agentworks.vm.shell")
    lookup_path = ".".join(command_path.split(".")[1:]) if "." in command_path else command_path
    dynamic = DYNAMIC_COMPLETIONS.get((lookup_path, param.name or ""))

    # Click models variadic Arguments via `nargs=-1` (not `multiple`), and
    # `multiple=True` on Options. Normalize both into ParamSpec.multiple so
    # completion generators have a single "accepts more than one value" flag.
    accepts_multi = bool(param.multiple) or (is_argument and param.nargs == -1)

    return ParamSpec(
        name=param.name or "",
        opts=opts,
        help=getattr(param, "help", None) or "",
        is_flag=getattr(param, "is_flag", False),
        is_argument=is_argument,
        multiple=accepts_multi,
        required=param.required,
        choices=choices,
        dynamic_completer=dynamic,
    )


# -- Version stamp ---------------------------------------------------------


def completion_version(spec: CommandSpec) -> str:
    """Compute a content hash of the spec for staleness detection."""
    serialized = json.dumps(_spec_to_dict(spec), sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


def _spec_to_dict(spec: CommandSpec) -> dict:  # type: ignore[type-arg]
    """Serialize a CommandSpec tree to a dict for hashing."""
    return {
        "name": spec.name,
        "help": spec.help,
        "params": [
            {
                "name": p.name,
                "opts": p.opts,
                "is_flag": p.is_flag,
                "is_argument": p.is_argument,
                "multiple": p.multiple,
                "required": p.required,
                "choices": p.choices,
                "dynamic_completer": p.dynamic_completer,
            }
            for p in spec.params
        ],
        "subcommands": {k: _spec_to_dict(v) for k, v in sorted(spec.subcommands.items())},
    }
