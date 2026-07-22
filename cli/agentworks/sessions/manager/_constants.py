"""Module-level constants for the sessions manager package."""

from __future__ import annotations

import re

# Template variable substitution: {{var}} double-brace syntax.
_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}

# Grace period (seconds) to wait after sending C-c before killing a session.
_STOP_GRACE_SECONDS = 5
