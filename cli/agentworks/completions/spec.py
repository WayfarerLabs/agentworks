"""Introspect the Typer/Click command tree and build a completion spec.

typer 0.26 vendored click into ``typer._click``; ``typer.main.get_command()``
now returns objects whose classes are ``typer._click.core.{Command,Group}``,
not the real ``click.core.*``. isinstance-checks against real click classes
therefore silently return ``False``, producing an empty spec tree.

To avoid coupling to either variant we duck-type against the surface both
provide (``.name`` / ``.params`` / ``.commands`` / ``.opts`` /
``.param_type_name`` / etc.) via the Protocol types below.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import typer

# -- Structural types for click-like objects ------------------------------
#
# Both real click and typer's vendored ``typer._click`` conform to these.


class _ClickParameter(Protocol):
    name: str | None
    opts: list[str]
    multiple: bool
    nargs: int
    required: bool
    type: Any  # click ParamType; Choice subtype exposes ``.choices``


class _ClickCommand(Protocol):
    name: str | None
    help: str | None
    params: list[Any]


class _ClickGroup(_ClickCommand, Protocol):
    commands: dict[str, _ClickCommand]


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
#   "vms"             -> agw vm list --names-only
#   "vm_hosts"        -> agw vm-host list --names-only
#   "workspaces"      -> agw workspace list --names-only
#   "ws_templates"    -> agw resource list --kind workspace-template --names-only
#   "git_credentials" -> agw resource list --kind git-credential --names-only
#   "sessions"        -> agw session list --names-only
#   "session_templates" -> agw resource list --kind session-template --names-only
#   "agents"          -> agw agent list --names-only
#   "vm_templates"    -> agw resource list --kind vm-template --names-only
#   "agent_templates" -> agw resource list --kind agent-template --names-only
#   "consoles"        -> agw console list --names-only
#   "secrets"         -> agw secret list --names-only
#                        (sources from the Resource Registry so
#                        auto-declared names like tailscale-auth-key
#                        complete the same as operator-declared ones)
#   "resource_kinds"  -> agw resource kinds --names-only
#                        (one kind per line, straight from KIND_REGISTRY;
#                        no config or registry build, so this completer
#                        works even with a broken config)
#   "resource_refs"   -> agw resource list --names-only
#                        (kind/name per line, verbatim -- the candidate
#                        IS the token for `resource describe KIND/NAME`)
#   "migrate_selectors" -> agw resource list --origin operator --names-only
#                        (a cross-product completer for `resource
#                        migrate`: each kind/name row emits BOTH the bare
#                        kind and the kind/name selector form, sort -u'd.
#                        Operator-origin includes YAML-declared rows that
#                        are already migrated; selecting one produces the
#                        clear already-migrated error, which beats adding
#                        CLI surface just to filter completion candidates.)
#
# The template + git_credentials completers source from the Resource
# Registry (via `agw resource list --kind X --names-only`) rather than
# regex-scraping `[X.*]` sections out of config.toml. The old sed-based
# approach had a subtle bug: the regex `\[X\.([^]]*)\]` greedy-matched
# sub-section headers too, so `[vm_templates.default.env]` emitted
# `default.env` as a bogus completion candidate. Registry-sourced
# completion also picks up the framework's always-materialized defaults
# and auto-declared entries the raw config text doesn't have.
#
# The per-kind Registry queries don't need `sort -u` on the shell side
# because the Registry stores one row per `(kind, name)` and the CLI's
# `--names-only` walks in insertion order -- names are already unique
# per kind. `resource_kinds` reads `agw resource kinds --names-only`
# (one kind per line, already sorted and unique). `migrate_selectors`
# still `sort -u`'s because it aggregates the kind prefix across all
# rows of the full listing.
# `/` is the parse-safe separator for the kind/name stream: it cannot
# appear in resource names (enforced at Registry.add), while `:` can.
#
# The ``--names-only`` flag is the explicit completion contract:
# every list command that backs a completer emits one name per line
# when the flag is passed, in the same order as its table rows.
# See ``.claude/rules/cli-conventions.md`` for the broader convention.
# ``agw resource list`` is the deliberate cross-kind divergence: it
# emits ``kind/name`` per line (the prefix is load-bearing -- two kinds
# can publish resources with the same name), and the completers slice
# the prefix off shell-side. The convention's "one name per line"
# spirit is preserved (one line per resource, no header or formatting).

DYNAMIC_COMPLETIONS: dict[tuple[str, str], str] = {
    ("vm.start", "name"): "vms",
    ("vm.stop", "name"): "vms",
    ("vm.delete", "name"): "vms",
    ("vm.rekey", "name"): "vms",
    ("vm.backup", "name"): "vms",
    ("vm.describe", "name"): "vms",
    ("vm.reinit", "name"): "vms",
    ("vm.exec", "name"): "vms",
    ("vm.exec", "workspace"): "workspaces",
    ("vm.shell", "name"): "vms",
    ("vm.shell", "workspace"): "workspaces",
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
    ("agent.grant-workspaces", "name"): "agents",
    ("agent.grant-workspaces", "workspaces"): "workspaces",
    ("agent.revoke-workspaces", "name"): "agents",
    ("agent.revoke-workspaces", "workspaces"): "workspaces",
    ("agent.exec", "name"): "agents",
    ("agent.exec", "workspace"): "workspaces",
    ("agent.shell", "name"): "agents",
    ("agent.shell", "workspace"): "workspaces",
    ("agent.delete", "name"): "agents",
    ("agent.list", "vm"): "vms",
    # Session commands
    ("session.create", "agent"): "agents",
    ("session.create", "workspace"): "workspaces",
    ("session.create", "template"): "session_templates",
    ("session.create", "workspace_template"): "ws_templates",
    ("session.create", "agent_template"): "agent_templates",
    ("session.create", "vm"): "vms",
    ("session.describe", "name"): "sessions",
    ("session.list", "workspace"): "workspaces",
    ("session.list", "vm"): "vms",
    ("session.list", "agent"): "agents",
    ("session.stop", "name"): "sessions",
    ("session.stop", "vm"): "vms",
    ("session.stop", "workspace"): "workspaces",
    ("session.stop", "agent"): "agents",
    ("session.restart", "name"): "sessions",
    ("session.restart", "vm"): "vms",
    ("session.restart", "workspace"): "workspaces",
    ("session.restart", "agent"): "agents",

    ("session.attach", "name"): "sessions",
    ("session.delete", "name"): "sessions",
    ("session.logs", "name"): "sessions",

    # VM console
    ("vm.console", "name"): "vms",

    # Env inspection
    ("env.show", "vm"): "vms",
    ("env.show", "workspace"): "workspaces",
    ("env.show", "agent"): "agents",
    ("env.show", "session"): "sessions",

    # Named consoles
    ("console.create", "vm"): "vms",
    ("console.create", "sessions"): "sessions",
    ("console.list", "vm"): "vms",
    ("console.list", "workspace"): "workspaces",
    ("console.list", "agent"): "agents",
    ("console.describe", "name"): "consoles",
    ("console.attach", "name"): "consoles",
    ("console.delete", "name"): "consoles",
    ("console.add-sessions", "name"): "consoles",
    ("console.add-sessions", "sessions"): "sessions",
    ("console.remove-sessions", "name"): "consoles",
    ("console.remove-sessions", "sessions"): "sessions",
    ("console.reorder-sessions", "name"): "consoles",
    ("console.reorder-sessions", "sessions"): "sessions",
    ("console.add-shell", "name"): "consoles",
    ("console.add-shell", "session"): "sessions",
    ("console.restore-session", "name"): "consoles",
    ("console.restore-session", "session"): "sessions",

    # Secret inspection
    ("secret.describe", "name"): "secrets",
    # Resource inspection (Phase 2c; describe took the single KIND/NAME
    # grammar in the display-syntax unification)
    ("resource.list", "kind"): "resource_kinds",
    ("resource.describe", "ref"): "resource_refs",
    ("resource.edit", "ref"): "resource_refs",
    # Resource migration + authoring (Phase 4). `resource sample`'s kind
    # argument is a static click.Choice (SAMPLE_KINDS), so it completes
    # via ParamSpec.choices rather than a dynamic completer.
    ("resource.migrate", "selectors"): "migrate_selectors",
}


# -- Introspection ---------------------------------------------------------


def build_spec(app: typer.Typer) -> CommandSpec:
    """Walk the Typer app and build a CommandSpec tree."""
    click_app = typer.main.get_command(app)
    return _build_command_spec(click_app, path="")


def _build_command_spec(cmd: _ClickCommand, path: str) -> CommandSpec:
    """Recursively build a CommandSpec from a Click-like command."""
    help_text = (cmd.help or "").split("\n")[0].strip()
    name = cmd.name or ""

    spec = CommandSpec(name=name, help=help_text)

    # Build params
    current_path = f"{path}.{name}" if path else name
    for param in cmd.params:
        if param.name == "help" or getattr(param, "hidden", False):
            continue
        spec.params.append(_build_param_spec(param, current_path))

    # Build subcommands for groups. Duck-type: Group has ``.commands``, plain
    # Command doesn't. We iterate the dict directly rather than through
    # ``list_commands(ctx) / get_command(ctx, name)`` because the ctx
    # construction differs between real click and typer._click, and both
    # default impls just walk this dict anyway.
    commands = getattr(cmd, "commands", None)
    if isinstance(commands, dict):
        for sub_name in sorted(commands):
            sub_cmd = commands[sub_name]
            if sub_cmd is not None:
                spec.subcommands[sub_name] = _build_command_spec(sub_cmd, path=current_path)

    return spec


def _build_param_spec(param: _ClickParameter, command_path: str) -> ParamSpec:
    """Build a ParamSpec from a Click-like parameter."""
    # ``param_type_name`` is a click class attribute set to "argument" /
    # "option" on the respective subclasses -- stable across click versions
    # and preserved by typer's vendored copy.
    param_kind = getattr(param, "param_type_name", "")
    is_argument = param_kind == "argument"
    is_option = param_kind == "option"

    choices = None
    # ``Choice`` param types expose ``.choices``; other ParamType subclasses
    # (STRING, INT, etc.) do not. Typer wraps ``click_type=`` params in its
    # ``FuncParamType``, which hides the real type on ``.func`` -- unwrap
    # one level so Choice values survive into the completion tree
    # (without this, every ``click_type=click.Choice(...)`` option
    # silently loses static completion).
    ptype = param.type
    if not hasattr(ptype, "choices"):
        inner = getattr(ptype, "func", None)
        if inner is not None and hasattr(inner, "choices"):
            ptype = inner
    if hasattr(ptype, "choices"):
        choices = list(ptype.choices)

    opts: list[str] = []
    if is_option:
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
