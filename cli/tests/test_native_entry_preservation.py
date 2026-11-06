"""Native commands cannot discard unrelated or incomplete operator entries."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from dataclasses import replace
from uuid import uuid4

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsValue
from agentworks.errors import ExternalError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from agentworks.plugins._harness_native.native import setup_user
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeTool
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture, record
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
    setup_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    if removal:
        setup_user("claude", NativeUserConfig(marketplaces=[str(market), str(other)]), invocation(transport, claims))
    settings = transport.home / ".claude/settings.json"
    initial = json.loads(settings.read_bytes())
    name = "fixture-market" if relevant else "operator-odd"
    initial["extraKnownMarketplaces"][name] = entry
    settings.write_text(json.dumps(initial))
    warnings: list[str] = []
    call = replace(invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=warnings.append)

    setup_user("claude", NativeUserConfig(marketplaces=[str(market)] if removal else [str(market), str(other)]), call)

    observed = json.loads(settings.read_bytes())
    assert observed["extraKnownMarketplaces"][name] == entry
    assert ("other-market" in observed["extraKnownMarketplaces"]) is not removal
    assert any(name in warning for warning in warnings)
    assert any(claim.identifier == "other-market" for claim in claims[-1]) is not removal
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


@pytest.mark.parametrize("failure_type", [ExternalError, KeyboardInterrupt])
@pytest.mark.parametrize("restoration_fails", [False, True])
def test_native_prune_then_failure_restores_protected_entries_and_retains_primary_error(
    transport: LocalFixtureTransport,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    restoration_fails: bool,
) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    settings = transport.home / ".claude/settings.json"
    initial = json.loads(settings.read_bytes())
    initial["extraKnownMarketplaces"]["operator-odd"] = 5
    settings.write_text(json.dumps(initial))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    original_add = NativeCLI.add_market
    original_publish = NativeFiles.publish
    failure = failure_type("native command completed before its failure was reported")
    secret = "x" + uuid4().hex.upper() + "\nsecond-part"
    path = str(settings)

    def prune_then_fail(self: NativeCLI, source: str) -> None:
        original_add(self, source)
        if not self.isolated:
            assert "operator-odd" not in json.loads(settings.read_bytes())["extraKnownMarketplaces"]
            raise failure

    def fail_restoration(self: NativeFiles, destination: str, content: bytes, **kwargs) -> None:
        if restoration_fails and destination == path:
            raise ExternalError("\x1b[" + secret + " " + path)
        original_publish(self, destination, content, **kwargs)

    monkeypatch.setattr(NativeCLI, "add_market", prune_then_fail)
    monkeypatch.setattr(NativeFiles, "publish", fail_restoration)
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    call = replace(
        invocation(transport, claims, prior=record(claims[-1], "claude")),
        report_incomplete=lambda _: None,
        secrets={"credential": secret},
    )
    with pytest.raises(failure_type) as caught:
        setup_user("claude", NativeUserConfig(marketplaces=[str(market), str(other)]), call)
    assert caught.value is failure
    observed = json.loads(settings.read_bytes())["extraKnownMarketplaces"]
    assert (observed.get("operator-odd") == 5) is not restoration_fails
    assert "other-market" in observed
    assert bool(warnings) is restoration_fails
    if restoration_fails:
        assert path in warnings[0]
        assert all(fragment not in warnings[0] for fragment in (secret, secret[1:].splitlines()[0], "\x1b"))
    assert not list((transport.root / "tmp").iterdir())


def test_actual_native_command_failure_after_rewrite_restores_protected_entries(
    transport: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    settings = transport.home / ".claude/settings.json"
    initial = json.loads(settings.read_bytes())
    initial["extraKnownMarketplaces"]["operator-odd"] = 5
    settings.write_text(json.dumps(initial))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    launcher = transport.root / "bin/claude"
    original = launcher.resolve()
    launcher.unlink()
    launcher.write_text(
        "#!/bin/bash\n"
        + shlex.join([str(original)])
        + ' "$@"\nstatus=$?\n'
        + 'if [ "$1 $2 $3" = "plugin marketplace add" ] && [ "$CLAUDE_CONFIG_DIR" = '
        + shlex.quote(str(settings.parent))
        + ' ]; then exit 37; fi\nexit "$status"\n'
    )
    launcher.chmod(0o700)
    call = replace(invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=lambda _: None)
    with pytest.raises(ExternalError):
        setup_user("claude", NativeUserConfig(marketplaces=[str(market), str(other)]), call)
    observed = json.loads(settings.read_bytes())["extraKnownMarketplaces"]
    assert observed["operator-odd"] == 5 and "other-market" in observed
    assert not list((transport.root / "tmp").iterdir())


def test_active_reinit_reports_missing_claude_user_source_without_reconstructing_it(
    transport: LocalFixtureTransport,
) -> None:
    market = market_fixture(transport, "claude")
    config = NativeUserConfig(marketplaces=[str(market)], plugins=["one@fixture-market"])
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("claude", config, invocation(transport, claims))
    prior = claims[-1]
    settings = transport.home / ".claude/settings.json"
    document = json.loads(settings.read_bytes())
    document["extraKnownMarketplaces"].pop("fixture-market")
    settings.write_text(json.dumps(document))
    warnings: list[str] = []
    call = replace(invocation(transport, claims, prior=record(prior, "claude")), report_incomplete=warnings.append)
    setup_user("claude", config, call)
    assert any("fixture-market" in reason for reason in warnings)
    assert set(claims[-1]) == set(prior)
    assert "fixture-market" not in json.loads(settings.read_bytes())["extraKnownMarketplaces"]
    assert not list((transport.root / "tmp").iterdir())
