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
_GUIDANCE_CATEGORIES = frozenset({"guide", "kind", "capability", "hint"})


def _authored_commands(text: str) -> set[str]:
    commands = {" ".join(command.split()) for pattern in _INLINE_COMMAND_PATTERNS for command in pattern.findall(text)}
    fence: str | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
        elif fence in {"bash", "sh", "shell"} and line.startswith("agw "):
            commands.add(line)
    return commands


def _string_templates(expression: ast.expr) -> tuple[str, ...]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,)
    if isinstance(expression, ast.JoinedStr):
        return (
            "".join(
                value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else "VALUE"
                for value in expression.values
            ),
        )
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return tuple(
            left + right for left in _string_templates(expression.left) for right in _string_templates(expression.right)
        )
    if isinstance(expression, ast.IfExp):
        return (*_string_templates(expression.body), *_string_templates(expression.orelse))
    return ()


def _is_hint_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered == "hint" or lowered.endswith("_hint")


def _is_hint_target(target: ast.expr) -> bool:
    if isinstance(target, ast.Name):
        return _is_hint_name(target.id)
    if isinstance(target, ast.Attribute):
        return _is_hint_name(target.attr)
    return False


def _assigned_hint_expression(node: ast.AST, indirect_names: set[str]) -> ast.expr | None:
    if isinstance(node, ast.Assign) and any(
        _is_hint_target(target) or isinstance(target, ast.Name) and target.id in indirect_names
        for target in node.targets
    ):
        return node.value
    if (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and (_is_hint_target(node.target) or isinstance(node.target, ast.Name) and node.target.id in indirect_names)
    ):
        return node.value
    return None


def _hint_templates(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    indirect_names = {
        keyword.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg is not None and _is_hint_name(keyword.arg) and isinstance(keyword.value, ast.Name)
    }
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        expressions: list[ast.expr] = []
        if isinstance(node, ast.Call):
            expressions.extend(
                keyword.value for keyword in node.keywords if keyword.arg is not None and _is_hint_name(keyword.arg)
            )
        elif assigned := _assigned_hint_expression(node, indirect_names):
            expressions.append(assigned)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_hint_name(node.name):
            expressions.extend(
                returned.value
                for returned in ast.walk(node)
                if isinstance(returned, ast.Return) and returned.value is not None
            )
        for expression in expressions:
            for template in _string_templates(expression):
                candidate = (expression.lineno, template)
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate


def _shipped_guidance(package_root: Path) -> Iterator[tuple[str, str, str]]:
    catalog = discover_concept_shells(package_root)
    yield (
        "guide",
        catalog.index.source.package_path,
        render_guide(None, GuideMode.AGENT, package_root=package_root).markdown,
    )
    for shell in catalog.topics:
        yield "guide", shell.source.package_path, render_shell(shell, GuideMode.AGENT, package_root=package_root)

    seat_installed_plugins()
    for kind, subject in sorted(KIND_REGISTRY.items()):
        if summary := summary_of(subject):
            yield "kind", f"kind {kind} description", summary
        if prose := prose_of(subject):
            yield "kind", f"kind {kind} overview", prose.overview

    for descriptor in capability_descriptors():
        for name, subject in sorted(descriptor.registry().items()):
            if summary := summary_of(subject):
                yield "capability", f"capability {descriptor.kind}/{name} description", summary
            if prose := prose_of(subject):
                yield "capability", f"capability {descriptor.kind}/{name} overview", prose.overview

    for path in sorted(package_root.rglob("*.py")):
        for line, template in _hint_templates(path):
            yield "hint", f"{path.relative_to(package_root)}:{line} hint", template


def _validate_command_prefix_and_options(command: str, root: CommandSpec) -> str | None:
    tokens = shlex.split(command)
    current = root
    path = [root]
    index = 1
    options_enabled = True
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            options_enabled = False
            index += 1
            continue
        if options_enabled and token.startswith("-"):
            option = token.partition("=")[0]
            parameter = next(
                (parameter for spec in path for parameter in spec.params if option in parameter.opts),
                None,
            )
            if parameter is None and option != "--help":
                return f"unknown option {option!r}"
            if parameter is not None and not parameter.is_flag and "=" not in token:
                index += 2 if index + 1 < len(tokens) else 1
                continue
            index += 1
            continue
        if current.subcommands:
            if token in _GENERIC_SEGMENTS:
                return None
            if token not in current.subcommands:
                return f"unknown command segment {token!r}"
            current = current.subcommands[token]
            path.append(current)
        index += 1
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
    assert _validate_command_prefix_and_options("agw guide --agent show TOPIC", spec) is None
    assert _validate_command_prefix_and_options("agw vm retired NAME", spec) is not None
    assert _validate_command_prefix_and_options("agw vm start NAME --retired", spec) is not None
    assert _validate_command_prefix_and_options("agw guide --agent retired TOPIC", spec) is not None


def test_hint_inventory_is_limited_to_hint_values_and_preserves_placeholders(tmp_path: Path) -> None:
    source = tmp_path / "guidance.py"
    source.write_text(
        '''"""`agw retired` is not an error hint."""

def build_hint(operation: str, name: str) -> str:
    return f"Run `agw session {operation} {name} --force`."

def fail(operation: str, name: str) -> None:
    remedy = build_hint(operation, name)
    raise RuntimeError("failed", hint=remedy)

def fail_direct(name: str) -> None:
    remedy = f"Run `agw vm start {name}`."
    raise RuntimeError("failed", hint=remedy)
''',
        encoding="utf-8",
    )

    assert list(_hint_templates(source)) == [
        (4, "Run `agw session VALUE VALUE --force`."),
        (11, "Run `agw vm start VALUE`."),
    ]


def test_shipped_guidance_commands_match_the_cli_spec() -> None:
    package_root = Path(agentworks_file).parent
    spec = build_spec(app)
    checked_categories: set[str] = set()

    for category, source, text in _shipped_guidance(package_root):
        commands = _authored_commands(text)
        for command in sorted(commands):
            problem = _validate_command_prefix_and_options(command, spec)
            assert problem is None, f"{source}: {command!r}: {problem}"
        if commands:
            checked_categories.add(category)

    assert checked_categories == _GUIDANCE_CATEGORIES
