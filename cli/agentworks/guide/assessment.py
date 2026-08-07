"""Pure onboarding assessment and inert action selection over guide facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentworks.errors import ValidationError
from agentworks.guide.contract import (
    ActionId,
    ActionInput,
    ConsentBoundary,
    GuideAction,
    validate_guide_action,
)
from agentworks.guide.view import GuideIdentity, GuideInstanceFact, GuideRelationship, GuideResourceFact


class OnboardingStatus(Enum):
    """A status justified by projected facts and explicit caller evidence."""

    DONE = "done"
    NOT_READY = "not-ready"
    DISABLED = "disabled"
    UNVERIFIABLE = "unverifiable"


class VerificationOutcome(Enum):
    """Caller-owned evidence from an explicitly authorized verification action."""

    VERIFIED = "verified"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """A verification outcome scoped to exactly one projected target."""

    action_id: ActionId
    target: GuideIdentity
    outcome: VerificationOutcome


@dataclass(frozen=True, slots=True)
class OnboardingSnapshot:
    """Bounded facts composed from anchor-scoped guide views."""

    resources: tuple[GuideResourceFact, ...]
    instances: tuple[GuideInstanceFact, ...]
    relationships: tuple[GuideRelationship, ...]


@dataclass(frozen=True, slots=True)
class OnboardingFinding:
    """Assessment of one projected registry or stored-instance fact."""

    identity: GuideIdentity
    status: OnboardingStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class OnboardingRelationshipFinding:
    """A projected dependency retained as a first-class assessment fact."""

    relationship: GuideRelationship
    status: OnboardingStatus


@dataclass(frozen=True, slots=True)
class OnboardingSummary:
    """Counts that preserve every per-finding status without precedence folding."""

    done: int
    not_ready: int
    disabled: int
    unverifiable: int


@dataclass(frozen=True, slots=True)
class OnboardingAssessment:
    """Deterministic findings and the actions still applicable to them."""

    findings: tuple[OnboardingFinding, ...]
    relationship_findings: tuple[OnboardingRelationshipFinding, ...]
    actions: tuple[GuideAction, ...]

    @property
    def summary(self) -> OnboardingSummary:
        """Count statuses without allowing one class to mask another."""
        counts = {status: 0 for status in OnboardingStatus}
        for finding in self.findings:
            counts[finding.status] += 1
        for relationship_finding in self.relationship_findings:
            counts[relationship_finding.status] += 1
        return OnboardingSummary(
            counts[OnboardingStatus.DONE],
            counts[OnboardingStatus.NOT_READY],
            counts[OnboardingStatus.DISABLED],
            counts[OnboardingStatus.UNVERIFIABLE],
        )

    @property
    def action_ids(self) -> tuple[ActionId, ...]:
        """Expose the ordered inert plan without duplicating action records."""
        return tuple(action.id for action in self.actions)


_ACTION_RECORDS = (
    GuideAction(
        ActionId("run-doctor"),
        "A projected resource is not ready and the stored reason needs host diagnosis.",
        (),
        ConsentBoundary.EXAMINE_WORKSTATION,
        ("agw", "doctor"),
        "Agentworks reports configuration and host-readiness checks without applying repairs.",
        None,
        "Keep the stored not-ready reason and troubleshoot manually without probing the workstation.",
    ),
    GuideAction(
        ActionId("verify-named-secret"),
        "A ready named secret declaration still needs resolution proof.",
        (ActionInput("SECRET_NAME", "Registered secret name.", True, sensitive=False),),
        ConsentBoundary.RESOLVE_NAMED_SECRET,
        ("agw", "secret", "verify", "$SECRET_NAME"),
        "The named secret resolves once without its value crossing the verification boundary.",
        None,
        "Use describe and backend readiness as prediction only; mark this secret unverifiable.",
    ),
    GuideAction(
        ActionId("verify-vm-connection"),
        "A stored named VM exists but connectivity has not been proved.",
        (ActionInput("VM_NAME", "Stored VM name.", True, sensitive=False),),
        ConsentBoundary.CONNECT_NAMED_VM,
        ("agw", "vm", "verify-connection", "$VM_NAME"),
        "One non-mutating no-op command confirms connectivity to the named VM.",
        None,
        "Retain the stored VM fact and mark connectivity unverifiable without connecting.",
    ),
)


def onboarding_actions() -> tuple[GuideAction, ...]:
    """Build and validate action content only inside a guide-scoped operation."""
    return tuple(validate_guide_action(action, "core:onboarding") for action in _ACTION_RECORDS)


def _applicable_action(identity: GuideIdentity, status: OnboardingStatus) -> ActionId | None:
    if status is OnboardingStatus.NOT_READY:
        return ActionId("run-doctor")
    if status is OnboardingStatus.UNVERIFIABLE and identity.kind == "secret":
        return ActionId("verify-named-secret")
    if status is OnboardingStatus.UNVERIFIABLE and identity.kind == "vm":
        return ActionId("verify-vm-connection")
    return None


def _validate_evidence(
    evidence: tuple[VerificationEvidence, ...],
    applicable: dict[GuideIdentity, ActionId],
    known_ids: frozenset[ActionId],
) -> dict[GuideIdentity, VerificationEvidence]:
    if type(evidence) is not tuple:
        raise ValidationError("verification evidence must be a tuple")
    by_target: dict[GuideIdentity, VerificationEvidence] = {}
    for item in evidence:
        if type(item) is not VerificationEvidence:
            raise ValidationError("verification evidence must contain exact VerificationEvidence records")
        if type(item.action_id) is not str or item.action_id not in known_ids:
            raise ValidationError(f"unknown onboarding action {item.action_id!r}")
        if type(item.target) is not GuideIdentity or type(item.outcome) is not VerificationOutcome:
            raise ValidationError("verification evidence has an invalid target or outcome")
        if item.target in by_target:
            raise ValidationError(f"duplicate verification evidence for {item.target.kind}/{item.target.name}")
        expected = applicable.get(item.target)
        if expected is None:
            raise ValidationError(
                f"verification evidence target {item.target.kind}/{item.target.name} is not applicable"
            )
        if item.action_id != expected:
            raise ValidationError(
                f"onboarding action {item.action_id!s} does not match target {item.target.kind}/{item.target.name}"
            )
        by_target[item.target] = item
    return by_target


def assess_onboarding(
    snapshot: OnboardingSnapshot,
    *,
    verification_evidence: tuple[VerificationEvidence, ...] = (),
) -> OnboardingAssessment:
    """Derive onboarding state without loading config, probing, or executing actions."""
    if type(snapshot) is not OnboardingSnapshot:
        raise ValidationError("onboarding assessment requires an exact OnboardingSnapshot")
    if any(type(items) is not tuple for items in (snapshot.resources, snapshot.instances, snapshot.relationships)):
        raise ValidationError("onboarding snapshot fact collections must be tuples")
    if any(type(item) is not GuideResourceFact for item in snapshot.resources):
        raise ValidationError("onboarding snapshot resources must be exact GuideResourceFact records")
    if any(type(item) is not GuideInstanceFact for item in snapshot.instances):
        raise ValidationError("onboarding snapshot instances must be exact GuideInstanceFact records")
    if any(type(item) is not GuideRelationship for item in snapshot.relationships):
        raise ValidationError("onboarding snapshot relationships must be exact GuideRelationship records")
    actions = onboarding_actions()
    findings: list[OnboardingFinding] = []
    for fact in snapshot.resources:
        if not fact.verdict.enabled:
            findings.append(OnboardingFinding(fact.identity, OnboardingStatus.DISABLED, fact.verdict.reason))
        elif not fact.verdict.ready:
            findings.append(OnboardingFinding(fact.identity, OnboardingStatus.NOT_READY, fact.verdict.reason))
        elif fact.identity.kind in {"secret", "vm"} and fact.identity.name != fact.identity.kind:
            findings.append(
                OnboardingFinding(fact.identity, OnboardingStatus.UNVERIFIABLE, "Explicit proof has not been recorded.")
            )
        else:
            findings.append(OnboardingFinding(fact.identity, OnboardingStatus.DONE, None))

    known = {finding.identity for finding in findings}
    for instance in snapshot.instances:
        identity = GuideIdentity(instance.kind, instance.name)
        if identity in known:
            continue
        status = OnboardingStatus.UNVERIFIABLE if identity.kind == "vm" else OnboardingStatus.DONE
        findings.append(OnboardingFinding(identity, status, "Stored instance exists."))
        known.add(identity)

    if not findings:
        findings.append(
            OnboardingFinding(
                GuideIdentity("onboarding", "projected-state"),
                OnboardingStatus.UNVERIFIABLE,
                "The supplied guide view exposes no assessable facts.",
            )
        )

    applicable = {
        finding.identity: action_id
        for finding in findings
        if (action_id := _applicable_action(finding.identity, finding.status)) is not None
    }
    evidence = _validate_evidence(verification_evidence, applicable, frozenset(action.id for action in actions))
    revised: list[OnboardingFinding] = []
    for finding in findings:
        evidence_item = evidence.get(finding.identity)
        if evidence_item is None:
            revised.append(finding)
            continue
        action = next(action for action in actions if action.id == evidence_item.action_id)
        if evidence_item.outcome is VerificationOutcome.REFUSED:
            revised.append(
                OnboardingFinding(finding.identity, OnboardingStatus.UNVERIFIABLE, action.refusal_alternative)
            )
        elif evidence_item.outcome is VerificationOutcome.FAILED:
            revised.append(
                OnboardingFinding(finding.identity, OnboardingStatus.NOT_READY, "Explicit verification failed.")
            )
        elif evidence_item.action_id == ActionId("run-doctor"):
            revised.append(finding)
        else:
            revised.append(OnboardingFinding(finding.identity, OnboardingStatus.DONE, None))

    selected_ids: set[ActionId] = set()
    for finding in revised:
        action_id = _applicable_action(finding.identity, finding.status)
        evidence_item = evidence.get(finding.identity)
        if action_id is not None and (evidence_item is None or evidence_item.action_id != action_id):
            selected_ids.add(action_id)
    selected = tuple(action for action in actions if action.id in selected_ids)
    relationships = tuple(
        OnboardingRelationshipFinding(relationship, OnboardingStatus.DONE) for relationship in snapshot.relationships
    )
    return OnboardingAssessment(tuple(revised), relationships, selected)


def guided_actions(assessment: OnboardingAssessment) -> tuple[GuideAction, ...]:
    """Return the canonical actions for a consent-prompting consumer."""
    return assessment.actions


def replayable_actions(assessment: OnboardingAssessment) -> tuple[GuideAction, ...]:
    """Return the same canonical actions for a non-interactive consumer."""
    return assessment.actions
