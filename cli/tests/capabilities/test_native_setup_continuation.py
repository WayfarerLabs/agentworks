"""Facet failures retain confirmed progress while common failures stop dispatch."""

import shlex
import sqlite3
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.application import ArtifactPublication, OwnedArtifactFile
from agentworks.capabilities.harness_integration import harness_integration_for
from agentworks.db import AppliedStateKey, VersionedPayload
from agentworks.errors import ExternalError, StateError, UserAbort
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.outcomes import SetupFailure, raise_setup_failures
from agentworks.harness_setup.state import UnsupportedNativeSetupVersionError, read_native_setup, write_native_setup
from agentworks.ssh import SSHError, SSHLogger
from tests.capabilities.test_native_setup_dispatch import setup_case as setup_case


def test_multiple_failures_keep_execution_and_report_order(setup_case):
    db, inputs, invocation, events, injected = setup_case
    injected.update(("first", "second"))
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert events == [("first", False), ("second", False)]
    assert [item.integration for item in failures] == ["first", "second"]
    assert all(not item.complete and item.claims and item.incomplete_reasons for item in state.records)
    with pytest.raises(StateError) as caught:
        raise_setup_failures(failures)
    rendered = str(caught.value)
    assert rendered.index("integration=first") < rendered.index("integration=second")
    assert all(item.reason in rendered and item.hint is not None and item.hint in rendered for item in failures)
    raise_setup_failures([])


@pytest.mark.parametrize("buffered", [False, True])
def test_publication_failure_keeps_checkpointed_files_and_claims(setup_case, monkeypatch, buffered):
    db, inputs, invocation, events, _ = setup_case
    owned = OwnedArtifactFile(path="/home/user/owned", sha256="a" * 64, origins=("b" * 64,))
    calls = 0

    def publish(runner, desired, previous, checkpoint, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            checkpoint((owned,))
            raise SSHError("fixture publication failed")
        return ArtifactPublication()

    monkeypatch.setattr("agentworks.harness_setup.dispatch.publish_artifacts", publish)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-create", buffered=buffered)
    assert events == [("first", False), ("second", False)]
    first, second = state.records
    assert first.artifact_files == (owned,) and first.claims[0].identifier == "first"
    assert not first.complete and second.complete and len(failures) == 1
    if buffered:
        assert read_native_setup(db, "agent", "agent").records == ()
        write_native_setup(db, "agent", "agent", state, operation="agent-create")
    assert read_native_setup(db, "agent", "agent") == state


@pytest.mark.parametrize("callback", ["claim", "incomplete", "file"])
def test_typed_callback_persistence_failure_is_original_and_fatal(setup_case, monkeypatch, callback):
    db, inputs, invocation, events, _ = setup_case
    original = write_native_setup
    failure = StateError(str(uuid4()))
    writes = 0

    def persist(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == (3 if callback == "file" else 2):
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.dispatch.write_native_setup", persist)
    if callback == "incomplete":

        def initialize(self, call):
            events.append((self.name, self.retiring))
            call.report_incomplete(str(uuid4()))
            pytest.fail("persistence callback returned")

        monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    elif callback == "file":

        def publish(runner, desired, previous, checkpoint, **kwargs):
            checkpoint((OwnedArtifactFile(path="/home/user/file", sha256="a" * 64, origins=("b" * 64,)),))
            pytest.fail("persistence callback returned")

        monkeypatch.setattr("agentworks.harness_setup.dispatch.publish_artifacts", publish)
    failures: list[SetupFailure] = []
    with pytest.raises(StateError) as caught:
        run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert caught.value is failure
    assert events == [("first", False)] and failures == []


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("bug"), OSError("disk"), sqlite3.DatabaseError("db"), UserAbort("cancel"), KeyboardInterrupt()],
)
def test_unknown_common_errors_and_cancellation_abort(setup_case, monkeypatch, failure):
    db, inputs, invocation, events, _ = setup_case

    def initialize(self, call):
        events.append((self.name, False))
        raise failure

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    with pytest.raises(type(failure)) as caught:
        run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert caught.value is failure
    assert events == [("first", False)] and failures == []


@pytest.mark.parametrize("future", [False, True])
def test_unreadable_shared_state_aborts_before_any_integration(setup_case, future):
    db, inputs, invocation, events, _ = setup_case
    payload = VersionedPayload(99, {}) if future else VersionedPayload(3, {"records": "invalid"})
    db.instance_state.replace_applied_slices(
        "agent", "agent", "agent-reinit", {AppliedStateKey.HARNESS_NATIVE_SETUP: payload}
    )
    failures: list[SetupFailure] = []
    with pytest.raises(UnsupportedNativeSetupVersionError if future else StateError):
        run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert events == [] and failures == []


