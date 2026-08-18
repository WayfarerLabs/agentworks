"""Tests for shell completion generation."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.cli import app
from agentworks.completions import generate
from agentworks.completions.spec import (
    COMPLETION_PROBE_OPTION,
    DATABASE_BACKED_COMPLETION_PATHS,
    DATABASE_BACKED_DYNAMIC_COMPLETERS,
    DATABASE_BACKED_DYNAMIC_COMPLETIONS,
    DYNAMIC_COMPLETIONS,
    RESOURCE_LIST_DYNAMIC_COMPLETIONS,
    CommandSpec,
    build_spec,
    completion_version,
    is_legacy_database_completion,
)


def _walk_commands(spec: CommandSpec, path: str = "") -> dict[str, CommandSpec]:
    """Walk the spec tree and return a map of dotted paths to specs."""
    result: dict[str, CommandSpec] = {}
    current = f"{path}.{spec.name}" if path else spec.name
    result[current] = spec
    for sub in spec.subcommands.values():
        result.update(_walk_commands(sub, current))
    return result


def _generated_block(script: str, start_line: str, end_line: str) -> str:
    """Return one generated handler without borrowing text from its neighbors."""
    lines = script.splitlines()
    start = lines.index(start_line)
    end = lines.index(end_line, start + 1)
    return "\n".join(lines[start : end + 1])


def _generated_braced_block(script: str, start_line: str) -> str:
    """Return one generated brace-delimited handler, including nested blocks."""
    lines = script.splitlines()
    start = lines.index(start_line)
    depth = 0
    for end in range(start, len(lines)):
        depth += lines[end].count("{") - lines[end].count("}")
        if depth == 0:
            return "\n".join(lines[start : end + 1])
    raise AssertionError(f"unterminated generated block: {start_line}")


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
            "database",
            "env",
            "graph",
            "guide",
            "resource",
            "secret",
            "session",
            "vm",
            "workspace",
        }
    )

    def test_expected_top_level_groups_match(self) -> None:
        spec = build_spec(app)
        # spec.subcommands includes both groups and direct commands (e.g.
        # `agentworks doctor`); subcommands whose own `subcommands` dict is
        # non-empty are groups.
        actual_groups = {name for name, sub in spec.subcommands.items() if sub.subcommands}
        missing = self.EXPECTED_GROUPS - actual_groups
        unexpected = actual_groups - self.EXPECTED_GROUPS
        assert not missing and not unexpected, (
            f"top-level command group drift: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}. Update EXPECTED_GROUPS in this "
            f"test if the change is intentional."
        )


class TestRetiredCommandsAbsent:
    """Pin deleted commands so completion introspection cannot resurrect them."""

    def test_workspace_group_carries_no_shell_or_console(self) -> None:
        spec = build_spec(app)
        workspace = spec.subcommands["workspace"]
        assert "shell" not in workspace.subcommands
        assert "console" not in workspace.subcommands

    def test_vm_group_carries_no_console(self) -> None:
        spec = build_spec(app)
        vm = spec.subcommands["vm"]
        assert "console" not in vm.subcommands
        assert ("vm.console", "name") not in DYNAMIC_COMPLETIONS


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
        from agentworks.completions.bash import (
            DYNAMIC_SNIPPETS as BASH_SNIPPETS,
        )
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS
        from agentworks.completions.zsh import COMPLETER_FUNC_NAMES

        for (command_path, param_name), completer_id in DYNAMIC_COMPLETIONS.items():
            assert completer_id in COMPLETER_FUNC_NAMES, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) has no zsh function mapping"
            )
            assert completer_id in DYNAMIC_SNIPPETS, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) has no PowerShell snippet mapping"
            )
            # The bash generator silently skips unknown completer ids, so
            # a rename missed in bash alone would ship as silently-dead
            # completion -- pin all three shell maps.
            assert completer_id in BASH_SNIPPETS, (
                f"Completer '{completer_id}' from ({command_path}, {param_name}) has no bash snippet mapping"
            )

    def test_guide_show_topics_use_the_list_stream_in_every_shell(self) -> None:
        from agentworks.completions.bash import DYNAMIC_SNIPPETS as BASH_SNIPPETS
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS as POWERSHELL_SNIPPETS
        from agentworks.completions.zsh import DYNAMIC_FUNCTIONS

        assert DYNAMIC_COMPLETIONS[("guide.show", "topic")] == "guide_topics"
        assert "agw guide list" in BASH_SNIPPETS["guide_topics"]
        assert "agw guide list" in POWERSHELL_SNIPPETS["guide_topics"]
        assert "agw guide list" in DYNAMIC_FUNCTIONS["guide_topics"]

    def test_guide_uses_ordinary_subcommands_and_one_dynamic_show_argument(self) -> None:
        spec = build_spec(app)
        guide = spec.subcommands["guide"]
        listed = guide.subcommands["list"]
        show = guide.subcommands["show"]
        (topic,) = [param for param in show.params if param.is_argument]
        group_options = [opt for param in guide.params for opt in param.opts]

        assert set(guide.subcommands) == {"list", "show"}
        assert not listed.params
        assert topic.name == "topic"
        assert topic.required
        assert not topic.multiple
        assert topic.dynamic_completer == "guide_topics"
        assert group_options == ["--agent", "--human"]

        generated = {shell: generate(shell) for shell in ("bash", "zsh", "powershell")}
        bash = _generated_block(generated["bash"], "        guide)", "        resource)")
        zsh_group = _generated_braced_block(generated["zsh"], "_agentworks_guide() {")
        zsh_show = _generated_braced_block(generated["zsh"], "_agentworks_guide_show() {")
        powershell = _generated_braced_block(generated["powershell"], "        'guide' {")

        assert 'compgen -W "list show --agent --human --help"' in bash
        assert "agw guide list" in bash
        assert "list" in zsh_group and "show" in zsh_group
        assert all(option in zsh_group for option in group_options)
        assert "1:topic:_agentworks_guide_topics" in zsh_show
        assert all(option in zsh_show for option in group_options)
        assert "CompletionResult]::new('list'" in powershell
        assert "CompletionResult]::new('show'" in powershell
        assert all(f"CompletionResult]::new('{option}'" in powershell for option in group_options)
        assert "agw guide list" in powershell

    def test_generated_bash_guide_completion_follows_the_group_grammar(self) -> None:
        from agentworks.guide.service import list_guide_topics

        expected_topics = list_guide_topics().markdown.splitlines()
        script = generate("bash")

        def complete(words: list[str]) -> list[str]:
            shell_words = " ".join(f"'{word}'" for word in words)
            invocation = f"""{script}
