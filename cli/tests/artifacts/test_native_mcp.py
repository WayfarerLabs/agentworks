"""Native MCP entries preserve foreign configuration and confirmed ownership."""

from __future__ import annotations

import grp
import json
import os
import stat
import sys
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentworks.artifacts.mcp import content_from_definition, parse_definition
from agentworks.artifacts.model import ArtifactComponent, ArtifactInput, ArtifactOrigin, ArtifactProvenance
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.mcp import MCP_CLAIM_ROLE, MCPTool, apply_mcp, render_mcp
from agentworks.capabilities.harness_integration.settings import (
    SettingsMapping,
    SettingsObject,
    parse_settings,
    serialize_settings,
)
from agentworks.capabilities.harness_integration.setup import WorkspaceSetupInvocation
from agentworks.errors import ExternalError, StateConflictError, StateError, UserAbort
from agentworks.execution.errors import PathRefusal, PathRefused
from agentworks.execution.files import Files
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native import setup_workspace
from agentworks.plugins._harness_native.native_cli import NativeMarket, NativePlugin, identity
from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig
from tests.artifacts._fixtures import received
from tests.native_setup_fixtures import LocalFixtureTransport, invocation, native_cli_double, record, setup_fixture_user

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest file helpers")


def server(
    name: str = "inventory",
    command: str = "server",
    *,
    definition: object | None = None,
    component: ArtifactComponent = "agent",
) -> ArtifactInput:
    definition = definition or {"type": "stdio", "command": command}
    return ArtifactInput(
        content=content_from_definition(name, parse_definition(definition)),
        origin=ArtifactOrigin(component, component, "fixture", entry=name),
        provenance=ArtifactProvenance(),
    )


@pytest.fixture
def transport(tmp_path: Path) -> LocalFixtureTransport:
    return LocalFixtureTransport(tmp_path)


def read(path: Path, tool: MCPTool = "codex") -> SettingsObject:
    return parse_settings(path.read_bytes(), format="json" if tool == "claude" else "toml")


def table(document: SettingsObject, name: str) -> SettingsObject:
    value = document[name]
    assert isinstance(value, dict)
    return value


def write(path: Path, document: SettingsObject, tool: MCPTool = "codex") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_settings(document, format="json" if tool == "claude" else "toml"))


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_static_render_preserves_reference_semantics(tool, monkeypatch):
    monkeypatch.setenv("TOKEN", "private-value-never-read")
    stdio = server(
        definition={
            "type": "stdio",
            "command": "inventory",
            "args": ["a path"],
            "env": {"TOKEN": "${TOKEN}", "PUBLIC": "ordinary"},
        }
    )
    http = server(
        definition={
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${TOKEN}", "X-Other": "${OTHER}", "X-Public": "ordinary"},
        }
    )
    native_stdio, native_http = render_mcp(stdio, tool), render_mcp(http, tool)
    if tool == "codex":
        assert native_stdio == {
            "command": "inventory",
            "args": ["a path"],
            "env": {"PUBLIC": "ordinary"},
            "env_vars": ["TOKEN"],
        }
        assert native_http == {
            "url": "https://example.test/mcp",
            "bearer_token_env_var": "TOKEN",
            "env_http_headers": {"X-Other": "OTHER"},
            "http_headers": {"X-Public": "ordinary"},
        }
    else:
        assert native_stdio["env"] == {"TOKEN": "${TOKEN}", "PUBLIC": "ordinary"}
        assert native_http["headers"] == http.content.metadata["headers"]
    assert "private-value-never-read" not in json.dumps([native_stdio, native_http])


