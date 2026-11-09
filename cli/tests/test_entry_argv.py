"""The CLI hands its arguments to the commands exactly as the shell passed them, on Windows too."""

from __future__ import annotations

import os
import sys
import types
from typing import TYPE_CHECKING

import pytest
import typer
import typer.core

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.windows
def test_windows_arguments_reach_the_command_unexpanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Typer expands wildcards, ``~`` and ``%VAR%`` in argv on Windows by default, even after ``--``.

    A forwarded command (``vm exec``, ``agent exec``) would then send the
    workstation's filenames and environment values to the guest instead of
    the literals the operator quoted.
    """
    from agentworks import cli as cli_mod

    received: list[str] = []
    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
    def run(ctx: typer.Context) -> None:
        received.extend(ctx.args)

    (tmp_path / "workstation-file.txt").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGW_ENTRY_CANARY", "expanded")
    # Only Typer's own module sees Windows, so its expansion runs here as it would there.
    monkeypatch.setattr(typer.core, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(cli_mod, "app", test_app)
    literals = ["*", "~", "%AGW_ENTRY_CANARY%", "$AGW_ENTRY_CANARY"]
    monkeypatch.setattr(sys, "argv", ["agw", "run", "--", *literals])
    monkeypatch.setenv("AGW_DEBUG", "")

    # The setup reaches Typer's Windows expansion: its default changes the literals.
    with pytest.raises(SystemExit):
        test_app(args=None)
    assert received != literals
    received.clear()

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 0
    assert received == literals
