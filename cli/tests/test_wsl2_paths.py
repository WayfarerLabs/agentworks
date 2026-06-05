"""WSL2 provisioner path resolution -- guards against the %LOCALAPPDATA% bug.

PowerShell does not expand %VAR% syntax (that's cmd.exe), so paths must be
resolved in Python before being handed to PowerShell or wsl.exe. A previous
regression passed the literal string ``%LOCALAPPDATA%\\agentworks\\wsl`` to
``New-Item`` and ``wsl --import``, which created a folder named
``%LOCALAPPDATA%`` in whatever directory the CLI happened to be run from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.vms.provisioners import wsl2


def test_local_app_data_resolves_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert wsl2._local_app_data() == Path(r"C:\Users\test\AppData\Local")


def test_local_app_data_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        wsl2._local_app_data()


def test_wsl_base_path_is_under_local_app_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert wsl2._wsl_base_path() == Path(r"C:\Users\test\AppData\Local\agentworks\wsl")


def test_cache_dir_is_under_local_app_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert wsl2._cache_dir() == Path(r"C:\Users\test\AppData\Local\agentworks\cache")


def test_paths_never_contain_unexpanded_percent_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: the resolved paths must not contain '%LOCALAPPDATA%'."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert "%" not in str(wsl2._wsl_base_path())
    assert "%" not in str(wsl2._cache_dir())


def test_ps_quote_wraps_in_single_quotes() -> None:
    assert wsl2._ps_quote(r"C:\Users\test\AppData\Local\agentworks\wsl") == (
        r"'C:\Users\test\AppData\Local\agentworks\wsl'"
    )


def test_ps_quote_escapes_embedded_single_quote() -> None:
    """PowerShell single-quoted strings escape ' by doubling it."""
    assert wsl2._ps_quote(r"C:\path with 'quote") == r"'C:\path with ''quote'"
