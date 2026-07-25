"""Self-tests for the stateful tmux model (``tests/_tmux_model.py``).

These prove the tmux physics the model exists to reproduce, in isolation
from the console layer: pane compaction/renumbering after a kill, the
last-window-kill that destroys a session, session/window/pane lifecycle
bookkeeping, and pane tagging through ``set-option`` / ``list-panes``.
Both surfaces are exercised: the direct mutator methods (which the
console-layer consequence tests use to build scenarios) and
``dispatch()`` (which parses the exact command strings the console layer
emits).
"""

from __future__ import annotations

import pytest

from tests._tmux_model import SHELL_INDEX_OPTION, TmuxModel

CON = "aw-console-con"


# -- pane compaction / renumbering -----------------------------------------


@pytest.mark.parametrize("base", [0, 1])
def test_killing_a_pane_renumbers_survivors_contiguously(base: int) -> None:
    """Pane indices are positional: killing a middle pane compacts the rest
    down with no gap, honoring pane_base_index."""
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=(None, 0, 1, 2))
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [pidx for _pid, pidx, _tag in rows] == [base, base + 1, base + 2, base + 3]

    # Kill the pane at base+1 (config-index-0 shell); survivors renumber.
    assert model.kill_pane(CON, "a", base + 1)
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [pidx for _pid, pidx, _tag in rows] == [base, base + 1, base + 2]
    # The tags that survived kept their identity but moved down a slot.
    assert [tag for _pid, _pidx, tag in rows] == [None, 1, 2]


@pytest.mark.parametrize("base", [0, 1])
def test_killing_the_session_attach_pane_compacts_a_shell_into_the_lowest_slot(base: int) -> None:
    """The mechanism at the heart of the original bug: the untagged session
    pane sits in the lowest slot; killing it leaves a tagged shell as the
    window's lowest-indexed pane."""
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=(None, 0))
    assert model.kill_pane(CON, "a", base)  # kill the untagged attach pane

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    # A single shell pane now occupies the lowest slot and it is tagged.
    assert len(rows) == 1
    _pid, pidx, tag = rows[0]
    assert pidx == base
    assert tag == 0


@pytest.mark.parametrize("base", [0, 1])
def test_killing_the_untagged_pane_by_id_compacts_a_shell_into_the_lowest_slot(base: int) -> None:
    """Kill-by-``%id`` must honor pane_base_index. Regression for the
    double-subtracted base in kill_pane_by_id: under base 1, killing the
    untagged attach pane by its id used to no-op (the round-tripped 0-based
    position went negative), leaving both panes alive. The surviving shell
    must compact into the window's lowest slot, exactly as kill-by-index."""
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=(None, 0))
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    attach_pane_id = rows[0][0]  # the untagged session-attach pane at slot base

    assert model.kill_pane_by_id(attach_pane_id)

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert len(rows) == 1
    _pid, pidx, tag = rows[0]
    assert pidx == base  # the shell compacted down into the lowest slot
    assert tag == 0


@pytest.mark.parametrize("base", [0, 1])
def test_killing_the_untagged_pane_by_id_via_dispatch(base: int) -> None:
    """The same fix reached through the command string the console layer
    emits (``kill-pane -t %id``), not just the direct mutator."""
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=(None, 0))
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    attach_pane_id = rows[0][0]

    assert model.dispatch(f"tmux kill-pane -t {attach_pane_id}").ok

    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [(pidx, tag) for _pid, pidx, tag in rows] == [(base, 0)]


# -- window index slots (persistent, gapped, lowest-free-fill) -------------


