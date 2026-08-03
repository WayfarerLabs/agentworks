"""ABC-level invariants for the ``Transport`` polymorphic surface.

These tests assert structural properties of the ABC itself; per-transport
behavior is covered in the sibling ``test_<transport>.py`` files.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import TYPE_CHECKING

import pytest

from agentworks.transports import (
    LimaTransport,
    RemoteLimaTransport,
    SSHTransport,
    Transport,
    WSL2Transport,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

REQUIRED_METHODS = {
    "describe",
    "run",
    "_interactive",
    "copy_to",
    "copy_from",
    "call_streaming",
}
# ``copy_dir_to`` and ``write_file`` are concrete defaults on the ABC
# (tarball + ``copy_to`` + remote extract; tempfile + ``copy_to``).
# They're not in REQUIRED_METHODS because the ABC ships a working
# implementation; subclasses may override but don't have to.
#
# ``interactive`` is likewise concrete: it wraps the abstract
# ``_interactive`` in the local-terminal guard, which is the point of
# the split (see ``Transport.interactive``). Subclasses implement the
# underscore-prefixed hook, so that is what belongs in REQUIRED_METHODS.


def test_transport_is_abstract() -> None:
    """``Transport`` cannot be instantiated directly."""
    with pytest.raises(TypeError):
        Transport()  # type: ignore[abstract]


def test_abc_surface_is_complete() -> None:
    """The ABC exposes every method the operator surface requires.

    If a method gets added (or removed), update REQUIRED_METHODS so the
    contract stays explicit. Locks in the agreed-on surface from the
    polymorphic-transports SDD.
    """
    abc_methods = {
        name for name, value in inspect.getmembers(Transport) if getattr(value, "__isabstractmethod__", False)
    }
    assert abc_methods == REQUIRED_METHODS


@pytest.mark.parametrize(
    "transport_cls",
    [SSHTransport, LimaTransport, RemoteLimaTransport, WSL2Transport],
)
def test_concrete_transports_implement_abc(transport_cls: type[Transport]) -> None:
    """Each concrete transport implements every abstract method."""
    assert issubclass(transport_cls, Transport)
    # Concrete classes must not have any leftover abstract methods.
    leftover: frozenset[str] = getattr(transport_cls, "__abstractmethods__", frozenset())
    assert leftover == frozenset(), f"{transport_cls.__name__} missing: {leftover}"


# ---------------------------------------------------------------------------
# interactive() wraps _interactive() in the local-terminal guard
# ---------------------------------------------------------------------------


class _RecordingTransport(Transport):
    """Minimal transport recording what ``_interactive`` received.

    Only ``_interactive`` does anything; the rest satisfy the ABC.
    Signatures are intentionally loose: this is a contract double, not a
    runnable transport.
    """

    def __init__(self, *, exit_code: int = 0, raises: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self._exit_code = exit_code
        self._raises = raises

    def describe(self) -> str:
        return "recording:test"

    def run(self, *a, **k):  # type: ignore[no-untyped-def] # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def _interactive(self, command: str, *, env: dict[str, str] | None = None) -> int:
        self.calls.append((command, env))
        if self._raises:
            raise RuntimeError("connection died")
        return self._exit_code

    def copy_to(self, *a, **k):  # type: ignore[no-untyped-def] # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def copy_from(self, *a, **k):  # type: ignore[no-untyped-def] # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    def call_streaming(self, *a, **k):  # type: ignore[no-untyped-def] # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError


def test_abc_reports_nothing_on_a_non_zero_interactive_exit(captured_output) -> None:  # noqa: ANN001
    """The default hook is a no-op on purpose. For Lima, WSL2, or any
    login shell a non-zero exit is usually just the remote command's own
    status, so narrating it would be noise. Only transports that can
    tell a connection failure apart from a command failure override."""
    t = _RecordingTransport(exit_code=255)
    t.interactive("")
    assert captured_output.warnings == []


def test_interactive_delegates_to_the_subclass_hook() -> None:
    """The concrete wrapper is pass-through: command, env, and exit code
    all survive the trip through the guard."""
    t = _RecordingTransport(exit_code=42)
    assert t.interactive("tmux attach -t s1", env={"K": "V"}) == 42
    assert t.calls == [("tmux attach -t s1", {"K": "V"})]


def test_interactive_runs_inside_the_terminal_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the abstract/concrete split. Asserted at the
    ABC so it holds for every transport, present and future, rather than
    being re-checked per subclass.
    """
    entered: list[str] = []

    @contextlib.contextmanager
    def _spy() -> Iterator[None]:
        entered.append("enter")
        try:
            yield
        finally:
            entered.append("exit")

    monkeypatch.setattr("agentworks.transports.base.guarded_terminal", _spy)
    t = _RecordingTransport()
    t.interactive("")
    assert entered == ["enter", "exit"]


def test_interactive_guard_closes_when_the_transport_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped attach can surface as an exception rather than a
    non-zero exit; the terminal still has to be restored."""
    entered: list[str] = []

    @contextlib.contextmanager
    def _spy() -> Iterator[None]:
        entered.append("enter")
        try:
            yield
        finally:
            entered.append("exit")

    monkeypatch.setattr("agentworks.transports.base.guarded_terminal", _spy)
    t = _RecordingTransport(raises=True)
    with pytest.raises(RuntimeError):
        t.interactive("")
    assert entered == ["enter", "exit"]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    """A subclass that doesn't implement every abstract method raises
    ``TypeError`` at construction. Locks in the ABC contract itself, so
    a future regression that turns ``abc.ABC`` into a ``Protocol`` or
    drops an ``@abstractmethod`` fails loudly rather than silently
    letting incomplete classes through.
    """

    class BrokenTransport(Transport):
        # Implements run() but is missing the other abstract methods.
        # Signatures intentionally unannotated to keep the broken-subclass
        # minimal: this is a contract test, not a runnable transport.
        def run(  # type: ignore[no-untyped-def, override] # noqa: ANN001, ANN201
            self,
            command,
            *,
            sudo=False,
            tty=None,
            check=True,
            timeout=None,
            env=None,
            retries=None,
            on_retry=None,
        ):
            raise NotImplementedError

    with pytest.raises(TypeError):
        BrokenTransport()  # type: ignore[abstract]
