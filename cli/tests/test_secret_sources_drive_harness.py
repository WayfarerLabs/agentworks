"""Permanent safety guards for the real-entry Secret Sources drive."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_harness() -> ModuleType:
    path = Path(__file__).parents[2] / "docs" / "testing" / "harnesses" / "secret_sources_drive.py"
    spec = importlib.util.spec_from_file_location("secret_sources_drive_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARNESS = _load_harness()


@pytest.mark.parametrize("platform_name", [pytest.param("nt", id="windows")])
def test_unsupported_windows_refuses_before_environment_or_provider_work(
    platform_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _ForbiddenOS:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"unsupported-host path accessed os.{name}")

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("unsupported-host path performed setup or provider work")

    monkeypatch.setattr(HARNESS, "os", _ForbiddenOS())
    monkeypatch.setattr(HARNESS, "_select_cli_layout", forbidden)
    monkeypatch.setattr(HARNESS, "_drive_root", forbidden)
    monkeypatch.setattr(HARNESS, "_write_fake_op", forbidden)
    monkeypatch.setattr(HARNESS.shutil, "which", forbidden)
    monkeypatch.setattr(HARNESS.subprocess, "run", forbidden)
    monkeypatch.setattr(HARNESS.tempfile, "TemporaryDirectory", forbidden)

    exit_code = HARNESS._run_drive(platform_name=platform_name)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == f"{HARNESS._UNSUPPORTED_HOST_MESSAGE}\n"


def test_cli_dir_override_selects_only_that_reviewed_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_dir = Path(__file__).parents[1].resolve()
    monkeypatch.setenv("AGW_CLI_DIR", str(cli_dir))

    layout = HARNESS._select_cli_layout()

    assert layout.root == cli_dir
    assert layout.executable.resolve().is_relative_to(cli_dir)
    assert layout.interpreter.is_relative_to(cli_dir)


def test_child_environment_drops_inherited_home_credentials_and_import_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "HOME": "/operator/home",
        "USERPROFILE": "C:/operator",
        "PYTHONHOME": "/operator/python",
        "PYTHONPATH": "/operator/imports",
        "OP_SERVICE_ACCOUNT_TOKEN": "operator-token",
        "AW_SECRET_REAL": "operator-secret",
        "SSH_AUTH_SOCK": "/operator/agent.sock",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    shim = tmp_path / "shim"
    closed_bin = tmp_path / "closed-bin"
    environment = HARNESS._child_environment(
        home=home,
        shim=shim,
        path_dir=closed_bin,
        extra_env={"AW_SECRET_FIXTURE": "fixture-value"},
    )

    assert environment["AGW_SECRET_DRIVE_HOME"] == str(home)
    assert environment["PYTHONPATH"] == str(shim)
    assert environment["PATH"] == str(closed_bin)
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["AW_SECRET_FIXTURE"] == "fixture-value"
    for key in inherited.keys() - {"PYTHONPATH"}:
        assert key not in environment
    assert environment["PYTHONPATH"] != inherited["PYTHONPATH"]

    with pytest.raises(RuntimeError):
        HARNESS._child_environment(home=home, shim=shim, path_dir=closed_bin, extra_env={"PATH": "/escape"})


def test_fake_provider_path_is_closed_and_cannot_fall_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy_bin = tmp_path / "decoy-bin"
    decoy_bin.mkdir()
    decoy = decoy_bin / "op"
    decoy.write_text("must not run")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(decoy_bin), os.environ.get("PATH", ""))))

    bin_dir, marker, fake = HARNESS._write_fake_op(tmp_path, Path(sys.executable))
    shim = HARNESS._write_isolation_shim(tmp_path)
    home = tmp_path / "home"
    environment = HARNESS._child_environment(
        home=home,
        shim=shim,
        path_dir=bin_dir,
        extra_env={
            "AGW_SECRET_DRIVE_MARKER": str(marker),
            "AGW_SECRET_DRIVE_SENTINEL": "fixture-sentinel",
        },
    )

    resolved = shutil.which("op", path=environment["PATH"])
    assert resolved is not None
    assert Path(resolved).resolve() == fake.resolve()
    assert environment["PATH"] == str(bin_dir)
    assert str(decoy_bin) not in environment["PATH"]

    completed = subprocess.run(
        [
            resolved,
            "read",
            "--no-newline",
            "--account",
            "work.example.com",
            "op://Work/item/password",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == "fixture-sentinel"
    assert marker.read_text() == "invoked"


def test_drive_root_removes_every_fixture_after_exit() -> None:
    with HARNESS._drive_root() as root:
        fixture = root / "nested" / "fixture"
        fixture.parent.mkdir()
        fixture.write_text("fixture")
        captured_root = root
    assert not captured_root.exists()