@pytest.mark.parametrize("base", [0, 1])
def test_killing_a_middle_window_leaves_a_gap_and_new_windows_fill_it(base: int) -> None:
    """Window indices are persistent slots (renumber-windows off): killing a
    non-last window leaves a gap, and the next new window fills the lowest
    free slot rather than appending at the tail."""
    model = TmuxModel(window_base_index=base)
    model.new_session(CON, "w0")
    model.new_window(CON, "w1")
    model.new_window(CON, "w2")
    assert model.windows_with_index(CON) == [(base, "w0"), (base + 1, "w1"), (base + 2, "w2")]

    # Kill the middle window: its slot (base+1) becomes a gap.
    assert model.kill_window(CON, "w1")
    assert model.windows_with_index(CON) == [(base, "w0"), (base + 2, "w2")]

    # The next new window fills the gap at base+1, not the tail.
    model.new_window(CON, "w3")
    assert model.windows_with_index(CON) == [(base, "w0"), (base + 1, "w3"), (base + 2, "w2")]


@pytest.mark.parametrize("base", [0, 1])
def test_swap_windows_across_a_gap_exchanges_slots_and_keeps_the_gap(base: int) -> None:
    """swap-window on GAPPED, non-contiguous indices (the case
    _reorder_session_windows faces) exchanges the two windows' slots and
    leaves the gap between them untouched."""
    model = TmuxModel(window_base_index=base)
    model.new_session(CON, "w0")  # slot base
    model.new_window(CON, "w1")  # slot base+1
    model.new_window(CON, "w2")  # slot base+2
    assert model.kill_window(CON, "w1")  # gap at base+1: w0(base), w2(base+2)

    # Swap the two occupied, non-adjacent slots across the gap.
    assert model.swap_windows(CON, base, base + 2)
    assert model.windows_with_index(CON) == [(base, "w2"), (base + 2, "w0")]

    # And through the command string the reorder path emits.
    assert model.dispatch(f"tmux swap-window -s {CON}:{base} -t {CON}:{base + 2}").ok
    assert model.windows_with_index(CON) == [(base, "w0"), (base + 2, "w2")]


# -- last-window-kill destroys the session ---------------------------------


def test_killing_the_last_window_destroys_the_session() -> None:
    """tmux requires at least one window, so killing a session's only window
    removes the session; a later has-session and new-window both fail."""
    model = TmuxModel()
    model.new_session(CON, "a")
    assert model.has_session(CON)

    assert model.kill_window(CON, "a")
    assert not model.has_session(CON)
    # new-window against the now-dead session fails.
    assert model.new_window(CON, "b") is None
    assert not model.dispatch(f"tmux has-session -t {CON} 2>/dev/null").ok
    assert not model.dispatch(f"tmux new-window -t {CON} -n b -P -F '#{{pane_index}}'").ok


def test_killing_a_non_last_window_keeps_the_session_alive() -> None:
    """Only the last window's death takes the session; other kills do not."""
    model = TmuxModel()
    model.new_session(CON, "a")
    model.new_window(CON, "b")

    assert model.kill_window(CON, "a")
    assert model.has_session(CON)
    assert model.window_names(CON) == ["b"]


def test_killing_a_windows_last_pane_closes_the_window_and_then_the_session() -> None:
    """Killing a window's last pane closes the window, and if it was the last
    window that in turn destroys the session."""
    model = TmuxModel()
    model.seed_session(CON, "a", pane_tags=(None,))
    assert model.kill_pane(CON, "a", 0)
    assert not model.has_session(CON)


# -- session / window / pane lifecycle bookkeeping -------------------------


def test_new_session_makes_one_window_with_one_pane() -> None:
    model = TmuxModel(pane_base_index=1)
    assert model.new_session(CON, "a")
    assert model.window_names(CON) == ["a"]
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert len(rows) == 1
    assert rows[0][1] == 1  # honors pane_base_index
    assert rows[0][2] is None  # the session pane is untagged


def test_new_session_refuses_a_duplicate() -> None:
    model = TmuxModel()
    assert model.new_session(CON, "a")
    assert not model.new_session(CON, "a")
    assert not model.dispatch(f"tmux new-session -d -s {CON} -n a").ok