@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
def test_create_adopt_update_and_entry_conflict_are_independent(transport, tool):
    path = transport.home / (".claude.json" if tool == "claude" else f".{tool}/config.toml")
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    first, second = server(), server("other")
    write(path, {"foreign": {"private": "fixture-secret-value"}}, tool)
    claims: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, claims), artifacts=received(first, second))
    result = apply_mcp(tool, call, destination=str(path))
    assert not result.files and not result.shared_paths and not result.deferred
    assert {c.source for c in claims[-1]} == {first.origin_identity, second.origin_identity}
    assert all(
        c.role == MCP_CLAIM_ROLE and c.destination == str(path) and c.sha256 is not None and len(c.sha256) == 64
        for c in claims[-1]
    )
    baseline = path.read_bytes()
    adopted: list[tuple[NativeClaim, ...]] = []
    apply_mcp(tool, replace(call, checkpoint=adopted.append), destination=str(path))
    assert adopted[-1] == claims[-1]
    assert path.read_bytes() == baseline
    document = read(path, tool)
    table(table(document, key), mcp_native_name(first))["command"] = "operator-changed"
    write(path, document, tool)
    updated_first, updated_second = server(command="new"), server("other", command="new")
    updated = replace(call, prior=record(claims[-1], "codex"), artifacts=received(updated_first, updated_second))
    result = apply_mcp(tool, updated, destination=str(path))
    assert [d.input_id for d in result.deferred] == [updated_first.identity]
    assert table(table(read(path, tool), key), mcp_native_name(first))["command"] == "operator-changed"
    assert table(table(read(path, tool), key), mcp_native_name(second))["command"] == "new"
    assert read(path, tool)["foreign"] == {"private": "fixture-secret-value"}
    assert "fixture-secret-value" not in "".join(transport.logged_output)


@pytest.mark.parametrize("modified,unavailable", [(False, False), (True, False), (False, True)])
def test_retirement_retains_modified_and_unavailable_ancestor_entries(transport, modified, unavailable):
    path = transport.home / ".grok/config.toml"
    item = server()
    claims: list[tuple[NativeClaim, ...]] = []
    reasons: list[str] = []
    call = replace(invocation(transport, claims), artifacts=received(item), report_incomplete=reasons.append)
    apply_mcp("grok", call, destination=str(path))
    prior = record(claims[-1], "codex")
    if modified:
        document = read(path)
        table(table(document, "mcp_servers"), mcp_native_name(item))["command"] = "operator"
        write(path, document)
    retire = replace(
        call,
        artifacts=received(),
        prior=prior,
        retain_artifact_origins=frozenset({item.origin_identity}) if unavailable else frozenset(),
    )
    apply_mcp("grok", retire, destination=str(path))
    present = mcp_native_name(item) in table(read(path), "mcp_servers")
    assert present is (modified or unavailable)
    assert bool(reasons) is modified
    assert bool(claims[-1]) is (modified or unavailable)


@pytest.mark.parametrize("case", ["new-user", "existing", "new-workspace"])
def test_mcp_publication_modes_follow_the_owner(transport, case):
    """A new file is private to a user or shared with a workspace group; an existing file keeps its mode."""
    group = grp.getgrgid(os.getgid()).gr_name
    root = transport.home if case != "new-workspace" else transport.root / "project"
    path = root / ".codex/config.toml"
    if case == "existing":
        write(path, {"foreign": True})
        path.chmod(0o644)
    item = server(component="workspace" if case == "new-workspace" else "agent")
    if case == "new-workspace":
        root.mkdir()
        call = WorkspaceSetupInvocation(
            vm=object(),
            access=(access := transport.access()),
            files=access.files(),
            prior=None,
            checkpoint=Mock(),
            workspace_name="fixture",
            root=str(root),
            linux_group=group,
            artifacts=received(item),
        )
    else:
        call = replace(invocation(transport, []), artifacts=received(item))
    apply_mcp("codex", call, destination=str(path))
    assert mcp_native_name(item) in table(read(path), "mcp_servers")
    file_mode, parent_mode = stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(path.parent.stat().st_mode)
    if case == "new-user":
        assert (file_mode, parent_mode) == (0o600, 0o700)
    elif case == "existing":
        assert file_mode == 0o644
    else:
        assert (file_mode, parent_mode) == (0o660, 0o2770)
        assert path.stat().st_gid == path.parent.stat().st_gid == grp.getgrnam(group).gr_gid


