"""Lowest-level grammar shared by resource declarations and selectors."""

from __future__ import annotations

import re

RESOURCE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
"""Canonical character grammar for every operator-addressable resource name."""

MAX_RESOURCE_NAME_LENGTH = 253
"""Largest resource-name ceiling supported by any registered kind."""
