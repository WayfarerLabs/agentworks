"""Current Typer parsing and completion metadata at the CLI boundary."""

from pathlib import Path
from typing import Annotated

import pytest
import typer
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.cli.commands.completion import Shell
from agentworks.completions.spec import build_spec


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["completion", "show"],
        ["completion", "install"],
        ["completion", "uninstall"],
        ["graph", "show"],
        ["vm", "set-desired-state"],
        ["session", "set-desired-state"],
        ["console", "set-desired-state"],
    ],
)
def test_help_constructs_the_complete_command_tree(command: list[str]) -> None:
    result = CliRunner().invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes
    assert result.stderr_bytes == b""


@pytest.mark.parametrize("verb", ["show", "install", "uninstall"])
@pytest.mark.parametrize("shell", ["bash", "zsh", "powershell", "pwsh", None])
def test_shell_choice_reaches_service_after_alias_or_autodetection(
    monkeypatch: pytest.MonkeyPatch, verb: str, shell: str | None
) -> None:
    from agentworks import completions
    from agentworks.completions import install

    generated: list[str] = []
    installed: list[tuple[str, str]] = []
    removed: list[str] = []
    monkeypatch.setattr(completions, "detect_shell", lambda: "zsh")
    monkeypatch.setattr(completions, "generate", lambda value: generated.append(value) or "generated-script")
    monkeypatch.setattr(install, "install_completions", lambda value, script: installed.append((value, script)))
    monkeypatch.setattr(install, "uninstall_completions", lambda value: removed.append(value))
    argv = ["completion", verb, *([] if shell is None else ["--shell", shell])]
    result = CliRunner().invoke(app, argv)
    expected = "zsh" if shell is None else "powershell" if shell == "pwsh" else shell

    assert result.exit_code == 0, result.output
    assert generated == ([] if verb == "uninstall" else [expected])
    assert installed == ([(expected, "generated-script")] if verb == "install" else [])
    assert removed == ([expected] if verb == "uninstall" else [])


@pytest.mark.parametrize("verb", ["show", "install", "uninstall"])
@pytest.mark.parametrize("shell", ["fish", "BASH", "PowerShell"])
def test_invalid_shell_is_usage_error_before_services(monkeypatch: pytest.MonkeyPatch, verb: str, shell: str) -> None:
    from agentworks import completions
    from agentworks.completions import install

    def forbidden(*args: object) -> None:
        pytest.fail("invalid choice reached completion service")

    monkeypatch.setattr(completions, "detect_shell", forbidden)
    monkeypatch.setattr(completions, "generate", forbidden)
    monkeypatch.setattr(install, "install_completions", forbidden)
    monkeypatch.setattr(install, "uninstall_completions", forbidden)
    result = CliRunner().invoke(app, ["completion", verb, "--shell", shell])
    assert result.exit_code == 2
    assert result.stdout_bytes == b""


def test_metadata_matches_optional_repeated_boolean_and_path_parsing(tmp_path: Path) -> None:
    fixture = typer.Typer(add_completion=False)
    received: list[tuple[object, ...]] = []

    @fixture.command()
    def parse(
        paths: Annotated[list[Path], typer.Argument(exists=True)],
        shell: Annotated[Shell | None, typer.Option("--shell")] = None,
        shells: Annotated[list[Shell] | None, typer.Option("--shells")] = None,
        enabled: Annotated[bool, typer.Option("--enabled/--disabled")] = False,
    ) -> None:
        received.append((paths, shell, shells, enabled))

    parameters = {param.name: param for param in build_spec(fixture).params}
    assert parameters["paths"].is_argument and parameters["paths"].multiple and parameters["paths"].required
    assert parameters["paths"].choices is None
    assert not parameters["shell"].required and not parameters["shell"].multiple
    assert parameters["shell"].choices == parameters["shells"].choices
    assert parameters["shells"].multiple and not parameters["shells"].is_flag
    assert parameters["enabled"].is_flag
    assert parameters["enabled"].opts == ["--enabled", "--disabled"]

    first, second = tmp_path / "first", tmp_path / "second"
    first.touch()
    second.touch()
    runner = CliRunner()
    result = runner.invoke(
        fixture,
        [str(first), str(second), "--shell", "pwsh", "--shells", "bash", "--shells", "zsh", "--enabled"],
    )
    assert result.exit_code == 0, result.output
    assert received == [([first, second], Shell.PWSH, [Shell.BASH, Shell.ZSH], True)]
    result = runner.invoke(fixture, [str(first), "--disabled"])
    assert result.exit_code == 0, result.output
    assert received[-1] == ([first], None, None, False)
    before = list(received)
    for argv in ([str(first), "--shells", "fish"], [str(tmp_path / "absent")], []):
        assert runner.invoke(fixture, argv).exit_code == 2
    assert received == before


@pytest.mark.parametrize(
    "command,option,service,keyword",
    [
        (["console", "add-sessions", "view", "work"], "--to-index", "add_sessions", "start_index"),
        (["console", "reorder-sessions", "view", "work"], "--to-index", "reorder_sessions", "start_index"),
        (["session", "logs", "work"], "--lines", "session_logs", "lines"),
    ],
)
def test_integer_options_preserve_grammar_defaults_and_service_validation(
    monkeypatch: pytest.MonkeyPatch, command: list[str], option: str, service: str, keyword: str
) -> None:
    from agentworks.cli.commands import console, session
    from agentworks.sessions import manager, multi_console

    calls: list[dict[str, object]] = []
    touched: list[str] = []
    cli_module = console if command[0] == "console" else session
    service_module = multi_console if command[0] == "console" else manager
    monkeypatch.setattr(cli_module, "get_db", lambda: touched.append("db") or object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: touched.append("config") or object())
    monkeypatch.setattr(service_module, service, lambda *_args, **kwargs: calls.append(kwargs))
    runner = CliRunner()
    for value, expected in [(None, None), ("2", 2), ("-1", -1), ("+3", 3)]:
        result = runner.invoke(app, [*command, *([] if value is None else [option, value])])
        assert result.exit_code == 0, result.output
        assert calls[-1][keyword] == expected
        assert touched[-2:] == ["db", "config"]
    before_calls, before_touched = list(calls), list(touched)
    for value in ("1.0", "two", "1e2"):
        result = runner.invoke(app, [*command, option, value])
        assert result.exit_code == 2
        assert result.stdout_bytes == b""
    assert calls == before_calls
    assert touched == before_touched
