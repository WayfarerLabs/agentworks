"""TOML-to-YAML resource migration (``agw resource migrate``).

A recurring, incremental mover -- not a one-time converter. Selectors
scope each run, output is append-only YAML, the TOML edit is mandatory
(dual-path makes a both-sources declaration a hard load error), and
every real run verifies registry equivalence before it counts as done.
The dual-path model this serves is ADR 0016; the operator-facing story
is docs/guides/resources.md.
"""

from __future__ import annotations

from agentworks.migrate.execute import ExecutionResult, execute_plan
from agentworks.migrate.planning import (
    MigrationPlan,
    MigrationUnit,
    plan_migration,
)

__all__ = [
    "ExecutionResult",
    "MigrationPlan",
    "MigrationUnit",
    "execute_plan",
    "plan_migration",
]
