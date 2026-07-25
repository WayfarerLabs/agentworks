"""Conformance oracle: pin ``TmuxModel`` to a real local tmux server.

``tests/_tmux_model.py`` is a hand-written stateful stand-in for tmux, and
a hand model can lie. This test runs the SAME operation sequences through
both the model and a real ``tmux`` server on a private socket and, after
every step, asserts they report the same OBSERVABLE STRUCTURE: which
windows exist at which indices, which panes each window holds at which
indices with which ``@agentworks-shell-index`` tag, and whether the session
still exists. Pane ids differ between the two (the model allocates ``%1..``,
real tmux ``%0..``), so the comparison is on ``(index, tag)`` shape, never on
literal ids. Real tmux is the ground truth: if the two disagree, the model
is wrong (or a fidelity boundary needs documenting in ``_tmux_model``).

The suite skips cleanly where ``tmux`` is unavailable, so it is a no-op on a
developer box without tmux while running for real in CI (ubuntu-latest ships
tmux). The server is isolated (a per-pid socket), started without inheriting
any ``~/.tmux.conf``, and torn down with ``kill-server`` even on failure, so
nothing leaks between tests.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Protocol

import pytest

from tests._tmux_model import SHELL_INDEX_OPTION, TmuxModel

_TMUX = shutil.which("tmux")

pytestmark = pytest.mark.skipif(_TMUX is None, reason="tmux binary is not available")

SESSION = "aw-console-con"
# A harmless long-lived command so panes do not exit on their own; teardown's
# kill-server reaps them.
HOLD = "sleep 100000"
# A generous per-command bound: normal tmux calls return in milliseconds, so
# this never trips under load, but a hung tmux fails the test instead of
# hanging the whole suite.
COMMAND_TIMEOUT_SECONDS = 10


class _Tmux(Protocol):
    """The observable surface both the model and the real server expose.

    Snapshotting against this Protocol lets one comparison function read
    structure from either side identically."""

    def has_session(self, session: str) -> bool: ...

    def windows_with_index(self, session: str) -> list[tuple[int, str]]: ...

    def pane_rows(self, session: str, window: str) -> list[tuple[str, int, int | None]] | None: ...


class RealTmux:
    """Drive a real, isolated tmux server over ``subprocess``.

    The server uses a private ``-L`` socket (per-pid, so parallel or repeated
    runs never collide) and is started from a temp config that sets only
    ``base-index`` / ``pane-base-index``. That config, rather than the literal
    ``-f /dev/null`` the task sketched, is required: base-index has to be set
    BEFORE the first window is created, but a tmux server with no session does
    not persist, so a separate ``set-option`` invocation cannot reach it. A
    one-line config read at server start is how the real console server picks
    these up from the admin ``~/.tmux.conf`` anyway, and pointing ``-f`` at our
    own file still avoids inheriting the developer's ``~/.tmux.conf``.
    ``renumber-windows`` is deliberately left at its default (off): that is the
    behavior finding 2 is about.
    """

    def __init__(self, socket: str, base_index: int) -> None:
        assert _TMUX is not None  # guarded by the module-level skipif
        self._tmux = _TMUX
        self._socket = socket
        fd, self._config = tempfile.mkstemp(prefix="aw-tmux-conf-", suffix=".conf")
        with os.fdopen(fd, "w") as handle:
            handle.write(f"set-option -g base-index {base_index}\n")
            handle.write(f"set-option -g pane-base-index {base_index}\n")
        self._started = False

    # -- process plumbing ---------------------------------------------------

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [self._tmux, "-L", self._socket]
        if not self._started:
            # -f is honored only at server start; pass it on the first call.
            cmd += ["-f", self._config]
        cmd += args
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT_SECONDS)
        self._started = True
        if check and result.returncode != 0:
            raise AssertionError(f"tmux {args} failed: rc={result.returncode} stderr={result.stderr!r}")
        return result

    def kill_server(self) -> None:
        subprocess.run(
            [self._tmux, "-L", self._socket, "kill-server"],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        with contextlib.suppress(OSError):
            os.unlink(self._config)

    # -- operations (mirror TmuxModel's method names) -----------------------

    def new_session(self, session: str, window: str) -> None:
        self._run(["new-session", "-d", "-s", session, "-n", window, HOLD])

    def new_window(self, session: str, window: str) -> None:
        self._run(["new-window", "-t", session, "-n", window, HOLD])

    def split_window(self, session: str, window: str) -> str:
        result = self._run(["split-window", "-t", f"{session}:{window}", "-P", "-F", "#{pane_id}", HOLD])
        return result.stdout.strip()

    def set_tag(self, pane_id: str, tag: int) -> None:
        self._run(["set-option", "-p", "-t", pane_id, SHELL_INDEX_OPTION, str(tag)])

    def kill_pane_by_id(self, pane_id: str) -> None:
        self._run(["kill-pane", "-t", pane_id])

    def kill_window(self, session: str, window: str) -> None:
        self._run(["kill-window", "-t", f"{session}:{window}"])

    def swap_windows(self, session: str, index_a: int, index_b: int) -> None:
        self._run(["swap-window", "-s", f"{session}:{index_a}", "-t", f"{session}:{index_b}"])

    # -- queries (mirror TmuxModel) -----------------------------------------

    def has_session(self, session: str) -> bool:
        # A destroyed last session also stops the server; treat "no server" as
        # "no session", exactly what a real caller's has-session would see.
        return self._run(["has-session", "-t", session], check=False).returncode == 0

    def windows_with_index(self, session: str) -> list[tuple[int, str]]:
        result = self._run(["list-windows", "-t", session, "-F", "#{window_index}|#{window_name}"], check=False)
        if result.returncode != 0:
            return []
        rows: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            idx, _, name = line.partition("|")
            rows.append((int(idx), name))
        rows.sort(key=lambda row: row[0])
        return rows

    def pane_rows(self, session: str, window: str) -> list[tuple[str, int, int | None]] | None:
        fmt = "#{pane_id}|#{pane_index}|#{" + SHELL_INDEX_OPTION + "}"
        result = self._run(["list-panes", "-t", f"{session}:{window}", "-F", fmt], check=False)
        if result.returncode != 0:
            return None
        rows: list[tuple[str, int, int | None]] = []
        for line in result.stdout.splitlines():
            pane_id, index_str, tag_str = line.split("|", 2)
            tag = int(tag_str) if tag_str != "" else None
            rows.append((pane_id, int(index_str), tag))
        rows.sort(key=lambda row: row[1])
        return rows


# Observable structure, ids stripped: (has_session, windows, panes-per-window).
# ``windows`` is ``((index, name), ...)``; ``panes`` is
# ``((index, name, ((pane_index, tag), ...)), ...)``.
Snapshot = tuple[bool, tuple[tuple[int, str], ...], tuple[tuple[int, str, tuple[tuple[int, int | None], ...]], ...]]


def _snapshot(tmux: _Tmux, session: str) -> Snapshot:
    if not tmux.has_session(session):
        return (False, (), ())
    windows = tuple(tmux.windows_with_index(session))
    panes = tuple(
        (
            idx,
            name,
            tuple((pane_index, tag) for _pane_id, pane_index, tag in (tmux.pane_rows(session, name) or [])),
        )
        for idx, name in windows
    )
    return (True, windows, panes)


def _lowest_untagged_pane_id(tmux: _Tmux, session: str, window: str) -> str:
    """The pane id of the window's lowest-indexed untagged pane (the
    session-attach pane). Used to reproduce the real bug shape: killing that
    pane and watching a tagged shell compact down into its slot."""
    rows = tmux.pane_rows(session, window)
    assert rows is not None
    for pane_id, _index, tag in rows:
        if tag is None:
            return pane_id
    raise AssertionError(f"no untagged pane in {session}:{window}")


class Pair:
    """A model and a real server driven in lockstep.

    Each mutator runs the same operation on both sides; ``agree`` snapshots
    both and asserts identical observable structure. Operations that need a
    pane id resolve it per-side (the two allocate different ids for the same
    structural pane), so the caller never handles a raw id."""

    def __init__(self, base_index: int, real: RealTmux) -> None:
        self.base_index = base_index
        self.model = TmuxModel(pane_base_index=base_index, window_base_index=base_index)
        self.real = real

    def new_session(self, window: str) -> None:
        assert self.model.new_session(SESSION, window)
        self.real.new_session(SESSION, window)

    def new_window(self, window: str) -> None:
        assert self.model.new_window(SESSION, window) is not None
        self.real.new_window(SESSION, window)

    def split_and_tag(self, window: str, tag: int) -> None:
        model_pane = self.model.split_window(SESSION, window)
        assert model_pane is not None
        assert self.model.set_tag(model_pane, tag)
        real_pane = self.real.split_window(SESSION, window)
        self.real.set_tag(real_pane, tag)

    def kill_window(self, window: str) -> None:
        assert self.model.kill_window(SESSION, window)
        self.real.kill_window(SESSION, window)

    def swap_windows(self, index_a: int, index_b: int) -> None:
        assert self.model.swap_windows(SESSION, index_a, index_b)
        self.real.swap_windows(SESSION, index_a, index_b)

    def kill_lowest_untagged_pane(self, window: str) -> None:
        model_pane = _lowest_untagged_pane_id(self.model, SESSION, window)
        real_pane = _lowest_untagged_pane_id(self.real, SESSION, window)
        assert self.model.kill_pane_by_id(model_pane)
        self.real.kill_pane_by_id(real_pane)

    def agree(self) -> None:
        model_snapshot = _snapshot(self.model, SESSION)
        real_snapshot = _snapshot(self.real, SESSION)
        assert model_snapshot == real_snapshot, f"model {model_snapshot!r} != real {real_snapshot!r}"


@pytest.fixture
def pair(request: pytest.FixtureRequest) -> Iterator[Pair]:
    base_index: int = request.param
    socket = f"aw-conf-{os.getpid()}-{base_index}"
    real = RealTmux(socket, base_index)
    try:
        yield Pair(base_index, real)
    finally:
        real.kill_server()


@pytest.mark.parametrize("pair", [0, 1], indirect=True, ids=["base0", "base1"])
def test_conformance_window_and_pane_lifecycle(pair: Pair) -> None:
    """The model matches real tmux across the operation classes that carry the
    bug: split + tag, gapped window indices, a swap across those gapped indices
    (the operation _reorder_session_windows performs), gap-filling new windows,
    killing the untagged attach pane so a shell compacts into its slot, and
    killing the last window to destroy the session."""
    base = pair.base_index
    # A session with one window and its untagged attach pane.
    pair.new_session("a")
    pair.agree()

    # split-window + tag: window "a" now holds the attach pane plus a shell.
    pair.split_and_tag("a", 0)
    pair.agree()

    # Two more windows. With base-index 1 these are indices 2 and 3.
    pair.new_window("b")
    pair.agree()
    pair.new_window("c")
    pair.agree()

    # Kill the MIDDLE window: real tmux (renumber-windows off) leaves a gap at
    # b's slot rather than renumbering c down. This is finding 2: the model
    # must leave the same gap, not a contiguous relabel.
    pair.kill_window("b")
    pair.agree()

    # Swap across the gap while it is still open: "a" sits at base and "c" at
    # base+2, with base+1 empty. This is the non-contiguous swap
    # _reorder_session_windows issues on live, gapped tmux indices, the exact
    # case that motivates the window-index-slot model. After the swap "c" must
    # hold base and "a" hold base+2, with base+1 still a gap, on both sides.
    pair.swap_windows(base, base + 2)
    pair.agree()

    # A new window fills the lowest free slot (b's old index), not the tail.
    pair.new_window("d")
    pair.agree()

    # The real bug shape: kill the untagged attach pane in "a"; the tagged
    # shell must compact down into the window's lowest pane index, under both
    # pane-base indices.
    pair.kill_lowest_untagged_pane("a")
    pair.agree()

    # Kill the remaining windows. The final kill takes the session's last
    # window, which tmux turns into destroying the session itself.
    for window in ("a", "c", "d"):
        pair.kill_window(window)
        pair.agree()
    assert not pair.model.has_session(SESSION)
    assert not pair.real.has_session(SESSION)


@pytest.mark.parametrize("pair", [0, 1], indirect=True, ids=["base0", "base1"])
def test_conformance_attach_pane_kill_compacts_shell_into_lowest_slot(pair: Pair) -> None:
    """Focused reproduction of the bug shape in isolation: a window with the
    untagged attach pane at the lowest index and one tagged shell above it;
    kill the attach pane by id and the shell must become the lowest-indexed
    pane on both sides, tag preserved."""
    pair.new_session("a")
    pair.split_and_tag("a", 0)
    pair.agree()

    pair.kill_lowest_untagged_pane("a")
    pair.agree()

    # The surviving pane is a single tagged shell at the base pane index.
    rows = pair.model.pane_rows(SESSION, "a")
    assert rows is not None
    assert [(index, tag) for _pane_id, index, tag in rows] == [(pair.model.pane_base_index, 0)]
