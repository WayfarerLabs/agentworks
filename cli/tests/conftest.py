"""Shared test fixtures."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Protocol

import pytest
import yaml

from agentworks.db import Database
from agentworks.manifests.envelope import API_VERSION
from agentworks.manifests.loader import RESOURCES_DIRNAME
from agentworks.output import Role, StatusStyle, _render_header
from agentworks.schema import CapabilityBlock

# The orchestrated-command suites' shared fixture trio (proxmox
# section, make_config, resolve_counter) lives in its own module so it
# reads as the suites' vocabulary rather than universal machinery.
pytest_plugins = ["tests.orchestrated_fixtures"]


@pytest.fixture(scope="session", autouse=True)
def _isolate_ssh_logs(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Keep default SSH logs in this worker's temporary directory."""
    import agentworks.ssh as ssh

    prior = ssh.LOG_DIR
    log_dir = tmp_path_factory.getbasetemp() / "ssh-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ssh.LOG_DIR = log_dir
    try:
        yield
    finally:
        ssh.LOG_DIR = prior


# ---------------------------------------------------------------------------
# Resource manifest authoring
#
# config.toml is settings-only now (ADR 0022); resources are declared in
# ``resources/*.yaml`` manifests beside it. Roughly 28 test files already
# author those manifests inline (mkdir a ``resources/`` dir, write an
# enveloped YAML document); this is the shared form of that pattern, so a
# fixture only varies the kind, name, and spec.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestDoc:
    """One resource-manifest document, in structured form.

    The envelope boilerplate (``apiVersion`` / ``kind`` / ``metadata``) is
    identical for every document, so a test only varies the resource
    ``kind``, its ``name``, the ``spec`` mapping, and an optional
    ``description`` (which belongs in ``metadata``, never in ``spec``).
    :func:`write_manifests` renders these into the ``resources/`` dir the
    framework auto-loads operator manifests from (``RESOURCES_DIRNAME``).
    """

    kind: str
    name: str
    spec: dict[str, object] = field(default_factory=dict)
    description: str | None = None


def render_manifest(doc: ManifestDoc | str) -> str:
    """Render one manifest document to YAML text.

    A :class:`ManifestDoc` is wrapped in the standard envelope; a raw
    string is dedented and returned unchanged, the escape hatch for tests
    that must author malformed or otherwise hand-shaped YAML.
    """
    if isinstance(doc, str):
        return dedent(doc)
    metadata: dict[str, object] = {"name": doc.name}
    if doc.description is not None:
        metadata["description"] = doc.description
    envelope = {
        "apiVersion": API_VERSION,
        "kind": doc.kind,
        "metadata": metadata,
        "spec": doc.spec,
    }
    return yaml.safe_dump(envelope, sort_keys=False)


def write_manifests(
    config_dir: Path,
    *docs: ManifestDoc | str,
    filename: str = "resources.yaml",
) -> Path:
    """Write resource manifests into ``<config_dir>/resources/`` and return
    that directory.

    Multiple ``docs`` are written as one multi-document YAML stream in
    ``filename``; call again with a distinct ``filename`` to spread
    declarations across files (e.g. to exercise load ordering, or to sit an
    unreadable file beside a good one).
    """
    resources_dir = config_dir / RESOURCES_DIRNAME
    resources_dir.mkdir(parents=True, exist_ok=True)
    stream = "---\n".join(render_manifest(doc) for doc in docs)
    (resources_dir / filename).write_text(stream)
    return resources_dir


def write_cfg(
    config_dir: Path,
    *manifests: ManifestDoc | str,
    settings: str = "",
    filename: str = "config.toml",
) -> Path:
    """Write a loadable settings-only config into ``config_dir``, with its
    ``resources/`` manifests beside it, and return the config path.

    The operator keypair is written here rather than taken as a parameter:
    ``load_config`` requires the two paths to exist and nothing reads their
    contents, so every caller wanted the same two throwaway files. A test
    that cares what is IN a key writes its own and points ``settings`` at
    it.

    ``settings`` is settings-only TOML appended after the ``[operator]``
    block (``[secret_config]``, ``[plugins]``, and so on). Resource
    declarations are ``manifests``, never settings: config.toml carries no
    resource topics (ADR 0022).
    """
    pub = config_dir / "id.pub"
    priv = config_dir / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path = config_dir / filename
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(settings),
    )
    if manifests:
        write_manifests(config_dir, *manifests)
    return path


