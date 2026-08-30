"""Transport adapter for the guest-side durable journal."""

from __future__ import annotations

import base64
import json
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .journal import JournalError, JournalState, UpgradeAction, UpgradePair

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.transports import Transport

REMOTE_ROOT = "/var/lib/agentworks/debian-upgrades"
_REMOTE_HELPER = f"{REMOTE_ROOT}/journal.py"


class RemoteJournal:
    """Invoke the canonical journal implementation on a Debian guest."""

    def __init__(self, target: Transport) -> None:
        self._target = target

    def install(self) -> None:
        source = Path(__file__).with_name("journal.py")
        staging = f"/var/tmp/agentworks-journal-{uuid.uuid4().hex}.py"
        try:
            self._target.copy_to(source, staging)
            command = (
                f"install -d -m 0700 {shlex.quote(REMOTE_ROOT)} && "
                f"install -m 0700 {shlex.quote(staging)} {shlex.quote(_REMOTE_HELPER)}"
            )
            self._target.run(command, sudo=True)
        finally:
            self._target.run(f"rm -f {shlex.quote(staging)}", check=False)

    def scan_incomplete(self, retained_pairs: Sequence[UpgradePair]) -> list[UpgradePair]:
        states = self.read_states(retained_pairs)
        return [pair for pair, state in states.items() if not state.is_complete]

    def select_incomplete(self, retained_pairs: Sequence[UpgradePair]) -> UpgradePair | None:
        pairs = self.scan_incomplete(retained_pairs)
        if len(pairs) > 1:
            names = ", ".join(pair.dirname for pair in pairs)
            raise JournalError(f"multiple incomplete Debian upgrade journals require repair: {names}")
        return pairs[0] if pairs else None

    def read_states(self, retained_pairs: Sequence[UpgradePair]) -> dict[UpgradePair, JournalState]:
        """Read and locally validate the fixed journal root without mutating it."""
        script = """
import json
import os
import sys

root = sys.argv[1]
if not os.path.exists(root):
    print("{}")
    raise SystemExit(0)
if not os.path.isdir(root):
    print("upgrade journal root is not a directory", file=sys.stderr)
    raise SystemExit(2)
inventory = {}
for name in sorted(os.listdir(root)):
    directory = os.path.join(root, name)
    if not os.path.isdir(directory):
        continue
    with open(os.path.join(directory, "state.json"), encoding="utf-8") as stream:
        inventory[name] = json.load(stream)
print(json.dumps(inventory, separators=(",", ":"), sort_keys=True))
""".strip()
        command = shlex.join(["python3", "-c", script, REMOTE_ROOT])
        result = self._target.run(command, sudo=True, check=False)
        if not result.ok:
            detail = (result.stderr or result.stdout or "cannot read remote upgrade journal root").strip()
            raise JournalError(detail[-2000:])
        try:
            inventory = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise JournalError("remote journal inventory returned invalid JSON") from error
        if not isinstance(inventory, dict):
            raise JournalError("remote journal inventory must be an object")
        states: dict[UpgradePair, JournalState] = {}
        for dirname, payload in inventory.items():
            if not isinstance(dirname, str) or not isinstance(payload, dict):
                raise JournalError("remote journal inventory contains an invalid entry")
            pair = UpgradePair.parse(dirname, retained_pairs=retained_pairs)
            states[pair] = JournalState.from_mapping(payload)
        return states

    def initialize(self, pair: UpgradePair, plan: Mapping[str, object]) -> JournalState:
        encoded = base64.urlsafe_b64encode(json.dumps(plan, sort_keys=True).encode()).decode("ascii")
        return self._state("initialize", pair.source, pair.target, encoded)

    def load(self, pair: UpgradePair) -> JournalState:
        return self._state("load", pair.source, pair.target)

    def load_plan(self, pair: UpgradePair) -> dict[str, object]:
        value = self._run("plan", pair.source, pair.target)
        if not isinstance(value, dict):
            raise JournalError("remote journal returned an invalid plan result")
        return value

    def claim(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        *,
        boot_id_before: str | None = None,
    ) -> JournalState:
        args = ["claim", pair.source, pair.target, action.value]
        if boot_id_before is not None:
            args.extend(["--boot-id", boot_id_before])
        return self._state(*args)

    def complete(self, pair: UpgradePair, action: UpgradeAction) -> JournalState:
        return self._state("complete", pair.source, pair.target, action.value)

    def fail(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        detail: str,
        *,
        repair_required: bool,
    ) -> JournalState:
        args = ["fail", pair.source, pair.target, action.value, detail]
        if repair_required:
            args.append("--repair-required")
        return self._state(*args)

    def retry(self, pair: UpgradePair) -> JournalState:
        return self._state("retry", pair.source, pair.target)

    def dispatch_reboot(self, pair: UpgradePair, boot_id: str) -> JournalState:
        return self._state("dispatch-reboot", pair.source, pair.target, boot_id)

    def redispatch_reboot(self, pair: UpgradePair, boot_id: str) -> JournalState:
        return self._state("redispatch-reboot", pair.source, pair.target, boot_id)

    def update_plan(self, pair: UpgradePair, plan: Mapping[str, object]) -> JournalState:
        encoded = base64.urlsafe_b64encode(json.dumps(plan, sort_keys=True).encode()).decode("ascii")
        return self._state("update-plan", pair.source, pair.target, encoded)

    def _state(self, *args: str) -> JournalState:
        value = self._run(*args)
        if not isinstance(value, dict):
            raise JournalError("remote journal returned an invalid state result")
        return JournalState.from_mapping(value)

    def _run(self, *args: str) -> object:
        command = shlex.join(["python3", _REMOTE_HELPER, "--root", REMOTE_ROOT, *args])
        result = self._target.run(command, sudo=True, check=False)
        if not result.ok:
            detail = (result.stderr or result.stdout or "remote journal operation failed").strip()
            raise JournalError(detail[-2000:])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise JournalError("remote journal returned invalid JSON") from error
