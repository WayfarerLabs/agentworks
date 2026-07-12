"""Phase 5 CLI surface: `vm create --site`, the boolean
`vm shell --platform` (with the legacy `--provisioner` alias), the
removed `--vm-host` / `vm-host` group, and the doctor VM-sites group.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _invoke(monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, capture: dict[str, Any]):
    def _spy(*args: object, **kwargs: object) -> None:
        capture.update(kwargs)

    monkeypatch.setattr(target, _spy)
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    monkeypatch.setattr("agentworks.cli.commands.vm.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda: object())
    return CliRunner().invoke(app, argv)


def test_vm_create_site_flag_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "create", "box", "--site", "azure-dev"],
        "agentworks.vms.manager.create_vm",
        captured,
    )
    assert result.exit_code == 0, result.output
    assert captured["site"] == "azure-dev"


def test_vm_create_platform_flag_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "create", "box", "--platform", "azure"],
        "agentworks.vms.manager.create_vm",
        captured,
    )
    assert result.exit_code != 0
    assert "--platform" in _ANSI_RE.sub("", result.output)


def test_vm_create_vm_host_flag_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "create", "box", "--vm-host", "gpu-box"],
        "agentworks.vms.manager.create_vm",
        captured,
    )
    assert result.exit_code != 0


def test_vm_host_group_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.cli._helpers.get_db", lambda: object())
    result = CliRunner().invoke(app, ["vm-host", "list"])
    assert result.exit_code != 0


def test_vm_shell_platform_flag_routes_native(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "shell", "box", "--platform"],
        "agentworks.vms.manager.shell_vm",
        captured,
    )
    assert result.exit_code == 0, result.output
    assert captured["platform_transport"] is True


def test_vm_shell_provisioner_alias_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy spelling survives one release as an alias."""
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "shell", "box", "--provisioner"],
        "agentworks.vms.manager.shell_vm",
        captured,
    )
    assert result.exit_code == 0, result.output
    assert captured["platform_transport"] is True


def test_doctor_vm_sites_group(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Declared sites report ok; the slug row shows; a stranded VM row
    fails with the paste-ready manifest hint."""
    from agentworks import doctor
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.resources import Registry
    from agentworks.vms import platforms as vm_platforms

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()

    db.insert_vm("good", site="lima", hostname="good")
    db.insert_vm("lost", site="gone-box", hostname="lost")
    db.set_setting("system_slug", "team-a")

    class _DbFactory:
        @staticmethod
        def check_schema(path: object = None) -> tuple[bool, int, int]:
            return (True, 27, 27)

        def __new__(cls) -> Database:  # type: ignore[misc]
            return db

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)

    group = doctor._check_vm_sites(registry)

    by_name = {c.name: c for c in group.checks}
    assert by_name["vm-site: lima"].status is doctor.Status.OK
    assert by_name["vm-site: wsl2"].status is doctor.Status.OK
    assert by_name["System slug"].message == "team-a"
    stranded = by_name["VM 'lost' site 'gone-box'"]
    assert stranded.status is doctor.Status.FAIL
    assert "name: gone-box" in (stranded.hint or "")
    assert "VM 'good' site 'lima'" not in by_name