def test_workspace_shared_paths_include_retained_claims_without_file_ownership(transport):
    item = server(component="workspace")
    root = transport.root / "project"
    root.mkdir()
    claims: list[tuple[NativeClaim, ...]] = []
    call = WorkspaceSetupInvocation(
        vm=object(),
        access=(access := transport.access()),
        files=access.files(),
        prior=None,
        checkpoint=claims.append,
        workspace_name="fixture",
        root=str(root),
        linux_group="",
        artifacts=received(item),
    )
    result = setup_workspace("codex", NativeWorkspaceConfig(), call)
    path = root / ".codex/config.toml"
    assert result.shared_paths == (str(path),)
    assert result.files == ()
    document = read(path)
    table(table(document, "mcp_servers"), mcp_native_name(item))["command"] = "operator"
    write(path, document)
    retained = setup_workspace(
        "codex", None, replace(call, prior=record(claims[-1], "codex"), report_incomplete=Mock())
    )
    assert retained.shared_paths == (str(path),)


def test_checkpoint_failure_and_expected_hash_conflict_escape(transport, monkeypatch):
    item = server()
    path = transport.home / ".codex/config.toml"
    call = replace(
        invocation(transport, []),
        artifacts=received(item),
        checkpoint=Mock(side_effect=ExternalError("fixture persistence failure")),
    )
    with pytest.raises(ExternalError):
        apply_mcp("codex", call, destination=str(path))
    assert mcp_native_name(item) in table(read(path), "mcp_servers")
    path.unlink()
    update = Files.update

    def competing_write(files: Files, destination: str, content: bytes, **kwargs: object) -> bool:
        Path(destination).write_text('owner = "concurrent"\n')
        return update(files, destination, content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Files, "update", competing_write)
    checkpoint = Mock()
    with pytest.raises(StateError):
        apply_mcp("codex", replace(call, checkpoint=checkpoint), destination=str(path))
    checkpoint.assert_not_called()
    assert read(path) == {"owner": "concurrent"}


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
@pytest.mark.parametrize("missing", [False, True])
def test_joint_codex_mapping_unmasks_withdrawn_native_key_in_one_pass(transport, strategy, missing):
    item = server()
    name = mcp_native_name(item)
    path = transport.home / ".codex/config.toml"
    claims: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    setup_fixture_user("codex", NativeUserConfig(), call)
    if missing:
        path.unlink()
    source = transport.root / "mapping.toml"
    write(source, {"mcp_servers": {name: {"command": "source-owned", "args": []}}})
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy))
    result = setup_fixture_user("codex", config, replace(call, artifacts=received(), prior=record(claims[-1], "codex")))
    document = read(path)
    if strategy == "skip-existing" and not missing:
        assert name not in table(document, "mcp_servers")
    else:
        assert table(table(document, "mcp_servers"), name)["command"] == "source-owned"
    assert claims[-1] == () and result.deferred == ()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
@pytest.mark.parametrize("existing", [False, True])
def test_joint_codex_mapping_desired_key_obeys_actual_policy(transport, strategy, existing):
    item = server()
    name = mcp_native_name(item)
    path = transport.home / ".codex/config.toml"
    claims: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    if existing:
        setup_fixture_user("codex", NativeUserConfig(), call)
        call = replace(call, prior=record(claims[-1], "codex"))
    source = transport.root / "mapping.toml"
    write(source, {"mcp_servers": {name: {"command": "source-owned", "args": []}}})
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy))
    result = setup_fixture_user("codex", config, call)
    mapping_wins = not existing or strategy in ("replace", "merge-overwrite")
    assert bool(result.deferred) is mapping_wins
    assert table(table(read(path), "mcp_servers"), name)["command"] == ("source-owned" if mapping_wins else "server")


