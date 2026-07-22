"""Shared test fixtures."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.db import Database
from agentworks.output import Role, _render_header

# The orchestrated-command suites' shared fixture trio (proxmox
# section, make_config, resolve_counter) lives in its own module so it
# reads as the suites' vocabulary rather than universal machinery.
pytest_plugins = ["tests.orchestrated_fixtures"]


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    """Provide a fresh database for each test, closed automatically."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Output capturing
# ---------------------------------------------------------------------------


@dataclass
class _CapturedProgress:
    label: str
    updates: list[tuple[int | None, str | None]] = field(default_factory=list)
    completed: bool = False
    done_message: str | None = None

    def update(self, current: int | None = None, message: str | None = None) -> None:
        self.updates.append((current, message))

    def done(self, message: str | None = None) -> None:
        self.completed = True
        self.done_message = message


@dataclass
class CapturedOutput:
    """All output captured during a test."""

    # Structural capture: every emitted line as (role, level, message).
    # New tests assert on role + level here; the message-list fields
    # below stay for existing substring assertions.
    lines: list[tuple[Role, int, str]] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    progress_items: list[_CapturedProgress] = field(default_factory=list)
    confirm_response: bool = True  # what confirm() returns in tests
    choose_response: int = 0  # what choose() returns in tests
    prompt_response: str = "test-value"  # what prompt() returns in tests
    secret_response: str = "test-secret"  # what prompt_secret() returns in tests


class _TestHandler:
    def __init__(self, captured: CapturedOutput) -> None:
        self._captured = captured

    def emit(self, role: Role, message: str, level: int) -> None:
        self._captured.lines.append((role, level, message))
        # Mirror into the legacy message lists so existing substring
        # assertions keep working. RESULT joins info so "final line"
        # checks still find the closing message. HEADER mirrors its
        # rendered form (e.g. "=== Preflight ===") into info so existing
        # phase()-header assertions keep passing until those call sites
        # move to section() (Phases 3-4); new tests read .lines instead.
        if role in (Role.BODY, Role.RESULT):
            self._captured.info.append(message)
        elif role is Role.HEADER:
            self._captured.info.append(_render_header(message, level))
        elif role is Role.DETAIL:
            self._captured.detail.append(message)
        elif role is Role.WARNING:
            self._captured.warnings.append(message)

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
        return self._captured.confirm_response

    def choose(self, message: str, options: list[str], level: int) -> int:
        return self._captured.choose_response

    def pause(self, message: str, level: int) -> None:
        pass  # no-op in tests

    def prompt(self, label: str, level: int, default: str | None = None) -> str:
        return self._captured.prompt_response

    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str:
        return self._captured.secret_response

    def progress(self, label: str, level: int, total: int | None = None) -> _CapturedProgress:
        p = _CapturedProgress(label=label)
        self._captured.progress_items.append(p)
        return p


@pytest.fixture
def captured_output() -> Generator[CapturedOutput, None, None]:
    """Capture all output emitted via agentworks.output.

    Usage::

        def test_something(captured_output):
            do_something()
            assert any("expected" in m for m in captured_output.info)
            assert len(captured_output.warnings) == 0
    """
    from agentworks import output
    from agentworks.output import get_handler, set_handler

    previous = get_handler()
    captured = CapturedOutput()
    set_handler(_TestHandler(captured))
    yield captured
    set_handler(previous)
    # Defense in depth: a test cannot leak a section level into the next,
    # even though section()'s reset-token discipline already prevents it.
    output._level.set(0)


@pytest.fixture
def warnings(captured_output: CapturedOutput) -> Generator[list[str], None, None]:
    """Capture warnings emitted via ``agentworks.output.warn``.

    Convenience wrapper for tests that only care about warnings.
    Reuses ``captured_output`` so both fixtures can coexist safely.
    """
    yield captured_output.warnings


