"""Tests for shell completion generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.cli import app
from agentworks.completions import generate
from agentworks.completions.spec import (
    DYNAMIC_COMPLETIONS,
    CommandSpec,
    build_spec,
    completion_version,
)


def _walk_commands(spec: CommandSpec, path: str = "") -> dict[str, CommandSpec]:
    """Walk the spec tree and return a map of dotted paths to specs."""
    result: dict[str, CommandSpec] = {}
    current = f"{path}.{spec.name}" if path else spec.name
    result[current] = spec
    for sub in spec.subcommands.values():
        result.update(_walk_commands(sub, current))
    return result


class TestTopLevelGroups:
    """Pin the set of top-level command groups so an accidental rename or
    removal surfaces as a test failure rather than silent CLI drift. The
    canonical example: when the `installer` group became `catalog`, this
    test would have caught a half-renamed callsite by failing to find the
    expected group in `app.subcommands`.

    Update the expected set deliberately when adding or renaming a group.
    """

    EXPECTED_GROUPS = frozenset(
        {
            "agent",
            "completion",
            "config",
            "console",
            "env",
            "resource",
            "secret",
            "session",
            "vm",
            "vm-host",
            "workspace",
        }
    )

    def test_expected_top_level_groups_match(self) -> None:
        spec = build_spec(app)
        # spec.subcommands includes both groups and direct commands (e.g.
        # `agentworks doctor`); subcommands whose own `subcommands` dict is
        # non-empty are groups.
        actual_groups = {
            name for name, sub in spec.subcommands.items() if sub.subcommands
        }
        missing = self.EXPECTED_GROUPS - actual_groups
        unexpected = actual_groups - self.EXPECTED_GROUPS
        assert not missing and not unexpected, (
            f"top-level command group drift: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}. Update EXPECTED_GROUPS in this "
            f"test if the change is intentional."
        )


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
                f"Completer '{completer_id}' from ({command_path}, {param_name}) has no zsh function mapping"
            )
            assert completer_id in DYNAMIC_SNIPPETS, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) has no PowerShell snippet mapping"
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


class TestRegistrySourcedCompleters:
    """Every completer whose kind lives in the Resource Registry sources
    from ``agw resource list --kind X --names-only`` (not from
    regex-scraping ``[X.*]`` sections out of config.toml).

    The old sed-based approach had a greedy-regex bug where
    ``\\[X\\.([^]]*)\\]`` matched sub-section headers, so
    ``[vm_templates.default.env]`` emitted ``default.env`` as a bogus
    completion candidate. Registry-sourcing fixes it and picks up
    always-materialized defaults + auto-declared entries the raw config
    text doesn't have.
    """

    _REGISTRY_SOURCED = (
        ("ws_templates", "workspace-template"),
        ("git_credentials", "git-credential"),
        ("session_templates", "session-template"),
        ("vm_templates", "vm-template"),
        ("agent_templates", "agent-template"),
    )

    def test_bash_snippets_source_from_registry(self) -> None:
        from agentworks.completions.bash import DYNAMIC_SNIPPETS

        for completer_id, kind in self._REGISTRY_SOURCED:
            snippet = DYNAMIC_SNIPPETS[completer_id]
            assert f"--kind {kind}" in snippet, (
                f"bash {completer_id!r} should source from Registry"
                f" (--kind {kind}); got: {snippet!r}"
            )
            assert "sed " not in snippet, (
                f"bash {completer_id!r} still uses sed-over-TOML: {snippet!r}"
            )

    def test_zsh_functions_source_from_registry(self) -> None:
        from agentworks.completions.zsh import DYNAMIC_FUNCTIONS

        for completer_id, kind in self._REGISTRY_SOURCED:
            fn = DYNAMIC_FUNCTIONS[completer_id]
            assert f"--kind {kind}" in fn, (
                f"zsh {completer_id!r} should source from Registry"
                f" (--kind {kind}); got: {fn!r}"
            )
            assert "sed " not in fn, (
                f"zsh {completer_id!r} still uses sed-over-TOML: {fn!r}"
            )

    def test_powershell_snippets_source_from_registry(self) -> None:
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS

        for completer_id, kind in self._REGISTRY_SOURCED:
            snippet = DYNAMIC_SNIPPETS[completer_id]
            assert f"--kind {kind}" in snippet, (
                f"powershell {completer_id!r} should source from Registry"
                f" (--kind {kind}); got: {snippet!r}"
            )
            assert "Select-String" not in snippet, (
                f"powershell {completer_id!r} still uses Select-String"
                f" regex over config.toml: {snippet!r}"
            )


def _assert_all_commands_present(spec, output: str) -> None:
    """Assert every command and subcommand name appears in the output."""
    for name, sub in spec.subcommands.items():
        assert name in output, f"Command '{name}' not found in generated output"
        for sub_name in sub.subcommands:
            assert sub_name in output, f"Subcommand '{name} {sub_name}' not found in generated output"


class TestDetectShell:
    """detect_shell only commits to bash or zsh; everything else is unknown."""

    def test_bash(self, monkeypatch) -> None:
        from agentworks.completions import detect_shell

        monkeypatch.setenv("SHELL", "/bin/bash")
        assert detect_shell() == "bash"

    def test_zsh(self, monkeypatch) -> None:
        from agentworks.completions import detect_shell

        monkeypatch.setenv("SHELL", "/usr/local/bin/zsh")
        assert detect_shell() == "zsh"

    def test_unset(self, monkeypatch) -> None:
        from agentworks.completions import detect_shell

        monkeypatch.delenv("SHELL", raising=False)
        assert detect_shell() is None

    def test_unknown(self, monkeypatch) -> None:
        from agentworks.completions import detect_shell

        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        assert detect_shell() is None

    def test_powershell_is_not_autodetected(self, monkeypatch) -> None:
        # PowerShell on Windows does not set $SHELL; if it somehow leaks in,
        # we still refuse to commit and force the user to pass --shell.
        from agentworks.completions import detect_shell

        monkeypatch.setenv("SHELL", "pwsh")
        assert detect_shell() is None


class TestResolveShell:
    """_resolve_shell normalizes aliases and reports a clean error on autodetect failure."""

    def test_pwsh_alias_normalizes(self, monkeypatch) -> None:
        from agentworks.cli import _resolve_shell

        assert _resolve_shell("pwsh") == "powershell"

    def test_explicit_shell_passed_through(self, monkeypatch) -> None:
        from agentworks.cli import _resolve_shell

        assert _resolve_shell("bash") == "bash"
        assert _resolve_shell("zsh") == "zsh"
        assert _resolve_shell("powershell") == "powershell"

    def test_autodetect_success(self, monkeypatch) -> None:
        from agentworks.cli import _resolve_shell

        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert _resolve_shell(None) == "zsh"

    def test_autodetect_failure_exits_with_message(self, monkeypatch, capsys) -> None:
        import typer

        from agentworks.cli import _resolve_shell

        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        with pytest.raises(typer.Exit) as exc_info:
            _resolve_shell(None)
        assert exc_info.value.exit_code == 1
        captured = capsys.readouterr()
        assert "unable to detect the shell" in captured.err.lower()


class TestCompletionCli:
    """End-to-end tests of `agentworks completion show|install` via CliRunner."""

    def test_show_with_explicit_shell_prints_script(self, monkeypatch) -> None:
        from typer.testing import CliRunner

        from agentworks.cli import app

        result = CliRunner().invoke(app, ["completion", "show", "--shell", "zsh"])
        assert result.exit_code == 0
        assert "#compdef" in result.stdout

    def test_show_autodetect_failure_exits_1(self, monkeypatch) -> None:
        from typer.testing import CliRunner

        from agentworks.cli import app

        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        result = CliRunner().invoke(app, ["completion", "show"])
        assert result.exit_code == 1
        assert "unable to detect the shell" in result.stderr.lower()

    def test_show_pwsh_alias_produces_powershell_script(self, monkeypatch) -> None:
        from typer.testing import CliRunner

        from agentworks.cli import app

        result_pwsh = CliRunner().invoke(app, ["completion", "show", "--shell", "pwsh"])
        result_ps = CliRunner().invoke(app, ["completion", "show", "--shell", "powershell"])
        assert result_pwsh.exit_code == 0
        assert result_ps.exit_code == 0
        assert result_pwsh.stdout == result_ps.stdout


class TestInstall:
    """Filesystem-level checks for `agentworks completion install`."""

    def test_bash_install_drops_agw_alias_symlink(
        self, monkeypatch, tmp_path
    ) -> None:
        """Bash's lazy autoload is keyed on the command name -- typing `agw`
        looks for a file named `agw`, not `agentworks`. Install must drop a
        symlink so both names trigger the same script."""
        from typer.testing import CliRunner

        # Redirect home via Path.home itself: setenv("HOME") only works on
        # POSIX; Path.home() reads USERPROFILE on Windows.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = CliRunner().invoke(
            app, ["completion", "install", "--shell", "bash"]
        )
        assert result.exit_code == 0

        completions_dir = (
            tmp_path / ".local" / "share" / "bash-completion" / "completions"
        )
        primary = completions_dir / "agentworks"
        alias = completions_dir / "agw"
        assert primary.is_file()
        # POSIX gets a symlink; Windows (no symlink privilege) falls back to a
        # content copy. Either way the alias resolves to the same script.
        assert alias.is_symlink() or alias.is_file()
        assert alias.read_text() == primary.read_text()

    def test_zsh_install_drops_agw_alias_symlink(
        self, monkeypatch, tmp_path
    ) -> None:
        """zsh's compinit autoload is keyed on the command name too: typing
        `agw<TAB>` looks for `_agw` in fpath. Without a symlink the
        `#compdef agentworks agw` directive inside `_agentworks` is never
        reached for the short name."""
        from typer.testing import CliRunner

        # Redirect home via Path.home itself: setenv("HOME") only works on
        # POSIX; Path.home() reads USERPROFILE on Windows.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Steer install away from Oh My Zsh detection so we land in ~/.zfunc.
        monkeypatch.delenv("ZSH_CUSTOM", raising=False)

        result = CliRunner().invoke(
            app, ["completion", "install", "--shell", "zsh"]
        )
        assert result.exit_code == 0

        zfunc = tmp_path / ".zfunc"
        primary = zfunc / "_agentworks"
        alias = zfunc / "_agw"
        assert primary.is_file()
        # POSIX gets a symlink; Windows (no symlink privilege) falls back to a
        # content copy. Either way the alias resolves to the same script.
        assert alias.is_symlink() or alias.is_file()
        assert alias.read_text() == primary.read_text()


class TestUninstall:
    """Filesystem-level checks for `agentworks completion uninstall`."""

    def test_bash_uninstall_removes_script_and_alias(
        self, monkeypatch, tmp_path
    ) -> None:
        from typer.testing import CliRunner

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        completions_dir = (
            tmp_path / ".local" / "share" / "bash-completion" / "completions"
        )
        completions_dir.mkdir(parents=True)
        (completions_dir / "agentworks").write_text("x")
        (completions_dir / "agw").write_text("x")

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", "bash"]
        )
        assert result.exit_code == 0
        assert not (completions_dir / "agentworks").exists()
        assert not (completions_dir / "agw").exists()

    def test_zsh_uninstall_removes_script_and_alias(
        self, monkeypatch, tmp_path
    ) -> None:
        from typer.testing import CliRunner

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("ZSH_CUSTOM", raising=False)
        zfunc = tmp_path / ".zfunc"
        zfunc.mkdir()
        (zfunc / "_agentworks").write_text("x")
        (zfunc / "_agw").write_text("x")

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", "zsh"]
        )
        assert result.exit_code == 0
        assert not (zfunc / "_agentworks").exists()
        assert not (zfunc / "_agw").exists()

    def test_powershell_uninstall_removes_script_and_profile_line(
        self, monkeypatch, tmp_path
    ) -> None:
        from typer.testing import CliRunner

        from agentworks.completions import install

        profile = tmp_path / "profile.ps1"
        completions_dir = tmp_path / "Completions"
        completions_dir.mkdir()
        script = completions_dir / "agentworks.ps1"
        script.write_text("x")
        profile.write_text(f'Write-Host hi\n. "{script}"\n')

        monkeypatch.setattr(install, "_query_powershell_profile", lambda: profile)

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", "powershell"]
        )
        assert result.exit_code == 0
        assert not script.exists()
        assert "agentworks.ps1" not in profile.read_text()
        # Unrelated profile content is preserved.
        assert "Write-Host hi" in profile.read_text()

    @pytest.mark.parametrize("shell", ["bash", "zsh", "powershell"])
    def test_uninstall_when_nothing_installed_is_clean(
        self, monkeypatch, tmp_path, shell
    ) -> None:
        """Every shell's uninstall exits 0 with a "nothing found" message
        when there's nothing to remove -- not just bash."""
        from typer.testing import CliRunner

        from agentworks.completions import install

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("ZSH_CUSTOM", raising=False)
        # For powershell, provide a resolvable profile that just doesn't
        # have the completions or the source line -- otherwise the "no
        # binary on PATH" failure path fires instead.
        if shell == "powershell":
            profile = tmp_path / "profile.ps1"
            monkeypatch.setattr(install, "_query_powershell_profile", lambda: profile)

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", shell]
        )
        assert result.exit_code == 0
        # Lowercase compare: the powershell message uses "PowerShell".
        assert f"no {shell} completions found" in result.stdout.lower()

    def test_powershell_uninstall_fails_when_no_binary(
        self, monkeypatch
    ) -> None:
        """If neither `pwsh` nor `powershell` is on PATH, uninstall exits
        non-zero with a clear error rather than silently succeeding."""
        from typer.testing import CliRunner

        from agentworks.completions import install

        monkeypatch.setattr(install, "_query_powershell_profile", lambda: None)

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", "powershell"]
        )
        assert result.exit_code != 0
        assert "could not determine PowerShell $PROFILE path" in result.stderr

    def test_powershell_uninstall_preserves_user_lines_mentioning_filename(
        self, monkeypatch, tmp_path
    ) -> None:
        """The $PROFILE strip must match the installer's exact
        dot-source-plus-quoted-path shape, not any line containing the
        string 'agentworks.ps1'. Comments, conditionals, and unrelated
        dot-sources that mention the name should survive uninstall."""
        from typer.testing import CliRunner

        from agentworks.completions import install

        profile = tmp_path / "profile.ps1"
        completions_dir = tmp_path / "Completions"
        completions_dir.mkdir()
        script = completions_dir / "agentworks.ps1"
        script.write_text("x")

        # Mix of the installer's real line and lines a user could plausibly
        # write that mention the filename but aren't the installer's line.
        installer_line = f'. "{script}"'
        user_comment = "# uses agentworks.ps1 for completions"
        user_conditional = 'if ($true) { Write-Host "agentworks.ps1 loaded" }'
        user_alt_dotsource = '. "$HOME/custom/agentworks.ps1.bak"'  # different filename suffix
        profile.write_text(
            "\n".join([user_comment, installer_line, user_conditional, user_alt_dotsource]) + "\n"
        )
        monkeypatch.setattr(install, "_query_powershell_profile", lambda: profile)

        result = CliRunner().invoke(
            app, ["completion", "uninstall", "--shell", "powershell"]
        )
        assert result.exit_code == 0

        remaining = profile.read_text()
        assert installer_line not in remaining
        assert user_comment in remaining
        assert user_conditional in remaining
        assert user_alt_dotsource in remaining


class TestVariadicPositionalCompletion:
    """Variadic Argument positionals (Click nargs=-1) must produce 'every
    subsequent position' completion in all three shells, not just position N."""

    def test_zsh_uses_star_for_variadic(self) -> None:
        output = generate("zsh")
        # console create's sessions positional is variadic with the sessions
        # completer; '*:' is zsh's "remaining positions" catchall.
        assert "'*:sessions:_agentworks_sessions'" in output

    def test_bash_uses_ge_for_variadic(self) -> None:
        output = generate("bash")
        # Look for the console-create block specifically: 'sessions' completer
        # snippet is `agentworks session list --no-status ...`, guarded by a
        # -ge cword check (matches every position from the variadic's offset on).
        assert "cword -ge" in output
        # And the standard -eq for non-variadic positionals still works.
        assert "cword -eq" in output

    def test_powershell_uses_ge_for_variadic(self) -> None:
        output = generate("powershell")
        # Same idea: -ge for the variadic.
        assert "tokenCount -ge" in output
        assert "tokenCount -eq" in output
