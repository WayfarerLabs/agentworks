"""Actual native setup helpers exercised against isolated local files and CLIs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from agentworks.capabilities.harness_integration.settings import SettingsMapping, parse_settings, serialize_settings
from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
from agentworks.db import VMRow
from agentworks.errors import ConfigError, ExternalError
from agentworks.execution.errors import FileConflict, PathRefusal, PathRefused
from agentworks.execution.files import Files
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native import setup_workspace
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeTool
from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig
from tests.native_setup_fixtures import (
    LocalFixtureTransport,
    invocation,
    market_fixture,
    native_cli,
    record,
    setup_fixture_user,
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


@pytest.mark.parametrize("link_parent", [False, True])
def test_settings_are_never_read_or_written_through_a_link(transport: LocalFixtureTransport, link_parent: bool) -> None:
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
    source = transport.root / "mapping.json"
    source.write_bytes(b'{"changed": true}')

    with pytest.raises(PathRefused) as refused:
        setup_fixture_user(
            "claude",
            NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite")),
            invocation(transport, []),
        )

    assert refused.value.reason is PathRefusal.UNSUITABLE
    assert refused.value.path == str(directory / "settings.json")
    assert sorted(os.listdir(outside)) == ["settings.json"]
    assert protected.read_bytes() == b"{}"


def test_a_search_only_ancestor_allows_settings_publication(transport: LocalFixtureTransport) -> None:
    ancestor = transport.home / "search-only"
    root = ancestor / ".claude"
    root.mkdir(parents=True)
    source = transport.root / "mapping.json"
    source.write_bytes(b'{"mapped": true}')
    ancestor.chmod(0o111)
    try:
        setup_fixture_user(
            "claude",
            NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite")),
            invocation(transport, [], env={"CLAUDE_CONFIG_DIR": str(root)}),
        )
    finally:
        ancestor.chmod(0o700)
    assert json.loads((root / "settings.json").read_bytes()) == {"mapped": True}


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
    setup_fixture_user(tool, config, invocation(transport, claims))
    assert not any(claims)
    provisioned = destination.read_bytes()
    source.unlink()
    setup_fixture_user(tool, None, invocation(transport, claims, prior=record((), tool)))
    assert not any(claims)
    assert destination.read_bytes() == provisioned


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_actual_native_lifecycle_and_checkpointed_idempotency(
    transport: LocalFixtureTransport, tool: NativeTool, monkeypatch
) -> None:
    market = market_fixture(transport, tool)
    reports: list[str] = []
    monkeypatch.setattr("agentworks.plugins._harness_native.native.output.info", reports.append)
    claims: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    setup_fixture_user(tool, config, invocation(transport, claims))
    assert [claim.role for claim in claims[-1]] == ["marketplace", "plugin"]
    assert stat.S_IMODE((transport.home / (".codex" if tool == "codex" else ".claude")).stat().st_mode) == 0o700
    assert len(reports) == 2
    assert "fixture-market" in reports[0] and "one@fixture-market" in reports[1]
    assert all(tool in report and str(market) not in report for report in reports)
    current = claims[-1]
    checkpoint_count = len(claims)
    setup_fixture_user(tool, config, invocation(transport, claims, prior=record(current, tool)))
    assert len(claims) == checkpoint_count
    assert len(reports) == 2
    setup_fixture_user(tool, None, invocation(transport, claims, prior=record(current, tool)))
    assert claims[-1] == ()
    assert len(reports) == 4
    assert "one@fixture-market" in reports[2] and "fixture-market" in reports[3]
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_identical_unowned_native_setup_is_adopted(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(tool, config, invocation(transport, claims))
    settings = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    before = settings.read_bytes()
    adopted: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(tool, config, invocation(transport, adopted))
    assert adopted[-1] == claims[-1]
    assert settings.read_bytes() == before


def test_workspace_mapping_uses_group_and_fixed_project_role(transport: LocalFixtureTransport) -> None:
    import grp

    # A supplementary group, when the account has one, is one a new file would not get by default.
    gid = next((group for group in os.getgroups() if group != os.getgid()), os.getgid())
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
            access=(recorded := transport.access()),
            files=recorded.files(),
            prior=None,
            checkpoint=claims.append,
            workspace_name="fixture",
            root=str(workspace),
            linux_group=grp.getgrgid(gid).gr_name,
        ),
    )
    destination = workspace / ".claude/settings.json"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o660
    assert destination.stat().st_gid == gid
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o2770
    assert destination.parent.stat().st_gid == gid
    assert not any(claims)
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
        setup_fixture_user(tool, config, invocation(transport, []))
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    assert not root.exists()
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("strategy", ["replace", "skip-existing"])
def test_replacement_keeps_explicit_plugins_and_captured_source(
    transport: LocalFixtureTransport, tool: NativeTool, strategy: str, monkeypatch: pytest.MonkeyPatch
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
    setup_fixture_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy=strategy),
        ),
        invocation(transport, claims),
    )
    with native_cli(transport, tool, transport.home / (".codex" if tool == "codex" else ".claude")) as cli:
        installed = cli.plugins(cli.markets())
        assert len(installed) == 1 and installed[0].enabled
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    document = parse_settings(destination.read_bytes(), format="toml" if tool == "codex" else "json")
    assert document["model_reasoning_effort" if tool == "codex" else "alwaysThinkingEnabled"] == (
        "low" if tool == "codex" else False
    )
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}


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
    setup_fixture_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy="skip-existing"),
        ),
        invocation(transport, claims),
    )
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}


@pytest.mark.parametrize("strategy", ["merge-preserve", "skip-existing"])
def test_mapping_discards_legacy_settings_claims_from_applied_state_and_preserves_native_claims(
    transport: LocalFixtureTransport, strategy: str
) -> None:
    tool: NativeTool = "codex"
    market = market_fixture(transport, tool)
    initial: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    setup_fixture_user(tool, config, invocation(transport, initial))
    destination = transport.home / ".codex/config.toml"
    content = serialize_settings(parse_settings(destination.read_bytes(), format="toml"), format="toml")
    destination.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    settings_claims = tuple(
        NativeClaim(
            role="settings",
            identifier=identifier,
            destination=str(destination),
            sha256=digest,
            strategy="merge-preserve",
        )
        for identifier in ("primary", "secondary")
    )
    prior_claims = (*initial[-1], *settings_claims)
    source = transport.root / "settings-source"
    source.write_bytes(b"")
    checkpoints: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy=strategy),
        ),
        invocation(transport, checkpoints, prior=record(prior_claims, tool)),
    )
    assert checkpoints[-1] == initial[-1]
    assert destination.read_bytes() == content


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
        setup_fixture_user(tool, config, invocation(transport, claims))
    assert [claim.role for claim in claims[-1]] == ["marketplace"]
    monkeypatch.setattr(NativeCLI, "install", original)
    setup_fixture_user(tool, config, invocation(transport, claims, prior=record(claims[-1], tool)))
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
        setup_fixture_user(tool, NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]), ctx)
    with native_cli(transport, tool, transport.home / (".codex" if tool == "codex" else ".claude")) as cli:
        assert len(cli.markets()) == 1
        assert cli.plugins(cli.markets()) == ()


@pytest.mark.parametrize("strategy", [None, "replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_claude_project_dependency_retains_only_its_marketplace(transport: LocalFixtureTransport, strategy) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
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
    before_project = project_settings.read_bytes()
    mapping = transport.root / "settings-source.json"
    mapping.write_text('{"mapped": true}')
    config = (
        None if strategy is None else NativeUserConfig(settings=SettingsMapping(source=str(mapping), strategy=strategy))
    )
    incomplete: list[str] = []
    previous_user_bytes = None
    previous_user_inode = None
    for _ in range(2):
        setup_fixture_user(
            "claude",
            config,
            replace(
                invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=incomplete.append
            ),
        )
        assert project_settings.read_bytes() == before_project
        assert [(claim.role, claim.identifier) for claim in claims[-1]] == [("marketplace", "fixture-market")]
        document = parse_settings(user_settings.read_bytes(), format="json")
        enabled = document.get("enabledPlugins", {})
        assert isinstance(enabled, dict)
        assert "one@fixture-market" not in enabled
        if strategy not in (None, "skip-existing"):
            assert document["mapped"] is True
        with native_cli(transport, "claude", user_settings.parent) as cli:
            assert {item.name for item in cli.markets()} == {"fixture-market"}
            assert {(item.identifier, item.scope) for item in cli.plugins(cli.markets())} == {
                ("two@fixture-market", "project")
            }
        current_user_bytes = user_settings.read_bytes()
        current_user_inode = user_settings.stat().st_ino
        if previous_user_bytes is not None:
            assert current_user_bytes == previous_user_bytes
            assert current_user_inode == previous_user_inode
        previous_user_bytes = current_user_bytes
        previous_user_inode = current_user_inode
    assert len(incomplete) == 2


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
        config={"settings": {"source": str(source), "strategy": "replace"}},
    )
    claims: list[tuple[NativeClaim, ...]] = []
    root = transport.root / "overridden-config"
    key = "CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"
    bound.user_init(invocation(transport, claims, env={key: str(root)}))
    settings = root / ("config.toml" if tool == "codex" else "settings.json")
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert not any(claims)
    assert not (transport.home / (".codex" if tool == "codex" else ".claude")).exists()


def test_codex_mapping_preserves_unrelated_plugin_and_market_fields(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    source = transport.root / "source.toml"
    source.write_text(
        '[plugins."one@fixture-market"]\nenabled = true\ncustom = "fixture-extra"\n'
        '[marketplaces.fixture-market]\ncustom = "fixture-market-extra"\n'
    )
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
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
    document["marketplaces"]["fixture-market"]["last_updated"] = "2026-09-11T00:00:00Z"
    destination = transport.home / ".codex/config.toml"
    destination.write_bytes(serialize_settings(document, format="toml"))
    source.write_text(source.read_text().replace("fixture-extra", "updated-extra"))
    setup_fixture_user(
        "codex",
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy="replace"),
        ),
        invocation(transport, claims, prior=record(claims[-1], "codex")),
    )
    updated = tomllib.loads(destination.read_text())
    assert updated["plugins"]["one@fixture-market"]["custom"] == "updated-extra"
    assert updated["marketplaces"]["fixture-market"]["last_updated"] == "2026-09-11T00:00:00Z"
    assert updated["marketplaces"]["fixture-market"]["source_type"] == "local"
    assert updated["marketplaces"]["fixture-market"]["source"] == str(market)


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_mapping_merge_does_not_reintroduce_removed_owned_plugin(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    source = transport.root / "source"
    source.write_bytes(b'model_reasoning_effort="low"\n' if tool == "codex" else b'{"alwaysThinkingEnabled":false}')
    setup_fixture_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)], settings=SettingsMapping(source=str(source), strategy="merge-overwrite")
        ),
        invocation(transport, claims, prior=record(claims[-1], tool)),
    )
    assert {claim.role for claim in claims[-1]} == {"marketplace"}
    with native_cli(transport, tool, transport.home / (".codex" if tool == "codex" else ".claude")) as cli:
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
        setup_fixture_user(
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
    setup_fixture_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    data = transport.home / ".claude/plugins/data/one@fixture-market/state.txt"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"fixture-user-data")
    setup_fixture_user("claude", None, invocation(transport, claims, prior=record(claims[-1], "claude")))
    assert data.read_bytes() == b"fixture-user-data"


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_requested_owned_disabled_plugin_is_enabled(transport: LocalFixtureTransport, tool: NativeTool) -> None:
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(tool, config, invocation(transport, claims))
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
    setup_fixture_user(tool, config, invocation(transport, claims, prior=prior))
    assert len(claims) == count + 1
    with native_cli(transport, tool, destination.parent) as cli:
        assert cli.plugins(cli.markets())[0].enabled is True


def test_codex_missing_marketplace_keeps_unobservable_plugin_cleanup_pending(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user("codex", config, invocation(transport, claims))
    prior = record(claims[-1], "codex")
    with native_cli(transport, "codex", transport.home / ".codex") as cli:
        cli.remove_market("fixture-market")
    before = (transport.home / ".codex/config.toml").read_bytes()
    count = len(claims)
    incomplete: list[str] = []
    setup_fixture_user(
        "codex", None, replace(invocation(transport, claims, prior=prior), report_incomplete=incomplete.append)
    )
    assert incomplete
    assert len(claims) == count + 1
    assert [claim.role for claim in claims[-1]] == ["plugin"]
    assert (transport.home / ".codex/config.toml").read_bytes() == before


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("inside_plugin", [False, True])
@pytest.mark.parametrize("strategy", ["merge-preserve", "merge-overwrite"])
def test_mapping_merges_current_changes_during_plugin_install(
    transport: LocalFixtureTransport, tool: NativeTool, inside_plugin: bool, strategy: str, monkeypatch
) -> None:
    market = market_fixture(transport, tool)
    format = "toml" if tool == "codex" else "json"
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    source = transport.root / "mapping"
    source.write_bytes(serialize_settings({"mapped": True}, format=format))
    config = NativeUserConfig(
        marketplaces=[str(market)],
        plugins=["one@fixture-market"],
        settings=SettingsMapping(source=str(source), strategy=strategy),
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
        if tool == "codex":
            marketplaces = document["marketplaces"]
            assert isinstance(marketplaces, dict)
            marketplace = marketplaces["fixture-market"]
            assert isinstance(marketplace, dict)
            marketplace["last_updated"] = "2026-09-11T00:00:00Z"
        destination.write_bytes(serialize_settings(document, format=format))

    monkeypatch.setattr(NativeCLI, "install", concurrent_edit)
    setup_fixture_user(tool, config, invocation(transport, claims))
    assert {claim.role for claim in claims[-1]} == {"marketplace", "plugin"}
    assert b"preserve" in destination.read_bytes()
    if tool == "codex":
        assert b"2026-09-11T00:00:00Z" in destination.read_bytes()
    monkeypatch.setattr(NativeCLI, "install", original)
    setup_fixture_user(tool, config, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert b"preserve" in destination.read_bytes()
    assert parse_settings(destination.read_bytes(), format=format)["mapped"] is True


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_retirement_uses_recorded_override_root_without_current_env(transport, tool):
    market = market_fixture(transport, tool)
    override_root = transport.root / "custom-native-home"
    key = "CODEX_HOME" if tool == "codex" else "CLAUDE_CONFIG_DIR"
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims, env={key: str(override_root)}),
    )
    assert all(claim.destination == str(override_root) for claim in claims[-1])
    default_file = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    default_file.parent.mkdir(exist_ok=True)
    default_file.write_bytes(b"unrelated-default-settings")
    setup_fixture_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
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
        setup_fixture_user("codex", config, invocation(transport, claims))
    assert claims == []
    assert not (transport.home / ".codex").exists()


def test_codex_relative_source_named_local_does_not_match_source_type(transport: LocalFixtureTransport) -> None:
    market = market_fixture(transport, "codex")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        "codex", NativeUserConfig(marketplaces=[str(market)], plugins=["one"]), invocation(transport, claims)
    )
    local = transport.home / "local"
    shutil.copytree(market, local)
    manifest = local / ".agents/plugins/marketplace.json"
    data = json.loads(manifest.read_text())
    data["name"] = "other-market"
    manifest.write_text(json.dumps(data))
    updated: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        "codex",
        NativeUserConfig(marketplaces=["local"], plugins=["one@other-market"]),
        invocation(transport, updated, prior=record(claims[-1], "codex")),
    )
    assert {(claim.role, claim.identifier) for claim in updated[-1]} == {
        ("marketplace", "other-market"),
        ("plugin", "one@other-market"),
    }
    repeated: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        "codex",
        NativeUserConfig(marketplaces=["local"], plugins=["one@other-market"]),
        invocation(transport, repeated, prior=record(updated[-1], "codex")),
    )
    assert repeated == []
    with native_cli(transport, "codex", transport.home / ".codex") as cli:
        assert [market.name for market in cli.markets()] == ["other-market"]


def test_settings_content_and_its_digests_stay_out_of_the_operation_log(transport: LocalFixtureTransport) -> None:
    source = transport.root / "mapping.json"
    source.write_bytes(b'{"mapped": "private-mapped-value"}')
    destination = transport.home / ".claude/settings.json"
    destination.parent.mkdir()
    destination.write_bytes(b'{"existing": "private-existing-value"}')
    before = hashlib.sha256(destination.read_bytes()).hexdigest()

    setup_fixture_user(
        "claude",
        NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite")),
        invocation(transport, []),
    )

    after = hashlib.sha256(destination.read_bytes()).hexdigest()
    records = "".join(transport.logged_output)
    assert str(destination) in records
    for private in ("private-mapped-value", "private-existing-value", before, after):
        assert private not in records


def test_json_null_survives_a_settings_merge(transport: LocalFixtureTransport) -> None:
    source = transport.root / "mapping.json"
    source.write_bytes(b'{"mapped": true, "nested": {"added": 1}}')
    destination = transport.home / ".claude/settings.json"
    destination.parent.mkdir()
    destination.write_bytes(b'{"cleared": null, "nested": {"unset": null}}')

    setup_fixture_user(
        "claude",
        NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite")),
        invocation(transport, []),
    )

    assert json.loads(destination.read_bytes()) == {
        "cleared": None,
        "nested": {"unset": None, "added": 1},
        "mapped": True,
    }


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("strategy", ["merge-overwrite", "merge-preserve"])
def test_a_malformed_destination_is_refused_and_left_byte_identical(
    transport: LocalFixtureTransport, tool: NativeTool, strategy: str
) -> None:
    source = transport.root / "mapping"
    source.write_bytes(b"mapped = true\n" if tool == "codex" else b'{"mapped": true}')
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    destination.parent.mkdir()
    malformed = b"[unterminated\nmapped = " if tool == "codex" else b'{"mapped": tru'
    destination.write_bytes(malformed)
    destination.chmod(0o640)

    with pytest.raises(ConfigError):
        setup_fixture_user(
            tool,
            NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy)),
            invocation(transport, []),
        )

    assert destination.read_bytes() == malformed
    assert destination.stat().st_mode & 0o777 == 0o640
    assert os.listdir(destination.parent) == [destination.name]


@pytest.mark.parametrize("initial", [None, b'{"before": true}'])
def test_settings_publication_refuses_a_change_made_after_its_read(
    transport: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch, initial: bytes | None
) -> None:
    source = transport.root / "mapping.json"
    source.write_bytes(b'{"mapped": true}')
    destination = transport.home / ".claude/settings.json"
    if initial is not None:
        destination.parent.mkdir()
        destination.write_bytes(initial)
    original = Files.write_if

    def concurrent_edit(self, path, data, **kwargs):
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(b'{"external": true}')
        original(self, path, data, **kwargs)

    monkeypatch.setattr(Files, "write_if", concurrent_edit)
    with pytest.raises(FileConflict) as refused:
        setup_fixture_user(
            "claude",
            NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-preserve")),
            invocation(transport, []),
        )
    assert refused.value.path == str(destination)
    assert destination.read_bytes() == b'{"external": true}'


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_settings_that_change_nothing_leave_the_file_and_its_mode_alone(
    transport: LocalFixtureTransport, tool: NativeTool
) -> None:
    format = "toml" if tool == "codex" else "json"
    source = transport.root / "mapping"
    source.write_bytes(serialize_settings({"mapped": True}, format=format))
    destination = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    destination.parent.mkdir()
    destination.write_bytes(serialize_settings({"mapped": True}, format=format))
    destination.chmod(0o640)
    before = destination.stat().st_ino

    setup_fixture_user(
        tool,
        NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite")),
        invocation(transport, []),
    )

    assert destination.stat().st_ino == before
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_marketplace_only_repeat_does_not_read_available_catalog(transport, monkeypatch):
    market = market_fixture(transport, "codex")
    config = NativeUserConfig(marketplaces=[str(market)])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user("codex", config, invocation(transport, claims))

    def unexpected_catalog(self):
        pytest.fail("no plugin selector needs catalog resolution")

    monkeypatch.setattr(NativeCLI, "available", unexpected_catalog)
    setup_fixture_user("codex", config, invocation(transport, claims, prior=record(claims[-1], "codex")))


@pytest.mark.parametrize("source", ["-x", "--help"])
def test_a_marketplace_source_that_reads_as_an_option_is_refused(source: str) -> None:
    with pytest.raises(ValidationError):
        NativeUserConfig.model_validate({"marketplaces": [source]})