COMP_WORDS=({shell_words})
COMP_CWORD={len(words) - 1}
_agentworks
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
            completed = subprocess.run(["bash"], input=invocation, capture_output=True, text=True, check=True)
            return [candidate for candidate in completed.stdout.splitlines() if candidate]

        assert complete(["agw", "guide", ""]) == ["list", "show", "--agent", "--human", "--help"]
        assert complete(["agw", "guide", "--"]) == ["--agent", "--human", "--help"]
        assert complete(["agw", "guide", "list", ""]) == []
        assert complete(["agw", "guide", "show", ""]) == expected_topics
        for option in ("--agent", "--human"):
            assert complete(["agw", "guide", "show", option, ""]) == expected_topics
            assert complete(["agw", "guide", "show", option, expected_topics[0], ""]) == []
        assert complete(["agw", "guide", "show", expected_topics[0], ""]) == []

    @pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed")
    def test_generated_powershell_guide_completion_follows_the_group_grammar(self, tmp_path: Path) -> None:
        import json

        from agentworks.guide.service import list_guide_topics

        expected_topics = list_guide_topics().markdown.splitlines()
        script_path = tmp_path / "agentworks-completion.ps1"
        script_path.write_text(generate("powershell"), encoding="utf-8")
        quoted_path = str(script_path).replace("'", "''")
        command = f"""
. '{quoted_path}'
function Complete([string]$line) {{
    @((TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches.CompletionText)
}}
[pscustomobject]@{{
    group = @(Complete 'agw guide --')
    agent = @(Complete 'agw guide show --agent ')
    human = @(Complete 'agw guide show --human ')
    terminal = @(Complete 'agw guide show --agent {expected_topics[0]} ')
}} | ConvertTo-Json -Depth 3 -Compress
"""
        completed = subprocess.run(
            [shutil.which("pwsh") or "pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(completed.stdout)

        assert result["group"] == ["--agent", "--human", "--help"]
        assert result["agent"] == expected_topics
        assert result["human"] == expected_topics
        assert result["terminal"] == []

    def test_database_backed_snippets_share_hidden_probe_contract(self) -> None:
        from agentworks.completions.bash import DYNAMIC_SNIPPETS as BASH_SNIPPETS
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS as POWERSHELL_SNIPPETS
        from agentworks.completions.zsh import DYNAMIC_FUNCTIONS

        assert set(BASH_SNIPPETS) == set(POWERSHELL_SNIPPETS) == set(DYNAMIC_FUNCTIONS)
        for completer in BASH_SNIPPETS:
            expected = completer in DATABASE_BACKED_DYNAMIC_COMPLETERS
            assert ("agw --completion-probe" in BASH_SNIPPETS[completer]) is expected
            assert ("agw --completion-probe" in POWERSHELL_SNIPPETS[completer]) is expected
            assert ("agw --completion-probe" in DYNAMIC_FUNCTIONS[completer]) is expected
            if expected:
                assert "2>/dev/null" in BASH_SNIPPETS[completer]
                assert "2>$null" in POWERSHELL_SNIPPETS[completer]
                assert "2>/dev/null" in DYNAMIC_FUNCTIONS[completer]

        assert frozenset(path for _completer, path in DATABASE_BACKED_DYNAMIC_COMPLETIONS) == (
            DATABASE_BACKED_COMPLETION_PATHS
        )
        assert set(RESOURCE_LIST_DYNAMIC_COMPLETIONS).isdisjoint(DATABASE_BACKED_DYNAMIC_COMPLETERS)
        for completer in RESOURCE_LIST_DYNAMIC_COMPLETIONS:
            assert "--completion-probe" not in BASH_SNIPPETS[completer]
            assert "--completion-probe" not in POWERSHELL_SNIPPETS[completer]
            assert "--completion-probe" not in DYNAMIC_FUNCTIONS[completer]
            assert "2>/dev/null" in BASH_SNIPPETS[completer]
            assert "2>$null" in POWERSHELL_SNIPPETS[completer]
            assert "2>/dev/null" in DYNAMIC_FUNCTIONS[completer]

    def test_generated_database_backed_invocations_all_carry_hidden_probe_marker(self) -> None:
        for shell in ("bash", "zsh", "powershell"):
            script = generate(shell)
            for command_path in DATABASE_BACKED_COMPLETION_PATHS:
                invocation = re.compile(
                    rf"\bagw (?P<marker>{re.escape(COMPLETION_PROBE_OPTION)} )?"
                    rf"{re.escape(' '.join(command_path))}\b[^\n]*--names-only"
                )
                matches = tuple(invocation.finditer(script))
                assert matches, f"{shell} generated no invocation for {' '.join(command_path)}"
                assert all(match.group("marker") is not None for match in matches), (
                    f"{shell} generated a marker-free database-backed invocation for {' '.join(command_path)}"
                )

    def test_database_restore_uses_native_file_completion_in_every_shell(self) -> None:
        from agentworks.completions.bash import DYNAMIC_SNIPPETS as BASH_SNIPPETS
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS as POWERSHELL_SNIPPETS
        from agentworks.completions.zsh import COMPLETER_FUNC_NAMES, DYNAMIC_FUNCTIONS

        spec = build_spec(app)
        restore = spec.subcommands["database"].subcommands["restore"]
        backup_path = next(param for param in restore.params if param.name == "backup_path")

        assert DYNAMIC_COMPLETIONS[("database.restore", "backup_path")] == "files"
        assert backup_path.required
        assert backup_path.is_argument
        assert backup_path.dynamic_completer == "files"
        assert "compgen -f" in BASH_SNIPPETS["files"]
        assert COMPLETER_FUNC_NAMES["files"] == "_agentworks_files"
        assert "_files" in DYNAMIC_FUNCTIONS["files"]
        assert "CompleteFilename" in POWERSHELL_SNIPPETS["files"]

        generated = {shell: generate(shell) for shell in ("bash", "zsh", "powershell")}
        assert 'done < <(compgen -f -- "$cur")' in generated["bash"]
        assert "_agentworks_files" in generated["zsh"]
        assert "CompleteFilename($wordToComplete)" in generated["powershell"]
        for script in generated.values():
            assert "database" in script
            assert "restore" in script
            assert "--yes" in script

    def test_generated_bash_preserves_spaces_in_restore_file_completion(self, tmp_path: Path) -> None:
        filename = "backup with spaces.db"
        (tmp_path / filename).touch()
        script = generate("bash")
        invocation = f"""{script}
COMP_WORDS=(agw database restore backup)
COMP_CWORD=3
_agentworks
printf '%s\\0' "${{COMPREPLY[@]}}"
"""

        completed = subprocess.run(
            ["bash"],
            cwd=tmp_path,
            input=invocation.encode(),
            capture_output=True,
            check=True,
        )

        assert completed.stdout.split(b"\0") == [filename.encode(), b""]

    def test_secret_verify_variadic_completion_contract_in_every_shell(self) -> None:
        spec = build_spec(app)
        verify = spec.subcommands["secret"].subcommands["verify"]
        names = next(param for param in verify.params if param.name == "names")

        assert ("secret.verify", "name") not in DYNAMIC_COMPLETIONS
        assert DYNAMIC_COMPLETIONS[("secret.verify", "names")] == "secrets"
        assert names.required
        assert names.multiple
        assert names.dynamic_completer == "secrets"

        generated = {shell: generate(shell) for shell in ("bash", "zsh", "powershell")}
        bash = _generated_block(
            generated["bash"],
            "                verify)",
            "                    ;;",
        )
        zsh = _generated_block(
            generated["zsh"],
            "_agentworks_secret_verify() {",
            "}",
        )
        powershell = _generated_braced_block(
            generated["powershell"],
            "                'verify' {",
        )

        bash_position = re.search(r"\$positional_count -(ge|eq) (\d+)", bash)
        assert bash_position is not None
        bash_operator, bash_threshold = bash_position.groups()
        assert bash_operator == "ge"
        assert bash_threshold == "0"
        assert "--allow-interaction) continue" in bash
        assert '"$cur" != -*' in bash
        assert "agw secret list --names-only" in bash

        assert "'*:names:_agentworks_secrets'" in zsh
        zsh_position = re.search(r"'([^:]+):names:_agentworks_secrets'", zsh)
        assert zsh_position is not None and zsh_position.group(1) == "*"
        assert all(zsh_position.group(1) == "*" for _position in (1, 2, 6))
        zsh_candidates = _generated_block(generated["zsh"], "_agentworks_secrets() {", "}")
        assert "agw secret list --names-only" in zsh_candidates

        powershell_position = re.search(r"\$positionalCount -(ge|eq) (\d+)", powershell)
        assert powershell_position is not None
        powershell_operator, powershell_threshold = powershell_position.groups()
        assert powershell_operator == "ge"
        assert powershell_threshold == "0"
        assert "$flagOptions" in powershell
        assert "$wordToComplete -notlike '-*'" in powershell
        assert "agw secret list --names-only" in powershell

        for block in (bash, zsh, powershell):
            assert "--allow-interaction" in block
            assert "--allow-interactive" not in block

    def test_guide_topic_completion_stream_uses_the_package_catalog(self) -> None:
        from agentworks.guide import discover_concept_shells
        from agentworks.guide.service import list_guide_topics
        from agentworks.release_notes import read_release_history

        response = list_guide_topics()
        expected = sorted(
            (
                *discover_concept_shells().names(),
                *(section.topic for section in read_release_history().sections),
            )
        )

        assert response.markdown.splitlines() == expected


class TestOptionFlagsInSpec:
    """Pin option flags that must (or must not) reach the completion tree.

    The tree is generated live from ``build_spec(app)``, so a flag rename flows
    through here. This is the direct completion-spec guard for the
    ``env show --reveal-secrets`` -> ``--resolve`` rename (R9.8): the renamed
    flag must complete, and the removed spelling must NOT appear (the
    keep-collateral-in-sync rule's completions row).
    """

    def _env_show_option_flags(self) -> list[str]:
        spec = build_spec(app)
        env_show = _walk_commands(spec)["agentworks.env.show"]
        return [opt for param in env_show.params for opt in param.opts]

    def test_resolve_flag_is_in_the_completion_spec(self) -> None:
        assert "--resolve" in self._env_show_option_flags()

    def test_removed_reveal_secrets_spelling_is_absent_from_the_spec(self) -> None:
        # --reveal-secrets was removed (breaking change, R9.8), not kept as a
        # hidden alias, so it appears nowhere in the completion spec.
        assert "--reveal-secrets" not in self._env_show_option_flags()

    def test_include_disabled_flag_reaches_the_completion_spec(self) -> None:
        # `resource list --include-disabled` is a plain boolean flag, captured
        # by the Typer introspection with no merge-tree change (it is not a
        # dynamic path element). The keep-collateral-in-sync rule's completions
        # row: a new CLI flag must reach the tree.
        spec = build_spec(app)
        resource_list = _walk_commands(spec)["agentworks.resource.list"]
        opts = [opt for param in resource_list.params for opt in param.opts]
        assert "--include-disabled" in opts

    def test_machine_output_options_reach_every_shell_completion(self) -> None:
        """Every JSON v1 command exposes the closed output choices to generated shells."""
        spec = build_spec(app)
        commands = _walk_commands(spec)
        expected_paths = (
            "agentworks.resource.list",
            "agentworks.resource.kinds",
            "agentworks.graph.show",
            "agentworks.secret.list",
            "agentworks.secret.describe",
            "agentworks.vm.list",
            "agentworks.vm.describe",
            "agentworks.workspace.list",
            "agentworks.workspace.describe",
            "agentworks.agent.list",
            "agentworks.agent.describe",
            "agentworks.session.list",
            "agentworks.session.describe",
            "agentworks.console.list",
            "agentworks.console.describe",
            "agentworks.doctor",
        )
        for path in expected_paths:
            output_param = next(param for param in commands[path].params if "--output" in param.opts)
            assert output_param.choices == ["human", "json"]

        for shell in ("bash", "zsh", "powershell"):
            assert "--output" in generate(shell)

    def test_session_resume_is_discoverable_and_restart_is_absent(self) -> None:
        spec = build_spec(app)
        session = _walk_commands(spec)["agentworks.session"]
        assert "resume" in session.subcommands
        assert "restart" not in session.subcommands
        for parameter in ("name", "vm", "workspace", "agent"):
            assert ("session.resume", parameter) in DYNAMIC_COMPLETIONS
            assert ("session.restart", parameter) not in DYNAMIC_COMPLETIONS


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
        ("admin_templates", "admin-template"),
        ("sites", "vm-site"),
    )

    def test_bash_snippets_source_from_registry(self) -> None:
        from agentworks.completions.bash import DYNAMIC_SNIPPETS

        for completer_id, kind in self._REGISTRY_SOURCED:
            snippet = DYNAMIC_SNIPPETS[completer_id]
            assert f"--kind {kind}" in snippet, (
                f"bash {completer_id!r} should source from Registry (--kind {kind}); got: {snippet!r}"
            )
            assert "sed " not in snippet, f"bash {completer_id!r} still uses sed-over-TOML: {snippet!r}"

    def test_zsh_functions_source_from_registry(self) -> None:
        from agentworks.completions.zsh import DYNAMIC_FUNCTIONS

        for completer_id, kind in self._REGISTRY_SOURCED:
            fn = DYNAMIC_FUNCTIONS[completer_id]
            assert f"--kind {kind}" in fn, (
                f"zsh {completer_id!r} should source from Registry (--kind {kind}); got: {fn!r}"
            )
            assert "sed " not in fn, f"zsh {completer_id!r} still uses sed-over-TOML: {fn!r}"

    def test_powershell_snippets_source_from_registry(self) -> None:
        from agentworks.completions.powershell import DYNAMIC_SNIPPETS

        for completer_id, kind in self._REGISTRY_SOURCED:
            snippet = DYNAMIC_SNIPPETS[completer_id]
            assert f"--kind {kind}" in snippet, (
                f"powershell {completer_id!r} should source from Registry (--kind {kind}); got: {snippet!r}"
            )
            assert "Select-String" not in snippet, (
                f"powershell {completer_id!r} still uses Select-String regex over config.toml: {snippet!r}"
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

    def test_bash_install_drops_agw_alias_symlink(self, monkeypatch, tmp_path) -> None:
        """Bash's lazy autoload is keyed on the command name -- typing `agw`
        looks for a file named `agw`, not `agentworks`. Install must drop a
        symlink so both names trigger the same script."""
        from typer.testing import CliRunner

        # Redirect home via Path.home itself: setenv("HOME") only works on
        # POSIX; Path.home() reads USERPROFILE on Windows.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = CliRunner().invoke(app, ["completion", "install", "--shell", "bash"])
        assert result.exit_code == 0

        completions_dir = tmp_path / ".local" / "share" / "bash-completion" / "completions"
        primary = completions_dir / "agentworks"
        alias = completions_dir / "agw"
        assert primary.is_file()
        # POSIX gets a symlink; Windows (no symlink privilege) falls back to a
        # content copy. Either way the alias resolves to the same script.
        assert alias.is_symlink() or alias.is_file()
        assert alias.read_text() == primary.read_text()

    def test_zsh_install_drops_agw_alias_symlink(self, monkeypatch, tmp_path) -> None:
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

        result = CliRunner().invoke(app, ["completion", "install", "--shell", "zsh"])
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

    def test_bash_uninstall_removes_script_and_alias(self, monkeypatch, tmp_path) -> None:
        from typer.testing import CliRunner

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        completions_dir = tmp_path / ".local" / "share" / "bash-completion" / "completions"
        completions_dir.mkdir(parents=True)
        (completions_dir / "agentworks").write_text("x")
        (completions_dir / "agw").write_text("x")

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", "bash"])
        assert result.exit_code == 0
        assert not (completions_dir / "agentworks").exists()
        assert not (completions_dir / "agw").exists()

    def test_zsh_uninstall_removes_script_and_alias(self, monkeypatch, tmp_path) -> None:
        from typer.testing import CliRunner

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("ZSH_CUSTOM", raising=False)
        zfunc = tmp_path / ".zfunc"
        zfunc.mkdir()
        (zfunc / "_agentworks").write_text("x")
        (zfunc / "_agw").write_text("x")

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", "zsh"])
        assert result.exit_code == 0
        assert not (zfunc / "_agentworks").exists()
        assert not (zfunc / "_agw").exists()

    def test_powershell_uninstall_removes_script_and_profile_line(self, monkeypatch, tmp_path) -> None:
        from typer.testing import CliRunner

        from agentworks.completions import install

        profile = tmp_path / "profile.ps1"
        completions_dir = tmp_path / "Completions"
        completions_dir.mkdir()
        script = completions_dir / "agentworks.ps1"
        script.write_text("x")
        profile.write_text(f'Write-Host hi\n. "{script}"\n')

        monkeypatch.setattr(install, "_query_powershell_profile", lambda: profile)

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", "powershell"])
        assert result.exit_code == 0
        assert not script.exists()
        assert "agentworks.ps1" not in profile.read_text()
        # Unrelated profile content is preserved.
        assert "Write-Host hi" in profile.read_text()

    @pytest.mark.parametrize("shell", ["bash", "zsh", "powershell"])
    def test_uninstall_when_nothing_installed_is_clean(self, monkeypatch, tmp_path, shell) -> None:
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

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", shell])
        assert result.exit_code == 0
        # Lowercase compare: the powershell message uses "PowerShell".
        assert f"no {shell} completions found" in result.stdout.lower()

    def test_powershell_uninstall_fails_when_no_binary(self, monkeypatch) -> None:
        """If neither `pwsh` nor `powershell` is on PATH, uninstall exits
        non-zero with a clear error rather than silently succeeding."""
        from typer.testing import CliRunner

        from agentworks.completions import install

        monkeypatch.setattr(install, "_query_powershell_profile", lambda: None)

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", "powershell"])
        assert result.exit_code != 0
        assert "could not determine PowerShell $PROFILE path" in result.stderr

    def test_powershell_uninstall_preserves_user_lines_mentioning_filename(self, monkeypatch, tmp_path) -> None:
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
        profile.write_text("\n".join([user_comment, installer_line, user_conditional, user_alt_dotsource]) + "\n")
        monkeypatch.setattr(install, "_query_powershell_profile", lambda: profile)

        result = CliRunner().invoke(app, ["completion", "uninstall", "--shell", "powershell"])
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
        # -ge positional-count check (matches every position from the
        # variadic's offset on while ignoring recognized options).
        assert "positional_count -ge" in output
        # And the standard -eq for non-variadic positionals still works.
        assert "positional_count -eq" in output

    def test_powershell_uses_ge_for_variadic(self) -> None:
        output = generate("powershell")
        # Same idea: -ge for the variadic.
        assert "positionalCount -ge" in output
        assert "positionalCount -eq" in output


class TestKindsSourcedCompleter:
    """The resource_kinds completer sources `agw resource kinds
    --names-only` in all three shells -- the config-free static path --
    not a scrape of the resource list."""

    def test_all_shells_call_resource_kinds(self) -> None:
        from agentworks.completions.bash import (
            DYNAMIC_SNIPPETS as BASH_SNIPPETS,
        )
        from agentworks.completions.powershell import (
            DYNAMIC_SNIPPETS as PS_SNIPPETS,
        )
        from agentworks.completions.zsh import DYNAMIC_FUNCTIONS

        for shell, source in (
            ("bash", BASH_SNIPPETS["resource_kinds"]),
            ("zsh", DYNAMIC_FUNCTIONS["resource_kinds"]),
            ("powershell", PS_SNIPPETS["resource_kinds"]),
        ):
            assert "resource kinds --names-only" in source, (
                f"{shell} resource_kinds should call the config-free kinds command; got: {source!r}"
            )
            assert "resource list" not in source, f"{shell} resource_kinds still scrapes resource list: {source!r}"


class TestStaticChoiceCompletion:
    """click.Choice values reach the completion tree. Typer wraps
    ``click_type=`` params in ``FuncParamType`` (the real type hides on
    ``.func``); the spec extraction unwraps one level -- without it,
    every Choice-typed option silently loses static completion (the
    generators' choices branches were dead code)."""

    def test_shell_option_choices_extracted(self) -> None:
        from agentworks.cli import app
        from agentworks.completions.spec import build_spec

        show = build_spec(app).subcommands["completion"].subcommands["show"]
        by_name = {p.name: p.choices for p in show.params}
        assert by_name["shell"] == ["bash", "zsh", "powershell", "pwsh"]

    def test_sample_kind_completes_dynamically(self) -> None:
        # The sample-kind argument is a plain string (no click.Choice: any
        # typed kind, capability kinds and typos included, must reach the
        # service layer for a clean, kind-aware domain error, issue #276).
        # It completes via the config-free resource_kinds dynamic completer,
        # the same one `resource list --kind` uses.
        from agentworks.cli import app
        from agentworks.completions.spec import build_spec

        sample = build_spec(app).subcommands["resource"].subcommands["sample"]
        (kind,) = [p for p in sample.params if p.name == "kind"]
        assert not kind.choices
        assert kind.dynamic_completer == "resource_kinds"

    def test_schema_kind_completes_dynamically(self) -> None:
        # `resource schema` takes its kind the same way `resource sample`
        # does, and for the same reason: a plain string, so a capability
        # kind or a typo reaches the service layer and gets a clean domain
        # error instead of a click.Choice parse failure.
        from agentworks.cli import app
        from agentworks.completions.spec import build_spec

        schema = build_spec(app).subcommands["resource"].subcommands["schema"]
        (kind,) = [p for p in schema.params if p.name == "kind"]
        assert not kind.choices
        assert kind.dynamic_completer == "resource_kinds"
        assert [opt for param in schema.params for opt in param.opts] == ["--install"]

    def test_graph_choices_and_depth_suggestions_are_distinct(self) -> None:
        show = build_spec(app).subcommands["graph"].subcommands["show"]
        by_name = {param.name: param for param in show.params}
        assert by_name["direction"].choices == ["dependencies", "dependents", "both"]
        assert by_name["output_format"].choices == ["human", "json"]
        assert by_name["depth"].choices is None
        assert by_name["depth"].suggestions == ["1", "2", "3", "all"]

    def test_all_shells_emit_graph_static_values(self) -> None:
        generated = {shell: generate(shell) for shell in ("bash", "zsh", "powershell")}
        for script in generated.values():
            for value in ("dependencies", "dependents", "both", "1", "2", "3", "all"):
                assert value in script

    def test_all_shells_emit_shell_choices(self) -> None:
        from agentworks.cli import app
        from agentworks.completions.bash import generate_bash
        from agentworks.completions.powershell import generate_powershell
        from agentworks.completions.spec import build_spec
        from agentworks.completions.zsh import generate_zsh

        spec = build_spec(app)
        # Shell-specific emission shapes (the zsh/bash forms put every
        # choice on one line; powershell emits one CompletionResult per
        # choice).
        assert ":shell:(bash zsh powershell pwsh)" in generate_zsh(spec, "t")
        assert 'compgen -W "bash zsh powershell pwsh"' in generate_bash(spec, "t")
        ps = generate_powershell(spec, "t")
        assert "::new('bash', 'bash'" in ps
        assert "::new('pwsh', 'pwsh'" in ps


@pytest.mark.parametrize(
    "command_path",
    sorted(DATABASE_BACKED_COMPLETION_PATHS),
)
def test_legacy_database_completion_recognizes_every_inventory_path(command_path: tuple[str, str]) -> None:
    assert is_legacy_database_completion([*command_path, "--names-only"])


@pytest.mark.parametrize(
    "argv",
    [
        ["workspace", "list", "--vm", "alpha", "--names-only"],
        ["session", "list", "--names-only", "--no-status", "--workspace=alpha"],
        ["agent", "list", "--vm=alpha", "--names-only"],
        ["console", "list", "--agent", "alpha", "--names-only"],
    ],
)
def test_legacy_database_completion_recognizes_shipped_option_shapes(argv: list[str]) -> None:
    assert is_legacy_database_completion(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["secret", "list", "--names-only"],
        ["vm", "list"],
        ["vm", "list", "--names-only", "--output", "json"],
        ["workspace", "list", "--names-only", "--agent", "alpha"],
        ["resource", "list", "--kind", "vm-site", "--include-disabled", "--names-only"],
        ["resource", "list", "--names-only", "--unknown"],
        ["vm", "describe", "--names-only"],
    ],
)
def test_legacy_database_completion_rejects_nearby_operator_invocations(argv: list[str]) -> None:
    assert not is_legacy_database_completion(argv)


def _installed_agw() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return Path(sys.executable).with_name(f"agw{suffix}")


def _isolated_subprocess_env(home: Path) -> dict[str, str]:
    """Build a subprocess environment isolated to `home`, verified by
    construction rather than assumed from a POSIX-only test run.

    `HOME` governs ``Path.home()`` on POSIX; `USERPROFILE` governs it on
    Windows. `agw`'s own `CONFIG_DIR` is `Path.home() / ".config" /
    "agentworks"` (computed at import time), so whichever variable the
    platform actually reads is what determines which config and state
    database a spawned `agw` touches. Setting only one leaves the other
    platform silently reading the operator's real home and state (a live
    Windows review of issue #503 caught exactly this: tests that isolated
    only `HOME` spawned probes against the operator's real config and
    database, and passed anyway for an unrelated reason). Resolving
    `Path.home()` in a throwaway child with this exact environment, rather
    than trusting the two assignments above, is what makes this correct by
    construction: a future platform where neither variable governs fails
    this assertion loudly instead of silently reading real state.
    """
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    resolved = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(resolved.stdout.strip()).resolve() == home.resolve()
    return env


def _assert_no_committed_writes(config_dir: Path, database_path: Path, before: dict[str, bytes]) -> None:
    """Assert a completion probe committed no writes to the state database.

    The main database file's bytes never change and no rollback-journal
    appears. Merely opening a real (non-immutable) read connection to a
    WAL-mode database can create or update the `-wal`/`-shm` coordination
    sidecars, even when the probe ultimately refuses; that is intrinsic
    SQLite WAL-reader bookkeeping (issue #502's fix trades the old veto on
    those sidecars' mere existence for accepting this harmless side effect
    of reading through them correctly), so a freshly appearing `-wal` is
    only checked for staying empty, and `-shm` is not checked at all.
    """
    after = {entry.name: entry.read_bytes() for entry in config_dir.iterdir()}
    assert after[database_path.name] == before[database_path.name]
    wal_name = f"{database_path.name}-wal"
    if wal_name in after:
        assert after[wal_name] == b""
    shm_name = f"{database_path.name}-shm"
    unrelated = (set(before) | set(after)) - {wal_name, shm_name}
    for name in unrelated:
        assert after.get(name) == before.get(name), name


def _write_warning_config(home: Path) -> Path:
    config_dir = home / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    public_key = home / "id_ed25519.pub"
    private_key = home / "id_ed25519"
    public_key.touch()
    private_key.touch()
    (config_dir / "config.toml").write_text(
        "[operator]\n"
        f"ssh_public_key = {str(public_key)!r}\n"
        f"ssh_private_key = {str(private_key)!r}\n"
        "unexpected_completion_test_key = true\n"
    )
    return config_dir


@pytest.mark.parametrize("command_path", sorted(DATABASE_BACKED_COMPLETION_PATHS))
def test_marker_probe_refuses_stale_database_for_every_dynamic_path_without_side_effects(
    tmp_path: Path,
    command_path: tuple[str, str],
) -> None:
    from agentworks.db import LATEST_VERSION, Database, backup_directory

    config_dir = _write_warning_config(tmp_path)
    database_path = config_dir / "agentworks.db"
    Database(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.execute("DELETE FROM schema_version WHERE version = ?", (LATEST_VERSION,))
    connection.commit()
    connection.close()
    before = {entry.name: entry.read_bytes() for entry in config_dir.iterdir()}
    env = _isolated_subprocess_env(tmp_path)
    env["PATH"] = f"{_installed_agw().parent}{os.pathsep}{env.get('PATH', '')}"
    command = f"agw --completion-probe {' '.join(command_path)} --names-only 2>/dev/null"

    completed = subprocess.run(
        ["bash", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == ""
    _assert_no_committed_writes(config_dir, database_path, before)
    assert not backup_directory(database_path).exists()


def test_shell_wrapped_probe_suppresses_config_warning_and_preserves_database_bytes(tmp_path: Path) -> None:
    from agentworks.db import Database

    config_dir = _write_warning_config(tmp_path)
    database_path = config_dir / "agentworks.db"
    Database(database_path).close()
    before = {entry.name: entry.read_bytes() for entry in config_dir.iterdir()}
    env = _isolated_subprocess_env(tmp_path)
    env["PATH"] = f"{_installed_agw().parent}{os.pathsep}{env.get('PATH', '')}"

    direct = subprocess.run(
        [str(_installed_agw()), "--completion-probe", "session", "list", "--names-only"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    completed = subprocess.run(
        ["bash", "-c", "agw --completion-probe session list --names-only 2>/dev/null"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert direct.returncode == 0
    assert direct.stdout == ""
    assert "Config: unexpected keys in [operator]" in direct.stderr
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    _assert_no_committed_writes(config_dir, database_path, before)


def test_shell_wrapped_probe_consumes_empty_stdout_when_config_is_invalid(tmp_path: Path) -> None:
    from agentworks.db import Database

    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    Database(config_dir / "agentworks.db").close()
    (config_dir / "config.toml").write_text("[database\n")
    before = {entry.name: entry.read_bytes() for entry in config_dir.iterdir()}
    env = _isolated_subprocess_env(tmp_path)
    env["PATH"] = f"{_installed_agw().parent}{os.pathsep}{env.get('PATH', '')}"

    completed = subprocess.run(
        ["bash", "-c", "agw --completion-probe resource list --names-only 2>/dev/null"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert {entry.name: entry.read_bytes() for entry in config_dir.iterdir()} == before


@pytest.mark.skipif(os.name != "posix", reason="legacy completion TTY shape uses a POSIX pseudo-terminal")
def test_marker_free_legacy_probe_refuses_stale_database_without_side_effects(tmp_path: Path) -> None:
    import pty

    from agentworks.db import LATEST_VERSION, Database, backup_directory

    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    database_path = config_dir / "agentworks.db"
    Database(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.execute("DELETE FROM schema_version WHERE version = ?", (LATEST_VERSION,))
    connection.commit()
    connection.close()
    before = {entry.name: entry.read_bytes() for entry in config_dir.iterdir()}
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(_installed_agw()), "vm", "list", "--names-only"],
        env=_isolated_subprocess_env(tmp_path),
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(slave)
    stdout, _stderr = process.communicate(timeout=10)
    os.close(master)

    assert process.returncode != 0
    assert stdout == ""
    _assert_no_committed_writes(config_dir, database_path, before)
    assert not backup_directory(database_path).exists()
