"""Native retry and conflict convergence against isolated native CLI fixtures."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsMapping, parse_settings, serialize_settings
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native import setup_user
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeTool
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture, record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local execution of Linux guest setup helpers")


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    root = tmp_path / "native-convergence"
    root.mkdir()
    return LocalFixtureTransport(root)


def copied_market(market: Path, tool: NativeTool, *, name: str = "fixture-market") -> Path:
    destination = market.parent / ("alternate-" + name)
    shutil.copytree(market, destination)
    manifest = destination / (
        ".agents/plugins/marketplace.json" if tool == "codex" else ".claude-plugin/marketplace.json"
    )
    value = json.loads(manifest.read_text())
    value["name"] = name
    manifest.write_text(json.dumps(value))
    return destination


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("step", ["marketplace", "plugin"])
def test_retry_adopts_native_mutation_after_its_checkpoint_failed(transport, tool, step):
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []

    def checkpoint(current):
        if current[-1].role == step:
            raise OSError("fixture lost persistence")
        claims.append(current)

    with pytest.raises(OSError):
        setup_user(tool, config, replace(invocation(transport, claims), checkpoint=checkpoint))
    before = claims[-1] if claims else ()
    settings = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    previous_bytes = settings.read_bytes()
    setup_user(tool, config, invocation(transport, claims, prior=record(before, tool)))
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "fixture-market"),
        ("plugin", "one@fixture-market"),
    }
    if step == "plugin":
        assert settings.read_bytes() == previous_bytes
    final = claims[-1]
    setup_user(tool, None, invocation(transport, claims, prior=record(final, tool)))
    assert claims[-1] == ()


@pytest.mark.parametrize("tool,role", [("codex", "marketplace"), ("claude", "marketplace"), ("claude", "plugin")])
def test_native_adoption_reports_the_adopted_entry(transport, tool, role, monkeypatch):
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"] if role == "plugin" else [])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, config, invocation(transport, claims))
    prior = record(tuple(item for item in claims[-1] if item.role != role), tool)
    reports: list[str] = []
    monkeypatch.setattr("agentworks.plugins._harness_native.native.output.info", reports.append)
    setup_user(tool, config, invocation(transport, claims, prior=prior))
    identifier = "one@fixture-market" if role == "plugin" else "fixture-market"
    assert len(reports) == 1 and identifier in reports[0]
    assert any(item.role == role and item.identifier == identifier for item in claims[-1])


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_skipped_plugin_is_preserved_by_mapping_while_an_independent_plugin_converges(transport, tool, strategy):
    market = market_fixture(transport, tool)
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, []),
    )
    settings = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    format = "toml" if tool == "codex" else "json"
    document = parse_settings(settings.read_bytes(), format=format)
    plugin_key = "plugins" if tool == "codex" else "enabledPlugins"
    table = document[plugin_key]
    assert isinstance(table, dict)
    table["one@fixture-market"] = {"enabled": False} if tool == "codex" else False
    settings.write_bytes(serialize_settings(document, format=format))
    source = transport.root / "source"
    source.write_bytes(
        serialize_settings(
            {
                plugin_key: {"one@fixture-market": {"enabled": True} if tool == "codex" else True},
                "model_reasoning_effort" if tool == "codex" else "alwaysThinkingEnabled": "low"
                if tool == "codex"
                else False,
            },
            format=format,
        )
    )
    incomplete: list[str] = []
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(market)],
            plugins=["one@fixture-market", "two@fixture-market"],
            settings=SettingsMapping(source=str(source), strategy=strategy),
        ),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "fixture-market"),
        ("plugin", "two@fixture-market"),
    }
    document = parse_settings(settings.read_bytes(), format=format)
    if strategy != "skip-existing":
        assert document["model_reasoning_effort" if tool == "codex" else "alwaysThinkingEnabled"] == (
            "low" if tool == "codex" else False
        )
    table = document[plugin_key]
    assert isinstance(table, dict)
    assert table["one@fixture-market"] == ({"enabled": False} if tool == "codex" else False)
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(settings.parent))
        installed = {item.identifier: item for item in cli.plugins(cli.markets())}
        assert not installed["one@fixture-market"].enabled and installed["two@fixture-market"].enabled


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_unowned_market_source_conflict_does_not_block_independent_market(transport, tool):
    market = market_fixture(transport, tool)
    setup_user(tool, NativeUserConfig(marketplaces=[str(market)]), invocation(transport, []))
    alternate = copied_market(market, tool)
    independent = copied_market(market, tool, name="independent")
    incomplete: list[str] = []
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(alternate), str(independent)], plugins=["one@fixture-market", "two@independent"]
        ),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "independent"),
        ("plugin", "two@independent"),
    }
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        current = {item.name: item for item in cli.markets()}
        observed_source = current["fixture-market"].source
        assert observed_source is not None
        assert str(market) in observed_source
        assert str(alternate) not in observed_source
        assert {item.identifier for item in cli.plugins(tuple(current.values()))} == {"two@independent"}


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_owned_marketplace_source_change_reinstalls_its_requested_plugin(transport, tool):
    market = market_fixture(transport, tool)
    initial: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, initial),
    )
    previous = initial[-1]
    alternate = copied_market(market, tool)
    incomplete: list[str] = []
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(alternate)], plugins=["one@fixture-market"]),
        replace(invocation(transport, claims, prior=record(previous, tool)), report_incomplete=incomplete.append),
    )
    assert not incomplete
    assert {item.role for item in claims[-1]} == {"marketplace", "plugin"}
    assert {item.source for item in claims[-1]} != {item.source for item in previous}
    assert all(item.source is not None and str(alternate) in item.source for item in claims[-1])
    setup_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert claims[-1] == ()


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite"])
def test_skipped_market_preserves_its_native_associations_during_mapping(transport, tool, strategy):
    market = market_fixture(transport, tool)
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, []),
    )
    settings = transport.home / (".codex/config.toml" if tool == "codex" else ".claude/settings.json")
    format = "toml" if tool == "codex" else "json"
    before = parse_settings(settings.read_bytes(), format=format)
    source = transport.root / "source"
    field = "model_reasoning_effort" if tool == "codex" else "alwaysThinkingEnabled"
    value = "low" if tool == "codex" else False
    source.write_bytes(serialize_settings({field: value}, format=format))
    incomplete: list[str] = []
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[str(copied_market(market, tool))],
            settings=SettingsMapping(source=str(source), strategy=strategy),
        ),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    after = parse_settings(settings.read_bytes(), format=format)
    assert incomplete and not any(claims)
    assert after[field] == value
    for key in ("marketplaces", "plugins") if tool == "codex" else ("extraKnownMarketplaces", "enabledPlugins"):
        assert after[key] == before[key]


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_market_replacement_retains_ownership_when_dependent_cannot_retire(transport, tool, monkeypatch):
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    previous = claims[-1]
    plugins = NativeCLI.plugins
    observed = 0

    def changing_inventory(cli, markets):
        nonlocal observed
        result = plugins(cli, markets)
        if not cli.isolated:
            observed += 1
            if observed == 1:
                return tuple(replace(plugin, source=None) for plugin in result)
        return result

    monkeypatch.setattr(NativeCLI, "plugins", changing_inventory)
    incomplete: list[str] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(copied_market(market, tool))], plugins=["one@fixture-market"]),
        replace(invocation(transport, claims, prior=record(previous, tool)), report_incomplete=incomplete.append),
    )
    assert incomplete and claims[-1] == previous
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        current = cli.markets()
        assert current[0].source == next(item.source for item in previous if item.role == "marketplace")
        assert {item.identifier for item in cli.plugins(current)} == {"one@fixture-market"}
