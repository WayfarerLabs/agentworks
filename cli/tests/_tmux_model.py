"""An opt-in stateful tmux model for the named-console test fakes.

The stateless ``_FakeTarget`` in ``tests/conftest.py`` is a substring
map: a ``kill-window`` has no effect on a later ``list-windows`` /
``list-panes`` / ``has-session``, because ``run()`` just replays a
seeded response. That is exactly the blind spot a real console-destroying
bug slipped through once: code that did ``kill-window`` then ``new-window``
looked fine against the fake (the post-kill listing still returned the
seed, and the follow-up ``new-window`` still returned OK), while against
real tmux, killing a session's last window destroys the session and the
follow-up ``new-window`` fails.

``TmuxModel`` closes that gap. It tracks sessions, windows, and panes,
mutates them from the console layer's own tmux commands, and answers the
console layer's queries from that state. ``dispatch(command)`` parses the
tmux subcommand off a command string and routes it to the same mutator /
query methods that the model self-tests drive directly. A command the
model does not understand (or a non-tmux command such as the sudo
preserve-env probe) falls through to a default-OK ``_FakeResult``.

These tmux behaviors are modeled faithfully, because that is where the
bug class lives:

1. Pane compaction. Pane indices are positional (``pane_base_index`` plus
   list position), so killing a pane renumbers the survivors contiguously
   with no bookkeeping. Killing an untagged session-attach pane leaves a
   tagged shell as the window's lowest-indexed pane, the exact shape
   ``restore_session`` refuses to repair.
2. Window index slots. Unlike panes, window indices are persistent slots:
   tmux does not renumber windows on kill (``renumber-windows`` is off by
   default and the console server does not set it), so killing a non-last
   window leaves a gap and a bare ``new-window`` fills the lowest free slot
   rather than the tail. A ``new-window`` with an explicit ``-t
   <session>:<idx>`` lands at that exact slot instead (how ``add_sessions``
   appends past the last window), and an already-occupied index is refused.
   The model stores each window's assigned index instead of deriving it from
   list position, which matters because ``_reorder_session_windows`` reads
   live, possibly-gapped indices.
3. Last-window-kill destroys the session. tmux requires at least one
   window, so killing a session's last window removes the session; a
   later ``has-session`` then fails and a ``new-window`` against it fails.
4. Session / window / pane lifecycle. ``new-session`` makes a session with
   one window (one pane at ``pane_base_index``); ``new-window`` adds a
   window with one pane; ``split-window`` adds a pane to a window.

``tests/test_tmux_model_conformance.py`` pins this model to a real local
tmux server: it drives identical operation sequences through both and
asserts the observable structure agrees, so a hand-model lie shows up as a
conformance failure. Real tmux is the ground truth there.

Fidelity boundaries (deliberate simplifications the conformance test stays
within, rather than bugs):

- ``split_window`` always appends the new pane at the tail. Real tmux
  assigns a split pane's index from its spatial position in the layout
  tree; for the linear splits the console layer performs (splitting to add
  a shell) that position is the tail, so the model matches within that
  envelope but does not reproduce tmux's general spatial insertion.

Opt-in: a ``_FakeTarget`` built without a ``model`` behaves exactly as
before, so every existing stateless test is untouched.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from tests.conftest import _FakeResult

# Mirror of ``multi_console_layout.SHELL_INDEX_OPTION``. Duplicated here so
# the test model does not import production layout code just to name a
# format token; the string is a stable tmux pane-option key.
SHELL_INDEX_OPTION = "@agentworks-shell-index"


@dataclass
class Pane:
    """One tmux pane. ``tag`` is the ``@agentworks-shell-index`` value a
    shell pane carries, or None for the untagged session-attach pane. The
    pane's index is not stored: it is its position in the window's pane
    list plus the model's ``pane_base_index`` (see ``pane_rows``)."""

    pane_id: str
    tag: int | None = None


