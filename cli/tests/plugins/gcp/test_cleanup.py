"""Bounded GCE rollback, survivor protection, and interrupt semantics."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.plugins.gcp.cleanup import (
    CleanupCoordinates,
    InstanceOwnership,
    InstanceState,
    RollbackReport,
    rollback_after_interrupt,
    rollback_partial_create,
    rollback_then_raise,
)
from agentworks.plugins.gcp.network import FirewallOwnership, FirewallState

_COORDINATES = CleanupCoordinates(
    project_id="project-a",
    zone="us-central1-a",
    instance_name="vm-a",
    allow_rule="vm-a-allow",
    deny_rule="vm-a-deny",
)
_NETWORK = "projects/project-a/global/networks/default"
_INSTANCE_OWNERSHIP = InstanceOwnership("201")
_ALLOW_OWNERSHIP = FirewallOwnership(_COORDINATES.allow_rule, "101")
_DENY_OWNERSHIP = FirewallOwnership(_COORDINATES.deny_rule, "102")


def _api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class _Operation:
    error_code = None

    def __init__(self, log: list[str], name: str, failure: Exception | None = None) -> None:
        self.log = log
        self.name = name
        self.failure = failure

    def result(self, *, timeout: float) -> None:
        self.log.append(f"wait:{self.name}:{timeout}")
        if self.failure is not None:
            raise self.failure


class _Instances:
    def __init__(self, states: Iterator[object | Exception | None], log: list[str]) -> None:
        self.states = states
        self.log = log

    def get(self, **_kwargs: object) -> object:
        self.log.append("instance:get")
        state = next(self.states)
        if isinstance(state, Exception):
            raise state
        if state is None:
            raise _api_error(api_exceptions.NotFound, "gone")
        return state

    def delete(self, **_kwargs: object) -> _Operation:
        self.log.append("instance:delete")
        return _Operation(self.log, "instance")


class _Firewalls:
    def __init__(
        self,
        states: dict[str, Iterator[compute_v1.Firewall | Exception | None]],
        log: list[str],
    ) -> None:
        self.states = states
        self.log = log

    def get(self, *, firewall: str, **_kwargs: object) -> compute_v1.Firewall:
        self.log.append(f"firewall:get:{firewall}")
        state = next(self.states[firewall])
        if isinstance(state, Exception):
            raise state
        if state is None:
            raise _api_error(api_exceptions.NotFound, "gone")
        return state

    def delete(self, *, firewall: str, **_kwargs: object) -> _Operation:
        self.log.append(f"firewall:delete:{firewall}")
        return _Operation(self.log, firewall)


def _allow(*, priority: int = 0) -> compute_v1.Firewall:
    return compute_v1.Firewall(
        id=101,
        name=_COORDINATES.allow_rule,
        network=_NETWORK,
        direction="INGRESS",
        priority=priority,
        target_tags=["vm-tag"],
        source_ranges=["203.0.113.7/32"],
        allowed=[compute_v1.Allowed(I_p_protocol="tcp", ports=["22"])],
    )


def _deny() -> compute_v1.Firewall:
    return compute_v1.Firewall(
        id=102,
        name=_COORDINATES.deny_rule,
        network=_NETWORK,
        direction="INGRESS",
        priority=1,
        target_tags=["vm-tag"],
        source_ranges=["0.0.0.0/0"],
        denied=[compute_v1.Denied(I_p_protocol="all")],
    )


def _instance(resource_id: int = 201) -> compute_v1.Instance:
    return compute_v1.Instance(id=resource_id, name=_COORDINATES.instance_name)


def test_total_rollback_closes_allow_before_instance_and_deny_after_absence() -> None:
    log: list[str] = []
    allow = _allow()
    deny = _deny()
    report = rollback_partial_create(
        instances=_Instances(iter([_instance(), None]), log),
        firewalls=_Firewalls(
            {
                allow.name: iter([allow, None]),
                deny.name: iter([deny, None]),
            },
            log,
        ),
        coordinates=_COORDINATES,
        expected_allow=allow,
        expected_deny=deny,
        allow_ownership=FirewallOwnership(allow.name, "101"),
        deny_ownership=FirewallOwnership(deny.name, "102"),
        instance_ownership=InstanceOwnership("201"),
        instance_possible=True,
        timeout=9,
    )
    assert report.clean
    assert log == [
        "firewall:get:vm-a-allow",
        "firewall:delete:vm-a-allow",
        "wait:vm-a-allow:9",
        "firewall:get:vm-a-allow",
        "instance:get",
        "instance:delete",
        "wait:instance:9",
        "instance:get",
        "firewall:get:vm-a-deny",
        "firewall:delete:vm-a-deny",
        "wait:vm-a-deny:9",
        "firewall:get:vm-a-deny",
    ]


def test_surviving_instance_keeps_deny_and_reports_exact_state() -> None:
    log: list[str] = []
    allow = _allow()
    deny = _deny()
    report = rollback_partial_create(
        instances=_Instances(iter([_instance(), _instance()]), log),
        firewalls=_Firewalls(
            {
                allow.name: iter([allow, None]),
                deny.name: iter([deny]),
            },
            log,
        ),
        coordinates=_COORDINATES,
        expected_allow=allow,
        expected_deny=deny,
        allow_ownership=FirewallOwnership(allow.name, "101"),
        deny_ownership=FirewallOwnership(deny.name, "102"),
        instance_ownership=InstanceOwnership("201"),
        instance_possible=True,
        timeout=9,
    )
    assert report.instance is InstanceState.SURVIVING
    assert report.instance_resource_id == "201"
    assert report.allow is FirewallState.ABSENT
    assert report.allow_resource_id is None
    assert report.deny is FirewallState.REALIZED
    assert report.deny_resource_id == "102"
    assert "firewall:delete:vm-a-deny" not in log


def test_indeterminate_instance_keeps_deny() -> None:
    log: list[str] = []
    allow = _allow()
    deny = _deny()
    unknown = _api_error(api_exceptions.ServiceUnavailable, "unknown")
    report = rollback_partial_create(
        instances=_Instances(iter([unknown, unknown]), log),
        firewalls=_Firewalls(
            {
                allow.name: iter([None]),
                deny.name: iter([deny]),
            },
            log,
        ),
        coordinates=_COORDINATES,
        expected_allow=allow,
        expected_deny=deny,
        allow_ownership=FirewallOwnership(allow.name, "101"),
        deny_ownership=FirewallOwnership(deny.name, "102"),
        instance_ownership=InstanceOwnership("201"),
        instance_possible=True,
        timeout=9,
    )
    assert report.instance is InstanceState.INDETERMINATE
    assert report.instance_resource_id is None
    assert report.deny is FirewallState.REALIZED
    assert "firewall:delete:vm-a-deny" not in log


def test_mismatched_rule_is_retained_not_deleted() -> None:
    log: list[str] = []
    allow = _allow()
    deny = _deny()
    report = rollback_partial_create(
        instances=_Instances(iter([]), log),
        firewalls=_Firewalls(
            {
                allow.name: iter([_allow(priority=1)]),
                deny.name: iter([deny, None]),
            },
            log,
        ),
        coordinates=_COORDINATES,
        expected_allow=allow,
        expected_deny=deny,
        allow_ownership=FirewallOwnership(allow.name, "101"),
        deny_ownership=FirewallOwnership(deny.name, "102"),
        instance_ownership=None,
        instance_possible=False,
        timeout=9,
    )
    assert report.allow is FirewallState.MISMATCHED
    assert "firewall:delete:vm-a-allow" not in log


@pytest.mark.parametrize(
    ("instance", "ownership", "expected_state", "observed_id"),
    [
        (_instance(202), InstanceOwnership("201"), InstanceState.COLLISION, "202"),
        (
            compute_v1.Instance(name=_COORDINATES.instance_name),
            InstanceOwnership("201"),
            InstanceState.INDETERMINATE,
            None,
        ),
        (_instance(201), None, InstanceState.INDETERMINATE, "201"),
    ],
    ids=("different-id", "missing-id", "missing-ownership"),
)
def test_instance_without_matching_provider_ownership_is_retained(
    instance: compute_v1.Instance,
    ownership: InstanceOwnership | None,
    expected_state: InstanceState,
    observed_id: str | None,
) -> None:
    log: list[str] = []
    allow = _allow()
    deny = _deny()
    report = rollback_partial_create(
        instances=_Instances(iter([instance]), log),
        firewalls=_Firewalls(
            {
                allow.name: iter([None]),
                deny.name: iter([deny]),
            },
            log,
        ),
        coordinates=_COORDINATES,
        expected_allow=allow,
        expected_deny=deny,
        allow_ownership=FirewallOwnership(allow.name, "101"),
        deny_ownership=FirewallOwnership(deny.name, "102"),
        instance_ownership=ownership,
        instance_possible=True,
        timeout=9,
    )
    assert report.instance is expected_state
    assert report.instance_resource_id == observed_id
    assert report.deny is FirewallState.REALIZED
    assert "instance:delete" not in log
    assert "firewall:delete:vm-a-deny" not in log


def test_ordinary_cleanup_cannot_replace_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = RuntimeError("safe primary")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)

    with pytest.raises(RuntimeError) as caught:
        rollback_then_raise(
            primary,
            lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
            _COORDINATES,
            instance_ownership=_INSTANCE_OWNERSHIP,
            allow_ownership=_ALLOW_OWNERSHIP,
            deny_ownership=_DENY_OWNERSHIP,
        )

    assert caught.value is primary
    assert len(warnings) == 1
    message = warnings[0]
    assert "GCE rollback failed unexpectedly" in message
    assert "project 'project-a'" in message
    assert "zone 'us-central1-a'" in message
    assert "instance 'vm-a'" in message
    assert "allow 'vm-a-allow'" in message
    assert "deny 'vm-a-deny'" in message
    assert "expected provider IDs: instance '201', allow '101', deny '102'" in message
    assert "Instance ownership is unknown" in message
    assert "gcloud compute instances delete vm-a --project project-a --zone us-central1-a" not in message
    assert "firewall-rules describe vm-a-allow --project project-a" in message
    assert "firewall-rules describe vm-a-deny --project project-a" in message
    assert "firewall-rules delete" not in message


def test_first_interrupt_rolls_back_and_reraises_original_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = KeyboardInterrupt("first")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)
    report = RollbackReport(InstanceState.ABSENT, FirewallState.ABSENT, FirewallState.ABSENT)

    with pytest.raises(KeyboardInterrupt) as caught:
        rollback_after_interrupt(
            primary,
            lambda: report,
            _COORDINATES,
            instance_ownership=_INSTANCE_OWNERSHIP,
            allow_ownership=_ALLOW_OWNERSHIP,
            deny_ownership=_DENY_OWNERSHIP,
        )

    assert caught.value is primary
    assert warnings == [
        "Interrupted: cleaning up partial GCE resources for 'vm-a', please wait (Ctrl-C again to abandon cleanup)..."
    ]


def test_second_interrupt_abandons_promptly_with_exact_safe_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = KeyboardInterrupt("first")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)

    with pytest.raises(KeyboardInterrupt) as caught:
        rollback_after_interrupt(
            primary,
            lambda: (_ for _ in ()).throw(KeyboardInterrupt("second")),
            _COORDINATES,
            instance_ownership=_INSTANCE_OWNERSHIP,
            allow_ownership=_ALLOW_OWNERSHIP,
            deny_ownership=_DENY_OWNERSHIP,
        )

    assert caught.value is primary
    message = "\n".join(warnings)
    assert "Cleanup abandoned" in message
    assert "project 'project-a'" in message
    assert "zone 'us-central1-a'" in message
    assert "instance 'vm-a'" in message
    assert "vm-a-allow" in message
    assert "vm-a-deny" in message
    assert "expected provider IDs: instance '201', allow '101', deny '102'" in message
    assert "gcloud compute instances delete vm-a --project project-a --zone us-central1-a" not in message
    assert "firewall-rules describe vm-a-allow --project project-a" in message
    assert "firewall-rules describe vm-a-deny --project project-a" in message
    assert "firewall-rules delete" not in message


def _warning_for_report(monkeypatch: pytest.MonkeyPatch, report: RollbackReport) -> str:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)
    primary = RuntimeError("primary")
    with pytest.raises(RuntimeError) as caught:
        rollback_then_raise(
            primary,
            lambda: report,
            _COORDINATES,
            instance_ownership=_INSTANCE_OWNERSHIP,
            allow_ownership=_ALLOW_OWNERSHIP,
            deny_ownership=_DENY_OWNERSHIP,
        )
    assert caught.value is primary
    assert len(warnings) == 1
    return warnings[0]


def test_manual_guidance_recommends_delete_only_for_verified_owned_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _warning_for_report(
        monkeypatch,
        RollbackReport(
            InstanceState.SURVIVING,
            FirewallState.REALIZED,
            FirewallState.REALIZED,
            instance_resource_id="201",
            allow_resource_id="101",
            deny_resource_id="102",
        ),
    )
    assert "Owned instance survivor (expected provider ID '201', observed '201')" in message
    assert "gcloud compute instances delete vm-a --project project-a --zone us-central1-a" in message
    assert "Owned allow rule survivor (expected provider ID '101', observed '101')" in message
    assert "gcloud compute firewall-rules delete vm-a-allow --project project-a" in message
    assert "Owned deny rule retained while instance absence is unproven" in message


def test_manual_guidance_for_provider_id_collisions_is_inspect_and_escalate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _warning_for_report(
        monkeypatch,
        RollbackReport(
            InstanceState.COLLISION,
            FirewallState.MISMATCHED,
            FirewallState.MISMATCHED,
            instance_resource_id="202",
            allow_resource_id="901",
            deny_resource_id="902",
        ),
    )
    assert "Same-name instance collision (expected provider ID '201', observed '202')" in message
    assert "Same-name allow rule collision or shape mismatch (expected provider ID '101', observed '901')" in message
    assert "Same-name deny rule collision or shape mismatch (expected provider ID '102', observed '902')" in message
    assert message.count("do not delete by name; escalate ownership") == 3
    assert "gcloud compute instances delete" not in message
    assert "gcloud compute firewall-rules delete" not in message


def test_manual_guidance_for_unknown_provider_ids_is_inspect_and_escalate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _warning_for_report(
        monkeypatch,
        RollbackReport(
            InstanceState.INDETERMINATE,
            FirewallState.INDETERMINATE,
            FirewallState.INDETERMINATE,
        ),
    )
    assert "Instance ownership is unknown (expected provider ID '201', observed 'unknown')" in message
    assert "Allow rule ownership is unknown (expected provider ID '101', observed 'unknown')" in message
    assert "Deny rule ownership is unknown (expected provider ID '102', observed 'unknown')" in message
    assert message.count("do not delete by name; escalate ownership") == 3
    assert "gcloud compute instances delete" not in message
    assert "gcloud compute firewall-rules delete" not in message
