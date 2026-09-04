"""Tmux geometry helpers for named consoles.

Carved out of ``multi_console.py`` to keep that file focused on the
public API (create_console, attach_console, add_sessions, etc.) and DB
orchestration. The functions here all operate on a live tmux target +
quoted console/window names; none of them take a Database or Config
instance as an argument (the module does import the
``AW_SESSION_VERTICAL_LAYOUT`` constant from config, but it doesn't
need a runtime Config).

Two clusters live here:

1. **Layout application** -- map a layout-name string (tmux preset or
   the agentworks-specific ``aw-session-vertical``) into the actual
   ``tmux select-layout`` calls. Includes the custom layout-string
   builder + tmux's 16-bit checksum.

2. **Pane / window reordering** -- ``_reorder_shell_panes`` keeps
   shell panes in their tagged config order within one window;
   ``_reorder_session_windows`` permutes session windows to match a
   DB-resolved order. Both rely on tmux's swap primitives and track
   index state in memory to avoid extra round trips.

Everything in this module is best-effort. A failed tmux call is
silently swallowed unless it's recoverable only by a full ``console
restart``, in which case the helper emits ``output.warn``
with that hint.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT

if TYPE_CHECKING:
    from agentworks.transports import Transport


# Tmux pane-option key used to tag a shell pane with its config index.
# Defined here because the layout helpers rely on it; re-exported by
# ``multi_console`` for backward compatibility with existing imports.
SHELL_INDEX_OPTION = "@agentworks-shell-index"


# -- Layout application ----------------------------------------------------


def _apply_layout(target: Transport, q_con: str, q_win: str, layout: str) -> bool:
    """Apply *layout* to a single console window.

    Tmux preset names (`tiled`, `main-vertical`, etc.) go through
    `select-layout` as-is. The agentworks-specific ``aw-session-vertical``
    has no tmux preset equivalent: it stacks all panes vertically and
    sizes the session pane (the lowest-indexed pane) to the top 50%, with
    shell panes sharing the bottom 50% in equal-height rows. We build a
    custom tmux layout string with explicit per-pane heights (the
    `select-layout even-vertical` + `resize-pane` approach can't give
    pixel-equal shell rows because tmux's resize delta only affects a
    single neighbor at a time).

    Best-effort; the caller's surrounding code doesn't `check=True` on
    layout failure today and we preserve that contract.
    """
    if layout == AW_SESSION_VERTICAL_LAYOUT:
        return _apply_aw_session_vertical_layout(target, q_con, q_win)
    return target.run(
        f"tmux select-layout -t {q_con}:{q_win} {shlex.quote(layout)}",
        check=False,
    ).ok


def _apply_aw_session_vertical_layout(target: Transport, q_con: str, q_win: str) -> bool:
    """Build and apply a hand-computed tmux layout string for
    ``aw-session-vertical``. Two transport round trips through the
    ``Transport`` abstraction (one ``target.run`` chains two tmux
    commands with ``&&`` to gather the query; the second applies the
    computed layout).

    A failed query is quiet (the downstream select-layout would warn on
    its own anyway). A single-pane window is a silent no-op -- a session
    member with zero configured shells legitimately has nothing to lay
    out and shouldn't trigger a warning. Genuine parse failure or
    too-small geometry gets a warning so an operator who sees a wrong
    layout has a breadcrumb.
    """
    query = target.run(
        f"tmux display-message -t {q_con}:{q_win} -p "
        f"'#{{window_width}}x#{{window_height}}' && "
        f"tmux list-panes -t {q_con}:{q_win} -F '#{{pane_index}} #{{pane_id}}'",
        check=False,
    )
    if not query.ok:
        return False
    pane_count = _count_panes_in_query_output(query.stdout)
    if pane_count is not None and pane_count <= 1:
        # Single-pane window: nothing to lay out, not an error.
        return True
    layout_string = _build_aw_session_vertical_layout_string(query.stdout)
    if layout_string is None:
        output.warn(
            f"could not build aw-session-vertical layout for "
            f"{q_con}:{q_win} (window too small or unparseable tmux "
            f"output); tmux will keep its current layout"
        )
        return False
    return target.run(
        f"tmux select-layout -t {q_con}:{q_win} {shlex.quote(layout_string)}",
        check=False,
    ).ok


def _count_panes_in_query_output(query_output: str) -> int | None:
    """Return the pane count reported by the query (lines after the
    leading WxH line), or None if the output is malformed enough that
    we can't tell. Lets the caller distinguish "single-pane, no work
    needed" from "broken query".
    """
    lines = query_output.strip().splitlines()
    if len(lines) < 1:
        return None
    return len(lines) - 1


def _build_aw_session_vertical_layout_string(query_output: str) -> str | None:
    """Build a tmux custom layout string for the aw-session-vertical layout.

    Input format (output of the query in _apply_aw_session_vertical_layout):
        WxH                  (first line: window width x height)
        pane_index pane_id   (one line per pane, in tmux index order)

    Output: a complete tmux layout string `<checksum>,WxH,0,0[children]`
    where children are leaf nodes laid out vertically:

      - the lowest-indexed pane (session) gets the top H/2 lines
      - the remaining panes (shells) share the remaining (H/2 - 1) lines
        evenly, accounting for 1-line borders between each pair of
        adjacent panes

    Pane indices normally start at 0, but the console tmux server inherits
    the admin user's ~/.tmux.conf, so `pane-base-index 1` makes them start
    at 1. Only the ordering by index matters here (the session pane is the
    lowest), not the absolute value, so both bases render identically.

    The top-level node is hard-coded as a vertical split (``[...]``); if a
    horizontal variant is ever wanted, switch to ``{...}`` and recompute
    geometry along the width axis.

    Returns None on any parse failure or when the window is too small to
    hold the layout (rare; tmux itself would refuse the layout anyway).
    """
    lines = query_output.strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        w_str, h_str = lines[0].split("x")
        W = int(w_str)
        H = int(h_str)
    except ValueError:
        return None
    # Don't trust tmux to emit list-panes output in pane_index order: parse
    # the index, sort by it, then take pane_ids in index order. Also require
    # the index sequence to be contiguous starting from the window's lowest
    # live pane index (0 normally, 1 under `pane-base-index 1`). A gap means
    # we can't tell which pane belongs in which slot, so we'd risk putting a
    # shell into the session slot and rendering the layout wrong.
    indexed: list[tuple[int, str]] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        indexed.append((idx, parts[1].removeprefix("%")))
    if not indexed:
        return None
    indexed.sort(key=lambda p: p[0])
    base_pidx = indexed[0][0]
    if [idx for idx, _ in indexed] != list(range(base_pidx, base_pidx + len(indexed))):
        # Non-contiguous indices: can't build a sensible layout without
        # knowing which pane belongs in which slot.
        return None
    pane_ids = [pid for _, pid in indexed]
    n = len(pane_ids)
    if n == 1:
        # Just the session pane; nothing to lay out.
        return None
    # Session takes the top H//2 lines. One border between top and
    # bottom area; (n-2) borders between adjacent shell panes inside
    # the bottom area. Shell heights are equal, with any leftover line
    # given to the last shell so total reconciles to H exactly.
    h_top = H // 2
    bottom_panes_only = H - h_top - 1 - (n - 2)
    if bottom_panes_only <= 0:
        return None
    n_shells = n - 1
    h_shell = bottom_panes_only // n_shells
    leftover = bottom_panes_only - h_shell * n_shells
    if h_shell <= 0:
        return None
    parts = [f"{W}x{h_top},0,0,{pane_ids[0]}"]
    y = h_top + 1
    for i in range(1, n):
        this_h = h_shell + (leftover if i == n - 1 else 0)
        parts.append(f"{W}x{this_h},0,{y},{pane_ids[i]}")
        y += this_h + 1
    body = f"{W}x{H},0,0[{','.join(parts)}]"
    csum = _tmux_layout_checksum(body)
    return f"{csum:04x},{body}"


def _tmux_layout_checksum(s: str) -> int:
    """16-bit rotating-add checksum that prefixes a tmux layout string.

    Replicates the algorithm in tmux's ``layout-custom.c`` so layout
    strings we construct here are accepted by ``tmux select-layout``
    without modification.
    """
    csum = 0
    for ch in s:
        csum = ((csum >> 1) + ((csum & 1) << 15)) & 0xFFFF
        csum = (csum + ord(ch)) & 0xFFFF
    return csum


def _focus_session_pane(target: Transport, q_con: str, q_win: str, base_pidx: int) -> bool:
    """Move tmux's active pane to the session pane of a window.

    Called after building or repairing a window so the operator lands on
    the session output, not the most-recently-created shell pane (which
    is whatever tmux selected after the last `split-window`).

    The session pane is the window's lowest-indexed pane. That is index 0
    normally, but the console tmux server inherits the admin user's
    ~/.tmux.conf, so `pane-base-index 1` makes it index 1 (and then no pane
    ever reports index 0, so a literal `.0` target would fail). Callers pass
    the base index they already know: the repair paths have it from the pane
    listing, and the build path captures it from the session pane created by
    `tmux new-window`.
    """
    return target.run(f"tmux select-pane -t {q_con}:{q_win}.{base_pidx}", check=False).ok


# -- Pane / window reordering ----------------------------------------------


def _list_panes_with_tags(target: Transport, q_con: str, q_win: str) -> list[tuple[str, int, int | None]] | None:
    """Return every live pane for a console window as (pane_id, pane_index,
    config_index_or_None), including the session pane.

    The session pane is created by ``tmux new-window`` and left untagged, so it
    comes back with config_index None; shell panes created by
    ``_split_shell_pane`` carry an ``@agentworks-shell-index`` tag. That is what
    lets ``restore_session`` tell "the session-attach pane is still there" from
    "the operator killed it and tmux renumbered a shell into its slot".

    Returns None if the tmux query failed.
    """
    res = target.run(
        f"tmux list-panes -t {q_con}:{q_win} -F '#{{pane_id}}|#{{pane_index}}|#{{{SHELL_INDEX_OPTION}}}'",
        check=False,
    )
    if not res.ok:
        return None
    panes: list[tuple[str, int, int | None]] = []
    for line in res.stdout.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        pid, pidx_s, cidx_s = parts
        try:
            pidx = int(pidx_s)
        except ValueError:
            continue
        cidx: int | None
        if cidx_s:
            try:
                cidx = int(cidx_s)
            except ValueError:
                cidx = None
        else:
            cidx = None
        panes.append((pid, pidx, cidx))
    return panes


def _reorder_shell_panes(target: Transport, q_con: str, q_win: str, configured_count: int) -> None:
    """Reorder shell panes so the pane tagged @agentworks-shell-index N sits
    one slot after the session pane plus N. The session pane is the
    lowest-indexed pane in the window (index 0 normally, index 1 when the
    inherited ~/.tmux.conf sets `pane-base-index 1`); shell panes are all the
    rest.

    One tmux list-panes round trip up front, then we track positions in
    memory across swaps: pane_ids are stable, so after each `swap-pane` we
    just exchange the pane_index values of the two affected entries in
    our local map. Best-effort: if a swap fails, we keep going and let
    select-layout handle the geometry.
    """
    all_panes = _list_panes_with_tags(target, q_con, q_win)
    if not all_panes:
        # Failed query, or a window with no panes we could parse: either way
        # there is nothing we can reorder safely.
        return
    base_pidx = min(pidx for _pid, pidx, _cidx in all_panes)
    # The session pane is not part of the configured shell list.
    panes = [p for p in all_panes if p[1] != base_pidx]
    # pane_index by pane_id; mutated as we issue swaps so the next iteration
    # sees the current layout without another SSH round trip.
    pidx_by_pid: dict[str, int] = {pid: pidx for pid, pidx, _cidx in panes}
    pid_by_cidx: dict[int, str] = {cidx: pid for pid, _pidx, cidx in panes if cidx is not None}

    for target_cidx in range(configured_count):
        target_pidx = base_pidx + 1 + target_cidx
        src_pid = pid_by_cidx.get(target_cidx)
        if src_pid is None:
            continue
        src_pidx = pidx_by_pid[src_pid]
        if src_pidx == target_pidx:
            continue
        # Find the pane currently sitting at target_pidx so we can update its
        # in-memory pane_index after the swap. There must be one (every pane
        # after the session pane is a shell pane by construction).
        displaced_pid = next(
            (pid for pid, pidx in pidx_by_pid.items() if pidx == target_pidx),
            None,
        )
        res = target.run(
            f"tmux swap-pane -s {shlex.quote(src_pid)} -t {q_con}:{q_win}.{target_pidx}",
            check=False,
        )
        # Only mirror the swap into the local map on success; a failed swap-pane
        # leaves tmux state unchanged, so the previous mapping is still correct.
        # Compounding stale state into subsequent iterations would target the
        # wrong panes and could scramble order further.
        if res.ok:
            pidx_by_pid[src_pid] = target_pidx
            if displaced_pid is not None:
                pidx_by_pid[displaced_pid] = src_pidx


def _reorder_session_windows(
    target: Transport,
    *,
    console_name: str,
    ordered_session_windows: list[str],
) -> None:
    """Reorder session windows in a live console to match *ordered_session_windows*.

    Walks the desired order and issues ``tmux swap-window`` for each slot
    that's out of place, tracking window indices in memory across swaps so
    we never need a second list-windows round trip. Best-effort: a failed
    swap doesn't abort the rest, and the local map is only mirrored on
    success so subsequent iterations target the right windows.

    Permutable slots are derived positively from the session set: only
    windows whose names appear in *ordered_session_windows* are candidates,
    and the slots are taken in ascending tmux index order. Everything else
    (the ``--admin--`` window, operator-created strays from a manual
    ``tmux new-window``, anything that was renamed) stays put. The sentinel
    name for the admin-shell window is rejected by validate_name, so no
    real session name can ever collide with it.
    """
    # Lazy import to avoid an import cycle with multi_console (which itself
    # imports symbols from this module).
    from agentworks.sessions.multi_console import tmux_session_name
    from agentworks.sessions.tmux import exact_tmux_target

    q_con = exact_tmux_target(tmux_session_name(console_name))
    res = target.run(
        f"tmux list-windows -t {q_con} -F '#{{window_index}}|#{{window_name}}'",
        check=False,
    )
    if not res.ok:
        return
    pairs: list[tuple[int, str]] = []
    for line in res.stdout.strip().splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        try:
            pairs.append((int(parts[0]), parts[1]))
        except ValueError:
            continue
    if not pairs:
        return
    pairs.sort(key=lambda p: p[0])
    desired_set = set(ordered_session_windows)

    # Duplicate window names among the session set break the swap-by-name
    # logic (the dict below would silently retain only the last index for
    # each name and we'd target the wrong window). tmux allows duplicates
    # but we can't disambiguate, so bail with a recovery hint rather than
    # scramble the layout. The DB is already updated; restart materializes
    # the new order from a clean slate.
    name_counts: dict[str, int] = {}
    for _idx, name in pairs:
        name_counts[name] = name_counts.get(name, 0) + 1
    duplicated = sorted(n for n, c in name_counts.items() if c > 1 and n in desired_set)
    if duplicated:
        q_console = shlex.quote(console_name)
        output.warn(
            f"console '{console_name}' has duplicate window name(s) "
            f"({', '.join(duplicated)}); cannot reorder live tmux safely. "
            f"DB order was updated; run "
            f"`agw console restart {q_console}` to rebuild "
            f"tmux from DB state."
        )
        return

    session_slots = [idx for idx, name in pairs if name in desired_set]
    widx_by_name: dict[str, int] = {name: idx for idx, name in pairs}
    name_by_widx: dict[int, str] = {idx: name for idx, name in pairs}

    # Filter the desired order down to windows actually live in tmux. When
    # the operator has manually killed a session window (or tmux hasn't
    # caught up to the DB yet), the surviving windows compact toward the
    # front rather than each holding their original positional index --
    # otherwise a missing entry at desired[i] would leave desired[i+1]
    # stranded at slot i+1 with no member at slot i.
    present_desired = [n for n in ordered_session_windows if n in widx_by_name]

    for k, desired_name in enumerate(present_desired):
        if k >= len(session_slots):
            # Defensive: session_slots and present_desired should be the
            # same length by construction (both filter on desired_set ∩
            # live-names). If they ever diverge, stop rather than risk a
            # bad swap. A console restart will reconcile.
            break
        target_idx = session_slots[k]
        src_idx = widx_by_name[desired_name]
        if src_idx == target_idx:
            continue
        displaced_name = name_by_widx[target_idx]
        swap = target.run(
            f"tmux swap-window -s {q_con}:{src_idx} -t {q_con}:{target_idx}",
            check=False,
        )
        if swap.ok:
            widx_by_name[desired_name] = target_idx
            widx_by_name[displaced_name] = src_idx
            name_by_widx[target_idx] = desired_name
            name_by_widx[src_idx] = displaced_name
