"""Actual native setup helpers exercised against isolated local files and CLIs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from agentworks.capabilities.harness_integration.native import setup_user, setup_workspace
from agentworks.capabilities.harness_integration.native_cli import NativeCLI, NativeTool
from agentworks.capabilities.harness_integration.native_config import NativeUserConfig, NativeWorkspaceConfig
from agentworks.capabilities.harness_integration.native_files import NativeFiles
from agentworks.capabilities.harness_integration.settings import SettingsMapping
from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
from agentworks.db import VMRow
from agentworks.errors import ConfigError, ExternalError, StateError
from agentworks.harness_setup.model import NativeClaim
from tests.native_setup_fixtures import (
    LocalFixtureTransport,
    invocation,
    market_fixture,
    record,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local execution of Linux guest setup helpers")


@pytest.fixture
def transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalFixtureTransport]:
    root = tmp_path / "native-fixture"
    root.mkdir()
    local_tmp = root / "local-tmp"
    local_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(local_tmp))
    fixture = LocalFixtureTransport(root)
    yield fixture
    shutil.rmtree(root)


def test_guarded_snapshot_and_atomic_publish(transport: LocalFixtureTransport) -> None:
    destination = transport.home / ".claude/settings.json"
    content = b'{"private-fixture-value": true}'
    with NativeFiles(transport) as files:
        assert files.read(str(destination)) is None
        files.publish(str(destination), content, expected=None)
        assert files.read(str(destination)) == content
        assert destination.stat().st_mode & 0o777 == 0o600
        assert destination.parent.stat().st_mode & 0o777 == 0o700
        with pytest.raises(StateError):
            files.publish(str(destination), b"{}", expected=None)
        assert destination.read_bytes() == content
    assert not list((transport.root / "tmp").iterdir())
    assert "private-fixture-value" not in "".join(transport.commands + transport.logged_output)


@pytest.mark.parametrize("link_parent", [False, True])
def test_links_cannot_redirect_native_files(transport: LocalFixtureTransport, link_parent: bool) -> None:
    outside = transport.root / "outside"
    outside.mkdir()
    protected = outside / "settings.json"
    protected.write_bytes(b"{}")
    directory = transport.home / ".claude"
    if link_parent:
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "settings.json").symlink_to(protected)
    with NativeFiles(transport) as files:
        with pytest.raises(StateError):
            files.read(str(directory / "settings.json"))
        with pytest.raises(StateError):
            files.publish(
                str(directory / "settings.json"), b'{"changed": true}', expected=hashlib.sha256(b"{}").hexdigest()
            )
    assert protected.read_bytes() == b"{}"


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_settings_only_replace_repairs_invalid_document_and_retirement_retains_it(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    root.mkdir()
    destination = root / ("config.toml" if tool == "codex" else "settings.json")
    destination.write_bytes(b"invalid-previous-document")
    source = transport.root / "source"
    source.write_bytes(b"answer = 42\n" if tool == "codex" else b'{"answer": 42}')
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="replace"))
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, config, invocation(transport, claims))
    assert claims[-1][0].role == "settings"
    provisioned = destination.read_bytes()
    source.unlink()
    setup_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert claims[-1] == ()
    assert destination.read_bytes() == provisioned


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_actual_native_lifecycle_and_checkpointed_idempotency(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    setup_user(tool, config, invocation(transport, claims))
    assert [claim.role for claim in claims[-1]] == ["marketplace", "plugin"]
    current = claims[-1]
    checkpoint_count = len(claims)
    setup_user(tool, config, invocation(transport, claims, prior=record(current, tool)))
    assert len(claims) == checkpoint_count
    setup_user(tool, None, invocation(transport, claims, prior=record(current, tool)))
    assert claims[-1] == ()
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_unowned_existing_native_setup_refused(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, config, invocation(transport, claims))
    settings = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    before = settings.read_bytes()
    with pytest.raises(ConfigError):
        setup_user(tool, config, invocation(transport, []))
    assert settings.read_bytes() == before


def test_workspace_mapping_uses_group_and_fixed_project_role(transport: LocalFixtureTransport) -> None:
    import grp

    workspace = transport.root / "workspace"
    workspace.mkdir()
    source = transport.root / "source.json"
    source.write_bytes(b'{"project": true}')
    claims: list[tuple[NativeClaim, ...]] = []
    setup_workspace(
        "claude",
        NativeWorkspaceConfig(settings=SettingsMapping(source=str(source), strategy="replace")),
        WorkspaceSetupInvocation(
            vm=cast(VMRow, object()),
            runner=transport,
            prior=None,
            checkpoint=claims.append,
            workspace_name="fixture",
            root=str(workspace),
            linux_group=grp.getgrgid(os.getgid()).gr_name,
        ),
    )
    destination = workspace / ".claude/settings.json"
    assert destination.stat().st_mode & 0o777 == 0o660
    assert destination.stat().st_gid == os.getgid()
    assert claims[-1][0].destination == str(destination)
    assert not (transport.home / ".claude/settings.json").exists()


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_mapping_conflict_precedes_real_native_writes(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    market = market_fixture(transport, tool)
    source = transport.root / "source"
    source.write_bytes(
        b'[plugins."one@fixture-market"]\nenabled=false\n'
        if tool == "codex"
        else b'{"enabledPlugins":{"one@fixture-market":false}}'
    )
    config = NativeUserConfig(
        marketplaces=[str(market)],
        plugins=["one@fixture-market"],
        settings=SettingsMapping(source=str(source), strategy="replace"),
    )
    with pytest.raises(ConfigError):
        setup_user(tool, config, invocation(transport, []))
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    assert not root.exists()
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_replacement_keeps_explicit_plugins_and_captured_source(
    transport: LocalFixtureTransport, tool: NativeTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = market_fixture(transport, tool)
    source = transport.root / "source"
    source.write_bytes(b'model_reasoning_effort="low"\n' if tool == "codex" else b'{"alwaysThinkingEnabled":false}')
    original = NativeCLI.install

    def change_source_after_install(self, selector):
        original(self, selector)
        source.write_bytes(b"invalid replacement source")

    monkeypatch.setattr(NativeCLI, "install", change_source_after_install)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy="replace"),
        ),
        invocation(transport, claims),
    )
    with NativeFiles(transport) as files:
        root = str(transport.home / (".codex" if tool == "codex" else ".claude"))
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=root)
        installed = cli.plugins(cli.markets())
        assert len(installed) == 1 and installed[0].enabled
    mapping = next(claim for claim in claims[-1] if claim.role == "settings")
    assert (
        mapping.written_keys == (("model_reasoning_effort",),)
        if tool == "codex"
        else (mapping.written_keys == (("alwaysThinkingEnabled",),))
    )
    assert "invalid replacement source" not in str(claims)


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_skip_existing_does_not_block_explicit_install(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    market = market_fixture(transport, tool)
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    root.mkdir()
    settings = root / ("config.toml" if tool == "codex" else "settings.json")
    settings.write_bytes(b'model_reasoning_effort="low"\n' if tool == "codex" else b'{"alwaysThinkingEnabled":false}')
    source = transport.root / "source"
    source.write_bytes(
        b'[plugins."one@fixture-market"]\nenabled=false\n'
        if tool == "codex"
        else b'{"enabledPlugins":{"one@fixture-market":false}}'
    )
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy="skip-existing"),
        ),
        invocation(transport, claims),
    )
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_failed_plugin_step_retains_retryable_marketplace_prefix(
    transport: LocalFixtureTransport, tool: NativeTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    original = NativeCLI.install

    def fail_install(self, selector):
        raise ExternalError("injected transport failure")

    monkeypatch.setattr(NativeCLI, "install", fail_install)
    with pytest.raises(ExternalError):
        setup_user(tool, config, invocation(transport, claims))
    assert [claim.role for claim in claims[-1]] == ["marketplace"]
    monkeypatch.setattr(NativeCLI, "install", original)
    setup_user(tool, config, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_checkpoint_failure_stops_before_next_native_mutation(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(transport, tool)

    def reject_checkpoint(claims):
        raise OSError("injected state persistence failure")

    ctx = invocation(transport, [])
    from dataclasses import replace

    ctx = replace(ctx, checkpoint=reject_checkpoint)
    with pytest.raises(OSError):
        setup_user(tool, NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]), ctx)
    with NativeFiles(transport) as files:
        root = str(transport.home / (".codex" if tool == "codex" else ".claude"))
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=root)
        assert len(cli.markets()) == 1
        assert cli.plugins(cli.markets()) == ()


def test_claude_project_dependency_blocks_all_retirement_writes(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    project = transport.root / "project"
    project.mkdir()
    import shlex

    result = transport.run(
        f"cd {shlex.quote(str(project))} && claude plugin install two@fixture-market --scope project"
    )
    assert result.ok
    user_settings = transport.home / ".claude/settings.json"
    project_settings = project / ".claude/settings.json"
    before_user, before_project = user_settings.read_bytes(), project_settings.read_bytes()
    checkpoint_count = len(claims)
    with pytest.raises(StateError):
        setup_user("claude", None, invocation(transport, claims, prior=record(claims[-1], "claude")))
    assert user_settings.read_bytes() == before_user
    assert project_settings.read_bytes() == before_project
    assert len(claims) == checkpoint_count


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_user_setup_binding_uses_native_model_and_config_home(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
    from agentworks.plugins.codex.harness_integration import CodexIntegration

    integration = CodexIntegration if tool == "codex" else ClaudeCodeIntegration
    source = transport.root / "source"
    source.write_bytes(b'model_reasoning_effort="low"\n' if tool == "codex" else b'{"alwaysThinkingEnabled":false}')
    bound = integration.for_setup(
        owner_kind="agent-template",
        owner_name="fixture",
        facet="user",
        config={"name": integration.name, "settings": {"source": str(source), "strategy": "replace"}},
    )
    claims: list[tuple[NativeClaim, ...]] = []
    root = transport.root / "overridden-config"
    key = "CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"
    bound.user_init(invocation(transport, claims, env={key: str(root)}))
    assert (root / ("config.toml" if tool == "codex" else "settings.json")).is_file()
    assert claims[-1][0].destination.startswith(str(root) + "/")
    assert not (transport.home / (".codex" if tool == "codex" else ".claude")).exists()


def test_codex_mapping_preserves_unrelated_plugin_and_market_fields(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    source = transport.root / "source.toml"
    source.write_text(
        '[plugins."one@fixture-market"]\nenabled = true\ncustom = "fixture-extra"\n'
        '[marketplaces.fixture-market]\ncustom = "fixture-market-extra"\n'
    )
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "codex",
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy="replace"),
        ),
        invocation(transport, claims),
    )
    import tomllib

    document = tomllib.loads((transport.home / ".codex/config.toml").read_text())
    assert document["plugins"]["one@fixture-market"]["enabled"] is True
    assert document["plugins"]["one@fixture-market"]["custom"] == "fixture-extra"
    assert document["marketplaces"]["fixture-market"]["custom"] == "fixture-market-extra"
    written = next(claim.written_keys for claim in claims[-1] if claim.role == "settings")
    assert ("plugins", "one@fixture-market", "custom") in written
    assert ("plugins", "one@fixture-market", "enabled") not in written


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_mapping_merge_does_not_reintroduce_removed_owned_plugin(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    source = transport.root / "source"
    source.write_bytes(b'model_reasoning_effort="low"\n' if tool == "codex" else b'{"alwaysThinkingEnabled":false}')
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)], settings=SettingsMapping(source=str(source), strategy="merge-overwrite")
        ),
        invocation(transport, claims, prior=record(claims[-1], tool)),
    )
    assert {claim.role for claim in claims[-1]} == {"marketplace", "settings"}
    with NativeFiles(transport) as files:
        cli = NativeCLI(
            tool,
            files,
            home=str(transport.home),
            config_root=str(transport.home / (".codex" if tool == "codex" else ".claude")),
        )
        assert cli.plugins(cli.markets()) == ()


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_malformed_existing_settings_prevents_plugin_reconciliation(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(transport, tool)
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    root.mkdir()
    settings = root / ("config.toml" if tool == "codex" else "settings.json")
    settings.write_bytes(b"invalid-existing-settings")
    source = transport.root / "source"
    source.write_bytes(b"" if tool == "codex" else b"{}")
    with pytest.raises(ConfigError):
        setup_user(
            tool,
            NativeUserConfig(
                marketplaces=[str(market)],
                plugins=["one@fixture-market"],
                settings=SettingsMapping(source=str(source), strategy="replace"),
            ),
            invocation(transport, []),
        )
    assert settings.read_bytes() == b"invalid-existing-settings"
    assert not (root / "plugins").exists()


def test_claude_uninstall_preserves_plugin_persistent_data(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    data = transport.home / ".claude/plugins/data/one@fixture-market/state.txt"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"fixture-user-data")
    setup_user("claude", None, invocation(transport, claims, prior=record(claims[-1], "claude")))
    assert data.read_bytes() == b"fixture-user-data"


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_requested_owned_disabled_plugin_is_enabled(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    from agentworks.capabilities.harness_integration.settings import parse_settings, serialize_settings

    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, config, invocation(transport, claims))
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    format = "toml" if tool == "codex" else "json"
    document = parse_settings(destination.read_bytes(), format=format)
    if tool == "codex":
        plugins = document["plugins"]
        assert isinstance(plugins, dict)
        plugin = plugins["one@fixture-market"]
        assert isinstance(plugin, dict)
        plugin["enabled"] = False
    else:
        enabled = document["enabledPlugins"]
        assert isinstance(enabled, dict)
        enabled["one@fixture-market"] = False
    destination.write_bytes(serialize_settings(document, format=format))
    prior = record(claims[-1], tool)
    count = len(claims)
    setup_user(tool, config, invocation(transport, claims, prior=prior))
    assert len(claims) == count + 1
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(destination.parent))
        assert cli.plugins(cli.markets())[0].enabled is True


def test_codex_missing_marketplace_keeps_unobservable_plugin_cleanup_pending(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("codex", config, invocation(transport, claims))
    prior = record(claims[-1], "codex")
    with NativeFiles(transport) as files:
        cli = NativeCLI("codex", files, home=str(transport.home), config_root=str(transport.home / ".codex"))
        cli.remove_market("fixture-market")
    before = (transport.home / ".codex/config.toml").read_bytes()
    count = len(claims)
    with pytest.raises(StateError):
        setup_user("codex", None, invocation(transport, claims, prior=prior))
    assert len(claims) == count
    assert (transport.home / ".codex/config.toml").read_bytes() == before


def test_search_only_ancestor_allows_native_read_and_publication(transport: LocalFixtureTransport) -> None:
    ancestor = transport.home / "search-only"
    ancestor.mkdir()
    destination = ancestor / ".claude/settings.json"
    destination.parent.mkdir()
    ancestor.chmod(0o111)
    try:
        with NativeFiles(transport) as files:
            assert files.read(str(destination)) is None
            files.publish(str(destination), b"{}", expected=None)
            assert files.read(str(destination)) == b"{}"
    finally:
        ancestor.chmod(0o700)


def test_missing_guest_python_refuses_before_staging(transport: LocalFixtureTransport, monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    probe = Mock(return_value=SimpleNamespace(ok=False))
    monkeypatch.setattr(transport, "run", probe)
    with pytest.raises(StateError) as caught, NativeFiles(transport):
        pytest.fail("missing prerequisite must stop before staging")
    assert caught.value.hint is not None
    assert "python3" in str(caught.value) and "apt_packages" in caught.value.hint
    assert probe.call_count == 1
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("inside_plugin", [False, True])
def test_mapping_refuses_unrelated_changes_during_plugin_install(
    transport: LocalFixtureTransport, tool: NativeTool, inside_plugin: bool, monkeypatch
) -> None:
    from agentworks.capabilities.harness_integration.settings import parse_settings, serialize_settings

    market = market_fixture(transport, tool)
    format = "toml" if tool == "codex" else "json"
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    source = transport.root / "mapping"
    source.write_bytes(serialize_settings({"mapped": True}, format=format))
    config = NativeUserConfig(
        marketplaces=[str(market)],
        plugins=["one@fixture-market"],
        settings=SettingsMapping(source=str(source), strategy="merge-preserve"),
    )
    claims: list[tuple[NativeClaim, ...]] = []
    original = NativeCLI.install

    def concurrent_edit(self, selector):
        original(self, selector)
        document = parse_settings(destination.read_bytes(), format=format)
        if inside_plugin and tool == "codex":
            plugins = document["plugins"]
            assert isinstance(plugins, dict)
            plugin = plugins[selector]
            assert isinstance(plugin, dict)
            plugin["external"] = "preserve"
        else:
            document["external"] = "preserve"
        destination.write_bytes(serialize_settings(document, format=format))

    monkeypatch.setattr(NativeCLI, "install", concurrent_edit)
    with pytest.raises(StateError):
        setup_user(tool, config, invocation(transport, claims))
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}
    assert b"preserve" in destination.read_bytes()
    monkeypatch.setattr(NativeCLI, "install", original)
    setup_user(tool, config, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert b"preserve" in destination.read_bytes()
    assert parse_settings(destination.read_bytes(), format=format)["mapped"] is True


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_retirement_uses_recorded_override_root_without_current_env(transport, tool):
    market = market_fixture(transport, tool)
    override_root = transport.root / "custom-native-home"
    key = "CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims, env={key: str(override_root)}),
    )
    assert all(claim.destination == str(override_root) for claim in claims[-1])
    default_file = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    default_file.parent.mkdir(exist_ok=True)
    default_file.write_bytes(b"unrelated-default-settings")
    setup_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert claims[-1] == ()
    assert default_file.read_bytes() == b"unrelated-default-settings"


@pytest.mark.parametrize("field", ['ref = "unrequested-ref"', 'sparse_paths = ["unexpected"]'])
def test_codex_mapping_cannot_add_unrequested_source_identity(transport: LocalFixtureTransport, field: str) -> None:
    market = market_fixture(transport, "codex")
    source = transport.root / "mapping.toml"
    source.write_text("[marketplaces.fixture-market]\n" + field + "\n")
    claims: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(
        marketplaces=[str(market)],
        plugins=["one@fixture-market"],
        settings=SettingsMapping(source=str(source), strategy="merge-overwrite"),
    )
    with pytest.raises(ConfigError):
        setup_user("codex", config, invocation(transport, claims))
    assert claims == []
    assert not (transport.home / ".codex").exists()


def test_codex_relative_source_named_local_does_not_match_source_type(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("codex", NativeUserConfig(marketplaces=[str(market)], plugins=["one"]), invocation(transport, claims))
    local = transport.home / "local"
    shutil.copytree(market, local)
    manifest = local / ".agents/plugins/marketplace.json"
    data = json.loads(manifest.read_text())
    data["name"] = "other-market"
    manifest.write_text(json.dumps(data))
    updated: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "codex",
        NativeUserConfig(marketplaces=["local"], plugins=["one@other-market"]),
        invocation(transport, updated, prior=record(claims[-1], "codex")),
    )
    assert {(claim.role, claim.identifier) for claim in updated[-1]} == {
        ("marketplace", "other-market"),
        ("plugin", "one@other-market"),
    }
    repeated: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "codex",
        NativeUserConfig(marketplaces=["local"], plugins=["one@other-market"]),
        invocation(transport, repeated, prior=record(updated[-1], "codex")),
    )
    assert repeated == []
    with NativeFiles(transport) as files:
        cli = NativeCLI("codex", files, home=str(transport.home), config_root=str(transport.home / ".codex"))
        assert [market.name for market in cli.markets()] == ["other-market"]
