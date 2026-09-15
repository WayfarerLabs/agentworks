from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

from agentworks import __file__ as agentworks_file
from agentworks.capabilities.descriptor import capability_descriptors
from agentworks.cli._app import app
from agentworks.completions.spec import CommandSpec, build_spec
from agentworks.guide.agent_mode import GuideMode
from agentworks.guide.catalog import discover_concept_shells
from agentworks.guide.render import render_shell
from agentworks.guide.service import render_guide
from agentworks.plugins.registration import seat_installed_plugins
from agentworks.resources import KIND_REGISTRY
from agentworks.topics import prose_of, summary_of

_INLINE_COMMAND_PATTERNS = (
    re.compile(r"`(agw(?:\s+[^`]+)?)`"),
    re.compile(r"'(agw(?:\s+[^']+)?)'"),
    re.compile(r'"(agw(?:\s+[^\"]+)?)"'),
)
_GENERIC_SEGMENTS = frozenset({"COMMAND", "GROUP", "VALUE"})


def _authored_commands(text: str) -> set[str]:
    commands = {" ".join(command.split()) for pattern in _INLINE_COMMAND_PATTERNS for command in pattern.findall(text)}
    fence: str | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
        elif fence in {"bash", "sh", "shell"} and line.startswith("agw "):
            commands.add(line)
    return commands


def _string_template(expression: ast.expr) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if isinstance(expression, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "VALUE"
            for value in expression.values
        )
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left = _string_template(expression.left)
        right = _string_template(expression.right)
        return None if left is None or right is None else left + right
    return None


def _is_hint_target(target: ast.expr) -> bool:
    if isinstance(target, ast.Name):
        return target.id == "hint" or target.id.endswith("_hint")
    if isinstance(target, ast.Attribute):
        return target.attr == "hint" or target.attr.endswith("_hint")
    return False


def _hint_templates(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        expressions: list[ast.expr] = []
        if isinstance(node, ast.Call):
            expressions.extend(
                keyword.value
                for keyword in node.keywords
                if keyword.arg is not None and (keyword.arg == "hint" or keyword.arg.endswith("_hint"))
            )
        elif (isinstance(node, ast.Assign) and any(_is_hint_target(target) for target in node.targets)) or (
            isinstance(node, ast.AnnAssign) and node.value is not None and _is_hint_target(node.target)
        ):
            expressions.append(node.value)
        for expression in expressions:
            template = _string_template(expression)
            if template is None:
                continue
            candidate = (expression.lineno, template)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _shipped_guidance(package_root: Path) -> Iterator[tuple[str, str]]:
    catalog = discover_concept_shells(package_root)
    yield catalog.index.source.package_path, render_guide(None, GuideMode.AGENT, package_root=package_root).markdown
    for shell in catalog.topics:
        yield shell.source.package_path, render_shell(shell, GuideMode.AGENT, package_root=package_root)

    seat_installed_plugins()
    for kind, subject in sorted(KIND_REGISTRY.items()):
        if summary := summary_of(subject):
            yield f"kind {kind} description", summary
        if prose := prose_of(subject):
            yield f"kind {kind} overview", prose.overview

    for descriptor in capability_descriptors():
        for name, subject in sorted(descriptor.registry().items()):
            if summary := summary_of(subject):
                yield f"capability {descriptor.kind}/{name} description", summary
            if prose := prose_of(subject):
                yield f"capability {descriptor.kind}/{name} overview", prose.overview

    for path in sorted(package_root.rglob("*.py")):
        for line, template in _hint_templates(path):
            yield f"{path.relative_to(package_root)}:{line} hint", template


def _validate_command_prefix_and_options(command: str, root: CommandSpec) -> str | None:
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


def test_authored_command_extraction_covers_inline_quotes_and_shell_fences() -> None:
    text = """Use `agw vm start NAME` or 'agw doctor'.

```bash
agw resource list --kind vm-site
```
"""

    assert _authored_commands(text) == {
        "agw doctor",
        "agw resource list --kind vm-site",
        "agw vm start NAME",
    }


def test_command_resolution_checks_paths_and_options_without_executing() -> None:
    spec = build_spec(app)

    assert _validate_command_prefix_and_options("agw vm start NAME", spec) is None
    assert _validate_command_prefix_and_options("agw VALUE", spec) is None
    assert _validate_command_prefix_and_options("agw vm retired NAME", spec) is not None
    assert _validate_command_prefix_and_options("agw vm start NAME --retired", spec) is not None


def test_hint_inventory_is_limited_to_hint_values_and_preserves_placeholders(tmp_path: Path) -> None:
    source = tmp_path / "guidance.py"
    source.write_text(
        '''"""`agw retired` is not an error hint."""

def fail(operation: str, name: str) -> None:
    raise RuntimeError("failed", hint=f"Run `agw session {operation} {name} --force`.")
''',
        encoding="utf-8",
    )

    assert list(_hint_templates(source)) == [(4, "Run `agw session VALUE VALUE --force`.")]


def test_shipped_guidance_commands_match_the_cli_spec() -> None:
    package_root = Path(agentworks_file).parent
    spec = build_spec(app)
    checked = 0

    for source, text in _shipped_guidance(package_root):
        for command in sorted(_authored_commands(text)):
            problem = _validate_command_prefix_and_options(command, spec)
            assert problem is None, f"{source}: {command!r}: {problem}"
            checked += 1

    assert checked > 0
