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
from agentworks.errors import ValidationError
from agentworks.guide import (
    ActionId,
    GuideIdentity,
    GuideInstanceFact,
    GuideMode,
    GuideOrigin,
    GuideRelationship,
    GuideResourceFact,
    GuideVerdict,
    OnboardingSnapshot,
    OnboardingStatus,
    VerificationEvidence,
    VerificationOutcome,
    assess_onboarding,
    guided_actions,
    onboarding_actions,
    render_guide,
    replayable_actions,
)
from agentworks.guide.service import build_onboarding_snapshot
from agentworks.resources import KIND_REGISTRY, Origin, Registry, ResourceReference
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
        "capability" if kind.endswith("backend") else "declarable",
        None,
        GuideOrigin("operator-declared", None),
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
    assert assessment.action_ids == (ActionId("run-doctor"), ActionId("verify-vm-connection"))


@pytest.mark.parametrize(
    ("outcome", "expected", "expected_actions"),
    [
        (VerificationOutcome.VERIFIED, OnboardingStatus.DONE, ()),
        (VerificationOutcome.FAILED, OnboardingStatus.NOT_READY, (ActionId("run-doctor"),)),
        (VerificationOutcome.REFUSED, OnboardingStatus.UNVERIFIABLE, ()),
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


def test_guided_and_replayable_outcomes_and_refusal_alternatives_are_equal() -> None:
    evidence = (_evidence("verify-vm-connection", "vm", "worker", VerificationOutcome.REFUSED),)
    guided = assess_onboarding(
        _snapshot(instances=(GuideInstanceFact("vm", "worker"),)), verification_evidence=evidence
    )
    replayable = assess_onboarding(
        _snapshot(instances=(GuideInstanceFact("vm", "worker"),)), verification_evidence=evidence
    )
    assert guided.findings == replayable.findings
    assert guided_actions(guided) == replayable_actions(replayable) == ()
    assert guided.findings[0].status is OnboardingStatus.UNVERIFIABLE
    assert guided.findings[0].reason == onboarding_actions()[2].refusal_alternative


def test_ready_rerun_with_accepted_proof_is_a_clean_no_op() -> None:
    assessment = assess_onboarding(
        _snapshot(_fact("secret", "token")),
        verification_evidence=(_evidence("verify-named-secret", "secret", "token", VerificationOutcome.VERIFIED),),
    )
    assert assessment.findings[0].status is OnboardingStatus.DONE
    assert assessment.actions == ()


@pytest.mark.parametrize("kind", ["secret", "vm"])
def test_resource_named_after_its_kind_still_requires_explicit_proof(kind: str) -> None:
    assessment = assess_onboarding(_snapshot(_fact(kind, kind)))
    assert assessment.findings[0].status is OnboardingStatus.UNVERIFIABLE
    expected = "verify-named-secret" if kind == "secret" else "verify-vm-connection"
    assert assessment.action_ids == (ActionId(expected),)


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
    assert "Use describe and backend readiness as prediction only" in result.stdout
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
    assert "verification evidence" in str(result.exception)
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
    assert "ACTION_ID:KIND/NAME=verified|failed|refused" in result.stderr
    assert loaded is False


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f", "\x80", "\x9f"])
def test_cli_rejects_control_bytes_in_evidence_without_echoing_them(control: str) -> None:
    value = f"verify-named-secret:secret/to{control}ken=verified"
    result = CliRunner().invoke(app, ["guide", "concept-onboarding", "--evidence", value])
    assert result.exit_code == 2
    assert control not in result.stderr
    assert "ACTION_ID:KIND/NAME=verified|failed|refused" in result.stderr


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

    assert str(raised.value) == "verification evidence has an invalid target or outcome"
    assert "\x1b" not in str(raised.value)
    assert "\x80" not in str(raised.value)


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


def test_verification_actions_run_once_and_have_no_second_verification_command() -> None:
    assert all(action.verification is None for action in onboarding_actions())


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
    assert len(calls) == 3


def test_real_views_compose_snapshot_and_render_target_scoped_evidence(
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
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
            return self.reqs

    monkeypatch.setitem(KIND_REGISTRY, "assessment-test", Handler())
    registry = Registry.empty()
    origin = Origin.built_in(source="assessment-test")
    registry.add(
        "assessment-test",
        "source",
        Node((ResourceReference("target", "assessment-test", "source uses target", ("assessment-test", "source")),)),
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
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_secrets", denied)
    monkeypatch.setattr("agentworks.transports.transport", denied)
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
    assert "No onboarding actions are needed" in response.markdown
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
        assert "Retain the stored VM fact and mark connectivity unverifiable" in markdown
        assert "### `verify-vm-connection`" not in markdown