def test_new_window_reports_the_session_pane_index_via_dispatch() -> None:
    """``new-window -P -F '#{pane_index}'`` prints the new pane's index; under
    pane-base-index 1 that is 1, which is what focus targets."""
    model = TmuxModel(pane_base_index=1)
    model.new_session(CON, "a")
    res = model.dispatch(f"tmux new-window -t {CON} -n b -P -F '#{{pane_index}}'")
    assert res.ok
    assert res.stdout.strip() == "1"
    assert model.window_names(CON) == ["a", "b"]


def test_split_window_adds_a_pane_at_the_tail_and_returns_its_id() -> None:
    model = TmuxModel()
    model.seed_session(CON, "a", pane_tags=(None, 0))
    res = model.dispatch(f"tmux split-window -t {CON}:a -P -F '#{{pane_id}}'")
    assert res.ok
    new_id = res.stdout.strip()
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert len(rows) == 3
    # The new pane is last (highest index) and untagged until set-option runs.
    assert rows[-1][0] == new_id
    assert rows[-1][2] is None


def test_split_window_against_a_missing_window_fails() -> None:
    model = TmuxModel()
    model.new_session(CON, "a")
    assert not model.dispatch(f"tmux split-window -t {CON}:nope -P -F '#{{pane_id}}'").ok


# -- pane tagging via set-option / list-panes ------------------------------


def test_set_option_tags_a_pane_and_list_panes_reads_it_back() -> None:
    """The tagged listing format ``pane_id|pane_index|tag`` renders an empty
    tag string for the untagged pane and the integer tag for shell panes,
    exactly as _list_panes_with_tags parses it."""
    model = TmuxModel()
    model.seed_session(CON, "a", pane_tags=(None,))
    split = model.dispatch(f"tmux split-window -t {CON}:a -P -F '#{{pane_id}}'")
    new_id = split.stdout.strip()

    tag = model.dispatch(f"tmux set-option -p -t {new_id} {SHELL_INDEX_OPTION} 0")
    assert tag.ok

    listing = model.dispatch(f"tmux list-panes -t {CON}:a -F '#{{pane_id}}|#{{pane_index}}|#{{{SHELL_INDEX_OPTION}}}'")
    lines = listing.stdout.strip().splitlines()
    assert lines == ["%1|0|", f"{new_id}|1|0"]


def test_set_option_against_an_unknown_pane_fails() -> None:
    model = TmuxModel()
    model.seed_session(CON, "a", pane_tags=(None,))
    assert not model.dispatch(f"tmux set-option -p -t %999 {SHELL_INDEX_OPTION} 0").ok


def test_list_panes_against_a_missing_window_fails() -> None:
    model = TmuxModel()
    model.new_session(CON, "a")
    res = model.dispatch(f"tmux list-panes -t {CON}:gone -F '#{{pane_id}}|#{{pane_index}}|#{{{SHELL_INDEX_OPTION}}}'")
    assert not res.ok


def test_swap_pane_exchanges_pane_positions() -> None:
    """The reorder path swaps a tagged pane into its target slot; swapping two
    panes exchanges their positions (and therefore their indices)."""
    model = TmuxModel()
    model.seed_session(CON, "a", pane_tags=(None, 1, 0))
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    tag1_id = rows[1][0]  # pane tagged 1, currently at index 1
    # Swap it with the pane at index 2 (tagged 0) so tags land in order.
    assert model.swap_panes(CON, "a", tag1_id, 2)
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [tag for _pid, _pidx, tag in rows] == [None, 0, 1]


# -- non-tmux and unrecognized commands fall through -----------------------


def test_non_tmux_and_unmodeled_commands_return_default_ok() -> None:
    """The sudo preserve-env probe and cosmetic tmux commands (select-pane,
    select-layout) the model has no state for return default-OK, matching the
    stateless fake's fall-through."""
    model = TmuxModel()
    model.new_session(CON, "a")
    assert model.dispatch("env FOO=1 sudo -n --preserve-env=FOO -u me true").ok
    assert model.dispatch(f"tmux select-pane -t {CON}:a.0").ok
    assert model.dispatch(f"tmux select-layout -t {CON}:a tiled").ok
