"""Native commands cannot discard unrelated or incomplete operator entries."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsMapping, SettingsValue
from agentworks.errors import ExternalError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeMarket, NativeTool
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture, record, setup_fixture_user
from tests.test_native_harness_setup import transport as transport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest native setup helpers")


@pytest.mark.parametrize(
    "entry",
    [5, [], {"source": "notobject"}, {"operator": "keep"}, {"source": {"source": "npm", "package": "INVALID PACKAGE"}}],
)
@pytest.mark.parametrize("relevant", [False, True])
@pytest.mark.parametrize("removal", [False, True])
def test_native_add_preserves_unavailable_operator_entry(
    transport: LocalFixtureTransport, entry: SettingsValue, relevant: bool, removal: bool
) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    if removal:
        setup_fixture_user(
            "claude", NativeUserConfig(marketplaces=[str(market), str(other)]), invocation(transport, claims)
        )
    settings = transport.home / ".claude/settings.json"
    initial = json.loads(settings.read_bytes())
    name = "fixture-market" if relevant else "operator-odd"
    initial["extraKnownMarketplaces"][name] = entry
    settings.write_text(json.dumps(initial))
    warnings: list[str] = []
    call = replace(invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=warnings.append)

    setup_fixture_user(
        "claude", NativeUserConfig(marketplaces=[str(market)] if removal else [str(market), str(other)]), call
    )

    observed = json.loads(settings.read_bytes())
    assert observed["extraKnownMarketplaces"][name] == entry
    assert ("other-market" in observed["extraKnownMarketplaces"]) is removal
    assert any(name in warning for warning in warnings)
    assert any(claim.identifier == "other-market" for claim in claims[-1]) is removal
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool,inventory", [("claude", True), ("codex", False), ("codex", True)])
def test_native_unreadable_marketplace_names_entry_and_preserves_its_source(
    transport: LocalFixtureTransport, tool: NativeTool, inventory: bool
) -> None:
    market = market_fixture(transport, tool)
    root = transport.home / (".claude" if tool == "claude" else ".codex")
    with NativeFiles(transport) as files:
        cli = NativeCLI(tool, files, home=str(transport.home), config_root=str(root))
        if tool == "claude":
            cli.add_market(str(market))
            path = root / "plugins/known_marketplaces.json"
            document = json.loads(path.read_bytes())
            document["fixture-market"]["source"] = {"source": "future-kind", "operator": "keep"}
            path.write_text(json.dumps(document))
        else:
            root.mkdir()
            path = root / "config.toml"
            path.write_text('[marketplaces.fixture-market]\nsource_type="local"\n')
        original = path.read_bytes()
        with pytest.raises(ExternalError) as caught:
            if inventory:
                cli.markets()
            else:
                cli.command(["plugin", "list", "--json"], structured=True)
        assert "fixture-market" in str(caught.value)
        assert str(root) in str(caught.value)
        assert path.read_bytes() == original
    assert not list((transport.root / "tmp").iterdir())


def test_active_reinit_reports_missing_claude_user_source_without_reconstructing_it(
    transport: LocalFixtureTransport,
) -> None:
    market = market_fixture(transport, "claude")
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user("claude", config, invocation(transport, claims))
    prior = claims[-1]
    settings = transport.home / ".claude/settings.json"
    document = json.loads(settings.read_bytes())
    document["extraKnownMarketplaces"].pop("fixture-market")
    settings.write_text(json.dumps(document))
    warnings: list[str] = []
    call = replace(invocation(transport, claims, prior=record(prior, "claude")), report_incomplete=warnings.append)
    setup_fixture_user("claude", config, call)
    assert any("fixture-market" in reason for reason in warnings)
    assert set(claims[-1]) == set(prior)
    assert "fixture-market" not in json.loads(settings.read_bytes())["extraKnownMarketplaces"]
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("add_plugin", [False, True])
def test_unmanaged_inventory_without_a_source_does_not_poison_reinit(transport, tool, add_plugin, monkeypatch):
    market = market_fixture(transport, tool)
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(tool, config, invocation(transport, claims))
    previous = record(claims[-1], tool)
    original = NativeCLI.markets

    def builtin(self):
        return (*original(self), NativeMarket("native-built-in", None, settings_conflict=True))

    monkeypatch.setattr(NativeCLI, "markets", builtin)
    reasons: list[str] = []
    if add_plugin:
        config = config.model_copy(update={"plugins": ["one@fixture-market", "two@fixture-market"]})
    setup_fixture_user(
        tool, config, replace(invocation(transport, claims, prior=previous), report_incomplete=reasons.append)
    )
    assert reasons == []
    assert set(previous.claims) <= set(claims[-1])
    assert {claim.identifier for claim in claims[-1] if claim.role == "plugin"} == set(config.plugins)


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_mapping_can_repair_an_unrelated_unobservable_entry(transport, strategy):
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    config = NativeUserConfig(marketplaces=[str(market)])
    setup_fixture_user("claude", config, invocation(transport, claims))
    settings = transport.home / ".claude/settings.json"
    document = json.loads(settings.read_bytes())
    document["extraKnownMarketplaces"]["operator-odd"] = 5
    settings.write_text(json.dumps(document))
    source = transport.root / "mapped-settings.json"
    corrected = {"source": {"source": "directory", "path": str(market)}}
    source.write_text(json.dumps({"extraKnownMarketplaces": {"operator-odd": corrected}}))
    reasons: list[str] = []
    setup_fixture_user(
        "claude",
        config.model_copy(update={"settings": SettingsMapping(source=str(source), strategy=strategy)}),
        replace(invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=reasons.append),
    )
    observed = json.loads(settings.read_bytes())["extraKnownMarketplaces"]["operator-odd"]
    assert observed == (corrected if strategy in ("replace", "merge-overwrite") else 5)
    assert reasons == []


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize("failure_type", [None, ExternalError, KeyboardInterrupt])
@pytest.mark.parametrize("mapped", [False, True])
def test_native_publication_does_not_restore_a_stale_skipped_entry(
    transport, monkeypatch, remove, failure_type, mapped
):
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    settings = transport.home / ".claude/settings.json"
    document = json.loads(settings.read_bytes())
    document["enabledPlugins"]["one@fixture-market"] = False
    settings.write_text(json.dumps(document))
    original = NativeCLI.install
    failure = None if failure_type is None else failure_type("fixture interrupted native command")

    def operator_edit(self, selector):
        original(self, selector)
        if self.isolated:
            return
        current = json.loads(settings.read_bytes())
        if remove:
            current["enabledPlugins"].pop("one@fixture-market", None)
        else:
            current["enabledPlugins"]["one@fixture-market"] = True
        settings.write_text(json.dumps(current))
        if failure is not None:
            raise failure

    monkeypatch.setattr(NativeCLI, "install", operator_edit)
    call = replace(invocation(transport, claims), report_incomplete=lambda _: None)
    mapping = transport.root / "mapping.json"
    mapping.write_text('{"mapped": true}')
    config = NativeUserConfig(
        marketplaces=[str(market)],
        plugins=["one@fixture-market", "two@fixture-market"],
        settings=SettingsMapping(source=str(mapping), strategy="replace") if mapped else None,
    )
    if failure_type is None:
        setup_fixture_user("claude", config, call)
    else:
        with pytest.raises(failure_type) as caught:
            setup_fixture_user("claude", config, call)
        assert caught.value is failure
    observed = json.loads(settings.read_bytes())["enabledPlugins"]
    assert ("one@fixture-market" not in observed) if remove else observed["one@fixture-market"] is True


@pytest.mark.parametrize("failure_type", [ExternalError, KeyboardInterrupt])
def test_confirmed_uninstall_is_not_resurrected_after_a_later_add_failure(transport, monkeypatch, failure_type):
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(
        "claude",
        NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"]),
        invocation(transport, claims),
    )
    original = NativeCLI.install
    failure = failure_type("fixture failure after native installation")

    def fail_after_install(self, selector):
        original(self, selector)
        if not self.isolated:
            raise failure

    monkeypatch.setattr(NativeCLI, "install", fail_after_install)
    with pytest.raises(failure_type) as caught:
        setup_fixture_user(
            "claude",
            NativeUserConfig(marketplaces=[str(market)], plugins=["two@fixture-market"]),
            invocation(transport, claims, prior=record(claims[-1], "claude")),
        )
    assert caught.value is failure
    document = json.loads((transport.home / ".claude/settings.json").read_bytes())
    assert "one@fixture-market" not in document["enabledPlugins"]
    assert all(claim.identifier != "one@fixture-market" for claim in claims[-1])


def test_mapping_repair_allows_a_later_native_retry_without_losing_the_repair(transport):
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    settings = transport.home / ".claude/settings.json"
    document = json.loads(settings.read_bytes())
    document["extraKnownMarketplaces"]["operator-odd"] = 5
    settings.write_text(json.dumps(document))
    corrected = {"source": {"source": "directory", "path": str(market)}}
    source = transport.root / "mapped-settings.json"
    source.write_text(json.dumps({"extraKnownMarketplaces": {"operator-odd": corrected}}))
    config = NativeUserConfig(
        marketplaces=[str(market), str(other)],
        settings=SettingsMapping(source=str(source), strategy="merge-overwrite"),
    )
    reasons: list[str] = []
    for attempt in range(2):
        reasons.clear()
        setup_fixture_user(
            "claude",
            config,
            replace(
                invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=reasons.append
            ),
        )
        assert json.loads(settings.read_bytes())["extraKnownMarketplaces"]["operator-odd"] == corrected
        assert bool(reasons) is (attempt == 0)
    assert any(claim.identifier == "other-market" for claim in claims[-1])
