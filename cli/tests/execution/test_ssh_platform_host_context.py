"""Configured SSH composition under real host claims and synthetic delivery.

No installed SSH, native macOS or platform provisioning is exercised here.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from agentworks.capabilities.base import OperationScope as DescriptiveScope
from agentworks.capabilities.base import RunContext, ScopeLevel
from agentworks.capabilities.vm_platform.lima import LimaPlatform, LimaSshPlacement
from agentworks.capabilities.vm_platform.ssh_host import (
    SSHPlatformHostAccessConfig,
    ssh_platform_host_execution_context,
)
from agentworks.config import Config, DefaultsConfig, OperatorConfig, PathsConfig, SessionConfig
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.db.operations import OperationRepository
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._platform_host_context import PlatformHostContextControlFact
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHSettings
from agentworks.execution.files import Create, NewMetadata
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from tests.execution.test_target_identity import SyntheticCarrier

_KEY = "explicit shared host"
_SCOPE = OperationScope(OperationResourceKind.PLATFORM_HOST, _KEY)
_ROOT = PurePosixPath("/explicit/core/root")


def _access(**changes: object) -> SSHPlatformHostAccessConfig:
    return SSHPlatformHostAccessConfig.model_validate(
        dict(resource_key=_KEY, endpoint="literal.example", user="worker", target_os="linux") | changes
    )


def _settings(**changes: object) -> SSHSettings:
    return replace(SSHSettings(Path("/synthetic/trust"), Path("/synthetic/key")), **changes)


def _context(settings: SSHSettings | None, scope: DescriptiveScope | None = None) -> RunContext:
    return RunContext(
        operation_scope=scope,
        config=Config(
            operator=OperatorConfig(
                Path("/synthetic/legacy.pub"),
                Path("/synthetic/legacy"),
                ssh_config=Path("/synthetic/config"),
                ssh=settings,
            ),
            paths=PathsConfig(),
            defaults=DefaultsConfig(),
            source_path=Path("/synthetic/config.toml"),
            session=SessionConfig(),
        ),
    )


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    db = Database(tmp_path / "state.db")
    carrier = SyntheticCarrier(identities={"worker": IdentityExpectation(1001, 1002, (1002,))})
    deliveries = []

    def execute(ssh, invocation, *, io, deadline, custody):
        # SSH delivery is the external boundary. Preparation, claims and closeout
        # above it remain the real product implementation.
        claim = db.operations.inspect(_SCOPE)
        assert claim is not None
        deliveries.append((ssh._connection, invocation, deadline, claim.ownership))
        return carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

    def forbidden(*args, **kwargs):
        pytest.fail("composition probed local SSH files or client")

    monkeypatch.setattr(SSHCarrier, "execute", execute)
    monkeypatch.setattr("agentworks.execution.carriers.ssh.client.admit_connection", forbidden)
    monkeypatch.setattr("agentworks.execution.carriers.ssh.client.resolve_client_executable", forbidden)
    try:
        yield db, carrier, deliveries
    finally:
        db.close()


@pytest.mark.parametrize(
    "target_os,python_path", [("linux", None), ("darwin", None), ("darwin", "/opt/python/bin/python3")]
)
@pytest.mark.parametrize("agent_socket", [None, "/synthetic/agent.sock"])
def test_exact_policy_runtime_key_and_original_budget(fixture, target_os, python_path, agent_socket):
    db, carrier, deliveries = fixture
    settings = _settings(
        agent_socket=agent_socket, ssh_executable="/synthetic/client", keepalive_interval=7, keepalive_count_max=3
    )
    access = _access(endpoint="2001:db8::1", port=2207, target_os=target_os, python_path=python_path)
    deadline = Deadline.after(30)
    incoming = _context(settings)
    with ssh_platform_host_execution_context(db, incoming, access, trusted_root=_ROOT, deadline=deadline) as ctx:
        target = ctx.platform_host_execution_target()
        assert target is not None and target.route is ExecutionRoute.PLATFORM_HOST
        execution = target.execution()
        assert execution is not None
        assert target.identity.account == "worker"
        assert ctx.config is incoming.config
        assert carrier.calls == ["worker"]
        assert all(selected is deadline for selected in carrier.deadlines)
        for connection, invocation, selected, ownership in deliveries:
            assert selected is deadline and ownership.scope == _SCOPE
            assert connection.host == "2001:db8::1" and connection.user == "worker" and connection.port == 2207
            assert connection.identity_file == settings.identity_file
            assert connection.trust == ManagedSSHTrust(settings.trust_store)
            assert connection.host_key_alias is None and connection.agent_socket == agent_socket
            assert connection.ssh_executable == settings.ssh_executable
            assert (connection.keepalive_interval, connection.keepalive_count_max) == (7, 3)
            marker = invocation.argv.index("agentworks-runtime-prerequisite")
            assert invocation.argv[marker + 2] == target_os
            candidates = invocation.argv[marker + 6 :]
            assert candidates == (
                (python_path,)
                if python_path is not None
                else ("/usr/bin/python3",)
                if target_os == "linux"
                else ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
            )
        before = len(deliveries)
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), sudo=True, profile=Protection.DIRECT)
        assert len(deliveries) == before
    assert db.operations.inspect(_SCOPE) is None
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)


def test_shared_configured_key_conflicts_across_sites_and_other_key_is_independent(fixture, monkeypatch):
    db, _, deliveries = fixture
    access = _access()
    sites = [
        LimaPlatform(name, dict(name="lima", placement=dict(mode="ssh", host=host, access=access.model_dump())))
        for name, host in (("site-one", "legacy@one"), ("site-two", "legacy@two"))
    ]
    contexts = [_context(_settings(), DescriptiveScope(ScopeLevel.VM, vm=name)) for name in ("site-one", "site-two")]
    first, second = [site.config.placement for site in sites]
    assert isinstance(first, LimaSshPlacement) and isinstance(second, LimaSshPlacement)
    with ssh_platform_host_execution_context(
        db, contexts[0], first.access, trusted_root=_ROOT, deadline=Deadline.after(30)
    ):
        before = len(deliveries)
        with (
            pytest.raises(StateError),
            ssh_platform_host_execution_context(
                db, contexts[1], second.access, trusted_root=_ROOT, deadline=Deadline.after(30)
            ),
        ):
            pytest.fail("conflicting site entered")
        assert len(deliveries) == before
        # A different declaration is independent even for an identical endpoint.
        original = SSHCarrier.execute

        def independent(ssh, invocation, *, io, deadline, custody):
            assert db.operations.inspect(OperationScope(OperationResourceKind.PLATFORM_HOST, "other-key")) is not None
            return original(ssh, invocation, io=io, deadline=deadline, custody=custody)

        monkeypatch.setattr(SSHCarrier, "execute", independent)
        with ssh_platform_host_execution_context(
            db, contexts[1], _access(resource_key="other-key"), trusted_root=_ROOT, deadline=Deadline.after(30)
        ):
            assert db.operations.inspect(_SCOPE) is not None
        assert db.operations.inspect(OperationScope(OperationResourceKind.PLATFORM_HOST, "other-key")) is None
    assert db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("missing", ["access", "config", "policy"])
def test_missing_configuration_refuses_before_claim_or_delivery(fixture, missing):
    db, _, deliveries = fixture
    ctx = RunContext() if missing == "config" else _context(None if missing == "policy" else _settings())
    with (
        pytest.raises(ConfigError),
        ssh_platform_host_execution_context(
            db, ctx, None if missing == "access" else _access(), trusted_root=_ROOT, deadline=Deadline.after(30)
        ),
    ):
        pytest.fail("missing configuration entered")
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize(
    "change",
    [{"endpoint": "me@box"}, {"endpoint": "[::1]"}, {"endpoint": "bad host"}, {"user": "-root"}],
)
def test_malformed_literal_connection_refuses_before_claim_or_delivery(fixture, change):
    db, _, deliveries = fixture
    with (
        pytest.raises(ValidationError),
        ssh_platform_host_execution_context(
            db, _context(_settings()), _access(**change), trusted_root=_ROOT, deadline=Deadline.after(30)
        ),
    ):
        pytest.fail("invalid connection entered")
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize(
    "change",
    [
        {"identity_file": Path("relative")},
        {"trust_store": Path("/bad%trust")},
        {"agent_socket": "relative"},
        {"ssh_executable": "bad client"},
        {"keepalive_interval": -1},
        {"keepalive_count_max": True},
    ],
)
def test_malformed_operator_policy_refuses_before_claim_or_delivery(fixture, change):
    db, _, deliveries = fixture
    with (
        pytest.raises(ValidationError),
        ssh_platform_host_execution_context(
            db, _context(_settings(**change)), _access(), trusted_root=_ROOT, deadline=Deadline.after(30)
        ),
    ):
        pytest.fail("invalid policy entered")
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("deadline", [Deadline.after(None), Deadline.after(0)])
def test_nonfinite_or_expired_budget_refuses_before_claim_or_delivery(fixture, deadline):
    db, _, deliveries = fixture
    with (
        pytest.raises(ValidationError),
        ssh_platform_host_execution_context(
            db, _context(_settings()), _access(), trusted_root=_ROOT, deadline=deadline
        ),
    ):
        pytest.fail("invalid budget entered")
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


def test_runtime_missing_refuses_without_body_or_retained_claim(fixture):
    db, carrier, _ = fixture
    carrier.runtime_missing.add("worker")
    with (
        pytest.raises(StateError),
        ssh_platform_host_execution_context(
            db, _context(_settings()), _access(), trusted_root=_ROOT, deadline=Deadline.after(30)
        ),
    ):
        pytest.fail("missing runtime entered")
    assert carrier.calls == ["worker"] and db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("committed", [False, True])
def test_body_control_preserved_and_exact_release_retried_without_dispatch(fixture, monkeypatch, committed):
    db, _, deliveries = fixture
    release = OperationRepository.release_resolved
    releases = []
    body = KeyboardInterrupt()
    cause = ValueError("body cause")
    body.__cause__ = cause

    def interrupted(repository, ownership):
        releases.append(ownership)
        if len(releases) == 1:
            if committed:
                release(repository, ownership)
            raise RuntimeError("interrupted database response")
        release(repository, ownership)

    monkeypatch.setattr(OperationRepository, "release_resolved", interrupted)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        ssh_platform_host_execution_context(
            db, _context(_settings()), _access(), trusted_root=_ROOT, deadline=Deadline.after(30)
        ) as ctx,
    ):
        target = ctx.platform_host_execution_target()
        assert target is not None
        execution = target.execution()
        assert execution is not None
        raise body
    assert caught.value is body
    fact = body.__cause__
    assert isinstance(fact, PlatformHostContextControlFact) and fact.__cause__ is cause
    assert (db.operations.inspect(_SCOPE) is None) is committed
    before = len(deliveries)
    assert execution is not None
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    fact.retry_cleanup(Deadline.after(10))
    fact.retry_cleanup(Deadline.after(10))
    assert len(deliveries) == before and len(set(releases)) == 1
    assert db.operations.inspect(_SCOPE) is None


@pytest.mark.parametrize("root", [None, "/tmp", Path("/tmp"), PurePosixPath("relative"), PurePosixPath("//invalid")])
def test_invalid_core_root_refuses_before_claim_or_delivery(fixture, root):
    db, _, deliveries = fixture
    with (
        pytest.raises(ValidationError),
        ssh_platform_host_execution_context(
            db, _context(_settings()), _access(), trusted_root=root, deadline=Deadline.after(30)
        ),
    ):
        pytest.fail("invalid root entered")
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


def test_core_root_has_no_implicit_default(fixture):
    db, _, deliveries = fixture
    with pytest.raises(TypeError):
        ssh_platform_host_execution_context(  # type: ignore[call-arg]
            db, _context(_settings()), _access(), deadline=Deadline.after(30)
        )
    assert deliveries == [] and db.operations.inspect(_SCOPE) is None


@pytest.mark.skipif(sys.platform != "linux", reason="actual ordinary helpers require Linux")
def test_ordinary_local_helper_command_and_exact_core_root_file_transfer(tmp_path, monkeypatch):
    import grp
    import pwd

    from tests.execution.files._file_publication_support import install_fixture_bundle as publication_bundle
    from tests.execution.files._file_read_support import install_fixture_bundle as read_bundle
    from tests.execution.files._file_snapshot_support import install_fixture_bundle as snapshot_bundle
    from tests.execution.files._file_stage_support import install_fixture_bundle as stage_bundle
    from tests.execution.test_inline_helper import LocalCarrier

    account = pwd.getpwuid(os.geteuid()).pw_name
    root = tmp_path / "approved"
    root.mkdir(mode=0o700)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    stage_bundle(monkeypatch)
    publication_bundle(monkeypatch)
    read_bundle(monkeypatch)
    snapshot_bundle(monkeypatch, scratch)
    local = LocalCarrier()

    def execute(ssh, invocation, *, io, deadline, custody):
        assert db.operations.inspect(_SCOPE) is not None
        return local.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(SSHCarrier, "execute", execute)
    db = Database(tmp_path / "state.db")
    try:
        with ssh_platform_host_execution_context(
            db,
            _context(_settings()),
            _access(user=account, python_path=sys.executable),
            trusted_root=PurePosixPath(root),
            deadline=Deadline.after(30),
        ) as ctx:
            target = ctx.platform_host_execution_target()
            assert target is not None and target.identity.uid == os.geteuid()
            execution, files = target.execution(), target.files()
            assert execution is not None and files is not None
            result = execution.run(Command(["/bin/echo", "ordinary"]), profile=Protection.DIRECT)
            assert result.ok and result.stdout.data == b"ordinary\n"
            path = PurePosixPath(root / "data")
            files.write_file(
                path,
                b"host-content",
                condition=Create(),
                create_metadata=NewMetadata(account, grp.getgrgid(os.getegid()).gr_name, 0o600),
            )
            read = files.read_file(path, max_bytes=64)
            assert read is not None and read.data == b"host-content"
            before = local.calls
            with pytest.raises(ValidationError):
                files.read_file(PurePosixPath(root.parent / "outside"), max_bytes=64)
            assert local.calls == before
        assert db.operations.inspect(_SCOPE) is None and list(scratch.iterdir()) == []
        assert local.local_delivery.settled
        before = local.calls
        with pytest.raises(StateError):
            files.read_file(path, max_bytes=64)
        assert local.calls == before
    finally:
        assert local.local_delivery.close(Deadline.after(3))
        db.close()
