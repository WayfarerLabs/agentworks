"""TOML-to-YAML resource migration (``agw resource migrate``).

A recurring, incremental mover -- not a one-time converter. Selectors
scope each run, output is append-only YAML, the TOML edit is mandatory
(dual-path makes a both-sources declaration a hard load error), and
every real run verifies registry equivalence before it counts as done.
Design: docs/sdd/2026-07-01-resource-manifests/migration-tool-lld.md
(promoted to permanent docs in Phase 5).
"""

from __future__ import annotations

from agentworks.migrate.execute import ExecutionResult, execute_plan
from agentworks.migrate.planning import (
    Layout,
    MigrationPlan,
    MigrationUnit,
    TomlMode,
    plan_migration,
)

__all__ = [
    "ExecutionResult",
    "Layout",
    "MigrationPlan",
    "MigrationUnit",
    "TomlMode",
    "execute_plan",
    "plan_migration",
]
