from __future__ import annotations

from agentworks.guide import (
    ActionId,
    GuideIdentity,
    GuideOrigin,
    GuideResourceFact,
    GuideVerdict,
    OnboardingSnapshot,
    OnboardingStatus,
    assess_onboarding,
)


def test_unavailable_readiness_assesses_as_unverifiable() -> None:
    reason = "host readiness unavailable: guide does not inspect the workstation"
    fact = GuideResourceFact(
        GuideIdentity("vm-site", "lima-local"),
        "declarable",
        None,
        GuideOrigin("built-in", None),
        GuideVerdict(enabled=True, ready=False, is_available=False, reason=reason),
    )

    assessment = assess_onboarding(OnboardingSnapshot((fact,), (), ()))

    assert assessment.findings[0].status is OnboardingStatus.UNVERIFIABLE
    assert assessment.findings[0].reason == reason
    assert assessment.action_ids == (ActionId("run-doctor"),)
