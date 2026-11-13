"""Native source tolerance and current-operation progress survive reconciliation."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.capabilities.harness_integration.settings import SettingsMapping, serialize_settings
from agentworks.errors import ExternalError
from agentworks.execution.results import Result
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native_cli import NativeCLI
from agentworks.plugins._harness_native.native_config import NativeUserConfig
from tests.native_setup_fixtures import (
    LocalFixtureTransport,
    invocation,
    market_fixture,
    native_cli,
    record,
    setup_fixture_user,
)
from tests.test_native_convergence import copied_market
from tests.vm_access_support import Call, RecordingVMAccess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local execution of Linux guest setup helpers")


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    return LocalFixtureTransport(tmp_path / "native-round5")


def test_claude_file_marketplace_converges_repeats_and_retires(transport):
    market = market_fixture(transport, "claude")
    source = str(market / ".claude-plugin/marketplace.json")
    claims: list[tuple[NativeClaim, ...]] = []
    initial = NativeUserConfig(marketplaces=[source], plugins=["one@fixture-market"])
    setup_fixture_user("claude", initial, invocation(transport, claims))
    desired = NativeUserConfig(marketplaces=[source], plugins=["two@fixture-market"])
    setup_fixture_user("claude", desired, invocation(transport, claims, prior=record(claims[-1], "claude")))
    root = transport.home / ".claude"
    with native_cli(transport, "claude", root) as cli:
        markets = cli.markets()
        assert json.loads(markets[0].source)["source"] == {"source": "file", "path": source}
        assert {item.identifier for item in cli.plugins(markets)} == {"two@fixture-market"}
    before = (root / "settings.json").read_bytes()
    setup_fixture_user("claude", desired, invocation(transport, claims, prior=record(claims[-1], "claude")))
    assert (root / "settings.json").read_bytes() == before
    setup_fixture_user("claude", None, invocation(transport, claims, prior=record(claims[-1], "claude")))
    assert claims[-1] == ()
    with native_cli(transport, "claude", root) as cli:
        assert cli.markets() == ()


def test_unrelated_claude_file_marketplace_does_not_block_independent_setup(transport):
    market = market_fixture(transport, "claude")
    root = transport.home / ".claude"
    with native_cli(transport, "claude", root) as cli:
        cli.add_market(str(market / ".claude-plugin/marketplace.json"))
        cli.install("one@fixture-market")
    independent = copied_market(market, "claude", name="independent")
    claims: list[tuple[NativeClaim, ...]] = []
    incomplete: list[str] = []
    setup_fixture_user(
        "claude",
        NativeUserConfig(marketplaces=[str(independent)], plugins=["two@independent"]),
        replace(invocation(transport, claims), report_incomplete=incomplete.append),
    )
    assert not incomplete
    assert {item.identifier for item in claims[-1]} == {"independent", "two@independent"}
    with native_cli(transport, "claude", root) as cli:
        assert {item.name for item in cli.markets()} == {"fixture-market", "independent"}
        assert {item.identifier for item in cli.plugins(cli.markets())} == {"one@fixture-market", "two@independent"}


@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("strategy", [None, "replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_new_marketplace_survives_ambiguous_plugin_protection_and_later_retirement(
    transport, monkeypatch, tool, strategy
):
    market = market_fixture(transport, tool)
    second = copied_market(market, tool, name="second-market")
    claims: list[tuple[NativeClaim, ...]] = []
    setup_fixture_user(tool, NativeUserConfig(marketplaces=[str(market)]), invocation(transport, claims))
    format = "json" if tool == "claude" else "toml"
    source = transport.root / "settings-source"
    source.write_bytes(serialize_settings({"mapped": True}, format=format))
    config = NativeUserConfig(
        marketplaces=[str(second)],
        plugins=["one"],
        settings=None if strategy is None else SettingsMapping(source=str(source), strategy=strategy),
    )
    incomplete: list[str] = []
    setup_fixture_user(
        tool,
        config,
        replace(invocation(transport, claims, prior=record(claims[-1], tool)), report_incomplete=incomplete.append),
    )
    assert incomplete
    assert {item.identifier for item in claims[-1]} == {"fixture-market", "second-market"}
    root = transport.home / (".claude" if tool == "claude" else ".codex")
    destination = root / ("settings.json" if tool == "claude" else "config.toml")
    with native_cli(transport, tool, root) as cli:
        assert {item.name for item in cli.markets()} == {"fixture-market", "second-market"}
        assert cli.plugins(cli.markets()) == ()
    before = destination.read_bytes(), destination.stat().st_ino
    registered: list[str] = []
    original_add = NativeCLI.add_market

    def track_add(cli, source):
        registered.append(source)
        return original_add(cli, source)

    monkeypatch.setattr(NativeCLI, "add_market", track_add)
    setup_fixture_user(
        tool,
        config,
        replace(invocation(transport, claims, prior=record(claims[-1], tool)), report_incomplete=incomplete.append),
    )
    assert not registered
    assert (destination.read_bytes(), destination.stat().st_ino) == before
    setup_fixture_user(tool, None, invocation(transport, claims, prior=record(claims[-1], tool)))
    assert claims[-1] == ()
    with native_cli(transport, tool, root) as cli:
        assert cli.markets() == ()


@pytest.mark.parametrize(
    "source",
    [
        {"source": "npm", "package": "@fixture/catalog"},
        {"source": "settings", "name": "unobservable", "plugins": []},
        {"source": "future-native-source"},
        {"source": "directory"},
        {"source": "directory", "path": 5},
        {"source": "git", "url": "/fixture.git", "ref": 5},
    ],
)
def test_unobservable_named_source_is_local_and_never_invents_an_association(transport, monkeypatch, source):
    root = transport.home / ".claude"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "unobservable": {"source": source},
                    "independent": {"source": {"source": "directory", "path": "/independent"}},
                }
            }
        )
    )
    payload = [
        {"name": "unobservable", **source},
        {"name": "independent", "source": "directory", "path": "/independent"},
    ]
    with native_cli(transport, "claude", root) as cli:
        monkeypatch.setattr(cli, "command", lambda *a, **k: payload)
        markets = {item.name: item for item in cli.markets()}
    assert markets["unobservable"].source is None
    assert markets["unobservable"].unavailable_reason
    assert json.loads(markets["independent"].source)["source"]["path"] == "/independent"


@pytest.mark.parametrize("offset", [0, 16384])
@pytest.mark.parametrize("read_only", [False, True])
def test_failed_native_inventory_only_exposes_bounded_named_selector(transport, monkeypatch, offset, read_only):
    executable = transport.root / "fault-native"
    selector = "fixture-fault-market"
    executable.write_text(
        "#!/usr/bin/python3\nimport os, sys\n"
        + ("os.chmod(os.readlink('/proc/self/fd/2'), 0o400)\n" if read_only else "")
        + f"sys.stderr.write('x' * {offset} + '\\nerror in `marketplaces.{selector}`\\n' "
        "+ '\\x1b]0;unsafe-title\\x07 https://fixture-secret@example.invalid/?token=fixture-query' "
        "+ 'x' * (1024 * 1024))\nsys.exit(1)\n"
    )
    executable.chmod(0o700)
    access: RecordingVMAccess = transport.access()
    answer = access.answer
    assert answer is not None
    replies: list[Result] = []

    def keep_reply(call: Call) -> Result | BaseException | None:
        reply = answer(call)
        if isinstance(reply, Result):
            replies.append(reply)
        return reply

    access.answer = keep_reply
    root = transport.home / ".codex"
    with native_cli(transport, "codex", root, access=access) as cli:
        cli._resolved = (str(executable), "/usr/bin:/bin")
        with pytest.raises(ExternalError) as caught:
            cli.command(["plugin", "marketplace", "list", "--json"], structured=True)
    diagnostic = str(caught.value)
    assert (selector in diagnostic) == (offset == 0)
    assert len(diagnostic.encode()) < 1024
    assert max(len(reply.stderr) for reply in replies) <= 8192
    assert all(value not in diagnostic for value in ("fixture-secret", "fixture-query", "unsafe-title", "\x1b", "\x07"))
    assert not any(
        value in text
        for value in ("fixture-secret", "fixture-query", "unsafe-title")
        for text in transport.logged_output
    )


def test_real_codex_inventory_failure_identifies_the_native_setting(transport):
    market_fixture(transport, "codex")
    root = transport.home / ".codex"
    root.mkdir()
    selector = "fixture-invalid-market"
    (root / "config.toml").write_text(f'[marketplaces.{selector}]\nsource_type="invalid-kind"\nsource="/fixture"\n')
    with native_cli(transport, "codex", root) as cli, pytest.raises(ExternalError) as caught:
        cli.markets()
    assert "marketplaces." + selector in str(caught.value)