def test_claude_user_role_and_config_override_are_independent_of_settings(transport):
    claims: list[tuple[NativeClaim, ...]] = []
    item = server()
    custom = transport.home / "custom-claude"
    call = replace(invocation(transport, claims, env={"CLAUDE_CONFIG_DIR": str(custom)}), artifacts=received(item))
    setup_fixture_user("claude", NativeUserConfig(), call)
    assert (custom / ".claude.json").exists()
    assert not (custom / "settings.json").exists()
    assert claims[-1][0].destination == str(custom / ".claude.json")


def test_mapping_only_skip_existing_preserves_bytes_and_replace_repairs_invalid_toml(transport):
    path = transport.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text("# preserve this comment\nmodel = 'existing'\n")
    source = transport.root / "mapping.toml"
    source.write_text('model = "mapped"\n')
    call = invocation(transport, [])
    baseline = path.read_bytes()
    setup_fixture_user(
        "codex", NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="skip-existing")), call
    )
    assert path.read_bytes() == baseline
    path.write_text("malformed = [\n")
    setup_fixture_user(
        "codex", NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="replace")), call
    )
    assert read(path) == {"model": "mapped"}


def test_plugin_progress_is_preserved_in_later_mcp_checkpoint(transport, monkeypatch):
    path = transport.home / ".codex/config.toml"
    write(path, {"foreign": True})
    market = NativeMarket("fixture", identity({"source_type": "local", "source": "/fixture-market"}))
    installed: list[NativePlugin] = []
    cli = native_cli_double()
    cli.markets.return_value = (market,)
    cli.plugins.side_effect = lambda _markets: tuple(installed)
    cli.available.return_value = ("one@fixture",)

    def install(selector: str) -> None:
        installed.append(NativePlugin(selector, "user", True, identity({"marketplace": market.source})))
        document = read(path)
        document["plugins"] = {selector: {"enabled": True}}
        write(path, document)

    cli.install.side_effect = install
    monkeypatch.setattr("agentworks.plugins._harness_native.native.NativeCLI", Mock(return_value=cli))
    claims: list[tuple[NativeClaim, ...]] = []
    item = server()
    setup_fixture_user(
        "codex",
        NativeUserConfig(plugins=["one@fixture"]),
        replace(invocation(transport, claims), artifacts=received(item)),
    )
    assert [c.role for c in claims[0]] == ["plugin"]
    assert {c.role for c in claims[-1]} == {"plugin", MCP_CLAIM_ROLE}
    assert all(any(c.role == "plugin" for c in prefix) for prefix in claims)
    assert table(table(read(path), "plugins"), "one@fixture")["enabled"] is True
    assert read(path)["foreign"] is True


@pytest.mark.parametrize("fault", ["malformed", "read", "publish"])
def test_native_configuration_failures_defer_without_claiming_or_disclosing(transport, monkeypatch, fault):
    path = transport.home / ".codex/config.toml"
    write(path, {"foreign": "private-fixture-only"})
    if fault == "malformed":
        path.write_text("private-fixture-only = [\n")
    elif fault == "read":
        monkeypatch.setattr(Files, "read", Mock(side_effect=ExternalError("private-fixture-only")))
    else:
        monkeypatch.setattr(Files, "update", Mock(side_effect=ExternalError("private-fixture-only")))
    baseline = path.read_bytes()
    claims: list[tuple[NativeClaim, ...]] = []
    item = server()
    result = apply_mcp("codex", replace(invocation(transport, claims), artifacts=received(item)), destination=str(path))
    assert [d.input_id for d in result.deferred] == [item.identity]
    assert "private-fixture-only" not in result.deferred[0].reason
    assert claims == [] and path.read_bytes() == baseline


