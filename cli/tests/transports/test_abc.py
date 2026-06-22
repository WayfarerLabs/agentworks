"""ABC-level invariants for the ``Transport`` polymorphic surface.

These tests assert structural properties of the ABC itself; per-transport
behavior is covered in the sibling ``test_<transport>.py`` files.
"""

from __future__ import annotations

import inspect

import pytest

from agentworks.transports import (
    LimaTransport,
    RemoteLimaTransport,
    SSHTransport,
    Transport,
    WSL2Transport,
)

REQUIRED_METHODS = {
    "run",
    "interactive",
    "copy_to",
    "copy_from",
    "copy_dir_to",
    "write_file",
    "call_streaming",
}


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
        name
        for name, value in inspect.getmembers(Transport)
        if getattr(value, "__isabstractmethod__", False)
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


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    """A subclass that doesn't implement every abstract method raises
    ``TypeError`` at construction. Locks in the ABC contract itself, so
    a future regression that turns ``abc.ABC`` into a ``Protocol`` or
    drops an ``@abstractmethod`` fails loudly rather than silently
    letting incomplete classes through.
    """

    class BrokenTransport(Transport):
        # Implements run() but is missing the other six abstract methods.
        # Signatures intentionally unannotated to keep the broken-subclass
        # minimal: this is a contract test, not a runnable transport.
        def run(self, command, *, sudo=False, tty=None, check=True, timeout=None, env=None):  # type: ignore[no-untyped-def] # noqa: ANN001, ANN201
            raise NotImplementedError

    with pytest.raises(TypeError):
        BrokenTransport()  # type: ignore[abstract]