# ---------------------------------------------------------------------------
# Fake tmux target (named-console tests)
#
# Several test modules drive the named-console SSH layer through a stand-in
# target that captures commands rather than actually running them on a VM.
# Defined here so all test files that import the classes (or use the
# fixture) share the same implementation.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal stand-in for ssh.SSHResult."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _FakeTarget:
    """Captures the commands run against it. Supports a per-test override map
    that lets us simulate (e.g.) `has-session` returning nonzero on first probe.
    """

    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self.commands: list[str] = []
        # Substring -> response. First matching substring wins; default = ok.
        self.responses = responses or {}

    def run(self, command: str, **kwargs: object) -> _FakeResult:
        self.commands.append(command)
        for needle, response in self.responses.items():
            if needle in command:
                return response
        return _FakeResult()


class _StubPlatform:
    """Minimal bound-platform stand-in for the vm-sites gates.

    ``vm_active`` is a no-op hold and ``status`` reports RUNNING so the
    gate proceeds without shelling out (the gate's real fast path,
    ``confirmed_active`` / ``_is_tailscale_reachable``, runs
    ``tailscale ping``).
    """

    name = "stub"

    def preflight(self, ctx: object) -> None:
        return None

    def runup(self, ctx: object) -> None:
        return None

    def vm_active(self, vm: object, *, config: object | None = None) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def status(self, vm: object, ctx: object) -> object:
        from agentworks.db import VMStatus

        return VMStatus.RUNNING

    def transient_route(self, vm: object) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def post_tailscale_ready(self, vm: object) -> None:
        return None


def publish_all_platforms(registry: object) -> None:
    """Publish every installed platform's capability row, bypassing the
    host-support gate. For registry-shape tests that need the full
    four-platform graph regardless of the test host's OS."""
    from agentworks.capabilities.vm_platform import (
        VM_PLATFORM_REGISTRY,
        VMPlatformEntry,
    )
    from agentworks.resources import Origin

    origin = Origin.built_in(source="tests.conftest")
    for name, cls in VM_PLATFORM_REGISTRY.items():
        registry.add(  # type: ignore[attr-defined]
            "vm-platform",
            name,
            VMPlatformEntry(name=name, description=cls.description),
            origin,
        )


def stub_platform_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every platform (and every site bound to one) report
    supported and enabled, regardless of the test host's OS and
    tooling.

    Platform capability rows are host-gated for real (wsl2 publishes
    nothing off Windows) and sites self-disable for real (lima-local
    needs a local limactl), so tests that want the full four-platform
    graph enabled must opt out of the host's actual state. Tests OF
    the disabled model itself patch the individual methods instead.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    for cls in VM_PLATFORM_REGISTRY.values():
        monkeypatch.setattr(cls, "unsupported_reason", classmethod(lambda c: None))
        monkeypatch.setattr(cls, "disabled_reason", lambda self: None)


def stub_vm_gates(monkeypatch: pytest.MonkeyPatch) -> _StubPlatform:
    """Stub the orchestrated activation gate so tests that exercise
    transport / rollback / env plumbing neither construct real platforms
    nor shell out to Tailscale.

    Two seams: the node factories bind their platform through
    ``resolve_site`` (the only constructor of platform instances), and
    the activation gate's fast path probes Tailscale reachability. Stub
    both so the gate fast-paths and holds via the stub platform's no-op
    ``vm_active``. Returns the stub platform for assertions.
    """
    platform = _StubPlatform()

    def _fake_resolve_site(name: object, registry: object) -> _StubPlatform:
        return platform

    monkeypatch.setattr("agentworks.vms.sites.resolve_site", _fake_resolve_site)
    monkeypatch.setattr("agentworks.vms.manager._is_tailscale_reachable", lambda host: True)
    return platform


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    """Install a FakeTarget for the transport layer and stub the VM gates."""
    target = _FakeTarget()
    # ``agentworks.transports.transport`` is the canonical admin-transport
    # factory; ``agentworks.sessions.manager.transport`` covers manager's
    # eager top-level import (used by batch_check_all_sessions and friends).
    fake_factory = lambda vm, config, **kwargs: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", fake_factory)
    # ``sessions.manager`` and the agents modules import ``transport`` at
    # module load (eager), so the agentworks.transports-side patch alone
    # wouldn't take effect for callers that already captured the binding.
    monkeypatch.setattr("agentworks.sessions.manager.transport", fake_factory)
    monkeypatch.setattr("agentworks.agents.manager.transport", fake_factory)
    monkeypatch.setattr("agentworks.agents.grants.transport", fake_factory)
    monkeypatch.setattr("agentworks.agents.initializer.transport", fake_factory)
    stub_vm_gates(monkeypatch)
    # The interactive code path now lives on the transport itself; the
    # fake target exposes it as a no-op so attach flows return cleanly.
    target.interactive = lambda command, **kwargs: 0  # type: ignore[attr-defined]
    return target