def test_skip_existing_still_installs_and_retires_mcp_entries(transport):
    path = transport.home / ".codex/config.toml"
    write(path, {"foreign": True})
    source = transport.root / "mapping.toml"
    source.write_text('source = "ignored"\n')
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="skip-existing"))
    claims: list[tuple[NativeClaim, ...]] = []
    item = server()
    call = replace(invocation(transport, claims), artifacts=received(item))
    setup_fixture_user("codex", config, call)
    assert read(path)["foreign"] is True and "source" not in read(path)
    assert mcp_native_name(item) in table(read(path), "mcp_servers")
    setup_fixture_user("codex", config, replace(call, artifacts=received(), prior=record(claims[-1], "codex")))
    assert claims[-1] == ()
    assert read(path) == {"foreign": True, "mcp_servers": {}}


def test_shared_document_is_observed_and_published_once(transport, monkeypatch):
    path = transport.home / ".codex/config.toml"
    source = transport.root / "mapping.toml"
    source.write_text("foreign = true\n")
    observed: list[str] = []
    published: list[str] = []
    native_read, native_update = Files.read, Files.update

    def reading(files: Files, destination: str, **kwargs: object) -> bytes | None:
        observed.append(destination)
        return native_read(files, destination, **kwargs)  # type: ignore[arg-type]

    def publishing(files: Files, destination: str, content: bytes, **kwargs: object) -> bool:
        wrote = native_update(files, destination, content, **kwargs)  # type: ignore[arg-type]
        if wrote:
            published.append(destination)
        return wrote

    monkeypatch.setattr(Files, "read", reading)
    monkeypatch.setattr(Files, "update", publishing)
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="merge-overwrite"))
    setup_fixture_user("codex", config, replace(invocation(transport, []), artifacts=received(server())))
    assert observed == published == [str(path)]


def test_explicit_replacement_can_repair_malformed_config_with_mcp_inputs(transport):
    path = transport.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text("malformed = [\n")
    source = transport.root / "mapping.toml"
    source.write_text('model = "mapped"\n')
    claims: list[tuple[NativeClaim, ...]] = []
    item = server()
    result = setup_fixture_user(
        "codex",
        NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="replace")),
        replace(invocation(transport, claims), artifacts=received(item)),
    )
    assert result.deferred == ()
    assert read(path)["model"] == "mapped"
    assert table(table(read(path), "mcp_servers"), mcp_native_name(item))["command"] == "server"
    assert claims[-1][0].source == item.origin_identity


