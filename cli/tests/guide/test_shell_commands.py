from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

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

_INLINE_SPAN_PATTERNS = (
    re.compile(r"(?<!`)`(?!`)([^`\n]+)`(?!`)"),
    re.compile(r"(?<![\w'])'(?!')([^'\n]+)'(?!')"),
    re.compile(r'(?<!")"(?!")([^"\n]+)"(?!")'),
)
_AUTHORED_COMMAND_SEGMENTS = frozenset({"COMMAND", "GROUP"})
_GUIDANCE_CATEGORIES = frozenset({"guide", "kind", "capability", "hint"})
_SHELL_OPERATOR_CHARS = frozenset("();|&")


def _starts_agw_command(segment: str) -> bool:
    return bool(segment) and segment.split(maxsplit=1)[0] == "agw"


def _shell_segments(expression: str) -> Iterator[str]:
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(expression):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == start or expression[index - 1].isspace()):
            if segment := expression[start:index].strip():
                yield segment
            return
        elif character in _SHELL_OPERATOR_CHARS:
            if segment := expression[start:index].strip():
                yield segment
            start = index + 1
    segment = expression[start:].strip()
    if quote is not None or escaped:
        if _starts_agw_command(segment):
            raise ValueError("malformed shell quoting in authored command")
        return
    if segment:
        yield segment


def _shell_commands(expression: str) -> Iterator[str]:
    for segment in _shell_segments(expression.replace("\\\n", " ")):
        if not _starts_agw_command(segment):
            continue
        tokens = shlex.split(segment)
        yield shlex.join(tokens)


def _authored_commands(text: str) -> set[str]:
    commands: set[str] = set()
    inline_lines: list[str] = []
    fence: str | None = None
    continuation = ""
    for line in text.splitlines():
        if line.startswith("```"):
            if continuation:
                raise ValueError("unfinished continuation in authored shell command")
            fence = None if fence is not None else line[3:].strip()
        elif fence in {"bash", "sh", "shell"}:
            if line.endswith("\\"):
                continuation += line[:-1] + " "
            else:
                commands.update(_shell_commands(continuation + line))
                continuation = ""
        elif fence is None:
            inline_lines.append(line)
    if continuation:
        raise ValueError("unfinished continuation in authored shell command")
    inline_text = "\n".join(inline_lines)
    commands.update(
        command
        for pattern in _INLINE_SPAN_PATTERNS
        for expression in pattern.findall(inline_text)
        for command in _shell_commands(expression)
    )
    return commands


def _string_templates(
    expression: ast.expr,
    bindings: dict[str, tuple[ast.expr, ...]] | None = None,
    resolving: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    bindings = bindings or {}
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,)
    if isinstance(expression, ast.Name) and expression.id in bindings and expression.id not in resolving:
        return tuple(
            template
            for value in bindings[expression.id]
            for template in _string_templates(value, bindings, resolving | {expression.id})
        )
    if isinstance(expression, ast.JoinedStr):
        templates: tuple[str, ...] = ("",)
        for value in expression.values:
            parts: tuple[str, ...]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts = (value.value,)
            elif isinstance(value, ast.FormattedValue):
                parts = _string_templates(value.value, bindings, resolving)
                if not parts:
                    parts = (str(value.value.value),) if isinstance(value.value, ast.Constant) else ("VALUE",)
            else:
                parts = ("VALUE",)
            templates = tuple(prefix + part for prefix in templates for part in parts)
        return templates
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return tuple(
            left + right
            for left in _string_templates(expression.left, bindings, resolving)
            for right in _string_templates(expression.right, bindings, resolving)
        )
    if isinstance(expression, ast.IfExp):
        return (
            *_string_templates(expression.body, bindings, resolving),
            *_string_templates(expression.orelse, bindings, resolving),
        )
    return ()


def _is_hint_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered in {"hint", "remediation", "remedy"} or lowered.endswith("_hint")


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


