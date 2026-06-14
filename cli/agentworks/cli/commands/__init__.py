"""Import side effects: register every command group's Typer subapp.

Importing this package wires up the full CLI surface by importing each
module, whose top-level statements add the subapp to the root `app` and
register the `@*.command` decorators. Import order determines the order
groups appear in `agentworks --help`, so it intentionally matches the
original layout in the pre-split cli.py (compute, project, identity,
workload, views, meta), not alphabetical. The I001 lint rule is disabled
for this file in pyproject.toml so isort does not reflow it.
"""

from __future__ import annotations

from . import vm_host  # noqa: F401
from . import vm  # noqa: F401
from . import workspace  # noqa: F401
from . import agent  # noqa: F401
from . import session  # noqa: F401
from . import console  # noqa: F401
from . import catalog  # noqa: F401
from . import config  # noqa: F401
from . import env  # noqa: F401
from . import completion  # noqa: F401
from . import doctor  # noqa: F401
