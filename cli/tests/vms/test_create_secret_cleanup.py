"""Operation-wide secret cleanup for the production ``vm create`` chain."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import ProvisionResult
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.config import load_config
from agentworks.errors import ExternalError, ProvisioningError
from agentworks.secrets.policy import InteractionPolicy
from agentworks.ssh import SSHError, SSHLogger
from agentworks.vms import manager as vm_manager
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import ProvisionRequest
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.templates import ResolvedVMTemplate


_SENTINEL = "operation-secret-cleanup-sentinel"


def _assert_secret_absent_from_agentworks_exception_graph(exc: BaseException, secret: str) -> None:
    """Inspect every Agentworks frame across causes, contexts, and groups."""
    pending = [exc]
    seen: set[int] = set()
    found_modules: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in repr(current)
        traceback = current.__traceback__
        while traceback is not None:
            module = str(traceback.tb_frame.f_globals.get("__name__", ""))
            if module.startswith("agentworks."):
                found_modules.add(module)
                assert secret not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    assert "agentworks.vms.manager.lifecycle" in found_modules


def _assert_remote_join_frames(exc: BaseException) -> None:
    expected = {
        ("agentworks.vms.manager.lifecycle", "create_vm"),
        ("agentworks.vms.manager.lifecycle", "_create_vm"),
        ("agentworks.capabilities.vm_platform.lima", "create"),
        ("agentworks.capabilities.vm_platform.lima", "_create"),
        ("agentworks.capabilities.vm_platform.lima", "_join_tailscale_ephemerally"),
        ("agentworks.capabilities.vm_platform.lima", "_run_lima"),
        ("agentworks.ssh", "run"),
    }
    found: set[tuple[str, str]] = set()
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            found.add(
                (
                    str(traceback.tb_frame.f_globals.get("__name__", "")),
                    traceback.tb_frame.f_code.co_name,
                )
            )
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    assert expected <= found


@pytest.fixture
def create_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public ssh key")
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", _SENTINEL)
    monkeypatch.setenv("AW_SECRET_GIT_TOKEN_GH", _SENTINEL)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *args, **kwargs: None)

    def _make(*, manifests: Sequence[ManifestDoc | str] = ()):
        path = tmp_path / "config.toml"
        path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n')
        if manifests:
            write_manifests(tmp_path, *manifests)
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


def _config_with_git_secret(create_config: Callable[..., Config]) -> Config:
    return create_config(
        manifests=(
            ManifestDoc("git-credential", "gh", {"provider": {"name": "github"}}),
            ManifestDoc("admin-template", "default", {"git_credentials": ["gh"]}),
        )
    )


def _capture_secret_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Resolver], list[ScopedSecrets], list[ProvisionRequest]]:
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.secrets.resolver import Resolver

    resolvers: list[Resolver] = []
    readers: list[ScopedSecrets] = []
    requests: list[ProvisionRequest] = []
    original_resolver_init = Resolver.__init__
    original_reader_init = ScopedSecrets.__init__

    def _resolver_init(self: Resolver, *args: object, **kwargs: object) -> None:
        original_resolver_init(self, *args, **kwargs)  # type: ignore[arg-type]
        resolvers.append(self)

    def _reader_init(self: ScopedSecrets, *args: object, **kwargs: object) -> None:
        original_reader_init(self, *args, **kwargs)  # type: ignore[arg-type]
        readers.append(self)

    monkeypatch.setattr(Resolver, "__init__", _resolver_init)
    monkeypatch.setattr(ScopedSecrets, "__init__", _reader_init)

    return resolvers, readers, requests


def _assert_owners_scrubbed(
    resolvers: list[Resolver],
    readers: list[ScopedSecrets],
    requests: list[ProvisionRequest],
) -> None:
    assert resolvers and readers and requests
    assert all(not resolver.resolved for resolver in resolvers)
    assert all(reader._values == {} for reader in readers)  # noqa: SLF001
    assert all(request.tailscale_auth_key is None for request in requests)


@pytest.mark.parametrize(
    "failure",
    [
        SSHError("ssh create failed"),
        KeyboardInterrupt("operator interrupt"),
        SystemExit(17),
        GeneratorExit(),
    ],
    ids=("ssh-error", "keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_platform_failure_scrubs_all_create_secret_owners_and_frames(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    resolvers, readers, requests = _capture_secret_owners(monkeypatch)

    def _fail_create(
        self: LimaPlatform,
        request: ProvisionRequest,
        ctx: RunContext,
    ) -> ProvisionResult:
        del self, ctx
        requests.append(request)
        raise failure

    monkeypatch.setattr(LimaPlatform, "create", _fail_create)

    expected_type = ProvisioningError if isinstance(failure, Exception) else type(failure)
    with pytest.raises(expected_type) as caught:
        vm_manager.create_vm(
            db,
            _config_with_git_secret(create_config),
            name="secret-failure",
            interaction=InteractionPolicy.REFUSE,
        )

    if not isinstance(failure, Exception):
        assert caught.value is failure
    assert db.get_vm("secret-failure") is None
    _assert_owners_scrubbed(resolvers, readers, requests)
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)


@pytest.mark.parametrize(
    "control_flow",
    [None, KeyboardInterrupt("operator interrupt"), SystemExit(19), GeneratorExit()],
    ids=("ssh-error", "keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_remote_lima_join_failure_scrubs_full_production_call_chain(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    control_flow: BaseException | None,
) -> None:
    from agentworks import remote_exec
    from agentworks.remote_exec import DetachedResult

    resolvers, readers, requests = _capture_secret_owners(monkeypatch)
    cleanup_events: list[str] = []
    remote_site = ManifestDoc(
        "vm-site",
        "remote-lima",
        {
            "platform": {
                "name": "lima",
                "placement": {"mode": "ssh", "host": "user@host"},
            }
        },
    )

    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    original_create = LimaPlatform.create

    def _recording_create(
        self: LimaPlatform,
        request: ProvisionRequest,
        ctx: RunContext,
    ) -> ProvisionResult:
        requests.append(request)
        return original_create(self, request, ctx)

    monkeypatch.setattr(LimaPlatform, "create", _recording_create)

    def _detached(*args: object, **kwargs: object) -> DetachedResult:
        del args, kwargs
        cleanup_events.append("create:remote-chain")
        return DetachedResult(exit_code=0, output="created")

    monkeypatch.setattr(remote_exec, "run_detached", _detached)
    monkeypatch.setattr(
        LimaPlatform,
        "_cleanup_partial_create",
        lambda self, name: cleanup_events.append(f"cleanup:{name}"),
    )

    def _subprocess_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        input_text = kwargs.get("input")
        if "mktemp -d" in repr(args):
            return subprocess.CompletedProcess(args, 0, stdout="/tmp/agentworks-lima-template.A1b2C3d4E5\n", stderr="")
        if input_text == f"{_SENTINEL}\n":
            if control_flow is not None:
                raise control_flow
            return subprocess.CompletedProcess(
                args,
                1,
                stdout=f"reflected {_SENTINEL}",
                stderr=f"rejected {_SENTINEL}",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("agentworks.ssh.subprocess.run", _subprocess_run)

    expected_type = ProvisioningError if control_flow is None else type(control_flow)
    with pytest.raises(expected_type) as caught:
        vm_manager.create_vm(
            db,
            create_config(manifests=(remote_site,)),
            name="remote-chain",
            site="remote-lima",
            interaction=InteractionPolicy.REFUSE,
        )

    if control_flow is not None:
        assert caught.value is control_flow
    assert cleanup_events == ["create:remote-chain", "cleanup:remote-chain"]
    assert db.get_vm("remote-chain") is None
    _assert_owners_scrubbed(resolvers, readers, requests)
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)
    _assert_remote_join_frames(caught.value)


@contextlib.contextmanager
def _noop_hold(self: object, vm: object, *, config: object | None = None):
    del self, vm, config
    yield


@pytest.mark.parametrize("phase_b_failure", [None, SSHError("phase b failed")], ids=("success", "phase-b"))
def test_post_platform_create_paths_scrub_request_contexts_tokens_cache_and_logger(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    phase_b_failure: SSHError | None,
) -> None:
    resolvers, readers, requests = _capture_secret_owners(monkeypatch)
    loggers: list[SSHLogger] = []

    def _create(
        self: LimaPlatform,
        request: ProvisionRequest,
        ctx: RunContext,
    ) -> ProvisionResult:
        del self, ctx
        requests.append(request)
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": "secret-success"},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.9",
        )

    def _bootstrap(
        db_: Database,
        config_: object,
        vm_template: ResolvedVMTemplate,
        vm_name: str,
        *args: object,
        tailscale_ctx: RunContext,
        git_tokens: dict[str, str],
        on_logger_ready: Callable[[SSHLogger], None],
        **kwargs: object,
    ) -> tuple[object, SSHLogger, str]:
        del db_, config_, args, kwargs
        secret_name = vm_template.tailscale_auth_key
        logger = SSHLogger(
            vm_name,
            "vm-create",
            redactions=(tailscale_ctx.secret(secret_name), *git_tokens.values()),
        )
        loggers.append(logger)
        on_logger_ready(logger)
        return SimpleNamespace(), logger, "/home/agentworks"

    def _initialize(*args: object, **kwargs: object) -> None:
        del args, kwargs
        if phase_b_failure is not None:
            raise phase_b_failure

    monkeypatch.setattr(LimaPlatform, "create", _create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _bootstrap)
    monkeypatch.setattr(vm_manager, "run_initialization", _initialize)

    if phase_b_failure is None:
        vm_manager.create_vm(
            db,
            _config_with_git_secret(create_config),
            name="secret-success",
            interaction=InteractionPolicy.REFUSE,
        )
    else:
        with pytest.raises(ExternalError) as caught:
            vm_manager.create_vm(
                db,
                _config_with_git_secret(create_config),
                name="secret-success",
                interaction=InteractionPolicy.REFUSE,
            )
        _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)

    assert db.get_vm("secret-success") is not None
    _assert_owners_scrubbed(resolvers, readers, requests)
    assert loggers and all(logger._redact == () for logger in loggers)  # noqa: SLF001


@pytest.mark.parametrize("control_flow", [SystemExit(23), GeneratorExit()], ids=("system-exit", "generator-exit"))
def test_nonordinary_lima_failure_cleans_backend_before_exact_pending_row(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    control_flow: BaseException,
) -> None:
    from agentworks.db import Database as DatabaseType

    events: list[str] = []
    original_delete_vm = DatabaseType.delete_vm
    monkeypatch.setattr(LimaPlatform, "_ensure_limactl", lambda self: None)
    monkeypatch.setattr(LimaPlatform, "_instance_exists", lambda self, name: False)
    monkeypatch.setattr(
        LimaPlatform,
        "_create_local",
        lambda self, name, yaml: events.append(f"backend-create:{name}"),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_join_tailscale_ephemerally",
        lambda self, name, key: (_ for _ in ()).throw(control_flow),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "_cleanup_partial_create",
        lambda self, name: events.append(f"backend-cleanup:{name}"),
    )

    def _delete_vm(self: DatabaseType, name: str) -> None:
        events.append(f"row-cleanup:{name}")
        original_delete_vm(self, name)

    monkeypatch.setattr(DatabaseType, "delete_vm", _delete_vm)

    with pytest.raises(type(control_flow)) as caught:
        vm_manager.create_vm(
            db,
            create_config(),
            name="ordered-cleanup",
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is control_flow
    assert events == [
        "backend-create:ordered-cleanup",
        "backend-cleanup:ordered-cleanup",
        "row-cleanup:ordered-cleanup",
    ]
    assert db.get_vm("ordered-cleanup") is None
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)


@pytest.mark.parametrize("stage", ["constructor", "append", "copy"], ids=("constructor", "append", "copy"))
def test_scoped_secret_registration_interruptions_never_strand_copied_values(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.vms.manager import lifecycle

    resolvers, readers, requests = _capture_secret_owners(monkeypatch)
    failure = SystemExit(41)

    if stage == "constructor":

        def fail_empty(cls: type[ScopedSecrets], names: object) -> ScopedSecrets:
            del cls, names
            raise failure

        monkeypatch.setattr(ScopedSecrets, "empty", classmethod(fail_empty))
    elif stage == "append":
        original_cleanup_init = lifecycle._CreateSecretCleanup.__init__  # noqa: SLF001

        class HostileReaders(list[ScopedSecrets]):
            def append(self, reader: ScopedSecrets) -> None:
                super().append(reader)
                raise failure

        def hostile_cleanup_init(self: object) -> None:
            original_cleanup_init(self)  # type: ignore[arg-type]
            self.readers = HostileReaders()  # type: ignore[attr-defined]

        monkeypatch.setattr(lifecycle._CreateSecretCleanup, "__init__", hostile_cleanup_init)  # noqa: SLF001
    else:
        original_copy_values = ScopedSecrets.copy_values

        def fail_copy(self: ScopedSecrets, values: dict[str, str]) -> None:
            secret_names = self._names & values.keys()  # noqa: SLF001
            if secret_names:
                name = next(iter(secret_names))
                self._values[name] = values[name]  # noqa: SLF001
                raise failure
            original_copy_values(self, values)

        monkeypatch.setattr(ScopedSecrets, "copy_values", fail_copy)

    with pytest.raises(SystemExit) as caught:
        vm_manager.create_vm(
            db,
            create_config(),
            name=f"scope-{stage}",
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    assert all(not resolver.resolved for resolver in resolvers)
    assert all(reader._values == {} for reader in readers)  # noqa: SLF001
    assert requests == []
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)


def test_provision_request_secret_assignment_is_owned_before_interruption(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.vm_platform import ProvisionRequest

    resolvers, readers, requests = _capture_secret_owners(monkeypatch)
    failure = GeneratorExit()
    original_setattr = ProvisionRequest.__setattr__

    def interrupt_after_assignment(self: ProvisionRequest, name: str, value: object) -> None:
        original_setattr(self, name, value)
        if name == "tailscale_auth_key" and value == _SENTINEL:
            requests.append(self)
            raise failure

    monkeypatch.setattr(ProvisionRequest, "__setattr__", interrupt_after_assignment)

    with pytest.raises(GeneratorExit) as caught:
        vm_manager.create_vm(
            db,
            create_config(),
            name="request-transfer",
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    _assert_owners_scrubbed(resolvers, readers, requests)
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)


def test_bootstrap_logger_callback_owns_logger_before_handoff_interrupt(
    create_config,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loggers: list[SSHLogger] = []
    failure = SystemExit(43)

    def _create(
        self: LimaPlatform,
        request: ProvisionRequest,
        ctx: RunContext,
    ) -> ProvisionResult:
        del self, ctx
        return ProvisionResult(
            native_transport=SimpleNamespace(),  # type: ignore[arg-type]
            platform_metadata={"instance_name": request.vm_name},
            bootstrap_complete=True,
            tailscale_ip="100.64.0.9",
        )

    def _bootstrap(
        db_: Database,
        config_: object,
        vm_template: ResolvedVMTemplate,
        vm_name: str,
        *args: object,
        tailscale_ctx: RunContext,
        git_tokens: dict[str, str],
        on_logger_ready: Callable[[SSHLogger], None],
        **kwargs: object,
    ) -> tuple[object, SSHLogger, str]:
        del db_, config_, args, kwargs
        logger = SSHLogger(
            vm_name,
            "vm-create",
            redactions=(tailscale_ctx.secret(vm_template.tailscale_auth_key), *git_tokens.values()),
        )
        loggers.append(logger)
        on_logger_ready(logger)
        raise failure

    monkeypatch.setattr(LimaPlatform, "create", _create)
    monkeypatch.setattr(LimaPlatform, "vm_active", _noop_hold)
    monkeypatch.setattr(vm_manager, "bootstrap_vm", _bootstrap)

    with pytest.raises(SystemExit) as caught:
        vm_manager.create_vm(
            db,
            create_config(),
            name="logger-handoff",
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is failure
    assert loggers and all(logger._redact == () for logger in loggers)  # noqa: SLF001
    _assert_secret_absent_from_agentworks_exception_graph(caught.value, _SENTINEL)
