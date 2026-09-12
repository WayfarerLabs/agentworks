"""Native executable discovery uses only a fixture-owned login environment."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworks.errors import ConnectivityError, ExternalError
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native import setup_user
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeTool
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from agentworks.plugins._harness_native.native_files import NativeFiles
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local execution of Linux guest setup helpers")


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalFixtureTransport]:
    target = LocalFixtureTransport(tmp_path / "native-login")
    monkeypatch.setattr(tempfile, "tempdir", str(target.root / "tmp"))
    target.environment["PATH"] = "/usr/bin:/bin"
    yield target
    shutil.rmtree(target.root)


def executable(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/python3\nimport json, os\n"
        f"print(json.dumps({{'selected': {label!r}, 'environment': dict(os.environ)}}))\n"
    )
    path.chmod(0o700)


def test_login_path_discovery_is_once_and_preserves_prepared_environment(target: LocalFixtureTransport) -> None:
    executable(target.home / ".local/bin/codex", "login")
    (target.home / ".profile").write_text(
        'export PATH="$HOME/.local/bin:/usr/bin:/bin"\n'
        "export TOKEN=profile-value AGENTWORKS_VM=profile-value\n"
        'printf x >> "$HOME/profile-runs"\n'
        "printf 'startup noise that is not JSON'\n"
    )
    prepared = {"TOKEN": "private-fixture-token", "AGENTWORKS_VM": "fixture-vm"}
    with NativeFiles(target) as files:
        cli = NativeCLI(
            "codex", files, home=str(target.home), config_root=str(target.home / ".codex"), environment=prepared
        )
        first = cli.command(["inspect"], structured=True)
        second = cli.command(["inspect"], structured=True)
    assert first == second
    assert isinstance(first, dict)
    assert first["selected"] == "login"
    assert first["environment"]["TOKEN"] == prepared["TOKEN"]
    assert first["environment"]["AGENTWORKS_VM"] == prepared["AGENTWORKS_VM"]
    assert first["environment"]["HOME"] == str(target.home)
    assert (target.home / "profile-runs").read_text() == "x"
    assert not list((target.root / "tmp").iterdir())
    assert "private-fixture-token" not in "".join(target.commands + target.logged_output)
    assert "startup noise" not in "".join(target.logged_output)


def test_explicit_prepared_path_wins_over_login_path(target: LocalFixtureTransport) -> None:
    executable(target.home / ".local/bin/codex", "login")
    executable(target.home / "explicit/codex", "explicit")
    explicit_path = str(target.home / "explicit") + ":/usr/bin:/bin"
    with NativeFiles(target) as files:
        cli = NativeCLI(
            "codex",
            files,
            home=str(target.home),
            config_root=str(target.home / ".codex"),
            environment={"PATH": explicit_path},
        )
        result = cli.command(["inspect"], structured=True)
    assert isinstance(result, dict)
    assert result["selected"] == "explicit"
    assert result["environment"]["PATH"] == explicit_path


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_real_marketplace_discovery_reuses_user_login_tool_under_isolated_home(
    target: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(target, tool)
    installed = (target.root / "bin" / tool).resolve()
    executable_path = target.home / ".local/bin" / tool
    executable_path.parent.mkdir(parents=True)
    node = target.root / "bin" / "node"
    if node.exists():
        executable_path.with_name("node").symlink_to(node.resolve())
    observations = target.root / "native-environments.jsonl"
    executable_path.write_text(
        "#!/usr/bin/python3\nimport json, os, sys\n"
        f"with open({str(observations)!r}, 'a') as output:\n"
        "    output.write(json.dumps({key:os.environ.get(key) for key in "
        "('HOME','CODEX_HOME','CLAUDE_CONFIG_DIR')})+'\\n')\n"
        f"os.execv({str(installed)!r}, [{tool!r}, *sys.argv[1:]])\n"
    )
    executable_path.chmod(0o700)
    (target.home / ".profile").write_text(
        'export PATH="$HOME/.local/bin:/usr/bin:/bin"\nprintf x >> "$HOME/profile-runs"\n'
    )
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool, NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]), invocation(target, claims)
    )
    observed = [json.loads(line) for line in observations.read_text().splitlines()]
    assert any(item["HOME"] != str(target.home) for item in observed)
    assert any(item["HOME"] == str(target.home) for item in observed)
    for item in observed:
        if item["HOME"] != str(target.home):
            assert item["HOME"].startswith(str(target.root / "tmp"))
            assert item["CODEX_HOME"] == item["HOME"] + "/.codex"
            assert item["CLAUDE_CONFIG_DIR"] == item["HOME"] + "/.claude"
    assert (target.home / "profile-runs").read_text() == "x"
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}
    assert not list((target.root / "tmp").iterdir())


def test_home_dependent_launcher_does_not_escape_discovery_isolation(target: LocalFixtureTransport) -> None:
    launcher = target.home / ".local/bin/codex"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        f"#!/usr/bin/python3\nimport os, sys\nsys.exit(0 if os.environ['HOME'] == {str(target.home)!r} else 17)\n"
    )
    launcher.chmod(0o700)
    with NativeFiles(target) as files:
        cli = NativeCLI("codex", files, home=str(target.home), config_root=str(target.home / ".codex"))
        with pytest.raises(ExternalError):
            cli.discover("fixture-source")
    assert not (target.home / ".codex").exists()
    assert not (target.home / ".claude").exists()
    assert not list((target.root / "tmp").iterdir())


@pytest.mark.parametrize("resolved", [False, True])
def test_native_response_transfer_preserves_transport_error(target, monkeypatch, resolved):
    executable(target.home / ".local/bin/codex", "login")
    with NativeFiles(target) as files:
        cli = NativeCLI("codex", files, home=str(target.home), config_root=str(target.home / ".codex"))
        if resolved:
            cli._resolve_command()
        failure = ConnectivityError("fixture transport disconnected")

        def disconnect(*args, **kwargs):
            raise failure

        monkeypatch.setattr(target, "copy_from", disconnect)
        with pytest.raises(ConnectivityError) as caught:
            cli.command(["inspect"], structured=True)
        assert caught.value is failure
