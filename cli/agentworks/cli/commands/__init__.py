"""Import side effects: register every command group's Typer subapp.

Importing this package wires up the full CLI surface by importing each
module, whose top-level statements add the subapp to the root `app` and
register the `@*.command` decorators. Import order determines the order
groups appear in `agentworks --help`, so it intentionally matches the
original layout in the pre-split cli.py (compute -> project -> identity
-> workload -> views -> meta), not alphabetical -- hence the file-level
isort disable below.
"""

# ruff: noqa: I001

from __future__ import annotations

from agentworks.cli.commands import vm_host  # noqa: F401
from agentworks.cli.commands import vm  # noqa: F401
from agentworks.cli.commands import workspace  # noqa: F401
from agentworks.cli.commands import agent  # noqa: F401
from agentworks.cli.commands import session  # noqa: F401
from agentworks.cli.commands import console  # noqa: F401
from agentworks.cli.commands import catalog  # noqa: F401
from agentworks.cli.commands import config  # noqa: F401
from agentworks.cli.commands import completion  # noqa: F401
from agentworks.cli.commands import doctor  # noqa: F401
