"""Phase 5 CLI surface: `vm create --site`, the boolean
`vm shell --platform` (with the legacy `--provisioner` alias), the
removed `--vm-host` / `vm-host` group, and the doctor VM-sites group.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
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


def test_vm_create_admin_template_flag_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "create", "box", "--admin-template", "work"],
        "agentworks.vms.manager.create_vm",
        captured,
    )
    assert result.exit_code == 0, result.output
    assert captured["admin_template"] == "work"


@pytest.mark.parametrize(
    "flag",
    ["--cpus", "--memory", "--disk", "--azure-vm-size", "--admin-username"],
)
def test_vm_create_template_override_flags_removed(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Hardware and admin-username overrides are gone from `vm create`:
    those values live in the vm-template / admin-template now."""
    captured: dict[str, Any] = {}
    result = _invoke(
        monkeypatch,
        ["vm", "create", "box", flag, "2"],
        "agentworks.vms.manager.create_vm",
        captured,
    )
    assert result.exit_code != 0
    assert flag in _ANSI_RE.sub("", result.output)


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


def _config_stub(default_site: str | None = None) -> Any:
    """The slice of Config that _check_vm_sites reads."""
    from types import SimpleNamespace

    return cast("Config", SimpleNamespace(defaults=SimpleNamespace(site=default_site)))


def test_doctor_vm_sites_defers_on_pending_migration(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending DB migration must NOT run inside the VM-sites group
    (opening the Database auto-migrates, interleaving migration output
    into the report and stealing the Database group's deliberate
    migration row); the group defers with a pointer instead."""
    from agentworks import doctor
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.resources import Registry
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()

    class _DbFactory:
        @staticmethod
        def check_schema(path: object = None) -> tuple[bool, int, int]:
            return (True, 26, 27)  # pending migration

        def __new__(cls) -> Database:  # type: ignore[misc]
            raise AssertionError("Database() must not open (would auto-migrate)")

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)
    # Deterministic site preflights: lima/wsl2 check their local
    # binary; pretend both are present regardless of the host.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    group = doctor._check_vm_sites(_config_stub(), registry)

    by_name = {c.name: c for c in group.checks}
    deferred = by_name["VM sites"]
    assert deferred.status is doctor.Status.INFO
    assert "pending database migration" in (deferred.message or "")

    # The System group reads the slug from the same database; it must
    # defer under the same guard.
    system = {c.name: c for c in doctor._check_system().checks}["System slug"]
    assert system.status is doctor.Status.INFO
    assert "pending database migration" in (system.message or "")


def test_doctor_system_group(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The install slug leads the report under its own System header
    (it namespaces install-wide, not per-site): a set slug is ok,
    unset and declined are informational."""
    from agentworks import doctor
    from agentworks.db import Database as _Database

    path = tmp_path / "test.db"

    class _DbFactory:
        @staticmethod
        def check_schema(p: object = None) -> tuple[bool, int, int]:
            return (True, 27, 27)

        # _check_system opens and closes its own handle; hand it a
        # fresh one each call so the fixture's connection stays open.
        def __new__(cls) -> Database:  # type: ignore[misc]
            return _Database(path)

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)

    unset = {c.name: c for c in doctor._check_system().checks}["System slug"]
    assert unset.status is doctor.Status.INFO
    assert "will ask" in (unset.message or "")

    db.set_setting("system_slug", "")
    declined = {c.name: c for c in doctor._check_system().checks}["System slug"]
    assert declined.status is doctor.Status.INFO
    assert "declined" in (declined.message or "")

    db.set_setting("system_slug", "team-a")
    row = {c.name: c for c in doctor._check_system().checks}["System slug"]
    assert row.status is doctor.Status.OK
    assert row.message == "team-a"

    # No database at all (fresh install): nothing has ever set the
    # slug, so the same unset row renders without opening the DB.
    monkeypatch.setattr(_DbFactory, "check_schema", staticmethod(lambda p=None: (False, 0, 0)))
    fresh = {c.name: c for c in doctor._check_system().checks}["System slug"]
    assert fresh.status is doctor.Status.INFO
    assert "will ask" in (fresh.message or "")


