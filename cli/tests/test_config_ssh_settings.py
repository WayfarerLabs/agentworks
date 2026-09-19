"""Additive SSH settings validate external policy without admitting connections."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import tomli_w

from agentworks.config import ConfigError, load_config
from agentworks.config.loaders_core import _load_operator

# Native path expansion and rendering must work on both workstation families.
pytestmark = pytest.mark.windows


def _operator(tmp_path: Path) -> dict[str, object]:
    return {
        "ssh_public_key": str(tmp_path / "legacy.pub"),
        "ssh_private_key": str(tmp_path / "legacy"),
    }


def _config(tmp_path: Path, settings: object, *, legacy_files: bool = True) -> Path:
    operator = _operator(tmp_path)
    operator["ssh"] = settings
    if legacy_files:
        (tmp_path / "legacy.pub").touch()
        (tmp_path / "legacy").touch()
    config = tmp_path / "config.toml"
    config.write_text(tomli_w.dumps({"operator": operator}), encoding="utf-8")
    return config


def test_absent_settings_preserve_legacy_values(tmp_path: Path) -> None:
    operator = _operator(tmp_path)
    operator.update(ssh_config="legacy-config", ssh_host_prefix="custom--", typo=True)
    issues: list[str] = []
    loaded = _load_operator({"operator": operator}, issues, workload_gated_issues_fatal=False)
    assert loaded.ssh is None
    assert loaded.ssh_private_key == tmp_path / "legacy"
    assert loaded.ssh_public_key == tmp_path / "legacy.pub"
    assert loaded.ssh_config == Path("legacy-config")
    assert loaded.ssh_host_prefix == "custom--"
    # The existing missing-key and unknown-key warnings remain nonfatal here.
    assert len(issues) == 3


def test_new_settings_default_identity_and_keep_legacy_files(tmp_path: Path) -> None:
    config = _config(tmp_path, {"trust_store": str(tmp_path / "not-created")})
    loaded = load_config(config, warn_issues=False)
    assert loaded.config_issues == ()
    settings = loaded.operator.ssh
    assert settings is not None
    assert settings.trust_store == tmp_path / "not-created"
    assert settings.identity_file == loaded.operator.ssh_private_key
    assert settings.agent_socket is None
    assert settings.ssh_executable == "ssh"
    assert settings.keepalive_interval == 15
    assert settings.keepalive_count_max == 4
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.keepalive_interval = 0  # type: ignore[misc]
    assert not settings.trust_store.exists()


def test_explicit_settings_expand_native_paths_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _config(
        tmp_path,
        {
            "trust_store": "~/new trust",
            "identity_file": "~/new identity",
            "agent_socket": "~/new socket",
            "ssh_executable": "~/client directory/ssh",
            "keepalive_interval": 0,
            "keepalive_count_max": 1,
        },
    )
    before = config.read_bytes()
    loaded = load_config(config, warn_issues=False)
    settings = loaded.operator.ssh
    assert settings is not None
    assert settings.trust_store == tmp_path / "new trust"
    assert settings.identity_file == tmp_path / "new identity"
    assert settings.agent_socket == str(tmp_path / "new socket")
    assert settings.ssh_executable == str(tmp_path / "client directory" / "ssh")
    assert settings.keepalive_interval == 0
    assert settings.keepalive_count_max == 1
    assert loaded.operator.ssh_private_key == tmp_path / "legacy"
    assert config.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {"legacy", "legacy.pub", "config.toml"}


@pytest.mark.parametrize("settings", [None, False, "ssh", [], {}, {"trust_store": False}])
def test_invalid_table_and_required_trust_shape(tmp_path: Path, settings: object) -> None:
    operator = _operator(tmp_path)
    operator["ssh"] = settings
    with pytest.raises(ConfigError):
        _load_operator({"operator": operator}, [], workload_gated_issues_fatal=False)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("identity_file", False),
        ("identity_file", ""),
        ("agent_socket", False),
        ("ssh_executable", []),
        ("ssh_executable", ""),
        ("ssh_executable", "-ssh"),
        ("ssh_executable", "ssh -v"),
        ("ssh_executable", "./ssh"),
        ("keepalive_interval", True),
        ("keepalive_interval", -1),
        ("keepalive_interval", 2_147_483_648),
        ("keepalive_interval", "15"),
        ("keepalive_count_max", False),
        ("keepalive_count_max", 0),
        ("keepalive_count_max", 2_147_483_648),
        ("keepalive_count_max", 1.5),
        ("proxy_command", "arbitrary command"),
        ("options", {"StrictHostKeyChecking": "no"}),
        ("trust_stroe", "typo"),
    ],
)
def test_reject_invalid_values_and_unsupported_options(tmp_path: Path, key: str, value: object) -> None:
    config = _config(tmp_path, {"trust_store": str(tmp_path / "trust"), key: value})
    with pytest.raises(ConfigError):
        load_config(config, warn_issues=False)


@pytest.mark.parametrize("key", ["trust_store", "identity_file", "agent_socket", "ssh_executable"])
@pytest.mark.parametrize("suffix", ["%h", "$HOME", 'quote"', "control\n", "control\x7f"])
def test_refuse_client_path_expansion(tmp_path: Path, key: str, suffix: str) -> None:
    config = _config(tmp_path, {"trust_store": str(tmp_path / "trust"), key: str(tmp_path / suffix)})
    with pytest.raises(ConfigError):
        load_config(config, warn_issues=False)


@pytest.mark.parametrize("key", ["trust_store", "identity_file", "agent_socket"])
def test_refuse_relative_paths(tmp_path: Path, key: str) -> None:
    config = _config(tmp_path, {"trust_store": str(tmp_path / "trust"), key: "relative"})
    with pytest.raises(ConfigError):
        load_config(config, warn_issues=False)


def test_local_load_needs_no_workload_files_or_client(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"trust_store": str(tmp_path / "missing"), "ssh_executable": "not-installed"},
        legacy_files=False,
    )
    with patch("subprocess.Popen", side_effect=AssertionError("config dispatched a process")):
        loaded = load_config(config, warn_issues=False, workload_gated_issues_fatal=False)
    assert loaded.operator.ssh is not None
    assert len(loaded.config_issues) == 2  # Existing missing legacy key warnings.
    assert list(tmp_path.iterdir()) == [config]


def test_fresh_config_load_has_no_legacy_execution_or_database_dependency(tmp_path: Path) -> None:
    config = _config(tmp_path, {"trust_store": str(tmp_path / "missing")}, legacy_files=False)
    script = """
import importlib.abc
import sys
from pathlib import Path

class RefuseLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in (
            "agentworks.ssh", "agentworks.ssh_config", "agentworks.ssh_identity",
            "agentworks.transports", "agentworks.runners", "agentworks.db",
        )):
            raise AssertionError("configuration imported " + fullname)

sys.meta_path.insert(0, RefuseLegacy())
from agentworks.config import load_config
config = load_config(Path(sys.argv[1]), warn_issues=False, workload_gated_issues_fatal=False)
assert config.operator.ssh is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config)],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
