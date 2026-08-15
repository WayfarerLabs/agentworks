from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import ConfigError
from agentworks.guide import (
    GuideIdentity,
    GuideMode,
    GuideTraversalError,
    UnknownGuideTopicError,
    VerificationEvidence,
)
from agentworks.guide.assessment import VerificationOutcome
from agentworks.guide.contract import ActionId
from agentworks.guide.service import _EmptyInventory, render_guide
from agentworks.guide.trail_sign import TRAIL_DESTINATIONS

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources import Registry


def _broken_config() -> Config:
    raise ConfigError("fixture configuration is malformed")


@pytest.mark.parametrize("mode", tuple(GuideMode))
def test_no_topic_trail_sign_bypasses_catalogs_and_live_context(
    mode: GuideMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog or live context was loaded")

    monkeypatch.setattr("agentworks.guide.service.build_authored_catalog", forbidden)
    monkeypatch.setattr("agentworks.guide.service._build_schema_catalog", forbidden)

    response = render_guide(
        (),
        mode,
        load_config_fn=forbidden,  # type: ignore[arg-type]
        load_registry_fn=forbidden,  # type: ignore[arg-type]
    )

    expected = {
        GuideMode.AGENT: (
            "concept-onboarding",
            "concept-management",
            "concept-troubleshooting",
            "concept-release-notes",
            "concept-migration",
            "concept-secrets",
            "concept-reporting-bugs",
        ),
        GuideMode.HUMAN: ("concept-onboarding", "concept-management"),
    }
    assert response.exit_code == 0
    assert response.names == expected[mode]


def test_every_fixed_destination_resolves_through_selected_topic_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_trail_sign(*args: object, **kwargs: object) -> None:
        raise AssertionError("selected topics used the no-topic renderer")

    monkeypatch.setattr("agentworks.guide.service.render_trail_sign", forbidden_trail_sign)

    for destination in TRAIL_DESTINATIONS:
        response = render_guide(
            (str(destination.slug),),
            GuideMode.AGENT,
            load_config_fn=_broken_config,
        )
        assert response.exit_code == 0


def test_static_selected_topic_does_not_load_live_context() -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("static topic loaded live context")

    response = render_guide(
        ("concept-troubleshooting",),
        GuideMode.AGENT,
        load_config_fn=forbidden,  # type: ignore[arg-type]
        load_registry_fn=forbidden,  # type: ignore[arg-type]
    )

    assert response.exit_code == 0


def test_shared_live_failure_is_one_warning_with_every_omitted_identity() -> None:
    response = render_guide(
        ("concept-onboarding", "concept-management"),
        GuideMode.AGENT,
        load_config_fn=_broken_config,
    )

    assert response.exit_code == 0
    assert response.markdown.count("fixture configuration is malformed") == 1
    assert "concept-onboarding/inventory" in response.markdown
    assert "concept-onboarding/derived-plan" in response.markdown
    assert "concept-management/inventory" in response.markdown


def test_known_exact_resource_degrades_but_healthy_absence_is_unknown() -> None:
    degraded = render_guide(("vm-template/missing",), GuideMode.AGENT, load_config_fn=_broken_config)
    assert degraded.exit_code == 0

    class EmptyRegistry:
        is_finalized = True

        def iter_kind_items(self, kind: str):
            return iter(())

    config = cast("Config", object())
    registry = cast("Registry", EmptyRegistry())
    with pytest.raises(UnknownGuideTopicError):
        render_guide(
            ("vm-template/missing",),
            GuideMode.AGENT,
            load_config_fn=lambda: config,
            load_registry_fn=lambda _config: registry,
        )


def test_unverifiable_well_formed_evidence_stays_unapplied() -> None:
    evidence = VerificationEvidence(
        ActionId("run-doctor"),
        GuideIdentity("onboarding", "doctor-readiness"),
        VerificationOutcome.VERIFIED,
    )

    response = render_guide(
        ("concept-onboarding",),
        GuideMode.AGENT,
        load_config_fn=_broken_config,
        verification_evidence=(evidence,),
    )

    assert response.exit_code == 0
    assert "concept-onboarding/derived-plan" in response.markdown


def test_structural_onboarding_snapshot_failure_remains_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyRegistry:
        is_finalized = True

        def iter_kind_items(self, kind: str):
            return iter(())

    def broken_snapshot(*args: object) -> None:
        raise GuideTraversalError("structural assessment defect")

    monkeypatch.setattr("agentworks.guide.service.build_onboarding_snapshot", broken_snapshot)
    with pytest.raises(GuideTraversalError):
        render_guide(
            ("concept-onboarding",),
            GuideMode.AGENT,
            load_config_fn=lambda: cast("Config", object()),
            load_registry_fn=lambda config: cast("Registry", EmptyRegistry()),
            db=cast("Database", _EmptyInventory()),
        )


def test_names_only_keeps_static_names_and_emits_no_diagnostic_lines() -> None:
    response = render_guide((), GuideMode.AGENT, names_only=True, load_config_fn=_broken_config)

    assert response.exit_code == 0
    assert tuple(response.markdown.splitlines()) == response.names
    assert set(str(destination.slug) for destination in TRAIL_DESTINATIONS) <= set(response.names)


def test_names_only_omits_names_from_an_unfinalized_registry() -> None:
    class UnfinalizedRegistry:
        is_finalized = False

        def iter_kind_items(self, kind: str):
            return iter((("ghost", object()),))

    response = render_guide(
        (),
        GuideMode.AGENT,
        names_only=True,
        load_config_fn=lambda: cast("Config", object()),
        load_registry_fn=lambda _config: cast("Registry", UnfinalizedRegistry()),
    )

    assert response.exit_code == 0
    assert "vm-template/ghost" not in response.names