def _scope_nodes(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    stack = list(reversed(scope.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _scope_bindings(nodes: tuple[ast.AST, ...]) -> dict[str, tuple[ast.expr, ...]]:
    values: dict[str, list[ast.expr]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            values.setdefault(node.target.id, []).append(node.value)
    return {name: tuple(expressions) for name, expressions in values.items()}


def _hint_templates(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for scope in scopes:
        nodes = tuple(_scope_nodes(scope))
        bindings = _scope_bindings(nodes)
        indirect_names = {
            keyword.value.id
            for node in nodes
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg is not None and _is_hint_name(keyword.arg) and isinstance(keyword.value, ast.Name)
        }
        expressions: list[ast.expr] = []
        for node in nodes:
            if isinstance(node, ast.Call):
                expressions.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg is not None
                    and _is_hint_name(keyword.arg)
                    and not (isinstance(keyword.value, ast.Name) and keyword.value.id in indirect_names)
                )
            elif assigned := _assigned_hint_expression(node, indirect_names):
                expressions.append(assigned)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_hint_name(scope.name):
            expressions.extend(node.value for node in nodes if isinstance(node, ast.Return) and node.value is not None)
        for expression in expressions:
            for template in _string_templates(expression, bindings):
                yield expression.lineno, template


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
    if not tokens or tokens[0] != "agw":
        return "command does not start with 'agw'"

    def validate_from(current: CommandSpec, index: int, options_enabled: bool = True) -> str | None:
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                options_enabled = False
                index += 1
                continue
            if options_enabled and token.startswith("-"):
                option = token.partition("=")[0]
                parameter = next((parameter for parameter in current.params if option in parameter.opts), None)
                if parameter is None and option != "--help":
                    return f"unknown option {option!r}"
                if parameter is not None and not parameter.is_flag and "=" not in token:
                    index += 2 if index + 1 < len(tokens) else 1
                    continue
                index += 1
                continue
            if current.subcommands:
                if token == "VALUE":
                    return f"unresolved command segment {token!r}"
                if token in _AUTHORED_COMMAND_SEGMENTS:
                    matches = (
                        validate_from(child, index + 1, options_enabled) is None
                        for child in current.subcommands.values()
                    )
                    if any(matches):
                        return None
                    return f"no command matches generic segment {token!r}"
                if token not in current.subcommands:
                    return f"unknown command segment {token!r}"
                current = current.subcommands[token]
            index += 1
        return None

    return validate_from(root, 1)


def test_authored_command_extraction_covers_quotes_fences_and_shell_boundaries() -> None:
    text = """Use `agw vm start NAME && agw doctor` or 'echo ok && agw resource kinds'.
Also use "printf ok; agw version" when checking the installed CLI.
Keep the quoted operator in `agw vm start 'A|B'` as an operand.

```bash
agw resource list --kind vm-site | jq --raw-output .
agw vm start \\
  OTHER
# agw retired && agw vm retired
```
"""

    assert _authored_commands(text) == {
        "agw doctor",
        "agw resource kinds",
        "agw resource list --kind vm-site",
        "agw version",
        "agw vm start 'A|B'",
        "agw vm start NAME",
        "agw vm start OTHER",
    }

    with pytest.raises(ValueError, match="malformed shell quoting"):
        _authored_commands('Use `agw retired "unterminated`.')

    assert _authored_commands("Describe `don't panic` without running a command.") == set()


def test_inline_backticks_after_a_shell_fence_remain_independent() -> None:
    text = """```bash
agw doctor
```
Then `echo ok && agw retired`.
"""

    assert _authored_commands(text) == {"agw doctor", "agw retired"}


def test_inline_spans_inside_shell_fences_are_not_scanned() -> None:
    text = """```bash
printf '%s' 'agw retired'
# 'agw retired' is obsolete
echo '`agw retired`'
```
"""

    assert _authored_commands(text) == set()


def test_shell_comments_start_at_word_boundaries() -> None:
    assert _authored_commands("Use `agw vm start NAME#literal --retired`.") == {"agw vm start 'NAME#literal' --retired"}
    assert _authored_commands("Use `agw doctor # agw retired`.") == {"agw doctor"}


@pytest.mark.parametrize(
    "text",
    (
        "```bash\nagw retired \\\n```",
        "```bash\nagw retired \\",
    ),
)
def test_authored_command_extraction_rejects_unfinished_continuations(text: str) -> None:
    with pytest.raises(ValueError, match="unfinished continuation"):
        _authored_commands(text)


def test_command_resolution_checks_paths_and_options_without_executing() -> None:
    spec = build_spec(app)

    assert _validate_command_prefix_and_options("agw vm start NAME", spec) is None
    assert _validate_command_prefix_and_options("agw guide --agent show TOPIC", spec) is None
    assert _validate_command_prefix_and_options("agw GROUP --help", spec) is None
    assert _validate_command_prefix_and_options("agw VALUE", spec) is not None
    assert _validate_command_prefix_and_options("agw GROUP --retired", spec) is not None
    assert _validate_command_prefix_and_options("agw vm retired NAME", spec) is not None
    assert _validate_command_prefix_and_options("agw vm start NAME --retired", spec) is not None
    assert _validate_command_prefix_and_options("agw guide --agent retired TOPIC", spec) is not None
    assert _validate_command_prefix_and_options("agw doctor --debug", spec) is not None
    assert _validate_command_prefix_and_options("agw VALUE --retired", spec) is not None
    assert _validate_command_prefix_and_options("agw session VALUE VALUE --retired", spec) is not None


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

def carry_remediation(name: str) -> str:
    remediation = f"Run `agw agent reinit {name}`."
    return build(remediation=remediation)

def command_hint(use_list: bool) -> str:
    command_path = "vm list" if use_list else "doctor"
    return f"Run `agw {command_path}`."
''',
        encoding="utf-8",
    )

    assert list(_hint_templates(source)) == [
        (4, "Run `agw session VALUE VALUE --force`."),
        (11, "Run `agw vm start VALUE`."),
        (15, "Run `agw agent reinit VALUE`."),
        (20, "Run `agw vm list`."),
        (20, "Run `agw doctor`."),
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
