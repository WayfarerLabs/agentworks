"""Console / tmux layout constants for named-console session windows.

Moved out of ``agentworks.config`` so the ``sessions`` domain owns these
next to their runtime home (``agentworks.sessions.multi_console_layout``,
which builds the custom tmux layout string). ``agentworks.config``'s
``[named_console]`` loader imports them from here for validation.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

# Agentworks-specific layout: session pane (pane 0) takes the top 50% of
# the window, shell panes stack vertically in the bottom 50% with equal
# heights. tmux has no preset that matches this geometry, so apply-time
# builds a custom tmux layout string from the live window dimensions and
# pane IDs and feeds it to `tmux select-layout`. See
# `_apply_aw_session_vertical_layout` in sessions/multi_console_layout.py.
AW_SESSION_VERTICAL_LAYOUT: Final = "aw-session-vertical"

# Valid layouts for named-console session windows. All values besides
# AW_SESSION_VERTICAL_LAYOUT map 1:1 to tmux's built-in select-layout
# names so operators can apply the same value to a window via
# `tmux select-layout` on the fly.
#
# The TYPE is the authored list and the tuple is derived from it, not the
# other way round: a runtime tuple cannot be the source of a ``Literal``,
# so restating the values would be two lists to keep in sync, while
# falling back to a validator would leave the choices out of the emitted
# schema and out of what `agw resource describe` can list.
TmuxLayout = Literal[
    "tiled",
    "even-vertical",
    "even-horizontal",
    "main-vertical",
    "main-horizontal",
    # The agentworks layout above, spelled out because a ``Literal`` cannot
    # be built from a name. It is the one value here that appears twice in
    # this module, and ``tests/sessions/test_layouts.py`` pins that the two
    # agree.
    "aw-session-vertical",
]

VALID_TMUX_LAYOUTS: Final[tuple[str, ...]] = get_args(TmuxLayout)
