"""Durable state and orchestration for adjacent Debian VM upgrades."""

from .engine import (
    ActionDisposition,
    FilesystemJournal,
    UpgradeActionError,
    UpgradeEngine,
    UpgradeExecution,
)
from .journal import (
    AttemptOutcome,
    JournalError,
    JournalProgress,
    JournalState,
    JournalStore,
    UpgradeAction,
    UpgradePair,
)
from .network import predict_interface_names, require_stable_interface_names, verify_interface_names
from .preflight import PreflightIssue, UpgradePreflight

__all__ = [
    "ActionDisposition",
    "AttemptOutcome",
    "FilesystemJournal",
    "JournalError",
    "JournalProgress",
    "JournalState",
    "JournalStore",
    "PreflightIssue",
    "UpgradeAction",
    "UpgradeActionError",
    "UpgradeEngine",
    "UpgradeExecution",
    "UpgradePair",
    "predict_interface_names",
    "require_stable_interface_names",
    "verify_interface_names",
    "UpgradePreflight",
]
