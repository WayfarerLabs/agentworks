"""Console / tmux layout constants for named-console session windows.

Moved out of ``agentworks.config`` so the ``sessions`` domain owns these
next to their runtime home (``agentworks.sessions.multi_console_layout``,
which builds the custom tmux layout string). ``agentworks.config``'s
``[named_console]`` loader imports them from here for validation.
"""

from __future__ import annotations

# Agentworks-specific layout: session pane (pane 0) takes the top 50% of
# the window, shell panes stack vertically in the bottom 50% with equal
# heights. tmux has no preset that matches this geometry, so apply-time
# builds a custom tmux layout string from the live window dimensions and
# pane IDs and feeds it to `tmux select-layout`. See
# `_apply_aw_session_vertical_layout` in sessions/multi_console_layout.py.
AW_SESSION_VERTICAL_LAYOUT = "aw-session-vertical"

# Valid layouts for named-console session windows. All values besides
# AW_SESSION_VERTICAL_LAYOUT map 1:1 to tmux's built-in select-layout
# names so operators can apply the same value to a window via
# `tmux select-layout` on the fly.
VALID_TMUX_LAYOUTS = (
    "tiled",
    "even-vertical",
    "even-horizontal",
    "main-vertical",
    "main-horizontal",
    AW_SESSION_VERTICAL_LAYOUT,
)
