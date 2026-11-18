"""``steps.update_and_apply``: a file that takes effect only through a follow-up action.

The step under test publishes a fragment, then validates and reloads it,
as the sysctl baseline and the tailscaled drop-in steps do. It runs over a
recording admin access whose file operations publish to the test's own
path (``tests/phase_b_support.py``), so a test reads what the step left on
disk and which commands it ran, in order.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, PathRefusal, PathRefused
from agentworks.execution.requests import Command, PythonProgram
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer import steps
from tests.phase_b_support import StepGuest, is_file_operation, requires_file_program
from tests.vm_access_support import Call, run_file_program_locally

pytestmark = requires_file_program

VALIDATE = Command(("validate",), elevated=True, deadline=60)
RELOAD = Command(("reload",), elevated=True, deadline=60)
NEW = b"new\n"
PRIOR = b"prior\n"
LOST = ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))

Outcome = Result | BaseException | None


@pytest.fixture
def fragment(tmp_path: Path) -> Path:
    return tmp_path / "conf.d" / "fragment.conf"


@pytest.fixture
def guest() -> StepGuest:
    return StepGuest()


def _step(guest: StepGuest, path: Path) -> None:
    """Publish ``NEW`` at ``path``, then validate and reload it, warning on failure as a Phase B step does."""

    def validate_and_reload() -> None:
        guest.files.access.run(VALIDATE, check=True)
        guest.files.access.run(RELOAD, check=True)

    try:
        prior = guest.files.read(str(path), elevated=True)
        steps.update_and_apply(
            guest.files,
            guest.log,
            str(path),
            NEW,
            observed=prior,
            mode=0o644,
            parent_mode=0o755,
            apply=validate_and_reload,
        )
    except steps.STEP_FAILURES as error:
        steps.warn_step(guest.log, "fragment install failed", error)


def _failed(stderr: bytes = b"") -> CommandFailed:
    return CommandFailed("box", 1, Result(Completed(1), b"", stderr), streams_logged=True)


def _prior(fragment: Path) -> Path:
    fragment.parent.mkdir()
    fragment.write_bytes(PRIOR)
    return fragment


def test_a_new_file_is_written_then_applied(guest: StepGuest, fragment: Path) -> None:
    _step(guest, fragment)

    assert fragment.read_bytes() == NEW
    assert guest.commands() == [VALIDATE, RELOAD]
    assert guest.log.warned == []


def test_an_unchanged_file_is_neither_rewritten_nor_applied(guest: StepGuest, fragment: Path) -> None:
    _step(guest, fragment)
    before = fragment.stat().st_ino
    guest.access.calls.clear()

    _step(guest, fragment)

    assert guest.commands() == []
    assert guest.writes() == []
    assert fragment.stat().st_ino == before


def test_a_rejected_file_gets_its_prior_content_back_and_is_never_reloaded(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    guest.respond(VALIDATE, _failed(b"bad configuration\n"))

    _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert guest.commands() == [VALIDATE]
    assert len(guest.log.warned) == 1
    assert list(fragment.parent.iterdir()) == [fragment]


def test_a_rejected_new_file_is_removed(guest: StepGuest, fragment: Path) -> None:
    guest.respond(VALIDATE, _failed())

    _step(guest, fragment)

    assert not fragment.exists()
    assert guest.commands() == [VALIDATE]
    assert len(guest.log.warned) == 1


def test_a_timeout_during_validation_restores_the_prior_content(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    guest.respond(VALIDATE, ExecutionIncomplete("box", Result(Unknown(UnknownReason.DEADLINE))))

    _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert guest.commands() == [VALIDATE]
    assert len(guest.log.warned) == 1


def test_a_failed_reload_restores_the_prior_content_and_the_next_run_retries(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    guest.respond(RELOAD, _failed(b"Failed to reload\n"))

    _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert len(guest.log.warned) == 1
    guest.access.responses.clear()
    guest.access.calls.clear()

    _step(guest, fragment)

    assert fragment.read_bytes() == NEW
    assert guest.commands() == [VALIDATE, RELOAD]
    assert len(guest.log.warned) == 1


def test_an_interrupt_during_validation_restores_the_prior_content_and_propagates(
    guest: StepGuest, fragment: Path
) -> None:
    _prior(fragment)
    guest.respond(VALIDATE, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR


def _is_fragment_write(call: Call, fragment: Path) -> bool:
    request = call.request
    if not isinstance(request, PythonProgram) or not is_file_operation(request):
        return False
    return (request.args[0], str(json.loads(request.args[1])["path"])) == ("write_if", str(fragment))


def _answering(
    guest: StepGuest,
    fragment: Path,
    *,
    validate: Outcome | Callable[[], Outcome] = None,
    write: Outcome = None,
    write_lands: bool = False,
    restore: Outcome = None,
) -> list[Call]:
    """Answer the validation and the fragment's two writes (the step's, then the restore); returns those writes.

    ``None`` runs the request as usual. A callable ``validate`` runs as it is
    asked (another writer, say) and returns the outcome. ``write_lands``
    runs the write before answering it with ``write``.
    """
    writes: list[Call] = []

    def answer(call: Call) -> Outcome:
        if call.request == VALIDATE:
            outcome = validate() if callable(validate) else validate
        elif _is_fragment_write(call, fragment):
            writes.append(call)
            outcome = write if len(writes) == 1 else restore
            if len(writes) == 1 and write_lands:
                run_file_program_locally(call)
        else:
            outcome = None
        return outcome if outcome is not None else run_file_program_locally(call)

    guest.access.answer = answer
    return writes


def test_a_restore_that_cannot_reach_the_file_warns_it_is_stranded_beside_the_step_warning(
    guest: StepGuest, fragment: Path
) -> None:
    _prior(fragment)
    _answering(guest, fragment, validate=_failed(b"bad configuration\n"), restore=LOST)

    _step(guest, fragment)

    assert fragment.read_bytes() == NEW
    stranded, step = guest.log.warned
    assert str(fragment) in stranded
    assert "bad configuration" in step


def test_an_interrupt_whose_restore_is_refused_warns_the_file_is_stranded_and_propagates(
    guest: StepGuest, fragment: Path
) -> None:
    _prior(fragment)
    _answering(
        guest,
        fragment,
        validate=KeyboardInterrupt(),
        restore=PathRefused("box", "write_if", str(fragment), PathRefusal.UNSUITABLE),
    )

    with pytest.raises(KeyboardInterrupt):
        _step(guest, fragment)

    [stranded] = guest.log.warned
    assert str(fragment) in stranded


@contextlib.contextmanager
def _delivers_a_held_interrupt() -> Iterator[None]:
    yield
    raise KeyboardInterrupt


def test_an_interrupt_held_through_a_stranding_restore_still_warns(
    guest: StepGuest, fragment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C held while the restore ran arrives as it ends, after the stranded warning."""
    _prior(fragment)
    _answering(guest, fragment, validate=_failed(), restore=LOST)
    monkeypatch.setattr(steps, "interrupts_deferred", _delivers_a_held_interrupt)

    with pytest.raises(KeyboardInterrupt):
        _step(guest, fragment)

    [stranded] = guest.log.warned
    assert str(fragment) in stranded