def test_doctor_vm_sites_group(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Declared sites report ok; a stranded VM row fails with the
    paste-ready manifest hint."""
    from agentworks import doctor
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.resources import Registry
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()

    db.insert_vm("good", site="lima-local", hostname="good")
    db.insert_vm("lost", site="gone-box", hostname="lost")

    class _DbFactory:
        @staticmethod
        def check_schema(path: object = None) -> tuple[bool, int, int]:
            return (True, 27, 27)

        def __new__(cls) -> Database:  # type: ignore[misc]
            return db

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)
    # Deterministic bundled-site preflights: lima/wsl2 check their local
    # binary; pretend both are present regardless of the host.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    group = doctor._check_vm_sites(_config_stub(), registry)

    by_name = {c.name: c for c in group.checks}
    assert by_name["lima-local"].status is doctor.Status.OK
    assert by_name["wsl2"].status is doctor.Status.OK
    stranded = by_name["VM 'lost'"]
    assert stranded.status is doctor.Status.FAIL
    assert "gone-box" in (stranded.message or "")
    assert "name: gone-box" in (stranded.hint or "")
    assert "VM 'good'" not in by_name


def test_doctor_vm_sites_disabled_and_preflight_rows(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DISABLED site is informational with its reason and skips
    preflight (normal for the host; the site still exists); an ENABLED
    site whose preflight fails is the error the operator's next command
    hits and warns."""
    from pathlib import Path as _Path

    from agentworks import doctor
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
    from agentworks.errors import ConfigError
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.resources import Origin, Registry
    from agentworks.vms.sites import VMSiteDecl
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.add(
        "vm-site",
        "mybox",
        VMSiteDecl(name="mybox", platform="lima", platform_config={"vm_host": "me@box"}),
        Origin.operator_declared(file=_Path("sites.yaml"), line=1),
    )
    registry.finalize()

    class _DbFactory:
        @staticmethod
        def check_schema(path: object = None) -> tuple[bool, int, int]:
            return (True, 27, 27)

        def __new__(cls) -> Database:  # type: ignore[misc]
            return db

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)
    # The LOCAL lima site disables itself; the remote mybox site stays
    # enabled but its preflight fails; wsl2 stays fully healthy.
    monkeypatch.setattr(
        LimaPlatform,
        "disabled_reason",
        lambda self: None if self.platform_config.get("vm_host") else "limactl not installed",
    )

    def _boom(self: object, ctx: object) -> None:
        raise ConfigError("preflight: ssh unreachable")

    monkeypatch.setattr(LimaPlatform, "preflight", _boom)
    monkeypatch.setattr(WSL2Platform, "preflight", lambda self, ctx: None)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    group = doctor._check_vm_sites(_config_stub(), registry)

    by_name = {c.name: c for c in group.checks}
    lima_row = by_name["lima-local"]
    assert lima_row.status is doctor.Status.INFO
    assert lima_row.message == "disabled (limactl not installed)"
    assert by_name["wsl2"].status is doctor.Status.OK
    operator_row = by_name["mybox"]
    assert operator_row.status is doctor.Status.WARN
    assert "preflight" in (operator_row.message or "")


def test_doctor_warns_on_references_to_disabled_sites(
    db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing references to a disabled site are warnings, not
    failures: the VM row and defaults.site each get one, with the
    reason. An undeclared site stays the stranded FAIL."""
    from agentworks import doctor
    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.manifests import builtin as builtin_manifests
    from agentworks.resources import Registry
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    registry.finalize()

    db.insert_vm("boxed", site="lima-local", hostname="boxed")

    class _DbFactory:
        @staticmethod
        def check_schema(path: object = None) -> tuple[bool, int, int]:
            return (True, 27, 27)

        def __new__(cls) -> Database:  # type: ignore[misc]
            return db

    monkeypatch.setattr("agentworks.db.Database", _DbFactory)
    monkeypatch.setattr(LimaPlatform, "disabled_reason", lambda self: "limactl not installed")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    group = doctor._check_vm_sites(_config_stub("lima-local"), registry)

    by_name = {c.name: c for c in group.checks}
    vm_row = by_name["VM 'boxed'"]
    assert vm_row.status is doctor.Status.WARN
    assert "limactl not installed" in (vm_row.message or "")
    default_row = by_name["defaults.site"]
    assert default_row.status is doctor.Status.WARN
    assert "lima-local" in (default_row.message or "")
