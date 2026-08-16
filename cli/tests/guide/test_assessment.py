"""Pure onboarding assessment and shared action-plan proofs."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.config import Config
from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from agentworks.guide import (
    ActionId,
    GuideAction,
    GuideIdentity,
    GuideInstanceFact,
    GuideMode,
    GuideRelationship,
    GuideResourceFact,
    GuideTraversalError,
    GuideVerdict,
    OnboardingSnapshot,
    OnboardingStatus,
    VerificationEvidence,
    VerificationOutcome,
    assess_onboarding,
    onboarding_actions,
    render_guide,
)
from agentworks.guide.service import build_onboarding_snapshot
from agentworks.resources import KIND_REGISTRY, Origin, Registry, ResourceReference
from agentworks.resources.graph import Enablement, Readiness
from agentworks.resources.kind import InstanceRef
from agentworks.secrets.base import SecretDecl


def _fact(
    kind: str,
    name: str,
    *,
    enabled: bool = True,
    ready: bool = True,
    available: bool = True,
) -> GuideResourceFact:
    reason = None if enabled and ready else f"{name} projected reason"
    return GuideResourceFact(
        GuideIdentity(kind, name),
        GuideVerdict(enabled, ready, reason, is_available=available),
    )


def _snapshot(
    *facts: GuideResourceFact,
    instances: tuple[GuideInstanceFact, ...] = (),
    inbound: tuple[GuideRelationship, ...] = (),
    outbound: tuple[GuideRelationship, ...] = (),
) -> OnboardingSnapshot:
    return OnboardingSnapshot(tuple(facts), instances, tuple((*outbound, *inbound)))


def _evidence(action: str, kind: str, name: str, outcome: VerificationOutcome) -> VerificationEvidence:
    return VerificationEvidence(ActionId(action), GuideIdentity(kind, name), outcome)


def _action(action_id: str) -> GuideAction:
    """Look one canonical action up by identity, so ordering is never pinned."""
    return next(action for action in onboarding_actions() if action.id == ActionId(action_id))


def test_mixed_projected_facts_relationships_and_instances_keep_individual_statuses() -> None:
    source = GuideIdentity("vm-template", "dev")
    target = GuideIdentity("workspace-template", "repo")
    assessment = assess_onboarding(
        _snapshot(
            _fact("workspace-template", "ready"),
            _fact("secret-backend", "off", enabled=False),
            _fact("vm-site", "missing-tool", ready=False),
            instances=(GuideInstanceFact("vm", "worker"), GuideInstanceFact("session", "existing")),
            outbound=(GuideRelationship(source, target, "creates workspace"),),
        )
    )

    statuses = {finding.identity: finding.status for finding in assessment.findings}
    assert statuses[GuideIdentity("workspace-template", "ready")] is OnboardingStatus.DONE
    assert statuses[GuideIdentity("secret-backend", "off")] is OnboardingStatus.DISABLED
    assert statuses[GuideIdentity("vm-site", "missing-tool")] is OnboardingStatus.NOT_READY
    assert statuses[GuideIdentity("vm", "worker")] is OnboardingStatus.UNVERIFIABLE
    assert statuses[GuideIdentity("session", "existing")] is OnboardingStatus.DONE
    assert assessment.relationship_findings[0].relationship.target == target
    assert assessment.relationship_findings[0].status is OnboardingStatus.DONE
    assert assessment.summary == type(assessment.summary)(done=3, not_ready=1, disabled=1, unverifiable=1)
    assert assessment.action_ids == (
        ActionId("run-doctor"),
        ActionId("verify-vm-connection"),
    )


@pytest.mark.parametrize(
    ("outcome", "expected", "expected_actions"),
    [
        (
            VerificationOutcome.VERIFIED,
            OnboardingStatus.DONE,
            (ActionId("run-doctor"),),
        ),
        (
            VerificationOutcome.FAILED,
            OnboardingStatus.NOT_READY,
            (ActionId("run-doctor"),),
        ),
        (
            VerificationOutcome.REFUSED,
            OnboardingStatus.UNVERIFIABLE,
            (ActionId("run-doctor"),),
        ),
    ],
)
def test_target_scoped_secret_evidence_changes_only_its_fact(
    outcome: VerificationOutcome, expected: OnboardingStatus, expected_actions: tuple[ActionId, ...]
) -> None:
    assessment = assess_onboarding(
        _snapshot(_fact("secret", "token"), _fact("workspace-template", "ready")),
        verification_evidence=(_evidence("verify-named-secret", "secret", "token", outcome),),
    )
    statuses = {finding.identity: finding.status for finding in assessment.findings}
    assert statuses[GuideIdentity("secret", "token")] is expected
    assert statuses[GuideIdentity("workspace-template", "ready")] is OnboardingStatus.DONE
    assert assessment.action_ids == expected_actions


def test_refused_verification_carries_its_action_refusal_alternative_as_the_reason() -> None:
    evidence = (_evidence("verify-vm-connection", "vm", "worker", VerificationOutcome.REFUSED),)

    assessment = assess_onboarding(
        _snapshot(instances=(GuideInstanceFact("vm", "worker"),)), verification_evidence=evidence
    )

    vm_action = _action("verify-vm-connection")
    assert assessment.findings[0].status is OnboardingStatus.UNVERIFIABLE
    assert assessment.findings[0].reason == vm_action.refusal_alternative
    assert assessment.action_ids == (ActionId("run-doctor"),)


def test_ready_rerun_with_accepted_proof_is_a_clean_no_op() -> None:
    assessment = assess_onboarding(
        _snapshot(_fact("secret", "token")),
        verification_evidence=(_evidence("verify-named-secret", "secret", "token", VerificationOutcome.VERIFIED),),
    )
    assert assessment.findings[0].status is OnboardingStatus.DONE
    assert assessment.action_ids == (ActionId("run-doctor"),)


@pytest.mark.parametrize("kind", ["secret", "vm"])
def test_resource_named_after_its_kind_still_requires_explicit_proof(kind: str) -> None:
    assessment = assess_onboarding(_snapshot(_fact(kind, kind)))
    assert assessment.findings[0].status is OnboardingStatus.UNVERIFIABLE
    expected = "verify-named-secret" if kind == "secret" else "verify-vm-connection"
    assert assessment.action_ids == (
        ActionId("run-doctor"),
        ActionId(expected),
    )


@pytest.mark.parametrize(
    ("outcome", "expected_actions"),
    [
        (None, (ActionId("run-doctor"),)),
        (VerificationOutcome.FAILED, ()),
        (VerificationOutcome.REFUSED, ()),
        (
            VerificationOutcome.VERIFIED,
            (ActionId("create-first-vm"), ActionId("create-first-session")),
        ),
    ],
)
def test_first_resource_actions_require_successful_caller_owned_doctor_proof(
    outcome: VerificationOutcome | None,
    expected_actions: tuple[ActionId, ...],
) -> None:
    evidence = () if outcome is None else (_evidence("run-doctor", "onboarding", "doctor-readiness", outcome),)

    assessment = assess_onboarding(
        _snapshot(_fact("workspace-template", "ready")),
        verification_evidence=evidence,
    )

    assert assessment.action_ids == expected_actions


def test_shared_doctor_record_covers_readiness_proof_and_resource_diagnosis() -> None:
    first_resource = assess_onboarding(_snapshot(_fact("workspace-template", "ready")))
    diagnosis = assess_onboarding(
        _snapshot(
            _fact("vm-site", "blocked", ready=False),
            instances=(GuideInstanceFact("vm", "worker"), GuideInstanceFact("session", "existing")),
        )
    )

    assert first_resource.action_ids == (ActionId("run-doctor"),)
    assert ActionId("run-doctor") in diagnosis.action_ids
    assert first_resource.actions[0] == next(action for action in diagnosis.actions if action.id == "run-doctor")


@pytest.mark.parametrize("available", [False, True])
def test_doctor_proof_does_not_override_unready_or_unavailable_projected_facts(available: bool) -> None:
    assessment = assess_onboarding(
        _snapshot(_fact("vm-site", "blocked", ready=False, available=available)),
        verification_evidence=(
            _evidence("run-doctor", "onboarding", "doctor-readiness", VerificationOutcome.VERIFIED),
        ),
    )

    assert ActionId("create-first-vm") not in assessment.action_ids
    assert ActionId("create-first-session") not in assessment.action_ids


@pytest.mark.parametrize("outcome", [VerificationOutcome.FAILED, VerificationOutcome.REFUSED])
def test_doctor_proof_does_not_override_failed_or_refused_named_proof(outcome: VerificationOutcome) -> None:
    assessment = assess_onboarding(
        _snapshot(_fact("secret", "token")),
        verification_evidence=(
            _evidence("run-doctor", "onboarding", "doctor-readiness", VerificationOutcome.VERIFIED),
            _evidence("verify-named-secret", "secret", "token", outcome),
        ),
    )

    assert ActionId("create-first-vm") not in assessment.action_ids
    assert ActionId("create-first-session") not in assessment.action_ids


def test_cli_replays_target_scoped_evidence_end_to_end(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "tailscale-auth-key",
        SecretDecl(name="tailscale-auth-key", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: cast("Config", SimpleNamespace()))
    monkeypatch.setattr("agentworks.bootstrap.load_guide_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.db.DB_PATH", SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr("agentworks.db.Database", lambda read_only: db)

    result = CliRunner().invoke(
        app,
        [
            "guide",
            "concept-onboarding",
            "--agent",
            "--evidence",
            "verify-named-secret:secret/tailscale-auth-key=verified",
        ],
    )

    assert result.exit_code == 0
    assert "`secret/tailscale-auth-key`: done" in result.stdout
    assert "### `verify-named-secret`" not in result.stdout


def test_cli_doctor_readiness_proof_unlocks_inert_first_resource_actions(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "tailscale-auth-key",
        SecretDecl(name="tailscale-auth-key", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: cast("Config", SimpleNamespace()))
    monkeypatch.setattr("agentworks.bootstrap.load_guide_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.db.DB_PATH", SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr("agentworks.db.Database", lambda read_only: db)

    result = CliRunner().invoke(
        app,
        [
            "guide",
            "concept-onboarding",
            "--agent",
            "--evidence",
            "run-doctor:onboarding/doctor-readiness=verified",
            "--evidence",
            "verify-named-secret:secret/tailscale-auth-key=verified",
        ],
    )

    assert result.exit_code == 0
    assert "`onboarding/doctor-readiness`: done" in result.stdout
    assert "### `create-first-vm`" in result.stdout
    assert "### `create-first-session`" in result.stdout


def test_cli_replays_refusal_as_manual_alternative_without_repeating_action(
    monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "tailscale-auth-key",
        SecretDecl(name="tailscale-auth-key", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: cast("Config", SimpleNamespace()))
    monkeypatch.setattr("agentworks.bootstrap.load_guide_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.db.DB_PATH", SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr("agentworks.db.Database", lambda read_only: db)

    result = CliRunner().invoke(
        app,
        [
            "guide",
            "concept-onboarding",
            "--agent",
            "--evidence",
            "verify-named-secret:secret/tailscale-auth-key=refused",
        ],
    )

    assert result.exit_code == 0
    assert "`secret/tailscale-auth-key`: unverifiable" in result.stdout
    assert _action("verify-named-secret").refusal_alternative in result.stdout
    assert "### `verify-named-secret`" not in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["--names-only"],
        [],
        ["concept-management"],
    ],
    ids=["names-only", "index", "non-onboarding-topic"],
)
def test_cli_rejects_evidence_for_shapes_that_cannot_consume_it(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    loaded = False

    def load_config(**kwargs: object) -> object:
        nonlocal loaded
        loaded = True
        return object()

    monkeypatch.setattr("agentworks.config.load_config", load_config)
    result = CliRunner().invoke(
        app,
        ["guide", *arguments, "--evidence", "verify-named-secret:secret/token=verified"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert isinstance(result.exception, ValidationError)
    assert loaded is False


@pytest.mark.parametrize(
    "records",
    [
        [
            "verify-named-secret:secret/tailscale-auth-key=verified",
            "verify-named-secret:secret/tailscale-auth-key=refused",
        ],
        ["unknown-action:secret/tailscale-auth-key=verified"],
        ["verify-vm-connection:secret/tailscale-auth-key=verified"],
        ["verify-named-secret:secret/missing=verified"],
    ],
    ids=["duplicate", "unknown-action", "mismatched-action", "inapplicable-target"],
)
def test_cli_rejects_semantically_invalid_evidence_before_rendering(
    monkeypatch: pytest.MonkeyPatch, db: Database, records: list[str]
) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "tailscale-auth-key",
        SecretDecl(name="tailscale-auth-key", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    monkeypatch.setattr("agentworks.config.load_config", lambda **kwargs: cast("Config", SimpleNamespace()))
    monkeypatch.setattr("agentworks.bootstrap.load_guide_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.db.DB_PATH", SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr("agentworks.db.Database", lambda read_only: db)
    arguments = ["guide", "concept-onboarding", "--agent"]
    for record in records:
        arguments.extend(("--evidence", record))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert isinstance(result.exception, ValidationError)


def test_cli_rejects_malformed_evidence_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = False

    def load_config(**kwargs: object) -> object:
        nonlocal loaded
        loaded = True
        return object()

    monkeypatch.setattr("agentworks.config.load_config", load_config)
    result = CliRunner().invoke(app, ["guide", "concept-onboarding", "--evidence", "not-evidence"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr
    assert loaded is False


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f", "\x80", "\x9f"])
def test_cli_rejects_control_bytes_in_evidence_without_echoing_them(control: str) -> None:
    value = f"verify-named-secret:secret/to{control}ken=verified"
    result = CliRunner().invoke(app, ["guide", "concept-onboarding", "--evidence", value])
    assert result.exit_code == 2
    assert result.stderr
    assert f"to{control}ken" not in result.stderr


def test_cli_rejects_line_break_in_evidence_without_echoing_the_value() -> None:
    value = "verify-named-secret:secret/to\nken=verified"
    result = CliRunner().invoke(app, ["guide", "concept-onboarding", "--evidence", value])
    assert result.exit_code == 2
    assert value not in result.stderr


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("sec\x1bret", "token"),
        ("sec\x80ret", "token"),
        ("secret", "to\x1bken"),
        ("secret", "to\x80ken"),
    ],
)
def test_service_rejects_control_bytes_in_evidence_identity(
    kind: str,
    name: str,
    db: Database,
) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "token",
        SecretDecl(name="token", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    evidence = (_evidence("verify-named-secret", kind, name, VerificationOutcome.VERIFIED),)

    with pytest.raises(ValidationError) as raised:
        render_guide(
            ("concept-onboarding",),
            GuideMode.AGENT,
            load_config_fn=lambda: cast("Config", SimpleNamespace()),
            load_registry_fn=lambda config: registry,
            db=db,
            verification_evidence=evidence,
        )

    assert "\x1b" not in str(raised.value)
    assert "\x80" not in str(raised.value)


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("plugin/guide", "topic"),
        ("plugin", "guide/topic"),
    ],
)
def test_service_rejects_evidence_with_slash_outside_selector_boundary(
    kind: str,
    name: str,
    db: Database,
) -> None:
    registry = Registry.empty()
    registry.add(
        "secret",
        "token",
        SecretDecl(name="token", description=""),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    evidence = (_evidence("verify-named-secret", kind, name, VerificationOutcome.VERIFIED),)

    with pytest.raises(ValidationError):
        render_guide(
            ("concept-onboarding",),
            GuideMode.AGENT,
            load_config_fn=lambda: cast("Config", SimpleNamespace()),
            load_registry_fn=lambda config: registry,
            db=db,
            verification_evidence=evidence,
        )

    baseline = render_guide(
        ("concept-onboarding",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", SimpleNamespace()),
        load_registry_fn=lambda config: registry,
        db=db,
    )
    assert "### `verify-named-secret`" in baseline.markdown


@pytest.mark.parametrize(
    "evidence",
    [
        [_evidence("verify-named-secret", "secret", "token", VerificationOutcome.VERIFIED)],
        (object(),),
        (_evidence("unknown", "secret", "token", VerificationOutcome.VERIFIED),),
        (_evidence("verify-vm-connection", "secret", "token", VerificationOutcome.VERIFIED),),
        (_evidence("verify-named-secret", "secret", "other", VerificationOutcome.VERIFIED),),
        (_evidence("verify-named-secret", "workspace-template", "ready", VerificationOutcome.VERIFIED),),
    ],
)
def test_malformed_mismatched_and_inapplicable_evidence_is_rejected(evidence: object) -> None:
    with pytest.raises(ValidationError):
        assess_onboarding(
            _snapshot(_fact("secret", "token"), _fact("workspace-template", "ready")),
            verification_evidence=evidence,  # type: ignore[arg-type]
        )


def test_proof_actions_run_once_and_creation_actions_use_json_verification() -> None:
    proof_ids = {"run-doctor", "verify-named-secret", "verify-vm-connection"}
    assert all(action.verification is None for action in onboarding_actions() if action.id in proof_ids)
    assert _action("create-first-vm").verification == ("agw", "vm", "describe", "$VM_NAME", "--output", "json")
    assert _action("create-first-session").verification == (
        "agw",
        "session",
        "describe",
        "$SESSION_NAME",
        "--output",
        "json",
    )


def test_action_validation_occurs_only_when_guide_operation_requests_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.assessment as module

    calls: list[object] = []

    def record_action(action, source):
        calls.append(action)
        return action

    monkeypatch.setattr(module, "validate_guide_action", record_action)
    assert calls == []
    module.onboarding_actions()
    assert calls == list(module._ACTION_RECORDS)


def test_direct_projection_composes_snapshot_and_renders_target_scoped_evidence(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    class Handler:
        kind = "assessment-test"
        category = "declarable"
        description = "Assessment test resources."
        miss_policy = "error"
        auto_declare_names: frozenset[str] = frozenset()
        builtin_override = "allow"

        def instances(self, db: object, registry: Registry, resource: object):
            yield InstanceRef("vm", "worker")
            yield InstanceRef("secret", "token")
            yield InstanceRef("session", "existing")

    @dataclass(frozen=True)
    class Node:
        reqs: tuple[ResourceReference, ...] = ()
        description: str | None = None
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
            return self.reqs

    monkeypatch.setitem(KIND_REGISTRY, "assessment-test", Handler())
    registry = Registry.empty()
    origin = Origin.built_in(source="assessment-test")
    registry.add(
        "assessment-test",
        "source",
        Node(
            (ResourceReference("target", "assessment-test", "source uses target", ("assessment-test", "source")),),
            description="Source resource.",
        ),
        origin,
    )
    registry.add("assessment-test", "target", Node(), origin)
    registry.finalize()
    snapshot = build_onboarding_snapshot(registry, db)
    assert [fact.identity.name for fact in snapshot.resources if fact.identity.kind == "assessment-test"] == [
        "source",
        "target",
    ]
    assert tuple(item for item in snapshot.instances if item.kind in {"secret", "session", "vm"}) == (
        GuideInstanceFact("secret", "token"),
        GuideInstanceFact("session", "existing"),
        GuideInstanceFact("vm", "worker"),
    )
    assert [
        (item.source.name, item.target.name, item.usage)
        for item in snapshot.relationships
        if item.source.kind == "assessment-test"
    ] == [("source", "target", "source uses target")]

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("denied power invoked")

    monkeypatch.setattr("agentworks.output.prompt", denied)
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", denied)
    monkeypatch.setattr("agentworks.transports.transport", denied)
    pending = render_guide(
        ("concept-onboarding",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", SimpleNamespace()),
        load_registry_fn=lambda config: registry,
        db=db,
    )
    vm_action = _action("verify-vm-connection")
    action_start = pending.markdown.index(f"### `{vm_action.id}`")
    action_end = pending.markdown.find("\n### `", action_start + 1)
    rendered_action = pending.markdown[action_start : action_end if action_end >= 0 else None]
    assert vm_action.refusal_alternative in rendered_action

    evidence = (
        _evidence("verify-named-secret", "secret", "tailscale-auth-key", VerificationOutcome.VERIFIED),
        _evidence("verify-vm-connection", "vm", "worker", VerificationOutcome.VERIFIED),
    )
    response = render_guide(
        ("concept-onboarding",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", SimpleNamespace()),
        load_registry_fn=lambda config: registry,
        db=db,
        verification_evidence=evidence,
    )
    assert "### `" not in response.markdown
    assert "`secret/tailscale-auth-key`: done" in response.markdown
    assert "`vm/worker`: done" in response.markdown

    refused = (_evidence("verify-vm-connection", "vm", "worker", VerificationOutcome.REFUSED),)
    outputs = [
        render_guide(
            ("concept-onboarding",),
            mode,
            load_config_fn=lambda: cast("Config", SimpleNamespace()),
            load_registry_fn=lambda config: registry,
            db=db,
            verification_evidence=refused,
        ).markdown
        for mode in (GuideMode.AGENT, GuideMode.HUMAN)
    ]
    for markdown in outputs:
        assert vm_action.refusal_alternative in markdown
        assert "### `verify-vm-connection`" not in markdown


def test_snapshot_does_not_rebuild_global_implementation_inventory_for_each_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.service as guide_service

    class Handler:
        def __init__(self, category: str) -> None:
            self.category = category
            self.description = "Snapshot test resources."

    class Graph:
        def readiness_of(self, kind: str, name: str) -> Readiness:
            return Readiness.ready()

        def enablement_of(self, kind: str, name: str) -> Enablement:
            return Enablement.enabled

        def edges_of(self, kind: str, name: str) -> tuple[object, ...]:
            return ()

        def dependents_of(self, kind: str, name: str) -> tuple[object, ...]:
            return ()

    class SnapshotRegistry:
        is_finalized = True
        graph = Graph()

        def __init__(self) -> None:
            self.iterated_kinds: list[str] = []

        def iter_kind_items(self, kind: str):
            self.iterated_kinds.append(kind)
            return iter((name, SimpleNamespace()) for name in ("first", "second", "third"))

        def lookup(self, kind: str, name: str) -> object:
            return SimpleNamespace()

    handlers = {"snapshot-capability": Handler("capability"), "snapshot-resource": Handler("declarable")}
    monkeypatch.setattr(guide_service, "KIND_REGISTRY", handlers)
    registry = SnapshotRegistry()

    snapshot = build_onboarding_snapshot(registry, SimpleNamespace())  # type: ignore[arg-type]

    assert [fact.identity for fact in snapshot.resources] == [
        GuideIdentity("snapshot-capability", name) for name in ("first", "second", "third")
    ] + [GuideIdentity("snapshot-resource", name) for name in ("first", "second", "third")]
    assert registry.iterated_kinds == ["snapshot-capability", "snapshot-resource"]


def test_snapshot_deduplication_keeps_first_seen_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.guide.service as guide_service

    class Handler:
        category = "declarable"

        def instances(self, db: object, registry: object, resource: object):
            if cast("SimpleNamespace", resource).name == "first":
                yield InstanceRef("vm", "a")
                yield InstanceRef("vm", "b")
            else:
                yield InstanceRef("vm", "b")
                yield InstanceRef("vm", "a")

    class Graph:
        def readiness_of(self, kind: str, name: str) -> Readiness:
            return Readiness.ready()

        def enablement_of(self, kind: str, name: str) -> Enablement:
            return Enablement.enabled

        def edges_of(self, kind: str, name: str):
            if name == "first":
                return (SimpleNamespace(kind="snapshot-test", name="second", usage="uses"),)
            return ()

        def dependents_of(self, kind: str, name: str):
            if name == "second":
                return (SimpleNamespace(source=("snapshot-test", "first"), usage="uses"),)
            return ()

    class SnapshotRegistry:
        is_finalized = True
        graph = Graph()

        def iter_kind_items(self, kind: str):
            assert kind == "snapshot-test"
            return iter(
                (
                    ("first", SimpleNamespace(name="first")),
                    ("second", SimpleNamespace(name="second")),
                )
            )

        def lookup(self, kind: str, name: str) -> object:
            assert kind == "snapshot-test"
            return SimpleNamespace(name=name)

    monkeypatch.setattr(guide_service, "KIND_REGISTRY", {"snapshot-test": Handler()})

    snapshot = build_onboarding_snapshot(SnapshotRegistry(), SimpleNamespace())  # type: ignore[arg-type]

    assert snapshot.instances == (GuideInstanceFact("vm", "a"), GuideInstanceFact("vm", "b"))
    assert snapshot.relationships == (
        GuideRelationship(
            GuideIdentity("snapshot-test", "first"),
            GuideIdentity("snapshot-test", "second"),
            "uses",
        ),
    )


def test_snapshot_requires_finalized_registry() -> None:
    registry = SimpleNamespace(is_finalized=False)

    with pytest.raises(GuideTraversalError):
        build_onboarding_snapshot(cast("Registry", registry), cast("Database", SimpleNamespace()))


def test_missing_finalized_lookup_remains_a_typed_traversal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.guide.service as guide_service

    class RegistryWithMissingLookup:
        is_finalized = True

        def iter_kind_items(self, kind: str):
            return iter((("missing", object()),))

        def lookup(self, kind: str, name: str) -> object:
            raise KeyError((kind, name))

    monkeypatch.setattr(
        guide_service,
        "KIND_REGISTRY",
        {"snapshot-test": SimpleNamespace(category="declarable")},
    )

    with pytest.raises(GuideTraversalError):
        build_onboarding_snapshot(
            cast("Registry", RegistryWithMissingLookup()),
            cast("Database", SimpleNamespace()),
        )


def test_handler_failures_degrade_only_the_onboarding_assessment(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.guide.service as guide_service

    failure = StateError("projected instance state is unavailable")

    class Handler:
        kind = "snapshot-test"
        category = "declarable"
        description = "Snapshot test resources."
        miss_policy = "error"
        auto_declare_names: frozenset[str] = frozenset()
        builtin_override = "allow"

        def instances(self, db: object, registry: object, resource: object):
            raise failure
            yield

    @dataclass(frozen=True)
    class Node:
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
            return ()

    monkeypatch.setitem(KIND_REGISTRY, "snapshot-test", Handler())
    registry = Registry.empty()
    registry.add("snapshot-test", "one", Node(), Origin.built_in(source="test"))
    registry.finalize()
    monkeypatch.setattr(guide_service, "KIND_REGISTRY", {"snapshot-test": Handler()})
    captured: list[StateError] = []
    real_framed_error = guide_service._framed_error

    def capture(error: StateError) -> str:
        captured.append(error)
        return real_framed_error(error)

    monkeypatch.setattr(guide_service, "_framed_error", capture)
    response = render_guide(
        ("concept-onboarding",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", SimpleNamespace()),
        load_registry_fn=lambda config: registry,
        db=cast("Database", SimpleNamespace()),
    )

    assert response.exit_code == 0
    assert captured == [failure]