def test_a_restore_that_finds_another_writers_content_warns_nothing_more_and_the_next_run_retries(
    guest: StepGuest, fragment: Path
) -> None:
    _prior(fragment)

    def another_writer_then_rejection() -> Outcome:
        fragment.write_bytes(b"other\n")
        return _failed(b"bad configuration\n")

    _answering(guest, fragment, validate=another_writer_then_rejection)

    _step(guest, fragment)

    assert fragment.read_bytes() == b"other\n"
    [step] = guest.log.warned
    assert str(fragment) not in step
    _answering(guest, fragment)
    guest.access.calls.clear()

    _step(guest, fragment)

    assert fragment.read_bytes() == NEW
    assert guest.commands() == [VALIDATE, RELOAD]


def test_a_write_that_landed_with_its_connection_lost_is_put_back(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    writes = _answering(guest, fragment, write=LOST, write_lands=True)

    _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert len(writes) == 2  # the write that landed, then the restore
    assert guest.commands() == []


def test_a_write_that_landed_before_an_interrupt_is_put_back(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    writes = _answering(guest, fragment, write=KeyboardInterrupt(), write_lands=True)

    with pytest.raises(KeyboardInterrupt):
        _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert len(writes) == 2


def test_a_write_that_did_not_land_is_not_reported_as_stranded(guest: StepGuest, fragment: Path) -> None:
    _prior(fragment)
    writes = _answering(guest, fragment, write=LOST)

    _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert len(writes) == 2  # the lost write, then the restore that found it had not landed
    [step] = guest.log.warned
    assert str(fragment) not in step


def test_a_write_the_vm_refused_to_authenticate_is_not_put_back(guest: StepGuest, fragment: Path) -> None:
    """Nothing ran when the VM refused the key, so there is nothing to restore."""
    _prior(fragment)
    writes = _answering(guest, fragment, write=ExecutionIncomplete("box", Result(Unknown(UnknownReason.AUTH_FAILED))))

    _step(guest, fragment)

    assert len(writes) == 1


def test_an_interrupt_held_after_a_write_that_did_not_land_warns_nothing(
    guest: StepGuest, fragment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prior(fragment)
    _answering(guest, fragment, write=LOST)
    monkeypatch.setattr(steps, "interrupts_deferred", _delivers_a_held_interrupt)

    with pytest.raises(KeyboardInterrupt):
        _step(guest, fragment)

    assert fragment.read_bytes() == PRIOR
    assert guest.log.warned == []
