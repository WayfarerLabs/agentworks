from __future__ import annotations

import re
import shlex
from pathlib import Path

from agentworks import __file__ as agentworks_file
from agentworks.cli._app import app
from agentworks.completions.spec import CommandSpec, build_spec

_INLINE_COMMAND = re.compile(r"`(agw(?:\s+[^`]+)?)`")
_GENERIC_SEGMENTS = frozenset({"COMMAND", "GROUP"})


def _authored_commands(path: Path) -> set[str]:
    markdown = path.read_text(encoding="utf-8")
    commands = {" ".join(command.split()) for command in _INLINE_COMMAND.findall(markdown)}
    fence: str | None = None
    for line in markdown.splitlines():
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
        elif fence in {"bash", "sh", "shell"} and line.startswith("agw "):
            commands.add(line)
    return commands


def _validate_command(command: str, root: CommandSpec) -> str | None:
    tokens = shlex.split(command)
    current = root
    path = [root]
    index = 1
    while index < len(tokens) and current.subcommands:
        token = tokens[index]
        if token.startswith("-"):
            break
        if token in _GENERIC_SEGMENTS:
            return None
        if token not in current.subcommands:
            return f"unknown command segment {token!r}"
        current = current.subcommands[token]
        path.append(current)
        index += 1

    options = {option for spec in path for param in spec.params for option in param.opts}
    options.add("--help")
    for token in tokens[1:]:
        if token.startswith("-") and token != "--":
            option = token.partition("=")[0]
            if option not in options:
                return f"unknown option {option!r}"
    return None


def test_authored_agw_command_examples_match_the_cli_spec() -> None:
    package_root = Path(agentworks_file).parent
    spec = build_spec(app)
    checked = 0

    for path in sorted(package_root.rglob("guide-content/*.md")):
        for command in sorted(_authored_commands(path)):
            problem = _validate_command(command, spec)
            assert problem is None, f"{path.relative_to(package_root)}: {command!r}: {problem}"
            checked += 1

    assert checked > 0
