"""Bound environment composition through real DIRECT and managed preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._bound_environment import _BoundEnvironment
from agentworks.execution._helper_launcher import IdentityPlan
from agentworks.execution._inline_request import REQUEST_HEADER_BYTES, OutputMode, decode_manifest
from agentworks.execution._managed_job_protocol import decode_managed_job_fact, encode_managed_job_fact
from agentworks.execution._managed_job_request import ManagedJobRequest
from agentworks.execution._managed_runs import ManagedOutputMode, ManagedRunReceipt
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import Deadline, FiniteInput, Retention
from agentworks.execution.models import Command, Input, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import CheckedExecutionError, ExitCode
from tests.execution import test_execution_access as direct_tests
from tests.execution import test_independent_execution_reads as resource_tests
from tests.execution import test_independent_execution_start as independent_tests
from tests.execution import test_managed_execution_access as managed_tests
from tests.execution import test_managed_foreground_access as foreground_tests
from tests.execution import test_managed_live_access as live_tests
from tests.execution import test_managed_live_custody as live_custody_tests
from tests.execution.test_managed_live_exchange import Sink

direct = direct_tests.bound
bound = managed_tests.bound
managed_view = managed_tests.view
observer = resource_tests.observer
independent = independent_tests.available
view = live_custody_tests.view
live_access = live_tests.access_view

SECRET = "bound-environment-private-canary"
PROTECTED = {"CORE_IDENTITY": "protected-identity-canary"}


def bind(
    access: ExecutionAccess, environment: _BoundEnvironment, *, elevated: IdentityPlan | None = None
) -> ExecutionAccess:
    return ExecutionAccess(
        access._operation,
        access._carrier,
        runtime_selection=access._runtime_selection,
        ordinary_plan=access._ordinary_plan,
        elevated_plan=access._elevated_plan if elevated is None else elevated,
        entity_kind=access._entity_kind,
        entity_name=access._entity_name,
        deadline=access._deadline,
        bound_environment=environment,
    )


def test_bound_value_copies_both_maps_and_derivatives_preserve_protection_and_sensitivity():
    values, protected = {"TOKEN": SECRET, "BASE": "original"}, dict(PROTECTED)
    original = _BoundEnvironment(values, protected=protected, sensitive=True)
    values["TOKEN"] = "changed"
    protected["CORE_IDENTITY"] = "changed"
    override = {"TOKEN": "public-replacement", "EXTRA": "derived"}
    derived = original.with_env(override)
    override["EXTRA"] = "changed"
    assert original.values["TOKEN"] == SECRET
    assert derived.values == {"TOKEN": "public-replacement", "BASE": "original", "EXTRA": "derived", **PROTECTED}
    assert derived.protected == original.protected == PROTECTED
    assert derived.sensitive and original.sensitive
    assert SECRET not in repr(original) and SECRET not in repr(derived)
    with pytest.raises(TypeError):
        derived.values["TOKEN"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        derived.protected["CORE_IDENTITY"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        derived.sensitive = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {1: SECRET},
        {"KEY": 1},
        {"bad-name": SECRET},
        {"": SECRET},
        {"A": SECRET + "\0"},
        {"A": SECRET + "\ud800"},
        {"A\0": SECRET},
    ],
)
def test_invalid_external_values_refuse_without_disclosing_values(invalid):
    creators: tuple[Callable[[], _BoundEnvironment], ...] = (
        lambda: _BoundEnvironment(invalid),
        lambda: _BoundEnvironment(protected=invalid),
        lambda: _BoundEnvironment().with_env(invalid),
    )
    for create in creators:
        with pytest.raises(ValidationError) as caught:
            create()
        assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_mapping_failures_and_duplicate_entries_cannot_leak_or_hide_a_collision():
    class Broken(dict):
        def items(self):
            raise ValueError(SECRET)

    class Duplicate(dict):
        def items(self):
            return (("CORE_IDENTITY", SECRET), ("CORE_IDENTITY", PROTECTED["CORE_IDENTITY"]))

    for invalid in (Broken(), Duplicate()):
        with pytest.raises(ValidationError) as caught:
            _BoundEnvironment(invalid, protected=PROTECTED)
        assert SECRET not in repr(caught.value) and caught.value.__context__ is None


@pytest.mark.parametrize("invalid", [None, 0, 1, "sensitive"])
def test_sensitivity_requires_explicit_boolean_even_when_already_sensitive(invalid):
    with pytest.raises(ValidationError):
        _BoundEnvironment(sensitive=invalid)
    with pytest.raises(ValidationError):
        _BoundEnvironment(sensitive=True).with_env({}, sensitive=invalid)


def test_only_core_supplied_keys_are_protected_and_equal_overrides_are_permitted():
    original = _BoundEnvironment({"AGENTWORKS_ORDINARY": "original"}, protected=PROTECTED)
    derived = original.with_env({"AGENTWORKS_ORDINARY": "replacement", **PROTECTED})
    assert derived.values == {"AGENTWORKS_ORDINARY": "replacement", **PROTECTED}
    creators: tuple[Callable[[], _BoundEnvironment], ...] = (
        lambda: _BoundEnvironment({"CORE_IDENTITY": SECRET}, protected=PROTECTED),
        lambda: derived.with_env({"CORE_IDENTITY": SECRET}),
    )
    for create in creators:
        with pytest.raises(ValidationError) as caught:
            create()
        assert SECRET not in repr(caught.value) and PROTECTED["CORE_IDENTITY"] not in repr(caught.value)


def test_derivation_is_passive_and_keeps_exact_authority_deadline_and_operation(direct, monkeypatch):
    database, owner, operation, carrier, access, _ = direct
    calls = []

    def policy():
        calls.append(True)
        return Deadline.after(2)

    access._deadline = policy
    root = bind(access, _BoundEnvironment({"TOKEN": SECRET}, protected=PROTECTED))
    before = database._conn.total_changes
    derived = root.with_env({"TOKEN": "replacement"}, sensitive=True)
    assert calls == [] and carrier.calls == 0 and database._conn.total_changes == before
    for attribute in ("_operation", "_carrier", "_runtime_selection", "_ordinary_plan", "_elevated_plan", "_deadline"):
        assert getattr(derived, attribute) is getattr(root, attribute)
    assert (derived._entity_kind, derived._entity_name) == (root._entity_kind, root._entity_name)
    assert derived._environment.sensitive and not root._environment.sensitive
    assert derived._environment.protected == PROTECTED
    operation.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    with pytest.raises(StateError):
        derived.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    assert carrier.calls == 0 and calls == [True]


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("sudo", [False, True])
def test_direct_prepared_payload_gets_composed_values_inside_final_identity(direct, monkeypatch, sensitive, sudo):
    database, _, _, carrier, original, deadline = direct
    access = bind(
        original,
        _BoundEnvironment(
            {"BASE": "bound", "TOKEN": SECRET, "PATH": "/payload-only"}, protected=PROTECTED, sensitive=sensitive
        ),
        elevated=original._ordinary_plan,
    ).with_env({"BASE": "derived", "ADDED": "derived"})
    seen = []
    execute = carrier.execute

    def capture(invocation, *, io, **options):
        assert isinstance(io.input, FiniteInput)
        manifest = decode_manifest(io.input.data[REQUEST_HEADER_BYTES:])
        seen.append((manifest, io.sensitive))
        for value in (SECRET, PROTECTED["CORE_IDENTITY"], "/payload-only"):
            assert all(value not in part for part in invocation.argv)
            assert value not in repr(invocation) + repr(io) + repr(manifest) + repr(access)
        return execute(invocation, io=io, **options)

    monkeypatch.setattr(carrier, "execute", capture)
    result = access.run(
        Script('printf "%s|%s|%s|%s|%s" "$BASE" "$TOKEN" "$ADDED" "$CORE_IDENTITY" "$PATH"', Shell.SH),
        profile=Protection.DIRECT,
        sudo=sudo,
        env={"BASE": "call", **PROTECTED},
    )
    assert result.status == ExitCode(0) and carrier.deadlines == [deadline]
    manifest, protected = seen[0]
    assert dict(manifest.env) == {
        "BASE": "call",
        "TOKEN": SECRET,
        "ADDED": "derived",
        "PATH": "/payload-only",
        **PROTECTED,
    }
    assert protected is sensitive
    assert manifest.output_mode is (OutputMode.SUPPRESS if sensitive else OutputMode.CAPTURE)
    assert result.stdout.retention is (Retention.SUPPRESSED if sensitive else Retention.CAPTURED)
    assert result.stdout.data == (
        b"" if sensitive else f"call|{SECRET}|derived|{PROTECTED['CORE_IDENTITY']}|/payload-only".encode()
    )
    assert all(
        SECRET.encode() not in row[0] for row in database._conn.execute("SELECT payload FROM lifecycle_obligations")
    )


def test_bound_sensitive_checked_failure_and_explicit_live_delivery(direct):
    _, _, _, carrier, original, _ = direct
    access = bind(original, _BoundEnvironment({"TOKEN": SECRET}, sensitive=True)).with_env({"TOKEN": "replacement"})
    with pytest.raises(CheckedExecutionError) as caught:
        access.run(Script('printf "%s" "$TOKEN"; exit 7', Shell.SH), profile=Protection.DIRECT, check=True)
    assert caught.value.result.stdout.retention is Retention.SUPPRESSED
    assert SECRET not in repr(caught.value) and "replacement" not in repr(caught.value)
    stdout, stderr = Sink(), Sink()
    result = access.run(
        Script('printf "%s" "$TOKEN"', Shell.SH), profile=Protection.DIRECT, output=Output.live(stdout, stderr)
    )
    assert result.stdout.retention is Retention.DELIVERED and bytes(stdout.data) == b"replacement"
    assert not stdout.closed and not stderr.closed and carrier.calls == 2


def test_direct_collision_and_invalid_core_value_refuse_before_dispatch(direct):
    database, _, _, carrier, original, _ = direct
    access = bind(original, _BoundEnvironment(protected=PROTECTED))
    before = database._conn.total_changes
    with pytest.raises(ValidationError):
        access.run(Command(["/bin/true"]), profile=Protection.DIRECT, env={"CORE_IDENTITY": SECRET})
    with pytest.raises(ValidationError):
        bind(original, {"TOKEN": SECRET})
    assert carrier.calls == 0 and database._conn.total_changes == before


@pytest.mark.parametrize("entry", ["with_env", "run", "start"])
@pytest.mark.parametrize("lifetime", [Lifetime.OPERATION, Lifetime.INDEPENDENT])
def test_protected_collision_refuses_before_any_managed_effect(request, entry, lifetime):
    fixture = request.getfixturevalue("managed_view" if lifetime is Lifetime.OPERATION else "independent")
    if lifetime is Lifetime.OPERATION:
        database, workflow, original, main, _ = fixture
    else:
        database, _, _, original, main, workflow = fixture
    access = bind(original, _BoundEnvironment(protected=PROTECTED))
    before = database._conn.total_changes
    with pytest.raises(ValidationError):
        if entry == "with_env":
            access.with_env({"CORE_IDENTITY": SECRET})
        else:
            getattr(access, entry)(
                Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=lifetime, env={"CORE_IDENTITY": SECRET}
            )
    assert database._conn.total_changes == before
    assert main.start.calls == 0 and not original._operation.managed_runs
    workflow.close(cleanup_deadline=Deadline.after(2))


@pytest.mark.parametrize("lifetime", [Lifetime.OPERATION, Lifetime.INDEPENDENT])
@pytest.mark.parametrize("entry", ["start", "run"])
@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize("sensitive", [False, True])
def test_managed_prepared_payloads_keep_environment_and_sensitivity_across_lifetimes(
    request, monkeypatch, lifetime, entry, sudo, sensitive
):
    fixture = request.getfixturevalue("managed_view" if lifetime is Lifetime.OPERATION else "independent")
    if lifetime is Lifetime.OPERATION:
        database, workflow, original, main, _ = fixture
    else:
        database, _, _, original, main, workflow = fixture
    elevated = replace(original._ordinary_plan, expected=replace(original._ordinary_plan.expected, groups=(0, 7)))
    access = bind(
        original,
        _BoundEnvironment({"BASE": "bound", "TOKEN": SECRET}, protected=PROTECTED, sensitive=sensitive),
        elevated=elevated,
    ).with_env({"BASE": "derived"})
    jobs = []
    response = main.start.response

    def capture(start):
        jobs.append(start.job)
        return response(start)

    monkeypatch.setattr(main.start, "response", capture)
    execute = main.start.execute

    def carried(invocation, *, io, **options):
        assert all(SECRET not in part and PROTECTED["CORE_IDENTITY"] not in part for part in invocation.argv)
        assert SECRET not in repr(invocation) + repr(io)
        return execute(invocation, io=io, **options)

    monkeypatch.setattr(main.start, "execute", carried)
    if entry == "run" and lifetime is Lifetime.OPERATION:
        foreground_tests.framed_stream_reads.__wrapped__(fixture)  # type: ignore[attr-defined]
    result = getattr(access, entry)(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        lifetime=lifetime,
        sudo=sudo,
        env={"BASE": "call", **PROTECTED},
    )
    (job,) = jobs
    assert isinstance(job, ManagedJobRequest)
    receipt = decode_managed_job_fact(job.launch)
    assert isinstance(receipt, ManagedRunReceipt)
    assert receipt.spec.workload == (elevated.expected if sudo else original._ordinary_plan.expected)
    assert dict(job.environment) == {"BASE": "call", "TOKEN": SECRET, **PROTECTED}
    assert job.output_mode == ("sensitivity-suppressed" if sensitive else "capture")
    assert SECRET not in repr(job) + repr(result) + repr(access)
    for row in database._conn.execute("SELECT payload FROM lifecycle_obligations"):
        assert SECRET.encode() not in row[0]
    assert SECRET not in repr(database._conn.execute("SELECT * FROM execution_runs").fetchall())
    assert main.start.calls == 1
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_bound_sensitive_environment_preserves_explicit_managed_live_output(live_access, monkeypatch):
    operation, original, main, _ = live_access
    access = bind(original, _BoundEnvironment({"TOKEN": SECRET}, sensitive=True))
    seen = []
    execute = main.execute

    def capture(invocation, *, io, **options):
        if live_custody_tests.is_live(io):
            seen.append(io.sensitive)
        return execute(invocation, io=io, **options)

    monkeypatch.setattr(main, "execute", capture)
    result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()))
    assert result.ok and result.stdout.retention is Retention.DELIVERED and seen == [True]
    run = operation.managed_runs[0]
    assert SECRET.encode() not in encode_managed_job_fact(run.receipt)
    assert run._repository.inspect(run.receipt.identity).output_policy.mode is ManagedOutputMode.DISCARD


@pytest.mark.parametrize("name", ["BASH_ENV", "ENV", "SHELLOPTS", "_agw_private"])
def test_bound_values_still_meet_existing_helper_reserved_name_checks(direct, name):
    _, _, _, carrier, original, _ = direct
    access = bind(original, _BoundEnvironment({name: SECRET}))
    with pytest.raises(ValidationError):
        access.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    assert carrier.calls == 0


@pytest.mark.parametrize("name", ["BASH_ENV", "ENV", "SHELLOPTS", "_agw_private"])
def test_managed_bound_values_retain_existing_helper_name_refusal_before_clock(managed_view, name):
    _, workflow, original, main, keeper = managed_view
    access = bind(original, _BoundEnvironment({name: SECRET}))
    with pytest.raises(ValidationError):
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    assert main.start.calls == keeper.clock.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(2))


def test_derived_managed_view_cannot_outlive_original_operation(managed_view):
    _, workflow, original, main, keeper = managed_view
    access = bind(original, _BoundEnvironment({"TOKEN": SECRET})).with_env({"ADDED": "value"})
    workflow.close(cleanup_deadline=Deadline.after(2))
    with pytest.raises(StateError):
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    assert main.start.calls == keeper.clock.calls == 0


@pytest.mark.parametrize("provenance", ["bound", "view", "input", "request"])
def test_effective_sensitivity_unions_all_explicit_sources(direct, provenance):
    _, _, _, _, original, _ = direct
    access = bind(original, _BoundEnvironment({"TOKEN": SECRET}, sensitive=provenance == "bound"))
    access = access.with_env({}, sensitive=provenance == "view")
    result = access.run(
        Script('printf "%s" "$TOKEN"', Shell.SH),
        profile=Protection.DIRECT,
        stdin=Input.sensitive(b"") if provenance == "input" else Input.eof(),
        sensitive=provenance == "request",
    )
    assert result.stdout.retention is Retention.SUPPRESSED and result.stdout.data == b""
