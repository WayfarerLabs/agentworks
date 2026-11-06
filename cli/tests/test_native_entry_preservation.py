"""Native commands cannot discard unrelated or incomplete operator entries."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsValue
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native import setup_user
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, market_fixture, record
from tests.test_native_harness_setup import transport as transport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest native setup helpers")


@pytest.mark.parametrize("entry", [5, [], {"source": "notobject"}, {"operator": "keep"}])
@pytest.mark.parametrize("relevant", [False, True])
def test_native_add_preserves_unavailable_operator_entry(
    transport: LocalFixtureTransport, entry: SettingsValue, relevant: bool
) -> None:
    market = market_fixture(transport, "claude")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_user("claude", NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    settings = transport.home / ".claude/settings.json"
    initial = json.loads(settings.read_bytes())
    name = "fixture-market" if relevant else "operator-odd"
    initial["extraKnownMarketplaces"][name] = entry
    settings.write_text(json.dumps(initial))
    other = market.with_name("other-market")
    shutil.copytree(market, other)
    manifest = other / ".claude-plugin/marketplace.json"
    content = json.loads(manifest.read_bytes())
    content["name"] = "other-market"
    manifest.write_text(json.dumps(content))
    warnings: list[str] = []
    call = replace(invocation(transport, claims, prior=record(claims[-1], "claude")), report_incomplete=warnings.append)

    setup_user("claude", NativeUserConfig(marketplaces=[str(market), str(other)]), call)

    observed = json.loads(settings.read_bytes())
    assert observed["extraKnownMarketplaces"][name] == entry
    assert "other-market" in observed["extraKnownMarketplaces"]
    assert any(name in warning for warning in warnings)
    assert any(claim.identifier == "other-market" for claim in claims[-1])
    assert not list((transport.root / "tmp").iterdir())