class _StubSessionTemplate:
    """Minimal stand-in for ``ResolvedSessionTemplate`` used by the helper below.

    Carries the ``(harness, harness_config)`` pair the session-node
    factory builds the harness from: the default is the ``shell`` harness
    with an empty config (a plain login shell)."""

    name = "default"
    harness = "shell"
    harness_config: dict[str, object] = {}  # noqa: RUF012 - mutable class attr is fine for a stub
    env: dict[str, str] = {}  # noqa: RUF012 - mutable class attr is fine for a stub


def empty_secret_target(label: str = "test"):  # noqa: ANN201 - test helper
    """A real, empty ``SecretTarget``: the stub for the env-chain seam.

    The orchestrated session commands register their env target on the
    operation's REAL resolver (``register_targets``), so a bare ``None``
    or sentinel object no longer survives the seam; an empty target
    walks the same code with zero referenced secrets.
    """
    from agentworks.secrets import SecretTarget

    return SecretTarget(vm={}, label=label)


def stub_session_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the session-template, env, and eager-resolve helpers in
    ``sessions.manager``.

    Several tests construct a ``SimpleNamespace`` config that omits the
    ``vm_templates`` / ``agent_templates`` attributes (and can't publish
    the registry rows) the real resolvers read. Patching the resolvers
    themselves keeps those tests scope-correct (they exercise rollback /
    transport plumbing, not env composition) without expanding the fake
    config.

    Also stubs the Phase 6 eager-prompting orchestration: ``create_session``
    and ``restart_session`` call ``_session_secret_target`` +
    ``resolve_for_command`` before the first mutation. Tests that don't
    care about secret resolution patch both out.
    """
    from agentworks.sessions import manager as session_manager

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _StubSessionTemplate())
    monkeypatch.setattr(session_manager, "_resolve_session_env", lambda *a, **k: {})
    monkeypatch.setattr(session_manager, "_session_secret_target", lambda *a, **k: empty_secret_target())
    monkeypatch.setattr(
        session_manager,
        "_session_secret_target_pre_create",
        lambda *a, **k: empty_secret_target(),
    )
    # ``resolve_for_command`` is imported locally inside create_session /
    # restart_session, so patch its module-level home; the import inside
    # the function picks up the patched version.
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})


class _StubRegistry:
    """Registry test double serving the consumer read surface from a
    (possibly ``SimpleNamespace``) config.

    The Phase 1 consumer repoint (resource-manifests SDD) routed all
    resource reads through Registry queries (``lookup`` /
    ``iter_kind`` / ``iter_kind_items``, usually via
    ``agentworks.resources.access``). Tests that fabricate minimal
    namespace configs can't feed the real ``build_registry`` (no
    ``publish_to``, rows aren't dataclasses), so this double answers
    the same queries straight off the config's attributes, falling
    back to real code-default singletons where the fake omits them.
    """

    _KIND_ATTRS = {
        "secret": "secrets",
        "vm-template": "vm_templates",
        "agent-template": "agent_templates",
        "workspace-template": "workspace_templates",
        "session-template": "session_templates",
        "git-credential": "git_credentials",
        "apt-source": "apt_sources",
        "apt-package": "apt_packages",
        "system-install-command": "system_install_commands",
        "user-install-command": "user_install_commands",
    }

    def __init__(self, config: object) -> None:
        self._config = config

    def _kind_dict(self, kind: str) -> dict[str, object]:
        attr = self._KIND_ATTRS.get(kind)
        if attr is None:
            return {}
        return dict(getattr(self._config, attr, None) or {})

    def lookup(self, kind: str, name: str) -> object:
        # Mirrors the real Registry's miss semantics: ``lookup`` raises
        # KeyError on unknown kinds and names so stubbed tests fail the
        # same way production does. The singleton kinds fall back to
        # code-default rows only for the reserved name.
        if kind == "admin-template":
            from agentworks.vms.admin import AdminConfig

            if name != "default":
                raise KeyError(name)
            admin = getattr(self._config, "admin", None)
            return admin if admin is not None else AdminConfig()
        if kind == "named-console-template":
            from agentworks.sessions.template import NamedConsoleConfig

            if name != "default":
                raise KeyError(name)
            console = getattr(self._config, "named_console", None)
            return console if console is not None else NamedConsoleConfig(name="default")
        if kind == "vm-site":
            # Serve the built-in same-named sites so resolve_site /
            # lookup_site work against namespace configs (a stubbed
            # test VM's site is one of the four platform names).
            from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
            from agentworks.vms.sites import VMSiteDecl

            if name not in VM_PLATFORM_REGISTRY:
                raise KeyError(name)
            return VMSiteDecl(name=name, platform=name)
        if kind not in self._KIND_ATTRS:
            raise KeyError(kind)
        return self._kind_dict(kind)[name]

    def iter_kind(self, kind: str):  # noqa: ANN201 - mirrors Registry
        return iter(self._kind_dict(kind).values())

    def iter_kind_items(self, kind: str):  # noqa: ANN201 - mirrors Registry
        return iter(self._kind_dict(kind).items())


def stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``agentworks.bootstrap.build_registry`` with ``_StubRegistry``.

    Manager entries call ``build_registry(config)`` before business
    logic and thread the result to every resource read (Phase 1 of the
    resource-manifests SDD). Tests that pass ``SimpleNamespace`` configs
    (which don't carry ``publish_to``) need this stub so those entries
    get a Registry-shaped object that answers reads from the fake
    config. Real ``Config`` flows still exercise the real
    ``build_registry`` via ``tests/resources/`` and the integration
    suites.

    Usage: bind to an autouse fixture in each test module that uses
    mock configs::

        @pytest.fixture(autouse=True)
        def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
            stub_build_registry(monkeypatch)
    """
    monkeypatch.setattr("agentworks.bootstrap.build_registry", _StubRegistry)

    # Namespace configs lack secret_config_data (and the stub registry
    # carries no backend rows), so stub the orchestration seam: eager
    # resolution returns no values, and compose_env sites receive {}
    # (namespace-config tests carry no secret-referencing env entries).
    def _stub_resolve_for_command(*args: object, **kwargs: object) -> dict[str, str]:
        return {}

    # Production consumers import resolve_for_command function-locally
    # from agentworks.secrets; patch the defining module and the
    # re-export so both binding shapes see the stub.
    for site in (
        "agentworks.secrets.orchestration.resolve_for_command",
        "agentworks.secrets.resolve_for_command",
    ):
        monkeypatch.setattr(site, _stub_resolve_for_command)


@pytest.fixture(autouse=True)
def _no_network_token_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must never reach the real network. Token verification
    probes (git credential ``runup()``) hit provider APIs in
    production; here any unmocked probe raises OSError, which the
    providers treat as network indeterminacy (warn + continue
    unverified), so unrelated tests keep passing while never leaving
    the process. Verification tests monkeypatch ``_http_probe`` with
    their own fakes, overriding this guard.
    """

    def _refuse(*_a: object, **_k: object) -> object:
        raise OSError("network disabled in tests")

    monkeypatch.setattr("agentworks.capabilities.git_credential.base._http_probe", _refuse)


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The suite must never touch the operator's real database. Several
    CLI tests isolate CONFIG_PATH but the module-level DB_PATH default
    still pointed at the live DB; used-by counts in `resource list`
    were silently querying it (and started crashing the moment the
    operator's DB schema moved ahead of this branch). Every test gets a
    fresh empty DB path; fixtures that build explicit DB state pass
    their own path and are unaffected.
    """
    monkeypatch.setattr("agentworks.db.DB_PATH", tmp_path / "isolated-test.db")