def test_invalid_claim_output_retains_previous_claims_and_stops_publication(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    run_setup(db, Mock(), inputs, invocation, failures=[], operation="agent-reinit")
    previous = read_native_setup(db, "agent", "agent").records[0].claims
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        if self.name == "first":
            call.checkpoint(({"role": "plugin"},))
            pytest.fail("invalid metadata callback returned")
        return original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    publish = Mock(return_value=ArtifactPublication())
    monkeypatch.setattr("agentworks.harness_setup.dispatch.publish_artifacts", publish)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert state.records[0].claims == previous and not state.records[0].complete
    assert state.records[1].complete and publish.call_count == 1
    assert [item.integration for item in failures] == ["first"]


def test_invalid_application_is_not_published(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        return None if self.name == "first" else original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    publish = Mock(return_value=ArtifactPublication())
    monkeypatch.setattr("agentworks.harness_setup.dispatch.publish_artifacts", publish)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert not state.records[0].complete and state.records[1].complete
    assert len(failures) == publish.call_count == 1


@pytest.mark.parametrize("with_logger", [False, True])
def test_failure_message_and_hint_redact_all_operation_secrets(setup_case, monkeypatch, tmp_path, with_logger):
    db, inputs, invocation, events, _ = setup_case
    token = "private-'" + uuid4().hex
    extra = uuid4().hex
    logger = None
    if with_logger:
        monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
        logger = SSHLogger("fixture", "setup", redactions=(extra,))
    invocation.runner.logger = logger
    invocation = replace(invocation, secrets={"token": token, "overlap": token[:8]})
    reason = f"{uuid4()}: {token} {shlex.quote(token)} {extra if with_logger else ''}"
    hint = f"{uuid4()}: {token} {shlex.quote(token)}"
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        if self.name == "first":
            raise ExternalError(reason, hint=hint)
        return original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    with pytest.raises(StateError) as caught:
        raise_setup_failures(failures)
    surfaces = [state.model_dump_json(), str(failures), str(caught.value)]
    if logger is not None:
        assert len(logger.warnings) == 1
        surfaces.extend((str(logger.warnings), logger.path.read_text()))
    assert all(token not in surface and shlex.quote(token) not in surface for surface in surfaces)
    if with_logger:
        assert all(extra not in surface for surface in surfaces)
    failure = failures[0]
    assert failure.hint is not None
    assert "[REDACTED]" in failure.reason and "[REDACTED]" in failure.hint
    assert state == read_native_setup(db, "agent", "agent")


def test_persisting_failure_reason_is_fatal_before_sibling(setup_case, monkeypatch):
    db, inputs, invocation, events, injected = setup_case
    injected.add("first")
    failure = StateError(str(uuid4()))
    original = write_native_setup
    writes = 0

    def persist(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr("agentworks.harness_setup.dispatch.write_native_setup", persist)
    failures: list[SetupFailure] = []
    with pytest.raises(StateError) as caught:
        run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert caught.value is failure
    assert events == [("first", False)] and failures == []


def test_warning_log_failure_is_fatal_before_sibling(setup_case, monkeypatch):
    db, inputs, invocation, events, injected = setup_case
    injected.add("first")
    failure = StateError(str(uuid4()))
    invocation.runner.logger = Mock(sanitize=lambda text, **kwargs: text, warning=Mock(side_effect=failure))
    with pytest.raises(StateError) as caught:
        run_setup(db, Mock(), inputs, invocation, failures=[], operation="agent-reinit")
    assert caught.value is failure and events == [("first", False)]


def test_failure_diagnostic_is_durable_before_next_integration(setup_case, monkeypatch):
    db, inputs, invocation, events, injected = setup_case
    injected.add("first")
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        if self.name == "second":
            first = read_native_setup(db, "agent", "agent").records[0]
            assert not first.complete and first.incomplete_reasons
            assert first.claims[0].identifier == "first"
        return original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert events == [("first", False), ("second", False)] and len(failures) == 1


def test_invalid_binding_aborts_before_execution_or_native_state(setup_case, monkeypatch):
    from agentworks.errors import ConfigError

    db, inputs, invocation, events, _ = setup_case
    failure = ConfigError(str(uuid4()))

    def binding(cls, **kwargs):
        raise failure

    monkeypatch.setattr(harness_integration_for("second"), "for_setup", classmethod(binding))
    failures: list[SetupFailure] = []
    with pytest.raises(ConfigError) as caught:
        run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert caught.value is failure
    assert events == [] and failures == []
    assert read_native_setup(db, "agent", "agent").records == ()


def test_soft_callback_diagnostics_are_redacted_without_new_failure(setup_case, monkeypatch):
    db, inputs, invocation, events, _ = setup_case
    token = "private-'" + uuid4().hex
    invocation = replace(invocation, secrets={"token": token})
    original = harness_integration_for("first").user_init
    warning = Mock()
    monkeypatch.setattr("agentworks.harness_setup.dispatch.output.warn", warning)

    def initialize(self, call):
        if self.name == "first":
            call.report_incomplete(f"{uuid4()}: {token} {shlex.quote(token)}")
        return original(self, call)

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert not state.records[0].complete and state.records[1].complete and failures == []
    assert token not in state.model_dump_json() and token not in str(warning.call_args)
    assert warning.call_count == 1
