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
    # Build the expected value with the same Path semantics as the
    # implementation so the assertion is portable: on POSIX (CI) the
    # backslashes in the env value are a single opaque component, on
    # Windows they're separators. Either way base / "agentworks" / "wsl"
    # must match.
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    expected = Path(r"C:\Users\test\AppData\Local") / "agentworks" / "wsl"
    assert wsl2._wsl_base_path() == expected


def test_cache_dir_is_under_local_app_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    expected = Path(r"C:\Users\test\AppData\Local") / "agentworks" / "cache"
    assert wsl2._cache_dir() == expected


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
