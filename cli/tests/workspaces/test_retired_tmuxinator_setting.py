"""The deprecation of the workspace-template ``tmuxinator`` setting, which asked for
the workspace tmuxinator projects releases up to 0.19 generated, and doctor's report
of it in stored settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conftest import ManifestDoc

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from tests.conftest import CapturedOutput


def _deprecation_lines(captured_output: CapturedOutput) -> list[str]:
    return [line for line in captured_output.warnings if "tmuxinator" in line]


# Any authored key is the deprecated setting, an explicit null included.
@pytest.mark.parametrize(
    ("spec", "expected"),
    [({"tmuxinator": True}, 1), ({"tmuxinator": False}, 1), ({"tmuxinator": None}, 1), ({}, 0)],
    ids=["true", "false", "null", "omitted"],
)
def test_an_authored_template_setting_warns_once_per_request(
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
    spec: dict[str, object],
    expected: int,
) -> None:
    from agentworks.bootstrap import begin_request_warning_scope, load_request_registry

    config = make_config(manifests=[ManifestDoc("workspace-template", "old", spec)])
    begin_request_warning_scope()

    registry = load_request_registry(config, include_live_resources=False, probe_host_readiness=False)
    load_request_registry(config, include_live_resources=False, probe_host_readiness=False)

    assert registry.lookup("workspace-template", "old").tmuxinator is spec.get("tmuxinator")
    assert len(_deprecation_lines(captured_output)) == expected


@pytest.mark.parametrize(("spec", "expected"), [('{"tmuxinator":true}', 1), ('{"tmuxinator":false}', 1), ("{}", 0)])
def test_an_explicit_instance_spec_setting_warns_and_a_stored_one_still_decodes(
    db: Database,
    captured_output: CapturedOutput,
    spec: str,
    expected: int,
) -> None:
    from agentworks import output
    from agentworks.instance_specs import get_instance_overlay, parse_instance_spec

    overlay = parse_instance_spec("workspace", spec)
    parse_instance_spec("workspace", spec)
    assert len(_deprecation_lines(captured_output)) == expected

    db.instance_state.put_desired_overlay("workspace", "old", overlay.payload)
    captured_output.warnings.clear()
    output.reset_deprecation_warning_scope()
    stored = get_instance_overlay(db, "workspace", "old")

    assert stored is not None and stored.payload == overlay.payload
    assert _deprecation_lines(captured_output) == []


# -- Doctor reports the setting whatever the ambient suppression -------------


@pytest.mark.parametrize("suppressed", [False, True])
def test_doctor_reports_manifest_and_stored_settings_and_ignores_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suppressed: bool
) -> None:
    from contextlib import closing

    from agentworks import output
    from agentworks.db import Database, VersionedPayload
    from agentworks.doctor import Status, _check_config
    from tests.conftest import write_cfg

    config_path = write_cfg(
        tmp_path,
        ManifestDoc("vm-template", "default"),
        ManifestDoc("admin-template", "default"),
        ManifestDoc("workspace-template", "old", {"tmuxinator": True}),
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)
    with closing(Database()) as database:
        database.insert_vm("box", site="lima-local", hostname="box")
        for name, stored in (("old", {"tmuxinator": False}), ("modern", {"repo": "r"})):
            database.insert_workspace(name, f"/srv/{name}", "box", f"ws-{name}")
            database.instance_state.put_desired_overlay("workspace", name, VersionedPayload(1, stored))
    output.set_suppress_deprecations(suppressed)

    group, _config, registry = _check_config()

    assert registry is not None
    rows = [check for check in group.checks if check.name == "Manifest"]
    assert [row.status for row in rows] == [Status.WARN]
    assert "workspace-template/old.tmuxinator" in (rows[0].message or "")
    stored_rows = [
        (check.name, check.status)
        for check in group.checks
        if check.name.startswith(("workspace/", "Stored workspace"))
    ]
    assert stored_rows == [("workspace/old.tmuxinator", Status.INFO)]
