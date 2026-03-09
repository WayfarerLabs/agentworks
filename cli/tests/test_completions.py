"""Tests for shell completion generation."""

from __future__ import annotations

from agentworks.cli import app
from agentworks.completions import generate
from agentworks.completions.spec import DYNAMIC_COMPLETIONS, build_spec, completion_version


def _walk_commands(spec, path: str = "") -> dict[str, object]:
    """Walk the spec tree and return a map of dotted paths to specs."""
    result = {}
    current = f"{path}.{spec.name}" if path else spec.name
    result[current] = spec
    for sub in spec.subcommands.values():
        result.update(_walk_commands(sub, current))
    return result


class TestDynamicCompletionsMapping:
    """Verify DYNAMIC_COMPLETIONS keys match real Typer commands and params."""

    def test_all_keys_resolve_to_real_commands(self) -> None:
        spec = build_spec(app)
        all_specs = _walk_commands(spec)

        for (command_path, param_name), _completer_id in DYNAMIC_COMPLETIONS.items():
            # The command_path in DYNAMIC_COMPLETIONS is relative (e.g. "vm.start")
            # but build_spec produces paths starting with the app name (e.g. "agentworks.vm.start")
            full_path = f"agentworks.{command_path}"
            assert full_path in all_specs, (
                f"DYNAMIC_COMPLETIONS key ({command_path}, {param_name}) "
                f"references non-existent command path: {command_path}"
            )

            cmd_spec = all_specs[full_path]
            param_names = [p.name for p in cmd_spec.params]
            assert param_name in param_names, (
                f"DYNAMIC_COMPLETIONS key ({command_path}, {param_name}) "
                f"references non-existent param '{param_name}' on command '{command_path}'. "
                f"Available params: {param_names}"
            )

    def test_completer_ids_are_known(self) -> None:
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS
        from agentworks.completions.zsh import COMPLETER_FUNC_NAMES

        for (command_path, param_name), completer_id in DYNAMIC_COMPLETIONS.items():
            assert completer_id in COMPLETER_FUNC_NAMES, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) "
                f"has no zsh function mapping"
            )
            assert completer_id in DYNAMIC_SNIPPETS, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) "
                f"has no PowerShell snippet mapping"
            )


class TestGeneration:
    """Smoke tests for completion script generation."""

    def test_zsh_generates_nonempty(self) -> None:
        output = generate("zsh")
        assert len(output) > 100
        assert "#compdef agentworks" in output
        assert "agentworks-completion-version:" in output

    def test_powershell_generates_nonempty(self) -> None:
        output = generate("powershell")
        assert len(output) > 100
        assert "Register-ArgumentCompleter" in output
        assert "agentworks-completion-version:" in output

    def test_version_is_deterministic(self) -> None:
        spec1 = build_spec(app)
        spec2 = build_spec(app)
        assert completion_version(spec1) == completion_version(spec2)

    def test_unsupported_shell_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="Unsupported shell"):
            generate("fish")


class TestCompleteness:
    """Verify all CLI commands appear in generated completions."""

    def test_zsh_contains_all_commands(self) -> None:
        output = generate("zsh")
        spec = build_spec(app)
        _assert_all_commands_present(spec, output)

    def test_powershell_contains_all_commands(self) -> None:
        output = generate("powershell")
        spec = build_spec(app)
        _assert_all_commands_present(spec, output)


def _assert_all_commands_present(spec, output: str) -> None:
    """Assert every command and subcommand name appears in the output."""
    for name, sub in spec.subcommands.items():
        assert name in output, f"Command '{name}' not found in generated output"
        for sub_name in sub.subcommands:
            assert sub_name in output, (
                f"Subcommand '{name} {sub_name}' not found in generated output"
            )
