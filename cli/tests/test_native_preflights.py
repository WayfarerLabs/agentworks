"""Conservative native preflights against actual isolated harness commands."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsMapping, parse_settings, serialize_settings
from agentworks.errors import ConfigError, ExternalError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native import setup_user
from agentworks.plugins._harness_native.native_cli import NativeCLI
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture, record
from tests.test_native_convergence import copied_market, git_market

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local execution of Linux guest setup helpers")


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    root = tmp_path / "native-preflights"
    root.mkdir()
    return LocalFixtureTransport(root)


@pytest.mark.parametrize(
    "tool,declaration",
    [
        ("codex", None),
        ("claude", None),
        ("claude", {}),
        ("claude", {"autoUpdate": False}),
        ("claude", False),
        ("claude", {"source": {"source": "git", "url": "file:///missing-stale-market"}}),
    ],
)
@pytest.mark.parametrize("option", ["ref", "sparse"])
def test_incomplete_or_differing_git_source_survives_mapping(transport, tool, declaration, option):
    market, source = git_market(transport, tool)
    independent = copied_market(market, tool, name="independent")
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    root.mkdir()
    destination = root / ("config.toml" if tool == "codex" else "settings.json")
    format = "toml" if tool == "codex" else "json"
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        args = ["plugin", "marketplace", "add", source]
        if option == "ref":
            if tool == "codex":
                args += ["--ref", "v1"]
            else:
                args[-1] += "#v1"
        elif tool == "codex":
            args += ["--sparse", ".agents/plugins", "--sparse", "plugins"]
        else:
            args += ["--sparse", ".claude-plugin", "plugins"]
        if tool == "claude":
            args += ["--scope", "user"]
        cli.command(args)
        cli.install("one@fixture-market")
        if tool == "claude":
            # Fixture evidence only: native list cannot expose the sparse paths.
            registered_source = json.loads((root / "plugins/known_marketplaces.json").read_bytes())["fixture-market"][
                "source"
            ]
            assert ("ref" if option == "ref" else "sparsePaths") in registered_source
            document = parse_settings(destination.read_bytes(), format=format)
            declarations = document["extraKnownMarketplaces"]
            assert isinstance(declarations, dict)
            declarations.pop("fixture-market")
            if declaration is not None:
                declarations["fixture-market"] = declaration
            destination.write_bytes(serialize_settings(document, format=format))
        before = cli.markets()
        assert (before[0].source is None) == (tool == "claude")
    mapping = transport.root / "mapping"
    mapping.write_bytes(serialize_settings({"mapped": True}, format=format))
    incomplete: list[str] = []
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(
            marketplaces=[source, str(independent)],
            plugins=["one@fixture-market", "two@independent"],
            settings=SettingsMapping(source=str(mapping), strategy="replace"),
        ),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "independent"),
        ("plugin", "two@independent"),
    }
    document = parse_settings(destination.read_bytes(), format=format)
    assert document["mapped"] is True
    if tool == "claude":
        declarations = document.get("extraKnownMarketplaces", {})
        enabled = document["enabledPlugins"]
        assert isinstance(declarations, dict) and isinstance(enabled, dict)
        # Unrelated native registration can discard source-less settings entries.
        assert declarations.get("fixture-market") in (None, declaration)
        assert enabled["one@fixture-market"] is True
        assert (
            json.loads((root / "plugins/known_marketplaces.json").read_bytes())["fixture-market"]["source"]
            == registered_source
        )
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        observed = cli.markets()
        assert next(item for item in observed if item.name == "fixture-market") == before[0]
        assert {item.identifier for item in cli.plugins(observed)} == {"one@fixture-market", "two@independent"}


@pytest.mark.parametrize("source_kind", ["local", "git"])
@pytest.mark.parametrize("action", ["retire", "replace"])
@pytest.mark.parametrize("declaration", [None, {}, {"autoUpdate": False}])
@pytest.mark.parametrize("dependent", [False, True])
def test_claude_missing_user_declaration_blocks_removal_before_dependent_uninstall(
    transport, source_kind, action, declaration, dependent
):
    if source_kind == "git":
        market, source = git_market(transport, "claude")
    else:
        market = market_fixture(transport, "claude")
        source = str(market)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "claude",
        NativeUserConfig(marketplaces=[source], plugins=["one@fixture-market"] if dependent else []),
        invocation(transport, claims),
    )
    previous = claims[-1]
    independent = copied_market(market, "claude", name="independent")
    destination = transport.home / ".claude/settings.json"
    document = json.loads(destination.read_bytes())
    document["extraKnownMarketplaces"].pop("fixture-market")
    if declaration is not None:
        document["extraKnownMarketplaces"]["fixture-market"] = declaration
    destination.write_text(json.dumps(document))
    mapping = transport.root / "mapping.json"
    mapping.write_text('{"mapped": true}')
    config = NativeUserConfig(
        marketplaces=[str(independent)] + ([str(copied_market(market, "claude"))] if action == "replace" else []),
        plugins=["two@independent"] + (["one@fixture-market"] if action == "replace" else []),
        settings=SettingsMapping(source=str(mapping), strategy="replace"),
    )
    incomplete: list[str] = []
    setup_user(
        "claude",
        config,
        replace(invocation(transport, claims, prior=record(previous, "claude")), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert set(previous) <= set(claims[-1])
    assert {item.identifier for item in claims[-1]} == {
        "fixture-market",
        "independent",
        "two@independent",
    } | ({"one@fixture-market"} if dependent else set())
    document = json.loads(destination.read_bytes())
    assert document["mapped"] is True
    assert document.get("extraKnownMarketplaces", {}).get("fixture-market") in (None, declaration)
    if dependent:
        assert document["enabledPlugins"]["one@fixture-market"] is True
    else:
        assert "one@fixture-market" not in document.get("enabledPlugins", {})
    with NativeFiles(transport) as files:
        cli = NativeCLI("claude", files, home=str(transport.home), config_root=str(destination.parent))
        assert {item.name for item in cli.markets()} == {"fixture-market", "independent"}
        assert {item.identifier for item in cli.plugins(cli.markets())} == {"two@independent"} | (
            {"one@fixture-market"} if dependent else set()
        )


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("replace_source", [False, True])
def test_ambiguous_plugin_preserves_claims_and_mapping_while_unique_selection_continues(
    transport, tool, replace_source
):
    market = market_fixture(transport, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        tool,
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    previous = claims[-1]
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    with NativeFiles(transport) as files:
        NativeCLI(tool, files, home=str(transport.home), config_root=str(root)).add_market(
            str(copied_market(market, tool, name="operator-market"))
        )
    format = "toml" if tool == "codex" else "json"
    mapping = transport.root / "mapping"
    mapping.write_bytes(serialize_settings({"mapped": True}, format=format))
    config = NativeUserConfig(
        marketplaces=[str(copied_market(market, tool))] if replace_source else [],
        plugins=["one", "two@operator-market"],
        settings=SettingsMapping(source=str(mapping), strategy="replace"),
    )
    incomplete: list[str] = []
    setup_user(
        tool,
        config,
        replace(invocation(transport, claims, prior=record(previous, tool)), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert set(previous) <= set(claims[-1])
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        observed = cli.markets()
        assert {item.name for item in observed} == {"fixture-market", "operator-market"}
        assert {item.identifier for item in cli.plugins(observed)} == {"one@fixture-market", "two@operator-market"}
    qualified = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    incomplete.clear()
    setup_user(
        tool,
        qualified,
        replace(invocation(transport, claims, prior=record(claims[-1], tool)), report_incomplete=incomplete.append),
    )
    assert not incomplete
    before = (root / ("config.toml" if tool == "codex" else "settings.json")).read_bytes()
    missing = "fixture-missing-selector"
    with pytest.raises(ConfigError) as caught:
        setup_user(tool, NativeUserConfig(plugins=[missing]), invocation(transport, []))
    assert missing in str(caught.value)
    assert (root / ("config.toml" if tool == "codex" else "settings.json")).read_bytes() == before


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("malformed", ["not-json", "[1]"])
def test_malformed_persisted_plugin_source_retains_on_retirement_and_heals_desired_adoption(transport, tool, malformed):
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, config, invocation(transport, claims))
    previous = tuple(
        item.model_copy(update={"source": malformed}) if item.role == "plugin" else item for item in claims[-1]
    )
    incomplete: list[str] = []
    count = len(claims)
    setup_user(
        tool,
        None,
        replace(invocation(transport, claims, prior=record(previous, tool)), report_incomplete=incomplete.append),
    )
    assert incomplete and len(claims) == count
    setup_user(tool, config, invocation(transport, claims, prior=record(previous, tool)))
    assert all(item.source != malformed for item in claims[-1])
    assert {item.role for item in claims[-1]} == {"marketplace", "plugin"}
    setup_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert claims[-1] == ()


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_ambiguous_selection_retains_both_previously_owned_marketplaces(transport, tool):
    market = market_fixture(transport, tool)
    second = copied_market(market, tool, name="second-market")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(tool, NativeUserConfig(marketplaces=[str(market), str(second)]), invocation(transport, claims))
    previous = claims[-1]
    incomplete: list[str] = []
    setup_user(
        tool,
        NativeUserConfig(plugins=["one"]),
        replace(invocation(transport, claims, prior=record(previous, tool)), report_incomplete=incomplete.append),
    )
    assert len(incomplete) == 1
    assert claims[-1] == previous
    root = transport.home / (".codex" if tool == "codex" else ".claude")
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        assert {item.name for item in cli.markets()} == {"fixture-market", "second-market"}
        assert cli.plugins(cli.markets()) == ()


@pytest.mark.parametrize("declaration", [False, {"source": {"source": "directory", "path": "/missing-stale-market"}}])
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("mapped", [False, True])
def test_claude_invalid_named_declaration_retains_association_and_converges_independent_market(
    transport, declaration, owned, mapped
):
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    previous = claims[-1] if owned else ()
    root = transport.home / ".claude"
    destination = root / "settings.json"
    document = json.loads(destination.read_bytes())
    document["extraKnownMarketplaces"]["fixture-market"] = declaration
    destination.write_text(json.dumps(document))
    registry_path = root / "plugins/known_marketplaces.json"
    registered = json.loads(registry_path.read_bytes())["fixture-market"]
    independent = copied_market(market, "claude", name="independent")
    mapping = transport.root / "settings-source.json"
    mapping.write_text('{"mapped": true}')
    incomplete: list[str] = []
    setup_user(
        "claude",
        NativeUserConfig(
            marketplaces=[str(market), str(independent)],
            plugins=["one@fixture-market", "two@independent"],
            settings=SettingsMapping(source=str(mapping), strategy="replace") if mapped else None,
        ),
        replace(invocation(transport, claims, prior=record(previous, "claude")), report_incomplete=incomplete.append),
    )
    assert incomplete and any("fixture-market" in item for item in incomplete)
    codex_market = market_fixture(transport, "codex")
    codex_claims: list[tuple[NativeClaim, ...]] = []
    setup_user(
        "codex",
        NativeUserConfig(marketplaces=[str(codex_market)], plugins=["one@fixture-market"]),
        invocation(transport, codex_claims),
    )
    assert {item.role for item in codex_claims[-1]} == {"marketplace", "plugin"}
    assert set(previous) <= set(claims[-1])
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "independent"),
        ("plugin", "two@independent"),
    } | ({(item.role, item.identifier) for item in previous} if owned else set())
    assert json.loads(registry_path.read_bytes())["fixture-market"] == registered
    final_document = json.loads(destination.read_bytes())
    assert final_document["extraKnownMarketplaces"]["fixture-market"] == declaration
    if mapped:
        assert final_document["mapped"] is True
    with NativeFiles(transport) as files:
        cli = NativeCLI("claude", files, home=str(transport.home), config_root=str(root))
        markets = {item.name: item for item in cli.markets()}
        assert markets["fixture-market"].source is None
        assert markets["fixture-market"].unavailable_reason is not None
        assert markets["independent"].source is not None
        assert {item.identifier for item in cli.plugins(tuple(markets.values()))} == {
            "one@fixture-market",
            "two@independent",
        }


@pytest.mark.parametrize(
    "payload,declarations",
    [
        ({}, {}),
        ([{"name": "one", "source": "directory"}], {}),
        ([{"name": "one", "source": "directory", "path": 5}], {}),
        ([{"name": "one", "source": "directory", "path": "/one"}], []),
        ([{"name": "one", "source": "directory", "path": "/one"}] * 2, {}),
    ],
)
def test_invalid_broad_marketplace_inventory_still_refuses(transport, monkeypatch, payload, declarations):
    root = transport.home / ".claude"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"extraKnownMarketplaces": declarations}))
    with NativeFiles(transport) as files:
        cli = NativeCLI("claude", files, home=str(transport.home), config_root=str(root))
        monkeypatch.setattr(cli, "command", lambda *a, **k: payload)
        with pytest.raises(ExternalError):
            cli.markets()


@pytest.mark.parametrize(
    "args,selector",
    [
        (
            [
                "plugin",
                "marketplace",
                "add",
                "https://fixture-secret@example.invalid/market.git?token=fixture-query#v1",
            ],
            "example.invalid/market.git",
        ),
        (["plugin", "marketplace", "remove", "fixture-market"], "fixture-market"),
        (["plugin", "install", "one@fixture-market"], "one@fixture-market"),
        (["plugin", "uninstall", "one@fixture-market"], "one@fixture-market"),
    ],
)
def test_native_operation_failure_identifies_request_without_provider_output_or_url_credentials(
    transport, args, selector
):
    with NativeFiles(transport) as files:
        cli = NativeCLI("claude", files, home=str(transport.home), config_root=str(transport.home / ".claude"))
        cli._resolved = ("/bin/false", "/usr/bin:/bin")
        with pytest.raises(ExternalError) as caught:
            cli.command(args)
    diagnostic = str(caught.value)
    assert selector in diagnostic
    assert all(operation in diagnostic for operation in args[:2])
    assert "fixture-secret" not in diagnostic
    assert "fixture-query" not in diagnostic
    assert not any(ord(character) < 32 for character in diagnostic)


@pytest.mark.parametrize("declaration", [False, {"source": {"source": "directory", "path": "/missing-unrelated"}}])
def test_unknown_unclaimed_market_does_not_block_unrelated_qualified_plugin(transport, declaration):
    market = market_fixture(transport, "claude")
    setup_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, []))
    destination = transport.home / ".claude/settings.json"
    document = json.loads(destination.read_bytes())
    document["extraKnownMarketplaces"]["fixture-market"] = declaration
    destination.write_text(json.dumps(document))
    independent = copied_market(market, "claude", name="independent")
    claims: list[tuple[NativeClaim, ...]] = []
    incomplete: list[str] = []
    setup_user(
        "claude",
        NativeUserConfig(marketplaces=[str(independent)], plugins=["two@independent"]),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    assert not incomplete
    assert {(item.role, item.identifier) for item in claims[-1]} == {
        ("marketplace", "independent"),
        ("plugin", "two@independent"),
    }
