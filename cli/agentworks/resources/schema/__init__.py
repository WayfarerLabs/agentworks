"""The framework-wide model vocabulary: bases, field markers, walkers.

Everything an agentworks surface needs to be DECLARED rather than
hand-validated lives here:

- :class:`AgwModel` / :class:`AgwRootModel`, the shared strict, frozen,
  closed-world bases every spec and capability config model extends.

This package sits BELOW the domains that use it. It may import
``resources/reference.py`` (the reference records it produces) and
nothing else of ours; in particular nothing here imports
``capabilities/``, ``manifests/``, ``plugins/``, or the kind registry,
which is what lets all of those import this package freely. The one edge
this package creates runs in a single direction: ``resources/schema/``
imports ``resources/reference.py``, never the reverse.
"""

from __future__ import annotations

from agentworks.resources.schema.base import AgwModel, AgwRootModel

__all__ = [
    "AgwModel",
    "AgwRootModel",
]
