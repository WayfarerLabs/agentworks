"""Rehome failures preserve real fixture files and report only confirmed targets."""

from __future__ import annotations

import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import InitStatus
from agentworks.errors import ExternalError, StateError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces.manager import rehome_workspace
from tests.artifacts.test_workspace_exclusion_rehome import RehomeGuest
from tests.capabilities.test_native_setup_deletion import deletion as deletion

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import Call

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guest filesystem operations")


class CopyTarget(RehomeGuest):
    """Run the requested commands, with copy tools that can stop after one real file.

    ``failing`` maps a command's program to the error it raises in place of
    running; ``on_copy`` is raised after a failed copy has run.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.source = self.home / "source 'quoted'"
        self.destination = self.home / "new parent" / "target 'quoted'"
        self.source.mkdir()
        (self.source / "kept.txt").write_bytes(b"source contents\n")
        (self.source / "later.txt").write_bytes(b"not copied on failure\n")
        self.rsync = True
        self.copy_calls = 0
        self.race = False
        self.reservation_error: BaseException | None = None
        self.on_copy: BaseException = ExternalError(
            "copy stopped", entity_kind="vm", entity_name="box", hint="upstream hint"
        )
        self.failing: dict[str, BaseException] = {}
        self.environment["COPY_FAIL"] = "0"
        bindir = root / "bin"
        bindir.mkdir()
        # Both tools are asked ``-a -- SOURCE TARGET``.
        for name in ("rsync", "cp"):
            executable = bindir / name
            executable.write_text(
                '#!/bin/sh\nset -eu\nif [ "$COPY_FAIL" = 1 ]; then\n'
                '  /bin/cp -a -- "$3/kept.txt" "$4/"\n  exit 17\nfi\n'
                'exec /bin/cp -a -- "$3/." "$4/"\n'
            )
            executable.chmod(0o700)

    def answer(self, call: Call) -> Result | None:
        request = call.request
        if not isinstance(request, Command):
            return super().answer(call)
        program = request.argv[0]
        if request.argv == ("which", "rsync"):
            self.commands.append("which rsync")
            return Result(Completed(0 if self.rsync else 1))
        if program in self.failing:
            raise self.failing[program]
        reservation = program == "mkdir" and request.argv[-1] == str(self.destination)
        if reservation and self.race:
            self.destination.mkdir()
            (self.destination / "unrelated.txt").write_bytes(b"preserve me\n")
        copy = program in ("rsync", "cp")
        if copy:
            self.copy_calls += 1
        result = super().answer(call)
        if reservation and self.reservation_error is not None:
            assert isinstance(result, Result) and result.completion == Completed(0)
            (self.destination / "unrelated.txt").write_bytes(b"preserve me\n")
            raise self.reservation_error
        if copy and isinstance(result, Result) and result.completion != Completed(0):
            raise self.on_copy
        return result


@dataclass
class RehomeCase:
    target: CopyTarget
    config: Config


@pytest.fixture
def rehome_case(db: Database, deletion, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RehomeCase:  # noqa: ANN001
    target = CopyTarget(tmp_path / "guest")
    db.update_vm_init_status("box", InitStatus.COMPLETE)
    db.update_vm_tailscale("box", "box")
    db.update_workspace_path("project", str(target.source))
    monkeypatch.setattr("agentworks.execution.vm_access", lambda *a, **k: target.access())
    monkeypatch.setattr("agentworks.workspaces.manager.rehome.gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.workspaces.templates.resolve_live_tmuxinator", lambda *a, **k: False)
    monkeypatch.setattr("agentworks.workspaces.acls.apply_workspace_acls", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.generate_vscode_workspace", lambda *a, **k: tmp_path / "x")
    return RehomeCase(target, deletion.config)


def rehome(db: Database, case: RehomeCase) -> None:
    rehome_workspace(
        db,
        case.config,
        "project",
        target_path=str(case.target.destination),
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )


@pytest.mark.parametrize("rsync", [False, True])
def test_copy_failure_preserves_source_row_partial_target_and_error(
    db: Database, rehome_case: RehomeCase, rsync: bool
) -> None:
    target = rehome_case.target
    target.rsync = rsync
    target.environment["COPY_FAIL"] = "1"
    original = target.on_copy
    assert isinstance(original, ExternalError)
    original_hint = original.hint

    with pytest.raises(ExternalError) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert caught.value.entity_kind == "vm" and caught.value.entity_name == "box"
    assert caught.value.hint is not None and original_hint is not None
    assert caught.value.hint.startswith(original_hint)
    assert str(target.destination) in caught.value.hint
    assert str(target.source) in caught.value.hint
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.source)
    assert (target.source / "kept.txt").read_bytes() == b"source contents\n"
    assert (target.source / "later.txt").read_bytes() == b"not copied on failure\n"
    assert (target.destination / "kept.txt").read_bytes() == b"source contents\n"
    assert not (target.destination / "later.txt").exists()
    assert target.copy_calls == 1


def test_interrupted_copy_preserves_source_row_partial_target_and_error(
    db: Database, rehome_case: RehomeCase, captured_output: CapturedOutput
) -> None:
    target = rehome_case.target
    target.environment["COPY_FAIL"] = "1"
    original = KeyboardInterrupt("copy fixture interrupted")
    target.on_copy = original

    with pytest.raises(KeyboardInterrupt) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert len(captured_output.warnings) == 1
    assert str(target.source) in captured_output.warnings[0]
    assert str(target.destination) in captured_output.warnings[0]
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.source)
    assert (target.source / "kept.txt").read_bytes() == b"source contents\n"
    assert (target.source / "later.txt").read_bytes() == b"not copied on failure\n"
    assert (target.destination / "kept.txt").read_bytes() == b"source contents\n"
    assert not (target.destination / "later.txt").exists()
    assert target.copy_calls == 1


@pytest.mark.parametrize("reservation", ["present", "raced", "unconfirmed"])
def test_unowned_or_unconfirmed_target_is_preserved_without_copy_hint(
    db: Database, rehome_case: RehomeCase, captured_output: CapturedOutput, reservation: str
) -> None:
    target = rehome_case.target
    expected: type[Exception] = StateError
    if reservation == "present":
        target.destination.mkdir(parents=True)
        (target.destination / "unrelated.txt").write_bytes(b"preserve me\n")
    elif reservation == "raced":
        target.race = True
        expected = CommandFailed
    else:
        target.reservation_error = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))
        expected = ExecutionIncomplete
    own_hint = getattr(target.reservation_error, "hint", None)

    with pytest.raises(expected) as caught:
        rehome(db, rehome_case)

    assert getattr(caught.value, "hint", None) == own_hint
    if reservation == "unconfirmed":
        assert caught.value is target.reservation_error
    assert len(captured_output.warnings) == (0 if reservation == "present" else 1)
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.source)
    assert (target.destination / "unrelated.txt").read_bytes() == b"preserve me\n"
    assert target.copy_calls == 0
    assert not (target.destination / "kept.txt").exists()


@pytest.mark.parametrize("rsync", [False, True])
def test_success_updates_the_row_after_copying_both_files(db: Database, rehome_case: RehomeCase, rsync: bool) -> None:
    target = rehome_case.target
    target.rsync = rsync

    rehome(db, rehome_case)

    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.destination)
    assert target.copy_calls == 1
    for name in ("kept.txt", "later.txt"):
        assert (target.destination / name).read_bytes() == (target.source / name).read_bytes()


@pytest.mark.parametrize("operation", ["chmod", "find"])
def test_failure_after_copy_preserves_the_target_row_and_original_error(
    db: Database, rehome_case: RehomeCase, operation: str
) -> None:
    target = rehome_case.target
    original = ExternalError("permission fixture failure", entity_kind="vm", entity_name="box", hint="upstream hint")
    target.failing[operation] = original

    with pytest.raises(ExternalError) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert (original.entity_kind, original.entity_name) == ("vm", "box")
    assert original.hint is not None and original.hint.startswith("upstream hint")
    assert str(target.source) in original.hint and str(target.destination) in original.hint
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.source)
    for name in ("kept.txt", "later.txt"):
        assert (target.destination / name).read_bytes() == (target.source / name).read_bytes()


@pytest.mark.parametrize("relocated", [False, True])
def test_failure_after_database_update_reports_the_actual_location(
    db: Database, rehome_case: RehomeCase, monkeypatch: pytest.MonkeyPatch, relocated: bool
) -> None:
    target = rehome_case.target
    location = target.home / "different location" if relocated else target.destination
    original = StateError("workspace output fixture failure", hint="upstream hint")

    def fail(*args: object) -> None:
        if relocated:
            db.update_workspace_path("project", str(location))
        raise original

    monkeypatch.setattr("agentworks.workspaces.backends.vm.generate_vscode_workspace", fail)

    with pytest.raises(StateError) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert original.hint is not None and original.hint.startswith("upstream hint")
    assert str(location) in original.hint and str(target.source) not in original.hint
    if relocated:
        assert str(target.destination) not in original.hint
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(location)
    for name in ("kept.txt", "later.txt"):
        assert (target.destination / name).read_bytes() == (target.source / name).read_bytes()


def test_state_lookup_failure_keeps_the_original_exception(
    db: Database, rehome_case: RehomeCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rehome_case.target
    original = ExternalError("permission fixture failure", hint="upstream hint")
    access = target.access()
    answer = target.answer

    def unavailable(*args: object) -> None:
        raise RuntimeError("database fixture unavailable")

    def fail(call: Call) -> Result | None:
        if isinstance(call.request, Command) and call.request.argv[0] == "chmod":
            monkeypatch.setattr(db, "get_workspace", unavailable)
            raise original
        return answer(call)

    monkeypatch.setattr(access, "answer", fail)

    with pytest.raises(ExternalError) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert original.hint is not None and original.hint.startswith("upstream hint")
    for name in ("kept.txt", "later.txt"):
        assert (target.destination / name).read_bytes() == (target.source / name).read_bytes()


def test_interrupted_reservation_preserves_the_unconfirmed_target(
    db: Database, rehome_case: RehomeCase, captured_output: CapturedOutput
) -> None:
    target = rehome_case.target
    original = KeyboardInterrupt("reservation fixture interrupted")
    target.reservation_error = original

    with pytest.raises(KeyboardInterrupt) as caught:
        rehome(db, rehome_case)

    assert caught.value is original
    assert len(captured_output.warnings) == 1
    row = db.get_workspace("project")
    assert row is not None and row.workspace_path == str(target.source)
    assert (target.destination / "unrelated.txt").read_bytes() == b"preserve me\n"
    assert target.copy_calls == 0
    assert not (target.destination / "kept.txt").exists()
