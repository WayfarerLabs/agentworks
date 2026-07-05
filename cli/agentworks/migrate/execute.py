"""Execute a migration plan: backup, write manifests, rewrite TOML, verify.

Ordering is load-bearing (migration-tool LLD):

1. Backup FIRST, before anything is written, so every partial state is
   recoverable from it.
2. Manifests before the TOML rewrite, so an interruption leaves the
   loud cross-source duplicate error rather than silently-lost rows.
3. The TOML rewrite is atomic (write-new-then-rename).
4. Verification last; a mismatch rolls everything back and raises.

Appends are text-only: existing YAML is never parsed or rewritten, new
documents arrive after a ``---`` separator (newline-guarded).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.migrate.verify import first_difference, normalized_rows

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.migrate.planning import MigrationPlan


@dataclass
class ExecutionResult:
    """What a real run did (for the command's summary output)."""

    backup_path: Path
    created: list[Path] = field(default_factory=list)
    appended: list[Path] = field(default_factory=list)
    verified_rows: int = 0
    dropped_secret_backends: bool = False


def execute_plan(plan: MigrationPlan, config: Config) -> ExecutionResult:
    """Run the plan. Raises ``StateError`` (after rollback) on
    verification mismatch."""
    backup_path = _take_backup(plan.config_path, config)
    result = ExecutionResult(
        backup_path=backup_path,
        dropped_secret_backends=plan.drops_secret_backends,
    )

    created_dirs: list[Path] = []
    appended_lengths: dict[Path, int] = {}
    try:
        for write in plan.writes:
            created_dirs.extend(_ensure_parents(write.path, plan.resources_dir))
            if write.path.exists():
                appended_lengths[write.path] = write.path.stat().st_size
                _append_documents(write.path, write.documents)
                result.appended.append(write.path)
            else:
                write.path.write_text(
                    "---\n".join(write.documents), encoding="utf-8"
                )
                result.created.append(write.path)

        _atomic_write(plan.config_path, plan.new_toml_text)

        result.verified_rows = _verify(plan)
    except Exception:
        _rollback(plan, backup_path, result, created_dirs, appended_lengths)
        raise
    return result


def _take_backup(config_path: Path, config: Config) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = config.paths.backups
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"config-{stamp}.toml"
    shutil.copy2(config_path, backup_path)
    return backup_path


def _ensure_parents(path: Path, resources_dir: Path) -> list[Path]:
    """Create missing parent directories; return the ones created."""
    created: list[Path] = []
    missing: list[Path] = []
    current = path.parent
    while not current.exists():
        missing.append(current)
        if current == resources_dir:
            break
        current = current.parent
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        created.extend(reversed(missing))
    return created


def _append_documents(path: Path, documents: list[str]) -> None:
    existing = path.read_bytes()
    prefix = "" if existing.endswith(b"\n") or not existing else "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix)
        for document in documents:
            handle.write("---\n")
            handle.write(document)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _verify(plan: MigrationPlan) -> int:
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    post_config = load_config(plan.config_path, warn_issues=False)
    post_rows = normalized_rows(build_registry(post_config))
    difference = first_difference(plan.pre_rows, post_rows)
    if difference is not None:
        raise StateError(
            f"migration verification failed: {difference}",
            hint=(
                "This is a migrate-tool bug, not a config problem; the run "
                "was rolled back and nothing changed. Please report it."
            ),
        )
    return len(post_rows)


def _rollback(
    plan: MigrationPlan,
    backup_path: Path,
    result: ExecutionResult,
    created_dirs: list[Path],
    appended_lengths: dict[Path, int],
) -> None:
    """Best-effort restore of every artifact the run produced."""
    with contextlib.suppress(OSError):
        shutil.copy2(backup_path, plan.config_path)
    for path in result.created:
        with contextlib.suppress(OSError):
            path.unlink()
    for path, length in appended_lengths.items():
        with contextlib.suppress(OSError), path.open("r+b") as handle:
            handle.truncate(length)
    for directory in reversed(created_dirs):
        with contextlib.suppress(OSError):
            directory.rmdir()
