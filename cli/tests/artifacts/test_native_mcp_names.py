"""Declared MCP names retain ordinary adoption and original input attribution."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.model import ArtifactInput
from agentworks.artifacts.native.mcp import (
    MCP_CLAIM_ROLE,
    apply_mcp,
    mcp_input_conflicts,
    render_mcp,
    session_mcp_conflicts,
)
from agentworks.capabilities.harness_integration.settings import SettingsObject
from agentworks.errors import ExternalError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_mcp import read, server, table, write
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest file helpers")


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    return LocalFixtureTransport(tmp_path)


def legacy_name(item: ArtifactInput) -> str:
    return f"agentworks-{item.content.name[:24].rstrip('-')}-{item.origin_identity[:20]}"


def claim_for(item: ArtifactInput, name: str, path: Path, tool="codex") -> NativeClaim:
    encoded = json.dumps(render_mcp(item, tool), sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return NativeClaim(
        role=MCP_CLAIM_ROLE,
        identifier=name,
        destination=str(path),
        source=item.origin_identity,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_literal_native_entry_is_adopted_without_publication_and_retry_is_unchanged(transport, monkeypatch, tool):
    item = server("inventory-" + "a" * 54)
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    write(path, {key: {item.content.name: render_mcp(item, tool)}, "foreign": True}, tool)
    before = path.read_bytes()
    publish = Mock(side_effect=AssertionError("identical adoption must not publish"))
    monkeypatch.setattr(NativeFiles, "publish", publish)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    events: list[str] = []

    def checkpoint(claims):
        checkpoints.append(claims)
        events.append("checkpoint")

    monkeypatch.setattr("agentworks.artifacts.native.mcp.output.info", lambda message: events.append("notice"))
    call = replace(invocation(transport, checkpoints), artifacts=received(item), checkpoint=checkpoint)
    assert not apply_mcp(tool, call, destination=str(path)).deferred
    assert checkpoints == [(claim_for(item, item.content.name, path, tool),)]
    assert events == ["checkpoint", "notice"]
    prior = record(checkpoints[-1], "codex")
    checkpoints.clear()
    events.clear()
    assert not apply_mcp(tool, replace(call, prior=prior), destination=str(path)).deferred
    assert not checkpoints and not events and path.read_bytes() == before
    publish.assert_not_called()


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_adoption_notice_requires_a_successful_checkpoint_and_an_existing_unowned_entry(transport, monkeypatch, tool):
    item = server()
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    write(path, {key: {item.content.name: render_mcp(item, tool)}}, tool)
    before = path.read_bytes()
    notice = Mock()
    monkeypatch.setattr("agentworks.artifacts.native.mcp.output.info", notice)
    failed = replace(
        invocation(transport, []),
        artifacts=received(item),
        checkpoint=Mock(side_effect=ExternalError("fixture checkpoint failure")),
    )
    with pytest.raises(ExternalError):
        apply_mcp(tool, failed, destination=str(path))
    assert path.read_bytes() == before
    notice.assert_not_called()
    checkpoints: list[tuple[NativeClaim, ...]] = []
    assert not apply_mcp(tool, replace(failed, checkpoint=checkpoints.append), destination=str(path)).deferred
    assert checkpoints == [(claim_for(item, item.content.name, path, tool),)]
    notice.assert_called_once()
    notice.reset_mock()
    created = server("new-entry")
    assert not apply_mcp(
        tool,
        replace(
            failed,
            prior=record(checkpoints[-1], "codex"),
            artifacts=received(item, created),
            checkpoint=checkpoints.append,
        ),
        destination=str(path),
    ).deferred
    notice.assert_not_called()


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
@pytest.mark.parametrize("old_state", ["unchanged", "modified", "unclaimed"])
@pytest.mark.parametrize("plain_exists", [False, True])
def test_recorded_legacy_name_migrates_atomically_without_prefix_authority(
    transport, monkeypatch, tool, old_state, plain_exists
):
    item = server()
    old_name = legacy_name(item)
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    original = render_mcp(item, tool)
    old_entry = {**original, "command": "operator"} if old_state == "modified" else original
    entries: SettingsObject = {old_name: old_entry}
    if plain_exists:
        entries[item.content.name] = original
    write(path, {key: entries, "foreign": True}, tool)
    previous = () if old_state == "unclaimed" else (claim_for(item, old_name, path, tool),)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    reports: list[str] = []
    call = replace(
        invocation(transport, checkpoints, prior=record(previous, "codex")),
        artifacts=received(item),
        report_incomplete=reports.append,
    )
    published: list[bytes] = []
    native_publish = NativeFiles.publish

    def publish(files, destination, content, **kwargs):
        published.append(content)
        native_publish(files, destination, content, **kwargs)

    monkeypatch.setattr(NativeFiles, "publish", publish)
    result = apply_mcp(tool, call, destination=str(path))
    assert not result.deferred
    final_entries = table(read(path, tool), key)
    assert final_entries[item.content.name] == original
    assert (old_name in final_entries) is (old_state != "unchanged")
    assert old_state == "unchanged" or final_entries[old_name] == old_entry
    assert {claim.identifier for claim in checkpoints[-1]} == (
        {old_name, item.content.name} if old_state == "modified" else {item.content.name}
    )
    assert bool(reports) is (old_state == "modified")
    assert len(published) == int(not plain_exists or old_state == "unchanged")
    assert read(path, tool)["foreign"] is True


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_equal_inputs_share_one_claim_and_withdrawing_one_keeps_the_entry(transport, monkeypatch, tool):
    first, second = server(), server(component="workspace")
    second = replace(second, content=replace(second.content, description="Different original metadata"))
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(first, second))
    assert mcp_input_conflicts(call.artifacts, tool) == {}
    assert not apply_mcp(tool, call, destination=str(path)).deferred
    assert len(checkpoints[-1]) == 1
    assert set(table(read(path, tool), key)) == {first.content.name}
    before = path.read_bytes()
    monkeypatch.setattr(
        NativeFiles, "publish", Mock(side_effect=AssertionError("remaining equal input must keep entry"))
    )
    assert not apply_mcp(
        tool,
        replace(call, prior=record(checkpoints[-1], "codex"), artifacts=received(second)),
        destination=str(path),
    ).deferred
    assert checkpoints[-1][0].source == second.origin_identity
    assert path.read_bytes() == before


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_unresolved_equal_representative_survives_local_withdrawal_and_recovery(transport, tool):
    ancestor, local = server(component="vm"), server()
    independent = server("independent")
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(ancestor, local))
    assert not apply_mcp(tool, call, destination=str(path)).deferred
    protected = checkpoints[-1][0]
    assert protected.source == ancestor.origin_identity
    unknown = replace(
        call,
        prior=record(checkpoints[-1], "codex"),
        artifacts=received(local),
        retain_artifact_origins=frozenset({ancestor.origin_identity}),
    )
    before = path.read_bytes()
    checkpoints.clear()
    for _ in range(2):
        assert not apply_mcp(tool, unknown, destination=str(path)).deferred
        assert not checkpoints and path.read_bytes() == before

    withdrawn = replace(unknown, artifacts=received(independent))
    assert not apply_mcp(tool, withdrawn, destination=str(path)).deferred
    assert protected in checkpoints[-1]
    assert table(read(path, tool), key) == {
        ancestor.content.name: render_mcp(ancestor, tool),
        independent.content.name: render_mcp(independent, tool),
    }
    current = checkpoints[-1]
    checkpoints.clear()
    retry = replace(withdrawn, prior=record(current, "codex"))
    before = path.read_bytes()
    assert not apply_mcp(tool, retry, destination=str(path)).deferred
    assert not checkpoints and path.read_bytes() == before

    recovered = replace(retry, artifacts=received(ancestor, independent), retain_artifact_origins=frozenset())
    assert not apply_mcp(tool, recovered, destination=str(path)).deferred
    assert not checkpoints and path.read_bytes() == before
    assert not apply_mcp(tool, replace(recovered, artifacts=received(independent)), destination=str(path)).deferred
    assert checkpoints[-1] == (claim_for(independent, independent.content.name, path, tool),)
    assert set(table(read(path, tool), key)) == {independent.content.name}
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
@pytest.mark.parametrize("missing", [False, True])
def test_unresolved_representative_defers_local_replacement_until_routing_recovers(transport, tool, missing):
    ancestor, local = server(component="vm"), server()
    changed, independent = server(command="changed"), server("independent")
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(ancestor, local))
    assert not apply_mcp(tool, call, destination=str(path)).deferred
    protected = checkpoints[-1][0]
    assert protected.source == ancestor.origin_identity
    if missing:
        write(path, {key: {}}, tool)
    unknown = replace(
        call,
        prior=record(checkpoints[-1], "codex"),
        artifacts=received(changed, independent),
        retain_artifact_origins=frozenset({ancestor.origin_identity}),
    )
    checkpoints.clear()
    for _ in range(2):
        result = apply_mcp(tool, unknown, destination=str(path))
        assert {entry.input_id for entry in result.deferred} == {changed.identity}
        entries = table(read(path, tool), key)
        assert (ancestor.content.name in entries) is not missing
        if not missing:
            assert entries[ancestor.content.name] == render_mcp(ancestor, tool)
        assert entries[independent.content.name] == render_mcp(independent, tool)
        assert protected in checkpoints[-1]
        unknown = replace(unknown, prior=record(checkpoints[-1], "codex"))
    before = path.read_bytes()
    current = checkpoints[-1]
    checkpoints.clear()
    assert {entry.input_id for entry in apply_mcp(tool, unknown, destination=str(path)).deferred} == {changed.identity}
    assert not checkpoints and path.read_bytes() == before

    recovered = replace(unknown, prior=record(current, "codex"), retain_artifact_origins=frozenset())
    assert not apply_mcp(tool, recovered, destination=str(path)).deferred
    assert table(read(path, tool), key)[changed.content.name] == render_mcp(changed, tool)
    assert {claim.source for claim in checkpoints[-1]} == {changed.origin_identity, independent.origin_identity}
    recovered = replace(recovered, prior=record(checkpoints[-1], "codex"))
    checkpoints.clear()
    assert not apply_mcp(tool, recovered, destination=str(path)).deferred
    assert not checkpoints
    assert not list((transport.root / "tmp").iterdir())


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_conflicting_literal_alias_preserves_claim_from_a_different_former_owner_and_key(transport, tool):
    former = server("former-key", component="vm")
    name = legacy_name(former)
    first, second = server(name), server(name, command="different", component="workspace")
    independent = server("independent")
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    previous = (claim_for(former, name, path, tool),)
    assert previous[0].source not in {first.origin_identity, second.origin_identity, independent.origin_identity}
    write(path, {key: {name: render_mcp(former, tool)}}, tool)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(
        invocation(transport, checkpoints, prior=record(previous, "codex")),
        artifacts=received(first, second, independent),
    )
    result = apply_mcp(tool, call, destination=str(path))
    assert {entry.input_id for entry in result.deferred} == {first.identity, second.identity}
    assert previous[0] in checkpoints[-1]
    assert table(read(path, tool), key) == {
        name: render_mcp(former, tool),
        independent.content.name: render_mcp(independent, tool),
    }
    before = path.read_bytes()
    call = replace(call, prior=record(checkpoints[-1], "codex"))
    checkpoints.clear()
    result = apply_mcp(tool, call, destination=str(path))
    assert {entry.input_id for entry in result.deferred} == {first.identity, second.identity}
    assert not checkpoints and path.read_bytes() == before


@pytest.mark.parametrize("previous_name", ["plain", "legacy"])
def test_conflicting_inputs_all_defer_and_preserve_prior_claims_while_independent_name_applies(
    transport, previous_name
):
    first = server()
    equal = server(component="workspace")
    differing = server(command="different", component="vm")
    independent = server("independent", component="session")
    path = transport.home / ".codex/config.toml"
    name = first.content.name if previous_name == "plain" else legacy_name(first)
    previous = (claim_for(first, name, path),)
    write(path, {"mcp_servers": {name: render_mcp(first, "codex")}})
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(
        invocation(transport, checkpoints, prior=record(previous, "codex")),
        artifacts=received(first, equal, differing, independent),
    )
    conflicts = mcp_input_conflicts(call.artifacts, "codex")
    assert set(conflicts) == {first.identity, equal.identity, differing.identity}
    result = apply_mcp("codex", call, destination=str(path))
    assert {entry.input_id for entry in result.deferred} == set(conflicts)
    assert previous[0] in checkpoints[-1]
    assert table(read(path), "mcp_servers") == {
        name: render_mcp(first, "codex"),
        independent.content.name: render_mcp(independent, "codex"),
    }


def test_conflicting_inputs_without_native_state_do_not_create_a_destination(transport):
    first, second = server(), server(command="different", component="workspace")
    path = transport.home / ".codex/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    result = apply_mcp(
        "codex", replace(invocation(transport, checkpoints), artifacts=received(first, second)), destination=str(path)
    )
    assert {entry.input_id for entry in result.deferred} == {first.identity, second.identity}
    assert not path.exists() and not checkpoints


@pytest.mark.parametrize("failure", ["foreign", "mapping", "read", "publish", "malformed", "location"])
def test_equal_input_deferrals_keep_every_original_identity(transport, monkeypatch, failure):
    first, second = server(), server(component="workspace")
    path = transport.home / ".codex/config.toml"
    previous: tuple[NativeClaim, ...] = ()
    mapping = None
    if failure in ("foreign", "mapping"):
        foreign = {"mcp_servers": {first.content.name: {"command": "operator"}}}
        write(path, foreign)
        if failure == "mapping":

            def mapping(content):
                return content, foreign
    elif failure in ("read", "publish"):
        monkeypatch.setattr(NativeFiles, failure, Mock(side_effect=ExternalError("private-native-value")))
    elif failure == "malformed":
        path.parent.mkdir()
        path.write_text("malformed = [\n")
    else:
        previous = (claim_for(second, legacy_name(second), transport.home / "other/config.toml"),)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    result = apply_mcp(
        "codex",
        replace(
            invocation(transport, checkpoints, prior=record(previous, "codex")),
            artifacts=received(first, second),
            report_incomplete=Mock(),
        ),
        destination=str(path),
        map_document=mapping,
    )
    assert {entry.input_id for entry in result.deferred} == {first.identity, second.identity}
    assert all("private-native-value" not in entry.reason for entry in result.deferred)
    assert not checkpoints


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_session_native_table_checks_use_the_declared_key_for_every_input(transport, tool):
    first, second = server(), server(component="workspace")
    workspace = transport.root / "project"
    workspace.mkdir()
    path = workspace / (".mcp.json" if tool == "claude" else ".codex/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    write(path, {key: {first.content.name: {"command": "operator"}}}, tool)
    conflicts = session_mcp_conflicts(
        tool,
        transport,
        received(first, second),
        home=str(transport.home),
        environment={},
        workspace=str(workspace),
    )
    assert set(conflicts) == {first.identity, second.identity}


@pytest.mark.parametrize("old_state", ["unchanged", "modified", "unclaimed"])
def test_foreign_literal_entry_does_not_authorize_retaining_an_obsolete_unchanged_alias(transport, old_state):
    item = server()
    path = transport.home / ".codex/config.toml"
    old_name = legacy_name(item)
    original = render_mcp(item, "codex")
    alias = {**original, "command": "operator-alias"} if old_state == "modified" else original
    foreign: SettingsObject = {"command": "operator-literal"}
    write(path, {"mcp_servers": {old_name: alias, item.content.name: foreign}})
    previous = () if old_state == "unclaimed" else (claim_for(item, old_name, path),)
    checkpoints: list[tuple[NativeClaim, ...]] = []
    reports: list[str] = []
    result = apply_mcp(
        "codex",
        replace(
            invocation(transport, checkpoints, prior=record(previous, "codex")),
            artifacts=received(item),
            report_incomplete=reports.append,
        ),
        destination=str(path),
    )
    assert [entry.input_id for entry in result.deferred] == [item.identity]
    entries = table(read(path), "mcp_servers")
    assert entries[item.content.name] == foreign
    assert (old_name in entries) is (old_state != "unchanged")
    if old_state == "unchanged":
        assert checkpoints == [()]
    else:
        assert not checkpoints and entries[old_name] == alias
    assert bool(reports) is (old_state == "modified")
