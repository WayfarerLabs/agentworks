"""Execute a migration plan: backup, write manifests, rewrite TOML, verify.

Ordering is load-bearing (migration-tool LLD):

1. Backup FIRST, before anything is written, so every partial state is
   recoverable from it.
2. Manifests before the TOML rewrite, so an interruption leaves the
   loud cross-source duplicate error rather than silently-lost rows.
3. The TOML rewrite is atomic (write-new-then-rename).
4. Verification last; a mismatch rolls everything back and raises.

New documents append text-only. The one exception is an existing YAML
session-template using the deprecated selector: it is round-tripped during
planning and replaced only when its digest still matches the planned input.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
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
    yaml_backup_path: Path | None = None
    config_rewritten: bool = False
    created: list[Path] = field(default_factory=list)
    appended: list[Path] = field(default_factory=list)
    replaced: list[Path] = field(default_factory=list)
    verified_rows: int = 0
    dropped_secret_backends: bool = False


def execute_plan(plan: MigrationPlan, config: Config) -> ExecutionResult:
    """Run the plan. Raises ``StateError`` (after rollback) on
    verification mismatch."""
    config_guard = "rewrite config.toml" if plan.new_toml_digest != plan.old_toml_digest else "validate config.toml"
    _require_digest(plan.config_path, plan.old_toml_digest, config_guard)
    backup_path, yaml_backup_path = _take_backup(plan, config)
    result = ExecutionResult(
        backup_path=backup_path,
        yaml_backup_path=yaml_backup_path,
        dropped_secret_backends=plan.drops_secret_backends,
    )

    created_dirs: list[Path] = []
    appended: dict[Path, tuple[str, str, Path]] = {}
    created: dict[Path, str] = {}
    replaced: dict[Path, tuple[Path, str, str]] = {}
    config_replaced = False
    try:
        for write in plan.writes:
            created_dirs.extend(_ensure_parents(write.path, plan.resources_dir))
            if write.path.exists():
                assert yaml_backup_path is not None  # existing outputs always have recovery copies
                snapshot = yaml_backup_path / write.path.relative_to(plan.config_path.parent)
                old_bytes = snapshot.read_bytes()
                old_digest = sha256(old_bytes).hexdigest()
                _require_digest(write.path, old_digest, f"append to {write.path}")
                old_text = old_bytes.decode("utf-8")
                new_text = _appended_text(old_text, write.documents)
                new_digest = sha256(new_text.encode()).hexdigest()
                appended[write.path] = (old_digest, new_digest, snapshot)
                _atomic_write(
                    write.path,
                    new_text,
                    expected_old_digest=old_digest,
                    operation=f"append to {write.path}",
                )
                result.appended.append(write.path)
            else:
                # The atomic replace leaves no destination artifact until
                # its write completes, so a mid-write failure needs no
                # speculative rollback record.
                new_text = "---\n".join(write.documents)
                created[write.path] = sha256(new_text.encode()).hexdigest()
                _atomic_write(write.path, new_text, expect_absent=True, operation=f"create {write.path}")
                result.created.append(write.path)

        for rewrite in plan.yaml_rewrites:
            _require_digest(rewrite.path, rewrite.old_digest, f"rewrite {rewrite.path}")
            assert yaml_backup_path is not None  # planned rewrites always have recovery copies
            # Register the intended replacement before os.replace: a platform
            # can complete replace then interrupt before this call returns.
            replaced[rewrite.path] = (
                yaml_backup_path / rewrite.path.relative_to(plan.config_path.parent),
                rewrite.old_digest,
                rewrite.new_digest,
            )
            _atomic_write(
                rewrite.path,
                rewrite.new_text,
                expected_old_digest=rewrite.old_digest,
                operation=f"rewrite {rewrite.path}",
            )
            result.replaced.append(rewrite.path)

        if plan.new_toml_digest != plan.old_toml_digest:
            # A concurrent edit may arrive after the initial guard while
            # manifests are being written. Check again immediately before
            # replacement, but leave a YAML-only plan's config inode intact.
            _require_digest(plan.config_path, plan.old_toml_digest, "rewrite config.toml")
            config_replaced = True
            _atomic_write(
                plan.config_path,
                plan.new_toml_text,
                expected_old_digest=plan.old_toml_digest,
                operation="rewrite config.toml",
            )
            result.config_rewritten = True

        result.verified_rows = _verify(plan)
    except BaseException as exc:
        recovery_failures = _rollback(
            plan,
            backup_path,
            result,
            created_dirs,
            appended,
            created,
            replaced,
            config_replaced,
        )
        if recovery_failures:
            if not isinstance(exc, Exception):
                exc.add_note("Manual recovery is required. " + " ".join(recovery_failures))
                raise
            raise StateError(
                f"migration failed and rollback is incomplete: {exc}",
                hint=(
                    "Manual recovery is required. " + " ".join(recovery_failures) + f" Config backup: {backup_path}."
                ),
            ) from exc
        raise
    return result


def _take_backup(plan: MigrationPlan, config: Config) -> tuple[Path, Path | None]:
    """Persist config and existing YAML originals before any migration write."""
    config_path = plan.config_path
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = config.paths.backups
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"config-{stamp}.toml"
    counter = 1
    while backup_path.exists():
        # Second-granularity stamps collide under scripted back-to-back
        # runs; overwriting would lose the earlier (truer) original.
        backup_path = backup_dir / f"config-{stamp}-{counter}.toml"
        counter += 1
    shutil.copy2(config_path, backup_path)
    yaml_originals = {rewrite.path: rewrite.old_bytes for rewrite in plan.yaml_rewrites}
    for write in plan.writes:
        if write.exists:
            yaml_originals.setdefault(write.path, write.path.read_bytes())
    if not yaml_originals:
        return backup_path, None

    yaml_backup_path = backup_path.parent / f"{backup_path.name}.resources"
    yaml_backup_path.mkdir()
    for path, original in yaml_originals.items():
        snapshot = yaml_backup_path / path.relative_to(config_path.parent)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(original)
    return backup_path, yaml_backup_path


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


def _appended_text(existing: str, documents: list[str]) -> str:
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    return existing + prefix + "".join(f"---\n{document}" for document in documents)


def _atomic_write(
    path: Path,
    text: str,
    *,
    expected_old_digest: str | None = None,
    expect_absent: bool = False,
    operation: str | None = None,
) -> None:
    _atomic_write_bytes(
        path,
        text.encode(),
        expected_old_digest=expected_old_digest,
        expect_absent=expect_absent,
        operation=operation,
    )


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    expected_old_digest: str | None = None,
    expect_absent: bool = False,
    operation: str | None = None,
) -> None:
    """Durably replace ``path``, with a final portable concurrent-edit guard.

    When replacing an existing target, callers provide its expected digest. New
    targets use ``expect_absent``. The
    check runs after the temp file is durable and immediately before
    ``os.replace``. POSIX and Windows expose no portable atomic
    compare-and-replace-by-content operation, so an infinitesimal check-to-rename
    race remains; this is the narrowest portable guard without claiming an
    advisory lock protects against non-cooperating editors.
    """
    if expected_old_digest is not None and expect_absent:
        raise ValueError("expected_old_digest and expect_absent are mutually exclusive")
    original = path.stat() if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if original is not None:
            # Apply metadata to the temp inode before replacement. A chmod or
            # chown failure must leave the old target intact, not create an
            # untracked post-replace mutation outside rollback bookkeeping.
            if chown := getattr(os, "chown", None):
                chown(tmp_name, original.st_uid, original.st_gid)
            os.chmod(tmp_name, stat.S_IMODE(original.st_mode))
        if expected_old_digest is not None:
            _require_digest(path, expected_old_digest, operation or f"replace {path}")
        elif expect_absent:
            if path.exists():
                raise StateError(
                    f"cannot {operation or f'create {path}'}: the target appeared after migration planning",
                    hint="Reconcile the new file, then re-run `agw resource migrate`.",
                )
        elif original is not None:
            raise StateError(f"cannot replace existing file without an expected digest: {path}")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _require_digest(path: Path, expected: str, operation: str) -> None:
    """Refuse to replace a file that changed after planning."""
    if _digest(path) != expected:
        raise StateError(
            f"cannot {operation}: it changed after migration planning",
            hint="Reconcile the edit, then re-run `agw resource migrate`.",
        )


def _digest(path: Path) -> str:
    """Digest a file, reporting absence as a recovery-safe observed value."""
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<unreadable-or-missing>"


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
                "This is a migrate-tool bug, not a config problem. Rollback "
                "is being attempted; inspect its outcome and report this error."
            ),
        )
    return len(post_rows)


def _rollback(
    plan: MigrationPlan,
    backup_path: Path,
    result: ExecutionResult,
    created_dirs: list[Path],
    appended: dict[Path, tuple[str, str, Path]],
    created: dict[Path, str],
    replaced: dict[Path, tuple[Path, str, str]],
    config_replaced: bool,
) -> list[str]:
    """Restore only outputs still owned by this run, reporting any gaps."""
    failures: list[str] = []
    # Do not clobber an operator's edit made after our atomic replacement.
    # If the run never touched config.toml, there is nothing to restore.
    if config_replaced:
        observed = _digest(plan.config_path)
        if observed == plan.new_toml_digest:
            try:
                _atomic_write_bytes(
                    plan.config_path,
                    backup_path.read_bytes(),
                    expected_old_digest=plan.new_toml_digest,
                    operation="roll back config.toml",
                )
            except (OSError, StateError) as rollback_error:
                failures.append(
                    _recovery_failure(
                        plan.config_path,
                        plan.new_toml_digest,
                        _digest(plan.config_path),
                        rollback_error,
                        recovery=backup_path,
                    )
                )
        elif observed != plan.old_toml_digest:
            failures.append(_recovery_failure(plan.config_path, plan.new_toml_digest, observed))
    for path, expected in created.items():
        observed = _digest(path)
        if observed == expected:
            try:
                path.unlink()
            except OSError as rollback_error:
                failures.append(_recovery_failure(path, expected, observed, rollback_error))
        elif observed != "<unreadable-or-missing>":
            failures.append(_recovery_failure(path, expected, observed))
    for path, (old_digest, expected, snapshot) in appended.items():
        observed = _digest(path)
        if observed == expected:
            try:
                old_bytes = snapshot.read_bytes()
                snapshot_digest = sha256(old_bytes).hexdigest()
                if snapshot_digest == old_digest:
                    _atomic_write_bytes(
                        path,
                        old_bytes,
                        expected_old_digest=expected,
                        operation=f"roll back {path}",
                    )
                else:
                    failures.append(_recovery_failure(snapshot, old_digest, snapshot_digest))
            except (OSError, StateError) as rollback_error:
                failures.append(_recovery_failure(path, expected, _digest(path), rollback_error, recovery=snapshot))
        elif observed != old_digest:
            failures.append(_recovery_failure(path, expected, observed, recovery=snapshot))
    for path, (snapshot, old_digest, new_digest) in replaced.items():
        observed = _digest(path)
        if observed == new_digest:
            try:
                old_bytes = snapshot.read_bytes()
                if sha256(old_bytes).hexdigest() == old_digest:
                    _atomic_write_bytes(
                        path,
                        old_bytes,
                        expected_old_digest=new_digest,
                        operation=f"roll back {path}",
                    )
                else:
                    failures.append(_recovery_failure(snapshot, old_digest, sha256(old_bytes).hexdigest()))
            except (OSError, StateError) as rollback_error:
                failures.append(_recovery_failure(path, new_digest, _digest(path), rollback_error, recovery=snapshot))
        elif observed == old_digest:
            # The temp write failed before replacement; this target was never
            # mutated despite its pre-registered rollback intent.
            continue
        else:
            failures.append(_recovery_failure(path, new_digest, observed, recovery=snapshot))
    for directory in reversed(created_dirs):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return failures


def _recovery_failure(
    path: Path, expected: str, observed: str, error: Exception | None = None, recovery: Path | None = None
) -> str:
    """An operator-actionable, digest-specific incomplete-recovery fact."""
    suffix = f"; rollback I/O error: {error}" if error is not None else ""
    recovery_hint = f" Recovery copy: {recovery}." if recovery is not None else ""
    return (
        f"{path}: expected digest {expected}, observed {observed}{suffix}. "
        f"Preserve the file and recover manually.{recovery_hint}"
    )