@pytest.mark.parametrize("value", [date(2026, 11, 11), time(12, 34, 56), datetime(2026, 11, 11, 12, 34, 56)])
@pytest.mark.parametrize("missing_hash", [False, True])
@pytest.mark.parametrize("operation", ["update", "withdraw"])
def test_native_toml_typed_edits_never_match_prior_string_fingerprints(transport, value, missing_hash, operation):
    path = transport.home / ".codex/config.toml"
    item = server(command=str(value))
    name = mcp_native_name(item)
    claims: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    apply_mcp("codex", call, destination=str(path))
    previous = tuple(claim.model_copy(update={"sha256": None}) if missing_hash else claim for claim in claims[-1])
    document = read(path)
    table(table(document, "mcp_servers"), name)["command"] = value
    write(path, document)
    baseline = path.read_bytes()
    claims.clear()
    reasons: list[str] = []
    updated = server(command="updated")
    result = apply_mcp(
        "codex",
        replace(
            call,
            prior=record(previous, "codex"),
            artifacts=received(updated) if operation == "update" else received(),
            report_incomplete=reasons.append,
        ),
        destination=str(path),
    )
    assert path.read_bytes() == baseline and claims == []
    assert table(table(read(path), "mcp_servers"), name)["command"] == value
    if operation == "update":
        assert [entry.input_id for entry in result.deferred] == [updated.identity]
    else:
        assert not result.deferred and len(reasons) == 1 and name in reasons[0]


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
@pytest.mark.parametrize("source_requests_key", [False, True])
def test_explicit_mapping_takeover_releases_modified_obsolete_claim_in_one_pass(
    transport, monkeypatch, strategy, source_requests_key
):
    path = transport.home / ".codex/config.toml"
    item = server()
    name = mcp_native_name(item)
    claims: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, claims), artifacts=received(item))
    setup_fixture_user("codex", NativeUserConfig(), call)
    previous = record(claims[-1], "codex")
    document = read(path)
    table(table(document, "mcp_servers"), name)["command"] = "operator-modified"
    write(path, document)
    source = transport.root / "mapping.toml"
    write(
        source,
        {"mcp_servers": {name: {"command": "source-owned", "args": []}}}
        if source_requests_key
        else {"model": "mapped"},
    )
    config = NativeUserConfig(settings=SettingsMapping(source=str(source), strategy=strategy))
    reasons: list[str] = []
    reports: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.native.mcp.output.info", reports.append)
    retire = replace(call, artifacts=received(), prior=previous, report_incomplete=reasons.append)
    takeover = strategy == "replace" or (source_requests_key and strategy == "merge-overwrite")
    for attempt in range(2):
        claims.clear()
        reasons.clear()
        reports.clear()
        result = setup_fixture_user("codex", config, retire)
        assert not result.deferred
        assert bool(reasons) is not takeover
        current = claims[-1] if claims else retire.prior.claims
        assert (current == ()) is takeover
        entries = read(path).get("mcp_servers", {})
        assert isinstance(entries, dict)
        if source_requests_key:
            assert table(entries, name)["command"] == ("source-owned" if takeover else "operator-modified")
        elif takeover:
            assert name not in entries
        else:
            assert table(entries, name)["command"] == "operator-modified"
        assert len(reports) == int(takeover and attempt == 0)
        assert all(name in report for report in reports)
        retire = replace(retire, prior=record(current, "codex"))


@pytest.mark.parametrize("operation", ["read", "update"])
@pytest.mark.parametrize(
    "error",
    [
        PathRefused("box", "read", "/guarded", PathRefusal.UNSUITABLE),
        StateError("internal"),
        StateConflictError("concurrent"),
        UserAbort("cancelled"),
        RuntimeError("bug"),
        KeyboardInterrupt(),
    ],
)
def test_optional_mcp_catches_only_typed_guarded_path_refusals(transport, monkeypatch, operation, error):
    path = transport.home / ".codex/config.toml"
    checkpoints: list[tuple[NativeClaim, ...]] = []
    call = replace(invocation(transport, checkpoints), artifacts=received(server()))
    apply_mcp("codex", call, destination=str(path))
    previous = record(checkpoints[-1], "codex")
    before = path.read_bytes()
    checkpoints.clear()
    reports: list[str] = []
    changed = server(command="changed")
    call = replace(call, prior=previous, artifacts=received(changed), report_incomplete=reports.append)
    with monkeypatch.context() as patch:
        patch.setattr(Files, operation, Mock(side_effect=error))
        if isinstance(error, PathRefused):
            result = apply_mcp("codex", call, destination=str(path))
            assert reports and {entry.input_id for entry in result.deferred} == {changed.identity}
        else:
            with pytest.raises(type(error)):
                apply_mcp("codex", call, destination=str(path))
    assert not checkpoints and path.read_bytes() == before


def test_required_mapping_without_mcp_still_propagates_guarded_path_refusals(transport):
    path = transport.home / ".codex/config.toml"
    path.mkdir(parents=True)
    source = transport.root / "mapping.toml"
    source.write_text('theme = "mapped"\n')
    with pytest.raises(PathRefused):
        setup_fixture_user(
            "codex",
            NativeUserConfig(settings=SettingsMapping(source=str(source), strategy="replace")),
            invocation(transport, []),
        )