@pytest.fixture(autouse=True)
def _restore_agw_debug() -> Generator[None, None, None]:
    """Snapshot and restore ``AGW_DEBUG`` around every test.

    A test that drives the CLI with ``--debug`` mirrors the flag into the
    ``AGW_DEBUG`` env var (see ``cli/_app.py`` ``_mirror_debug_to_env``,
    called from the root Typer callback), a process-global mutation pytest
    does not undo on its own. Without this,
    such a test would leak ``AGW_DEBUG=1`` into every later test in the
    process. That env var is the propagation vector for debug state: the CLI
    re-seeds its internal ``_debug`` flag from ``AGW_DEBUG`` on every
    invocation, and the azure-identity logger suppression reads it directly, so
    a leaked value would silently flip debug-gated behavior in later tests.
    Restoring it here keeps that contained.
    """
    had = "AGW_DEBUG" in os.environ
    prior = os.environ.get("AGW_DEBUG", "")
    try:
        yield
    finally:
        if had:
            os.environ["AGW_DEBUG"] = prior
        else:
            os.environ.pop("AGW_DEBUG", None)


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    """Provide a fresh database for each test, closed automatically."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@contextmanager
def held_exclusive_lock(path: Path) -> Iterator[None]:
    """Hold BEGIN EXCLUSIVE on `path` for the with-block's duration,
    blocking every other reader and writer. Used to synthesize a busy
    database for tests, shared across test modules so each does not carry
    its own copy of the lock/rollback/close idiom."""
    locker = sqlite3.connect(path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        locker.rollback()
        locker.close()


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
    notices: list[str] = field(default_factory=list)
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
        elif role is Role.NOTICE:
            self._captured.notices.append(message)
        elif role is Role.WARNING:
            self._captured.warnings.append(message)

    def style_status(self, text: str, style: StatusStyle) -> str:
        # The test handler never colorizes: tests assert on plain text.
        return text

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


class _SupportsDispatch(Protocol):
    """The one method ``_FakeTarget`` needs from a stateful tmux model.

    Typed as a Protocol (rather than importing ``tests._tmux_model.TmuxModel``)
    because that module imports ``_FakeResult`` from here; a concrete import
    the other way would be circular. ``TmuxModel`` satisfies it structurally.
    """

    def dispatch(self, command: str) -> _FakeResult: ...


class _FakeTarget:
    """Captures the commands run against it. Supports a per-test override map
    that lets us simulate (e.g.) `has-session` returning nonzero on first probe.

    Optionally stateful: pass a ``model`` (a ``tests._tmux_model.TmuxModel``)
    and ``run()`` answers from live tmux state instead of a fixed default.
    Resolution order is override map first, then the model, then default-OK,
    so a test can still force a specific failure (e.g. "make this one
    split-window fail") on top of an otherwise stateful target. Without a
    model the target is the original stateless substring map, so every
    existing test is unaffected.
    """

    def __init__(
        self,
        responses: dict[str, _FakeResult] | None = None,
        *,
        model: _SupportsDispatch | None = None,
    ) -> None:
        self.commands: list[str] = []
        # Substring -> response. First matching substring wins; default = ok.
        self.responses = responses or {}
        self.model = model

    def run(self, command: str, **kwargs: object) -> _FakeResult:
        self.commands.append(command)
        for needle, response in self.responses.items():
            if needle in command:
                return response
        if self.model is not None:
            return self.model.dispatch(command)
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

    def transient_route(self, vm: object, ctx: object, *, config: object | None = None) -> AbstractContextManager[None]:
        return contextlib.nullcontext()

    def post_tailscale_ready(self, vm: object, ctx: object) -> None:
        return None

    def secure_failed_vm(self, vm: object, ctx: object) -> None:
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

    Platform capability rows publish unconditionally (R13), but their
    readiness is host-gated for real (wsl2 is not-ready off Windows) and
    sites are not-ready for real (lima-local needs a local limactl), so
    tests that want the full four-platform graph READY must opt out of the
    host's actual state. Tests OF the readiness model itself patch the
    individual methods instead.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.resources.graph import Readiness

    for cls in VM_PLATFORM_REGISTRY.values():
        monkeypatch.setattr(cls, "unsupported_reason", classmethod(lambda c: None))
        monkeypatch.setattr(cls, "not_ready", classmethod(lambda c, config: Readiness.ready()))


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


def install_fake_target(monkeypatch: pytest.MonkeyPatch, target: _FakeTarget) -> _FakeTarget:
    """Route the transport layer through ``target`` and stub the VM gates.

    Shared by the ``fake_target`` fixture (stateless) and the
    ``console_target_factory`` fixture (stateful, model-backed), so both
    install the same transport seams and gate stubs. Returns ``target`` for
    the caller to keep a reference.
    """
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


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    """Install a FakeTarget for the transport layer and stub the VM gates."""
    return install_fake_target(monkeypatch, _FakeTarget())


@pytest.fixture
def console_target_factory(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - returns a local factory
    """Return a factory that installs a stateful, model-backed fake target.

    Usage::

        def test_x(db, console_target_factory):
            model = TmuxModel()
            model.seed_session("aw-console-con", "a", pane_tags=(None, 0))
            target = console_target_factory(model)
            restore_session(db, _StubConfig(), console_name="con", session_name="a")
            assert model.has_session("aw-console-con")

    The installed target routes through the same transport seams / gate
    stubs as ``fake_target``; ``responses`` still layers per-test overrides
    on top of the model (override map wins), so a test can force one command
    to fail while the rest stay stateful.
    """

    def _make(model: _SupportsDispatch | None = None, responses: dict[str, _FakeResult] | None = None) -> _FakeTarget:
        return install_fake_target(monkeypatch, _FakeTarget(responses=responses, model=model))

    return _make


class _StubSessionTemplate:
    """Minimal stand-in for ``ResolvedSessionTemplate`` used by the helper below.

    Carries the ``(harness_integration, harness_integration_config)`` pair the
    session-node factory builds the harness integration from: the default is
    the ``shell`` integration with an empty config (a plain login shell)."""

    name = "default"
    harness_integration = "shell"
    harness_integration_config: dict[str, object] = {}  # noqa: RUF012 - mutable class attr is fine for a stub
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
    and ``resume_session`` call ``_session_secret_target`` +
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
    # resume_session, so patch its module-level home; the import inside
    # the function picks up the patched version.
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})


#: The minimum config each platform needs to VALIDATE, for the built-in
#: same-named sites ``_StubRegistry`` serves below.
#:
#: Only platforms with a required field appear: lima's ``placement`` says
#: where limactl runs, and it is required precisely so that no site can
#: leave its execution mechanism unsaid, stub sites included. wsl2 takes
#: no configuration, and the cloud platforms' sites are never served here
#: (a stubbed VM's site is a local one).
_STUB_PLATFORM_CONFIG: dict[str, dict[str, object]] = {"lima": {"placement": {"mode": "local"}}}


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
            return VMSiteDecl(name=name, platform=CapabilityBlock.of(name, **_STUB_PLATFORM_CONFIG.get(name, {})))
        if kind not in self._KIND_ATTRS:
            raise KeyError(kind)
        return self._kind_dict(kind)[name]

    def iter_kind(self, kind: str):  # noqa: ANN201 - mirrors Registry
        return iter(self._kind_dict(kind).values())

    def iter_kind_items(self, kind: str):  # noqa: ANN201 - mirrors Registry
        return iter(self._kind_dict(kind).items())

    @property
    def graph(self) -> _StubGraph:
        """The dependency-graph read surface. Phase 4 routes edge / readiness
        reads through ``registry.graph``; the stub computes edges on demand
        from each row's ``dependencies`` (empty build context, since the stub
        publishes no backend rows) and reports every node ready (the namespace
        fixtures model runnable resources)."""
        return _StubGraph(self)


class _StubGraph:
    """Minimal ``DependencyGraph`` double over a :class:`_StubRegistry`.

    ``edges_of`` recomputes a row's outbound edges from its ``dependencies``
    (the stub is not finalized, so there is no frozen edge map to read); the
    two closures DFS-walk those, filtered by relationship exactly as the real
    graph does; readiness is always ready. Enough for the consumer reads the
    manager entries make against namespace fixtures.

    The double must carry EVERY closure the real graph offers, not only the
    ones today's consumers reach: a missing one surfaces as an
    ``AttributeError`` from a fixture rather than as behavior, which is a
    confusing way to learn that the double is behind.
    """

    def __init__(self, registry: _StubRegistry) -> None:
        self._registry = registry

    def edges_of(self, kind: str, name: str):  # noqa: ANN201 - mirrors DependencyGraph
        from agentworks.resources.graph import FinalizeContext

        row = self._registry.lookup(kind, name)  # KeyError on unknown, like the real graph
        method = getattr(row, "dependencies", None)
        if method is None:
            return ()
        return tuple(method(FinalizeContext()))

    def runtime_reachable_from(self, kind: str, name: str) -> list[tuple[str, str]]:
        from agentworks.resources.reference import RefRelationship

        return self._closure(kind, name, RefRelationship.USES)

    def composed_from(self, kind: str, name: str) -> list[tuple[str, str]]:
        from agentworks.resources.reference import RefRelationship

        return self._closure(kind, name, RefRelationship.INHERITS)

    def _closure(self, kind: str, name: str, crossing: object) -> list[tuple[str, str]]:
        # Tolerate a missing start node, exactly as the real graph does: the
        # DFS below catches the edges_of KeyError and yields an empty closure,
        # so a consumer that walks a template resolved off a namespace fixture
        # (not a registry row) gets [] rather than a KeyError. The recipe
        # use-gate (ensure_recipe_enabled) relies on this.
        visited: set[tuple[str, str]] = {(kind, name)}
        ordered: list[tuple[str, str]] = []
        stack: list[tuple[str, str]] = [(kind, name)]
        while stack:
            node = stack.pop()
            try:
                edges = self.edges_of(*node)
            except KeyError:
                continue
            for ref in edges:
                if ref.relationship is not crossing:
                    continue
                target = (ref.kind, ref.name)
                if target not in visited:
                    visited.add(target)
                    ordered.append(target)
                    stack.append(target)
        return ordered

    def readiness_of(self, kind: str, name: str):  # noqa: ANN201 - mirrors DependencyGraph
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    def is_ready(self, kind: str, name: str) -> bool:
        return True

    def enablement_of(self, kind: str, name: str):  # noqa: ANN201 - mirrors DependencyGraph
        # Every node is enabled in the stub (no plugin opt-out producer here),
        # matching the real graph's every-node-is-enabled default. The session
        # harness integration gate (``ensure_harness_integration_enabled``) reads this at
        # the build sites; a built-in harness integration (``shell``) stays enabled, so the gate is a
        # no-op under the stub.
        from agentworks.resources.graph import Enablement

        return Enablement.enabled


def stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub registry composition with ``_StubRegistry``.

    Manager entries call ``load_request_registry(config)`` before business
    logic and thread the result to every resource read (Phase 1 of the
    resource-manifests SDD). Tests that pass ``SimpleNamespace`` configs
    (which don't carry ``publish_to``) need this stub so those entries
    get a Registry-shaped object that answers reads from the fake
    config. Real ``Config`` flows still exercise the real
    the pure ``build_registry`` via ``tests/resources/`` and the integration
    suites.

    Usage: bind to an autouse fixture in each test module that uses
    mock configs::

        @pytest.fixture(autouse=True)
        def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
            stub_build_registry(monkeypatch)
    """
    monkeypatch.setattr("agentworks.bootstrap.build_registry", _StubRegistry)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", _StubRegistry)

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
