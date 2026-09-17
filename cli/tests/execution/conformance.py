"""Reusable, measured buffered proof cases for local and real carriers.

The caller supplies an explicitly authorized carrier. Importing this module
does not discover credentials or contact a target. Cases use synthetic payloads
and perform no intentional filesystem mutations or persistent guest work.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.execution.carrier import Carrier, Deadline, Dispatch, Failure, Retention
from agentworks.execution.preparation import Command, Script, Shell, decode_output, prepare


@dataclass(frozen=True)
class Observation:
    """One checked case, retaining only non-sensitive execution evidence."""

    case: str
    dispatch: Dispatch
    prepared_exit: int | None
    local_status: int | None
    failure: Failure | None
    streams_complete: bool
    suppressed: bool


def _require(condition: bool, case: str) -> None:
    # This harness also runs outside pytest, where optimized Python drops assert.
    if not condition:
        raise AssertionError(f"Buffered proof case failed: {case}")


def check_buffered_contract(carrier: Carrier, *, seconds_per_case: float = 15.0) -> list[Observation]:
    """Run the bounded Linux proof vectors; raise on a failed measurement.

    This is not full carrier acceptance: identity/elevation, startup policy,
    interruption, live I/O and platform prerequisites have separate test lanes.
    A 255 case may correctly remain ambiguous, but cannot be guessed successful.
    """
    observations: list[Observation] = []
    secret = b"synthetic-proof-input\x00\xff\n"
    cases: list[tuple[str, Command | Script, bytes, dict[str, str], str | None, bool, bytes, bytes, int]] = [
        (
            "literal-arguments",
            Command(("/usr/bin/printf", "<%s>\n", "", "a b", "'\"$() ; *", "line\nend")),
            b"",
            {},
            None,
            False,
            b"<>\n<a b>\n<'\"$() ; *>\n<line\nend>\n",
            b"",
            0,
        ),
        (
            "source-and-binary-input",
            Script("printf '\\000\\377\\n'; /bin/cat; printf '\\200err\\n' >&2", Shell.fixed("sh")),
            b"input\x00\xfe\n\n",
            {},
            None,
            False,
            b"\x00\xff\ninput\x00\xfe\n\n",
            b"\x80err\n",
            0,
        ),
        (
            "explicit-bash",
            Script("v=(one two); printf '%s\\n' \"${v[1]}\"", Shell.fixed("bash")),
            b"",
            {},
            None,
            False,
            b"two\n",
            b"",
            0,
        ),
        (
            "environment-and-directory",
            Script("printf '%s\\n' \"$AGW_PROOF_VALUE\"; pwd", Shell.fixed("sh")),
            b"",
            {"AGW_PROOF_VALUE": "value\nwith 'quotes'"},
            "/",
            False,
            b"value\nwith 'quotes'\n/\n",
            b"",
            0,
        ),
        (
            "eof-no-staging-readiness",
            Script("/bin/cat; printf ready", Shell.fixed("sh")),
            b"",
            {"TMPDIR": "/agw-proof-must-not-create"},
            None,
            False,
            b"ready",
            b"",
            0,
        ),
        ("exit-1", Script("exit 1", Shell.fixed("sh")), b"", {}, None, False, b"", b"", 1),
        ("exit-255", Script("exit 255", Shell.fixed("sh")), b"", {}, None, False, b"", b"", 255),
        (
            "sensitive-reflection",
            Script("/bin/cat; printf private >&2", Shell.fixed("sh")),
            secret,
            {"AGW_PROOF_SECRET": "synthetic-environment"},
            None,
            True,
            b"",
            b"",
            0,
        ),
    ]
    for name, request, stdin, env, cwd, sensitive, expected_out, expected_err, expected_exit in cases:
        deadline = Deadline.after(seconds_per_case)
        prepared = prepare(request, stdin=stdin, env=env, cwd=cwd, sensitive=sensitive)
        report = carrier.execute(prepared.invocation, io=prepared.io, deadline=deadline)
        output = decode_output(prepared, report.stdout)
        complete = output.stdout_complete and output.stderr_complete
        if sensitive:
            _require(report.stdout.retention == Retention.SUPPRESSED, name)
            _require(report.stderr.retention == Retention.SUPPRESSED, name)
            _require(report.stdout.data == report.stderr.data == b"", name)
            _require(output.suppressed and output.stdout == output.stderr == b"", name)
        else:
            _require(not output.framing_error and not output.bootstrap_failed, name)
            _require(complete, name)
            _require(output.stdout == expected_out and output.stderr == expected_err, name)
        if expected_exit == 255 and report.completion is None:
            _require(report.local_status == 255, name)
            _require(report.dispatch != Dispatch.NOT_SENT, name)
            _require(report.failure in (None, Failure.DISPATCH, Failure.OBSERVATION), name)
        else:
            _require(report.failure is None, name)
            _require(report.completion is not None and report.completion.code == expected_exit, name)
            _require(report.dispatch == Dispatch.SENT, name)
        observations.append(
            Observation(
                name,
                report.dispatch,
                report.completion.code if report.completion else None,
                report.local_status,
                report.failure,
                complete,
                output.suppressed,
            )
        )
    return observations
