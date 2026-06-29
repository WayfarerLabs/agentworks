"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.db import Database


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

    def info(self, message: str) -> None:
        self._captured.info.append(message)

    def detail(self, message: str, indent: int = 1) -> None:
        self._captured.detail.append(message)

    def warn(self, message: str) -> None:
        self._captured.warnings.append(message)

    def confirm(self, message: str, default: bool = False) -> bool:
        return self._captured.confirm_response

    def choose(self, message: str, options: list[str]) -> int:
        return self._captured.choose_response

    def pause(self, message: str) -> None:
        pass  # no-op in tests

    def prompt(self, label: str, default: str | None = None) -> str:
        return self._captured.prompt_response

    def prompt_secret(self, label: str, hint: str | None = None) -> str:
        return self._captured.secret_response

    def progress(self, label: str, total: int | None = None) -> _CapturedProgress:
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
    from agentworks.output import get_handler, set_handler

    previous = get_handler()
    captured = CapturedOutput()
    set_handler(_TestHandler(captured))
    yield captured
    set_handler(previous)


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


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    """Install a FakeTarget for the transport layer and stub VM-running checks."""
    target = _FakeTarget()
    # ``agentworks.transports.transport`` is the canonical admin-transport
    # factory; ``agentworks.sessions.manager.transport`` covers manager's
    # eager top-level import (used by batch_check_all_sessions and friends).
    fake_factory = lambda vm, config, **kwargs: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", fake_factory)
    # ``sessions.manager`` and ``agents.manager`` import ``transport`` at module
    # load (eager), so the agentworks.transports-side patch alone wouldn't take
    # effect for callers that already captured the binding.
    monkeypatch.setattr(
        "agentworks.sessions.manager.transport", fake_factory
    )
    monkeypatch.setattr(
        "agentworks.agents.manager.transport", fake_factory
    )
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )
    # The interactive code path now lives on the transport itself; the
    # fake target exposes it as a no-op so attach flows return cleanly.
    target.interactive = lambda command, **kwargs: 0  # type: ignore[attr-defined]
    return target


class _StubSessionTemplate:
    """Minimal stand-in for ``ResolvedSessionTemplate`` used by the helper below."""

    name = "default"
    command = ""
    restart_command = None
    required_commands: list[str] = []  # noqa: RUF012 - mutable class attr is fine for a stub
    env: dict[str, str] = {}  # noqa: RUF012 - mutable class attr is fine for a stub


def stub_session_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the session-template, env, and eager-resolve helpers in
    ``sessions.manager``.

    Several tests construct a ``SimpleNamespace`` config that omits the
    ``vm_templates`` / ``agent_templates`` / ``secret_resolver`` attributes
    the real Phase 3+ resolvers read. Patching the resolvers themselves
    keeps those tests scope-correct (they exercise rollback / transport
    plumbing, not env composition) without expanding the fake config.

    Also stubs the Phase 6 eager-prompting orchestration: ``create_session``
    and ``restart_session`` call ``_session_secret_target`` +
    ``resolve_for_command`` before the first mutation. Tests that don't
    care about secret resolution patch both out.
    """
    from agentworks.sessions import manager as session_manager

    monkeypatch.setattr(
        session_manager, "_resolve_template", lambda *a, **k: _StubSessionTemplate()
    )
    monkeypatch.setattr(
        session_manager, "_resolve_session_env", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        session_manager, "_session_secret_target", lambda *a, **k: None
    )
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: None
    )
    # ``resolve_for_command`` is imported locally inside create_session /
    # restart_session, so patch its module-level home; the import inside
    # the function picks up the patched version.
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", lambda *a, **k: {}
    )
    # Phase 2a's manager-entry hoist calls ``build_registry(config)`` at
    # the top of ``create_session`` before any business logic. Stub it
    # to a no-op for SimpleNamespace configs that don't carry
    # ``publish_to``. Real Config flows still exercise the hoist's
    # framework-error guarantee in the tests under tests/resources/.
    monkeypatch.setattr(
        "agentworks.bootstrap.build_registry", lambda config: None
    )
