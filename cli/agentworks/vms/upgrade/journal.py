"""Root-owned, atomic journal for one adjacent Debian upgrade.

This module deliberately uses only the Python standard library. The manager
copies this exact file to the guest and invokes its small private CLI, so the
local tests and the remote journal share one parser and one state validator.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import TextIO

JOURNAL_VERSION = 1
DEFAULT_ROOT = Path("/var/lib/agentworks/debian-upgrades")
_PAIR_PART = re.compile(r"[a-z][a-z0-9-]*")
_PACKAGE_LOCK_PATHS = (
    "/var/lib/dpkg/lock",
    "/var/lib/dpkg/lock-frontend",
    "/var/cache/apt/archives/lock",
    "/var/lib/apt/lists/lock",
)


class JournalError(Exception):
    """A persisted journal is invalid, ambiguous, or currently locked."""


class UpgradeAction(StrEnum):
    """Mutating actions in their only supported order."""

    SOURCE_UPDATE = "source-update"
    SWITCH_SOURCES = "switch-sources"
    MINIMAL_UPGRADE = "minimal-upgrade"
    FULL_UPGRADE = "full-upgrade"
    REBOOT = "reboot"


class JournalProgress(StrEnum):
    """Last action whose postcondition was durably proved."""

    PREPARED = "prepared"
    SOURCE_CURRENT = "source-current"
    SOURCES_SWITCHED = "sources-switched"
    MINIMAL_UPGRADE_COMPLETE = "minimal-upgrade-complete"
    FULL_UPGRADE_COMPLETE = "full-upgrade-complete"
    REBOOT_COMPLETE = "reboot-complete"


class AttemptOutcome(StrEnum):
    """Outcome of the current or most recent action attempt."""

    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REPAIR_REQUIRED = "repair-required"


_ACTION_PROGRESS: dict[UpgradeAction, JournalProgress] = {
    UpgradeAction.SOURCE_UPDATE: JournalProgress.SOURCE_CURRENT,
    UpgradeAction.SWITCH_SOURCES: JournalProgress.SOURCES_SWITCHED,
    UpgradeAction.MINIMAL_UPGRADE: JournalProgress.MINIMAL_UPGRADE_COMPLETE,
    UpgradeAction.FULL_UPGRADE: JournalProgress.FULL_UPGRADE_COMPLETE,
    UpgradeAction.REBOOT: JournalProgress.REBOOT_COMPLETE,
}
_PROGRESS_ACTION: dict[JournalProgress, UpgradeAction] = {
    JournalProgress.PREPARED: UpgradeAction.SOURCE_UPDATE,
    JournalProgress.SOURCE_CURRENT: UpgradeAction.SWITCH_SOURCES,
    JournalProgress.SOURCES_SWITCHED: UpgradeAction.MINIMAL_UPGRADE,
    JournalProgress.MINIMAL_UPGRADE_COMPLETE: UpgradeAction.FULL_UPGRADE,
    JournalProgress.FULL_UPGRADE_COMPLETE: UpgradeAction.REBOOT,
}


@dataclass(frozen=True, slots=True)
class UpgradePair:
    """One adjacent source-to-target pair, encoded only by its directory."""

    source: str
    target: str

    def __post_init__(self) -> None:
        if not _PAIR_PART.fullmatch(self.source) or not _PAIR_PART.fullmatch(self.target):
            raise JournalError("upgrade pair contains an invalid Debian codename")
        if self.source == self.target:
            raise JournalError("upgrade pair source and target must differ")

    @property
    def dirname(self) -> str:
        return f"{self.source}-to-{self.target}"

    @classmethod
    def parse(cls, dirname: str, *, retained_pairs: Sequence[UpgradePair] = ()) -> UpgradePair:
        matches = [pair for pair in retained_pairs if pair.dirname == dirname]
        if retained_pairs:
            if len(matches) != 1:
                raise JournalError(f"upgrade journal directory names an unrecognized adjacent pair: {dirname}")
            return matches[0]

        source, separator, target = dirname.partition("-to-")
        if not separator or "-to-" in target:
            raise JournalError(f"invalid upgrade journal directory name: {dirname}")
        return cls(source, target)


@dataclass(frozen=True, slots=True)
class JournalState:
    """Validated state that crosses local invocations and SSH sessions."""

    version: int
    attempt_id: str | None
    last_completed: JournalProgress
    active_action: UpgradeAction | None
    active_started_at: str | None
    boot_id_before: str | None
    outcome: AttemptOutcome
    failure: str | None

    @classmethod
    def prepared(cls) -> JournalState:
        return cls(
            version=JOURNAL_VERSION,
            attempt_id=None,
            last_completed=JournalProgress.PREPARED,
            active_action=None,
            active_started_at=None,
            boot_id_before=None,
            outcome=AttemptOutcome.READY,
            failure=None,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> JournalState:
        """Validate persisted JSON at the cross-execution trust boundary."""
        expected = {
            "version",
            "attempt_id",
            "last_completed",
            "active_action",
            "active_started_at",
            "boot_id_before",
            "outcome",
            "failure",
        }
        if set(value) != expected:
            raise JournalError("state.json has an unsupported field inventory")
        try:
            state = cls(
                version=_required_int(value, "version"),
                attempt_id=_optional_str(value, "attempt_id"),
                last_completed=JournalProgress(_required_str(value, "last_completed")),
                active_action=(
                    None if value["active_action"] is None else UpgradeAction(_required_str(value, "active_action"))
                ),
                active_started_at=_optional_str(value, "active_started_at"),
                boot_id_before=_optional_str(value, "boot_id_before"),
                outcome=AttemptOutcome(_required_str(value, "outcome")),
                failure=_optional_str(value, "failure"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise JournalError("state.json contains an invalid journal value") from error
        state.validate()
        return state

    def validate(self) -> None:
        if self.version != JOURNAL_VERSION:
            raise JournalError(f"unsupported upgrade journal version: {self.version}")
        active_fields = (self.attempt_id, self.active_action, self.active_started_at)
        if any(field is None for field in active_fields) != all(field is None for field in active_fields):
            raise JournalError("active attempt fields must be all null or all populated")
        if self.active_action is None:
            if self.outcome is AttemptOutcome.RUNNING:
                raise JournalError("a running journal must name its active action")
            if self.boot_id_before is not None:
                raise JournalError("boot_id_before is valid only for an active reboot")
        else:
            expected = _PROGRESS_ACTION.get(self.last_completed)
            if expected is None or self.active_action is not expected:
                raise JournalError("active action does not immediately follow last_completed")
            if self.active_action is not UpgradeAction.REBOOT and self.boot_id_before is not None:
                raise JournalError("boot_id_before is valid only for reboot")
            if self.outcome not in {
                AttemptOutcome.RUNNING,
                AttemptOutcome.FAILED,
                AttemptOutcome.REPAIR_REQUIRED,
            }:
                raise JournalError("active action has an invalid attempt outcome")
        if self.outcome in {AttemptOutcome.FAILED, AttemptOutcome.REPAIR_REQUIRED}:
            if self.failure is None:
                raise JournalError("failed journal attempt must record failure detail")
        elif self.failure is not None:
            raise JournalError("non-failed journal attempt cannot record failure detail")
        if self.last_completed is JournalProgress.REBOOT_COMPLETE and self.active_action is not None:
            raise JournalError("a complete journal cannot have an active action")

    @property
    def is_complete(self) -> bool:
        return self.last_completed is JournalProgress.REBOOT_COMPLETE and self.active_action is None

    @property
    def next_action(self) -> UpgradeAction | None:
        return _PROGRESS_ACTION.get(self.last_completed)

    def claim(self, action: UpgradeAction, *, boot_id_before: str | None = None) -> JournalState:
        if self.active_action is not None:
            raise JournalError(f"upgrade action already active: {self.active_action.value}")
        if self.next_action is not action:
            raise JournalError(f"cannot claim out-of-order upgrade action: {action.value}")
        if (action is UpgradeAction.REBOOT) != (boot_id_before is not None):
            raise JournalError("reboot claims require a boot ID and other actions forbid one")
        return JournalState(
            version=self.version,
            attempt_id=str(uuid.uuid4()),
            last_completed=self.last_completed,
            active_action=action,
            active_started_at=datetime.now(UTC).isoformat(),
            boot_id_before=boot_id_before,
            outcome=AttemptOutcome.RUNNING,
            failure=None,
        )

    def complete_active(self, expected_attempt_id: str) -> JournalState:
        if self.active_action is None:
            raise JournalError("no active upgrade action to complete")
        self._require_attempt(expected_attempt_id)
        return JournalState(
            version=self.version,
            attempt_id=None,
            last_completed=_ACTION_PROGRESS[self.active_action],
            active_action=None,
            active_started_at=None,
            boot_id_before=None,
            outcome=AttemptOutcome.SUCCEEDED,
            failure=None,
        )

    def fail_active(
        self,
        expected_attempt_id: str,
        detail: str,
        *,
        repair_required: bool,
    ) -> JournalState:
        if self.active_action is None:
            raise JournalError("no active upgrade action to fail")
        self._require_attempt(expected_attempt_id)
        detail = _bounded_detail(detail)
        return JournalState(
            version=self.version,
            attempt_id=self.attempt_id,
            last_completed=self.last_completed,
            active_action=self.active_action,
            active_started_at=self.active_started_at,
            boot_id_before=self.boot_id_before,
            outcome=(AttemptOutcome.REPAIR_REQUIRED if repair_required else AttemptOutcome.FAILED),
            failure=detail,
        )

    def retry_active(self, expected_attempt_id: str) -> JournalState:
        if self.active_action is None or self.outcome is not AttemptOutcome.FAILED:
            raise JournalError("only a failed active action can be retried")
        self._require_attempt(expected_attempt_id)
        return JournalState(
            version=self.version,
            attempt_id=str(uuid.uuid4()),
            last_completed=self.last_completed,
            active_action=self.active_action,
            active_started_at=datetime.now(UTC).isoformat(),
            boot_id_before=self.boot_id_before,
            outcome=AttemptOutcome.RUNNING,
            failure=None,
        )

    def redispatch_reboot(self, current_boot_id: str, expected_attempt_id: str) -> JournalState:
        """Record another safe dispatch while the original boot is still running."""
        if (
            self.active_action is not UpgradeAction.REBOOT
            or self.last_completed is not JournalProgress.FULL_UPGRADE_COMPLETE
            or self.boot_id_before != current_boot_id
        ):
            raise JournalError("reboot can be redispatched only from the unchanged pre-reboot system")
        self._require_attempt(expected_attempt_id)
        return JournalState(
            version=self.version,
            attempt_id=str(uuid.uuid4()),
            last_completed=self.last_completed,
            active_action=self.active_action,
            active_started_at=datetime.now(UTC).isoformat(),
            boot_id_before=self.boot_id_before,
            outcome=AttemptOutcome.RUNNING,
            failure=None,
        )

    def _require_attempt(self, expected_attempt_id: str) -> None:
        if self.attempt_id != expected_attempt_id:
            raise JournalError("active upgrade attempt identity changed")

    def to_mapping(self) -> dict[str, object]:
        data = asdict(self)
        data["last_completed"] = self.last_completed.value
        data["active_action"] = None if self.active_action is None else self.active_action.value
        data["outcome"] = self.outcome.value
        return data


class JournalStore:
    """Filesystem authority for guest upgrade journals."""

    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root

    def initialize(self, pair: UpgradePair, plan: Mapping[str, object]) -> JournalState:
        self._ensure_root()
        directory = self.path_for(pair)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as error:
            raise JournalError(f"upgrade journal already exists: {pair.dirname}") from error
        directory.chmod(0o700)
        (directory / "lock").touch(mode=0o600, exist_ok=False)
        (directory / "lock").chmod(0o600)
        _atomic_json(directory / "plan.json", dict(plan))
        state = JournalState.prepared()
        _atomic_json(directory / "state.json", state.to_mapping())
        return state

    def read_states(self, retained_pairs: Sequence[UpgradePair]) -> dict[UpgradePair, JournalState]:
        """Read and validate the retained journal inventory without mutation."""
        _reject_symlink_ancestors(self.root)
        try:
            self.root.lstat()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise JournalError(f"cannot inspect upgrade journal directory: {self.root}") from error
        _validate_directory(self.root, expected_mode=0o700)
        states: dict[UpgradePair, JournalState] = {}
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            entry_stat = entry.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                raise JournalError(f"upgrade journal root contains a symlink: {entry}")
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue
            pair = UpgradePair.parse(entry.name, retained_pairs=retained_pairs)
            states[pair] = self.load(pair)
        return states

    def load(self, pair: UpgradePair) -> JournalState:
        path = self._journal_directory(pair) / "state.json"
        value = _read_json(path, label="state")
        if not isinstance(value, dict):
            raise JournalError("state.json must contain a JSON object")
        return JournalState.from_mapping(value)

    def load_plan(self, pair: UpgradePair) -> dict[str, object]:
        path = self._journal_directory(pair) / "plan.json"
        value = _read_json(path, label="plan")
        if not isinstance(value, dict):
            raise JournalError("plan.json must contain a JSON object")
        return value

    def write_state(self, pair: UpgradePair, state: JournalState) -> None:
        state.validate()
        _atomic_json(self._journal_directory(pair) / "state.json", state.to_mapping())

    def path_for(self, pair: UpgradePair) -> Path:
        return self.root / pair.dirname

    @contextlib.contextmanager
    def locked(self, pair: UpgradePair) -> Iterator[None]:
        """Take the journal's one non-blocking writer lock."""
        import fcntl

        path = self._journal_directory(pair) / "lock"
        try:
            descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise JournalError(f"cannot open upgrade journal lock: {path}") from error
        try:
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise JournalError(f"upgrade journal lock has unsafe ownership or mode: {path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise JournalError(f"another Agentworks upgrade process owns {pair.dirname}") from error
            yield
        finally:
            os.close(descriptor)

    def _ensure_root(self) -> None:
        _reject_symlink_ancestors(self.root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _validate_directory(self.root)
        self.root.chmod(0o700)
        _validate_directory(self.root, expected_mode=0o700)

    def _journal_directory(self, pair: UpgradePair) -> Path:
        _validate_directory(self.root, expected_mode=0o700)
        directory = self.path_for(pair)
        _validate_directory(directory, expected_mode=0o700)
        return directory


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current /= part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(current_stat.st_mode):
            raise JournalError(f"upgrade journal path contains a symlink: {current}")


def _validate_directory(path: Path, *, expected_mode: int | None = None) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise JournalError(f"cannot inspect upgrade journal directory: {path}") from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise JournalError(f"upgrade journal path is not a real directory: {path}")
    if path_stat.st_uid != os.geteuid():
        raise JournalError(f"upgrade journal directory has the wrong owner: {path}")
    if expected_mode is not None and stat.S_IMODE(path_stat.st_mode) != expected_mode:
        raise JournalError(f"upgrade journal directory has an unsafe mode: {path}")


def _read_json(path: Path, *, label: str) -> object:
    try:
        _validate_private_file(path, label=label)
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except JournalError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JournalError(f"cannot read upgrade journal {label}: {path}") from error


def _validate_private_file(path: Path, *, label: str, missing_ok: bool = False) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    except OSError as error:
        raise JournalError(f"cannot inspect upgrade journal {label}: {path}") from error
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != os.geteuid()
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise JournalError(f"upgrade journal {label} has unsafe ownership or mode: {path}")


def _required_str(value: Mapping[str, object], key: str) -> str:
    result = value[key]
    if not isinstance(result, str) or not result:
        raise TypeError(key)
    return result


def _optional_str(value: Mapping[str, object], key: str) -> str | None:
    result = value[key]
    if result is None:
        return None
    if not isinstance(result, str) or not result:
        raise TypeError(key)
    return result


def _required_int(value: Mapping[str, object], key: str) -> int:
    result = value[key]
    if not isinstance(result, int) or isinstance(result, bool):
        raise TypeError(key)
    return result


def _bounded_detail(detail: str) -> str:
    detail = detail.strip()
    if not detail or "\n" in detail or "\r" in detail or len(detail) > 2000:
        raise JournalError("journal failure detail must be a non-blank bounded single line")
    return detail


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    _validate_directory(path.parent, expected_mode=0o700)
    _validate_private_file(path, label=path.name, missing_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_mapping(encoded: str) -> dict[str, object]:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JournalError("invalid private journal CLI payload") from error
    if not isinstance(value, dict):
        raise JournalError("private journal CLI payload must be an object")
    return value


def _write_result(value: object, stream: TextIO = sys.stdout) -> None:
    json.dump(value, stream, separators=(",", ":"), sort_keys=True)
    stream.write("\n")


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentworks-debian-upgrade-journal")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan")
    scan.add_argument("pairs", nargs="*")
    subparsers.add_parser("ensure-root")
    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("source")
    initialize.add_argument("target")
    initialize.add_argument("plan")
    for command in ("load", "plan", "claim", "complete", "fail", "retry", "update-plan"):
        action_parser = subparsers.add_parser(command)
        action_parser.add_argument("source")
        action_parser.add_argument("target")
        if command in {"claim", "complete", "fail"}:
            action_parser.add_argument("action")
        if command in {"complete", "fail", "retry"}:
            action_parser.add_argument("attempt_id")
        if command == "claim":
            action_parser.add_argument("--boot-id")
        if command == "fail":
            action_parser.add_argument("detail")
            action_parser.add_argument("--repair-required", action="store_true")
        if command == "update-plan":
            action_parser.add_argument("plan")
    reboot = subparsers.add_parser("dispatch-reboot")
    reboot.add_argument("source")
    reboot.add_argument("target")
    reboot.add_argument("boot_id")
    redispatch = subparsers.add_parser("redispatch-reboot")
    redispatch.add_argument("source")
    redispatch.add_argument("target")
    redispatch.add_argument("boot_id")
    redispatch.add_argument("attempt_id")

    args = parser.parse_args(argv)
    store = JournalStore(args.root)
    if args.command == "ensure-root":
        store._ensure_root()
        _write_result({"ready": True})
        return 0
    if args.command == "scan":
        retained = tuple(UpgradePair.parse(name) for name in args.pairs)
        _write_result({pair.dirname: state.to_mapping() for pair, state in store.read_states(retained).items()})
        return 0

    pair = UpgradePair(args.source, args.target)
    if args.command == "initialize":
        state = store.initialize(pair, _decode_mapping(args.plan))
    elif args.command == "load":
        state = store.load(pair)
    elif args.command == "plan":
        _write_result(store.load_plan(pair))
        return 0
    elif args.command == "dispatch-reboot":
        with store.locked(pair):
            state = store.load(pair)
            if not _package_locks_clear():
                raise JournalError("reboot cannot be dispatched while package-manager ownership is uncertain")
            state = state.claim(UpgradeAction.REBOOT, boot_id_before=args.boot_id)
            store.write_state(pair, state)
            subprocess.Popen(
                ["systemctl", "reboot"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    elif args.command == "redispatch-reboot":
        with store.locked(pair):
            state = store.load(pair).redispatch_reboot(args.boot_id, args.attempt_id)
            if not _package_locks_clear():
                raise JournalError("reboot cannot be redispatched while package-manager ownership is uncertain")
            store.write_state(pair, state)
            subprocess.Popen(
                ["systemctl", "reboot"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    else:
        with store.locked(pair):
            state = store.load(pair)
            if args.command == "claim":
                state = state.claim(UpgradeAction(args.action), boot_id_before=args.boot_id)
            elif args.command == "complete":
                if state.active_action is not UpgradeAction(args.action):
                    raise JournalError("completion action does not match active action")
                state = state.complete_active(args.attempt_id)
            elif args.command == "fail":
                if state.active_action is not UpgradeAction(args.action):
                    raise JournalError("failure action does not match active action")
                state = state.fail_active(
                    args.attempt_id,
                    args.detail,
                    repair_required=args.repair_required,
                )
            elif args.command == "retry":
                state = state.retry_active(args.attempt_id)
            elif args.command == "update-plan":
                if (
                    state.last_completed
                    not in {
                        JournalProgress.SOURCE_CURRENT,
                        JournalProgress.FULL_UPGRADE_COMPLETE,
                    }
                    or state.active_action is not None
                ):
                    raise JournalError("plan cannot be updated at this upgrade stage")
                _atomic_json(store._journal_directory(pair) / "plan.json", _decode_mapping(args.plan))
            else:  # pragma: no cover - argparse owns the closed command set
                raise AssertionError(args.command)
            store.write_state(pair, state)
    _write_result(state.to_mapping())
    return 0


def _package_locks_clear() -> bool:
    fuser = shutil.which("fuser")
    if fuser is not None:
        fuser_result = subprocess.run([fuser, *_PACKAGE_LOCK_PATHS], capture_output=True, check=False)
        return fuser_result.returncode == 1
    lslocks = shutil.which("lslocks")
    if lslocks is None:
        return False
    lslocks_result = subprocess.run(
        [lslocks, "--noheadings", "--output", "PATH,PID"],
        capture_output=True,
        text=True,
        check=False,
    )
    if lslocks_result.returncode != 0:
        return False
    return all(
        not line.split() or line.split()[0] not in _PACKAGE_LOCK_PATHS for line in lslocks_result.stdout.splitlines()
    )


def main() -> int:
    try:
        return _cli()
    except JournalError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
