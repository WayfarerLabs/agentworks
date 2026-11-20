"""The deprecation of the workspace-template ``tmuxinator`` setting, which asked for
the workspace tmuxinator projects releases up to 0.19 generated, and doctor's report
of it in stored settings.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ConfigError
from agentworks.manifests.envelope import API_VERSION

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from tests.conftest import CapturedOutput

# An authored ``tmuxinator`` key is the deprecated setting whatever it decodes to.
# Each row is the authored spec, its decoded value and how many deprecations it makes.
_AUTHORED = [
    pytest.param("{tmuxinator: true}", True, 1, id="true"),
    pytest.param("{tmuxinator: false}", False, 1, id="false"),
    pytest.param("{tmuxinator: null}", None, 1, id="null"),
    pytest.param("\n  tmuxinator:", None, 1, id="empty"),
    pytest.param("{}", None, 0, id="omitted"),
]


def _template(spec: str) -> str:
    return f"apiVersion: {API_VERSION}\nkind: workspace-template\nmetadata:\n  name: old\nspec: {spec}\n"


def _deprecation_lines(captured_output: CapturedOutput) -> list[str]:
    return [line for line in captured_output.warnings if "tmuxinator" in line]


@pytest.mark.parametrize(("spec", "value", "expected"), _AUTHORED)
def test_an_authored_template_setting_warns_once_per_request(
    make_config,  # noqa: ANN001
    captured_output: CapturedOutput,
    spec: str,
    value: bool | None,
    expected: int,
) -> None:
    from agentworks.bootstrap import begin_request_warning_scope, load_request_registry

    config = make_config(manifests=[_template(spec)])
    begin_request_warning_scope()

    registry = load_request_registry(config, include_live_resources=False, probe_host_readiness=False)
    load_request_registry(config, include_live_resources=False, probe_host_readiness=False)

    assert registry.lookup("workspace-template", "old").tmuxinator is value
    assert len(_deprecation_lines(captured_output)) == expected


@pytest.mark.parametrize(("spec", "value", "expected"), _AUTHORED)
def test_a_first_party_bundle_authoring_the_setting_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    spec: str,
    value: bool | None,
    expected: int,
) -> None:
    from agentworks.manifests.package import load_manifest_package

    anchor = "retired_tmuxinator_setting_bundle"
    (tmp_path / anchor / "manifests").mkdir(parents=True)
    (tmp_path / anchor / "__init__.py").write_text("")
    (tmp_path / anchor / "manifests" / "bundle.yaml").write_text(_template(spec))
    monkeypatch.syspath_prepend(str(tmp_path))
    request.addfinalizer(lambda: sys.modules.pop(anchor, None))

    with pytest.raises(ConfigError) if expected else nullcontext():
        manifests = load_manifest_package(anchor=anchor, subdir="manifests")
        assert [entry.resource.tmuxinator for entry in manifests.entries] == [value]


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
def test_doctor_reports_stored_workspace_settings_and_ignores_suppression(suppressed: bool) -> None:
    from contextlib import closing

    from agentworks import output
    from agentworks.db import Database, VersionedPayload
    from agentworks.doctor import HealthGroup, Status
    from agentworks.doctor_state import append_workspace_deprecation_checks

    with closing(Database()) as database:
        database.insert_vm("box", site="lima-local", hostname="box")
        for name in ("old", "modern"):
            database.insert_workspace(name, f"/srv/{name}", "box", f"ws-{name}")
        database.instance_state.put_desired_overlay("workspace", "old", VersionedPayload(1, {"tmuxinator": False}))
        database.instance_state.put_desired_overlay("workspace", "modern", VersionedPayload(1, {"repo": "r"}))
    output.set_suppress_deprecations(suppressed)
    group = HealthGroup("Configuration")

    append_workspace_deprecation_checks(group)

    assert [(check.name, check.status) for check in group.checks] == [("workspace/old.tmuxinator", Status.INFO)]


@pytest.mark.parametrize("suppressed", [False, True])
def test_doctor_reports_manifest_and_stored_settings_and_ignores_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suppressed: bool
) -> None:
    from contextlib import closing

    from agentworks import output
    from agentworks.db import Database, VersionedPayload
    from agentworks.doctor import Status, _check_config
    from tests.conftest import ManifestDoc, write_cfg

    config_path = write_cfg(
        tmp_path,
        ManifestDoc("vm-template", "default"),
        ManifestDoc("admin-template", "default"),
        ManifestDoc("workspace-template", "old", {"tmuxinator": True}),
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)
    with closing(Database()) as database:
        database.insert_vm("box", site="lima-local", hostname="box")
        database.insert_workspace("stored", "/srv/stored", "box", "ws-stored")
        database.instance_state.put_desired_overlay("workspace", "stored", VersionedPayload(1, {"tmuxinator": True}))
    output.set_suppress_deprecations(suppressed)

    group, _config, registry = _check_config()

    assert registry is not None
    rows = [check for check in group.checks if check.name == "Manifest"]
    assert [row.status for row in rows] == [Status.WARN]
    assert "workspace-template/old.tmuxinator" in (rows[0].message or "")
    assert [check.status for check in group.checks if check.name == "workspace/stored.tmuxinator"] == [Status.INFO]