@dataclass
class Window:
    """One tmux window: an ordered list of panes plus its assigned index.

    The index is a stored slot, not a list position: unlike panes, tmux
    does not renumber windows on kill, so slots can be gapped (see the
    module docstring's window-index-slots note)."""

    name: str
    index: int
    panes: list[Pane] = field(default_factory=list)


@dataclass
class Session:
    """One tmux session: an ordered list of windows."""

    name: str
    windows: list[Window] = field(default_factory=list)


class TmuxModel:
    """Stateful stand-in for a tmux server.

    Sessions are keyed by their tmux session name (e.g. the
    ``aw-console-<console>`` string ``tmux_session_name`` produces), which
    is what appears in the ``-t`` targets of the commands the console layer
    emits. ``pane_base_index`` and ``window_base_index`` model the
    ``pane-base-index`` / ``base-index`` options the console tmux server
    inherits from the admin user's ``~/.tmux.conf``; every index the model
    reports honors them.
    """

    def __init__(self, *, pane_base_index: int = 0, window_base_index: int = 0) -> None:
        self.pane_base_index = pane_base_index
        self.window_base_index = window_base_index
        self._sessions: dict[str, Session] = {}
        self._pane_counter = 0

    # -- pane id allocation -------------------------------------------------

    def _alloc_pane_id(self) -> str:
        self._pane_counter += 1
        return f"%{self._pane_counter}"

    # -- scenario builders (test setup, not tmux commands) ------------------

    def seed_session(self, session: str, window_name: str, *, pane_tags: tuple[int | None, ...] = (None,)) -> Session:
        """Create a session with a single window whose panes carry the given
        tags (None for the untagged session pane, an int for a tagged shell
        pane). Raises if the session already exists, mirroring tmux's refusal
        to create a duplicate session."""
        if session in self._sessions:
            raise ValueError(f"session '{session}' already exists in the model")
        s = Session(session)
        self._sessions[session] = s
        self._add_window(s, window_name, pane_tags)
        return s

    def seed_window(self, session: str, window_name: str, *, pane_tags: tuple[int | None, ...] = (None,)) -> Window:
        """Add a window with the given pane tags to an existing session."""
        return self._add_window(self._sessions[session], window_name, pane_tags)

    def _add_window(self, s: Session, name: str, pane_tags: tuple[int | None, ...]) -> Window:
        """Add a window at the lowest free index (tmux's default placement)."""
        return self._add_window_at(s, name, None, pane_tags)

    def _add_window_at(self, s: Session, name: str, index: int | None, pane_tags: tuple[int | None, ...]) -> Window:
        """Add a window at ``index``, or the lowest free index when ``index`` is
        None (tmux's default placement)."""
        if index is None:
            index = self._next_window_index(s)
        panes = [Pane(self._alloc_pane_id(), tag) for tag in pane_tags]
        w = Window(name, index, panes)
        s.windows.append(w)
        # Keep windows in ascending index order, the order tmux lists them in.
        s.windows.sort(key=lambda win: win.index)
        return w

    def _next_window_index(self, s: Session) -> int:
        """The lowest free window index at or above ``window_base_index``.

        tmux (with ``renumber-windows`` off) assigns a new window the
        lowest unused slot, filling any gap a killed window left behind
        rather than always appending at the tail."""
        used = {w.index for w in s.windows}
        idx = self.window_base_index
        while idx in used:
            idx += 1
        return idx

    # -- tmux operation methods (also reached via dispatch) -----------------

    def new_session(self, session: str, window_name: str) -> bool:
        """Model ``tmux new-session``: a session with one window holding one
        untagged pane. Returns False if the session already exists."""
        if session in self._sessions:
            return False
        self.seed_session(session, window_name, pane_tags=(None,))
        return True

    def new_window(self, session: str, window_name: str, *, index: int | None = None) -> str | None:
        """Model ``tmux new-window``: add a window with one untagged pane.
        Returns the new pane's index string (for ``-P -F '#{pane_index}'``),
        or None on failure.

        With ``index`` None the window fills the lowest free slot (tmux's
        default ``-t <session>`` placement). An explicit ``index`` (tmux's
        ``-t <session>:<idx>``) lands the window at that exact slot, which is
        how ``add_sessions`` appends past the last window; real tmux refuses an
        already-occupied index without ``-k``, so the model returns None (no
        mutation) in that case. Also returns None if the session does not exist
        (the last-window-kill case)."""
        s = self._sessions.get(session)
        if s is None:
            return None
        if index is not None and any(w.index == index for w in s.windows):
            return None
        self._add_window_at(s, window_name, index, (None,))
        return str(self.pane_base_index)

    def split_window(self, session: str, window_name: str) -> str | None:
        """Model ``tmux split-window``: add an untagged pane at the tail of a
        window. Returns the new pane's id (for ``-P -F '#{pane_id}'``), or None
        if the window does not exist."""
        w = self.get_window(session, window_name)
        if w is None:
            return None
        pane = Pane(self._alloc_pane_id(), None)
        w.panes.append(pane)
        return pane.pane_id

    def set_tag(self, pane_id: str, tag: int) -> bool:
        """Model ``tmux set-option -p @agentworks-shell-index``: tag the pane
        with the given id. Returns False if no pane has that id."""
        pane = self._pane_by_id(pane_id)
        if pane is None:
            return False
        pane.tag = tag
        return True

    def kill_window(self, session: str, window_name: str) -> bool:
        """Model ``tmux kill-window``. Killing a session's last window
        destroys the session (tmux requires at least one window). Returns
        False if the window was not found."""
        s = self._sessions.get(session)
        if s is None:
            return False
        window = next((w for w in s.windows if w.name == window_name), None)
        if window is None:
            return False
        self._remove_window(s, window)
        return True

    def kill_pane(self, session: str, window_name: str, pane_index: int) -> bool:
        """Model ``tmux kill-pane`` by index. Remaining panes renumber
        contiguously (positional indices). Killing a window's last pane closes
        the window, which, if it was the last window, destroys the session."""
        w = self.get_window(session, window_name)
        if w is None:
            return False
        pos = pane_index - self.pane_base_index
        if pos < 0 or pos >= len(w.panes):
            return False
        del w.panes[pos]
        if not w.panes:
            self._remove_window(self._sessions[session], w)
        return True

    def kill_pane_by_id(self, pane_id: str) -> bool:
        """Model ``tmux kill-pane`` targeted by pane id.

        ``_index_of`` returns a 0-based list position, but ``kill_pane``
        expects a tmux pane index (``pane_base_index`` plus list position)
        and subtracts the base back off. Add the base here so the two agree;
        otherwise, under ``pane_base_index=1``, killing list-position 0 would
        no-op and killing position 1 would delete the wrong pane."""
        for session_name, s in self._sessions.items():
            for w in s.windows:
                for pane in w.panes:
                    if pane.pane_id == pane_id:
                        index = self.pane_base_index + self._index_of(w, pane)
                        return self.kill_pane(session_name, w.name, index)
        return False

    def kill_session(self, session: str) -> bool:
        """Model ``tmux kill-session``. Returns False if the session is gone."""
        return self._sessions.pop(session, None) is not None

    def swap_panes(self, session: str, window_name: str, pane_id: str, pane_index: int) -> bool:
        """Model ``tmux swap-pane -s <pane_id> -t <session>:<window>.<index>``:
        exchange the positions of the source pane and the pane at the target
        index within one window."""
        w = self.get_window(session, window_name)
        if w is None:
            return False
        src = next((i for i, p in enumerate(w.panes) if p.pane_id == pane_id), None)
        dst = pane_index - self.pane_base_index
        if src is None or dst < 0 or dst >= len(w.panes):
            return False
        w.panes[src], w.panes[dst] = w.panes[dst], w.panes[src]
        return True

    def swap_windows(self, session: str, index_a: int, index_b: int) -> bool:
        """Model ``tmux swap-window``: exchange the two windows occupying the
        given index slots. The windows trade slots (and therefore trade the
        indices tmux reports); no renumbering of any other window occurs."""
        s = self._sessions.get(session)
        if s is None:
            return False
        win_a = next((w for w in s.windows if w.index == index_a), None)
        win_b = next((w for w in s.windows if w.index == index_b), None)
        if win_a is None or win_b is None:
            return False
        win_a.index, win_b.index = win_b.index, win_a.index
        s.windows.sort(key=lambda win: win.index)
        return True

    # -- queries ------------------------------------------------------------

    def has_session(self, session: str) -> bool:
        return session in self._sessions

    def window_names(self, session: str) -> list[str]:
        s = self._sessions.get(session)
        return [w.name for w in s.windows] if s else []

    def windows_with_index(self, session: str) -> list[tuple[int, str]]:
        s = self._sessions.get(session)
        if s is None:
            return []
        # ``s.windows`` is kept sorted by index, so this is already ascending.
        return [(w.index, w.name) for w in s.windows]

    def get_window(self, session: str, window_name: str) -> Window | None:
        s = self._sessions.get(session)
        if s is None:
            return None
        return next((w for w in s.windows if w.name == window_name), None)

    def pane_rows(self, session: str, window_name: str) -> list[tuple[str, int, int | None]] | None:
        """Return ``(pane_id, pane_index, tag)`` for every pane in a window in
        order, or None if the window does not exist. Pane indices are
        positional and honor ``pane_base_index``."""
        w = self.get_window(session, window_name)
        if w is None:
            return None
        return [(p.pane_id, self.pane_base_index + i, p.tag) for i, p in enumerate(w.panes)]

    # -- internals ----------------------------------------------------------

    def _remove_window(self, s: Session, window: Window) -> None:
        s.windows.remove(window)
        if not s.windows:
            # tmux requires at least one window: the session dies with its last.
            self._sessions.pop(s.name, None)

    def _pane_by_id(self, pane_id: str) -> Pane | None:
        for s in self._sessions.values():
            for w in s.windows:
                for pane in w.panes:
                    if pane.pane_id == pane_id:
                        return pane
        return None

    @staticmethod
    def _index_of(window: Window, pane: Pane) -> int:
        return window.panes.index(pane)

    # -- command dispatch ---------------------------------------------------

    def dispatch(self, command: str) -> _FakeResult:
        """Parse a tmux command string and mutate / answer from state.

        Keyed off the subcommand token and stable ``-F`` substrings rather
        than full-string equality, so incidental changes to quoting or
        trailing arguments do not break dispatch. Anything the model does not
        recognize returns a default-OK result, matching the stateless fake's
        fall-through so mixed command streams (sudo probes, select-pane,
        resize) stay quiet."""
        stripped = command.strip()
        if not stripped.startswith("tmux"):
            return _FakeResult()
        # The aw-session-vertical layout query chains two tmux commands with
        # ``&&``; handle it before tokenizing the compound string.
        if "display-message" in stripped:
            return self._dispatch_layout_query(stripped)
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            return _FakeResult()
        if len(tokens) < 2:
            return _FakeResult()
        sub = tokens[1]
        handler = self._HANDLERS.get(sub)
        if handler is None:
            # select-pane, select-layout, resize-*, and anything else the
            # model has no state for: accept without recording an effect.
            return _FakeResult()
        return handler(self, tokens)

    def _dispatch_has_session(self, tokens: list[str]) -> _FakeResult:
        target = _opt(tokens, "-t") or ""
        session = target.split(":", 1)[0]
        if self.has_session(session):
            return _FakeResult(returncode=0)
        return _FakeResult(returncode=1, stderr=f"can't find session: {session}")

    def _dispatch_list_windows(self, tokens: list[str]) -> _FakeResult:
        session = (_opt(tokens, "-t") or "").split(":", 1)[0]
        if not self.has_session(session):
            return _FakeResult(returncode=1, stderr=f"can't find session: {session}")
        fmt = _opt(tokens, "-F") or ""
        if "window_index" in fmt and "window_name" in fmt:
            lines = [f"{idx}|{name}" for idx, name in self.windows_with_index(session)]
        elif "window_index" in fmt:
            # Index-only form (``-F '#{window_index}'``), used by
            # ``_last_window_index`` to find the tail slot for an append.
            lines = [str(idx) for idx, _name in self.windows_with_index(session)]
        else:
            lines = self.window_names(session)
        return _FakeResult(stdout=_join(lines))

    def _dispatch_list_panes(self, tokens: list[str]) -> _FakeResult:
        session, window, _pidx = _parse_target(_opt(tokens, "-t") or "")
        rows = self.pane_rows(session, window or "")
        if rows is None:
            return _FakeResult(returncode=1, stderr="can't find pane")
        fmt = _opt(tokens, "-F") or ""
        if SHELL_INDEX_OPTION in fmt:
            lines = [f"{pid}|{pidx}|{'' if tag is None else tag}" for pid, pidx, tag in rows]
        else:
            # ``#{pane_index} #{pane_id}`` (space-separated), the layout query form.
            lines = [f"{pidx} {pid}" for pid, pidx, _tag in rows]
        return _FakeResult(stdout=_join(lines))

    def _dispatch_new_session(self, tokens: list[str]) -> _FakeResult:
        session = _opt(tokens, "-s") or ""
        window_name = _opt(tokens, "-n") or ""
        if self.new_session(session, window_name):
            return _FakeResult(returncode=0)
        return _FakeResult(returncode=1, stderr=f"duplicate session: {session}")

    def _dispatch_new_window(self, tokens: list[str]) -> _FakeResult:
        session, index = _parse_window_index_target(_opt(tokens, "-t") or "")
        if not self.has_session(session):
            return _FakeResult(returncode=1, stderr=f"can't find session: {session}")
        window_name = _opt(tokens, "-n") or ""
        pane_index = self.new_window(session, window_name, index=index)
        if pane_index is None:
            # The session exists (checked above), so the only remaining failure
            # is an explicit target index that is already occupied (real tmux
            # refuses without -k).
            return _FakeResult(returncode=1, stderr=f"index in use: {index}")
        stdout = _join([pane_index]) if "-P" in tokens else ""
        return _FakeResult(stdout=stdout)

    def _dispatch_split_window(self, tokens: list[str]) -> _FakeResult:
        session, window, _pidx = _parse_target(_opt(tokens, "-t") or "")
        pane_id = self.split_window(session, window or "")
        if pane_id is None:
            return _FakeResult(returncode=1, stderr="can't find window")
        stdout = _join([pane_id]) if "-P" in tokens else ""
        return _FakeResult(stdout=stdout)

    def _dispatch_set_option(self, tokens: list[str]) -> _FakeResult:
        pane_id = _opt(tokens, "-t") or ""
        if SHELL_INDEX_OPTION not in tokens:
            return _FakeResult()
        value_pos = tokens.index(SHELL_INDEX_OPTION) + 1
        if value_pos >= len(tokens):
            return _FakeResult(returncode=1, stderr="set-option: missing value")
        try:
            tag = int(tokens[value_pos])
        except ValueError:
            return _FakeResult(returncode=1, stderr="set-option: non-integer tag")
        if self.set_tag(pane_id, tag):
            return _FakeResult(returncode=0)
        return _FakeResult(returncode=1, stderr=f"can't find pane: {pane_id}")

    def _dispatch_kill_window(self, tokens: list[str]) -> _FakeResult:
        session, window, _pidx = _parse_target(_opt(tokens, "-t") or "")
        if self.kill_window(session, window or ""):
            return _FakeResult(returncode=0)
        return _FakeResult(returncode=1, stderr="can't find window")

    def _dispatch_kill_pane(self, tokens: list[str]) -> _FakeResult:
        target = _opt(tokens, "-t") or ""
        if target.startswith("%"):
            ok = self.kill_pane_by_id(target)
        else:
            session, window, pidx = _parse_target(target)
            ok = pidx is not None and self.kill_pane(session, window or "", pidx)
        return _FakeResult(returncode=0 if ok else 1)

    def _dispatch_kill_session(self, tokens: list[str]) -> _FakeResult:
        session = (_opt(tokens, "-t") or "").split(":", 1)[0]
        return _FakeResult(returncode=0 if self.kill_session(session) else 1)

    def _dispatch_swap_pane(self, tokens: list[str]) -> _FakeResult:
        src_pid = _opt(tokens, "-s") or ""
        session, window, pidx = _parse_target(_opt(tokens, "-t") or "")
        ok = pidx is not None and self.swap_panes(session, window or "", src_pid, pidx)
        return _FakeResult(returncode=0 if ok else 1)

    def _dispatch_swap_window(self, tokens: list[str]) -> _FakeResult:
        src = _opt(tokens, "-s") or ""
        dst = _opt(tokens, "-t") or ""
        s_session, s_idx = _parse_window_index_target(src)
        d_session, d_idx = _parse_window_index_target(dst)
        if s_session != d_session or s_idx is None or d_idx is None:
            return _FakeResult(returncode=1)
        return _FakeResult(returncode=0 if self.swap_windows(s_session, s_idx, d_idx) else 1)

    def _dispatch_layout_query(self, command: str) -> _FakeResult:
        """Answer the aw-session-vertical geometry query
        (``display-message ... && list-panes -F '#{pane_index} #{pane_id}'``)
        with a plausible window size and the target window's pane rows."""
        target = _opt(shlex.split(command.split("&&")[0]), "-t") or ""
        session, window, _pidx = _parse_target(target)
        rows = self.pane_rows(session, window or "")
        if rows is None:
            return _FakeResult(returncode=1, stderr="can't find window")
        lines = ["80x36", *[f"{pidx} {pid}" for pid, pidx, _tag in rows]]
        return _FakeResult(stdout=_join(lines))

    _HANDLERS = {
        "has-session": _dispatch_has_session,
        "list-windows": _dispatch_list_windows,
        "list-panes": _dispatch_list_panes,
        "new-session": _dispatch_new_session,
        "new-window": _dispatch_new_window,
        "split-window": _dispatch_split_window,
        "set-option": _dispatch_set_option,
        "kill-window": _dispatch_kill_window,
        "kill-pane": _dispatch_kill_pane,
        "kill-session": _dispatch_kill_session,
        "swap-pane": _dispatch_swap_pane,
        "swap-window": _dispatch_swap_window,
    }


def _opt(tokens: list[str], flag: str) -> str | None:
    """Return the token following ``flag`` in ``tokens``, or None."""
    try:
        return tokens[tokens.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _join(lines: list[str]) -> str:
    """Join output lines the way tmux does: newline-separated with a trailing
    newline (empty when there are no lines). Callers strip before parsing, so
    the trailing newline is cosmetic but keeps the fake output faithful."""
    return "".join(f"{line}\n" for line in lines)


def _parse_target(target: str) -> tuple[str, str | None, int | None]:
    """Split a ``<session>[:<window>[.<pane_index>]]`` target. Window and
    session names are ``[a-z0-9_-]`` (validate_name forbids dots), so the
    final ``.`` unambiguously separates the pane index."""
    session, _, rest = target.partition(":")
    if not rest:
        return session, None, None
    if "." in rest:
        window, _, pane_str = rest.rpartition(".")
        try:
            return session, window, int(pane_str)
        except ValueError:
            return session, rest, None
    return session, rest, None


def _parse_window_index_target(target: str) -> tuple[str, int | None]:
    """Split a ``<session>:<window_index>`` swap-window target."""
    session, _, idx_str = target.partition(":")
    try:
        return session, int(idx_str)
    except ValueError:
        return session, None
