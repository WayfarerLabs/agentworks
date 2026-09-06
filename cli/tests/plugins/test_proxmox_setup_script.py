"""Behavioral tests for the one-time Proxmox host setup script."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.conftest import requires_posix_shell

_SCRIPT = Path(__file__).parents[3] / "scripts" / "proxmox-setup.sh"
pytestmark = requires_posix_shell
_LIFECYCLE_PRIVILEGES = {
    "VM.Allocate",
    "VM.Audit",
    "VM.Config.CPU",
    "VM.Config.Cloudinit",
    "VM.Config.Disk",
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.PowerMgmt",
}


def _install_system_commands(bin_dir: Path) -> None:
    for name in ("chmod", "grep", "mkdir", "python3", "rm", "rmdir", "sed", "tr"):
        target = shutil.which(name)
        assert target is not None
        (bin_dir / name).symlink_to(target)


def _install_fakes(bin_dir: Path, *, include_virt_customize: bool) -> None:
    dispatcher = bin_dir / "fake-proxmox-command"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        + dedent(
            """
            import json
            import os
            import sys
            from pathlib import Path

            command = Path(sys.argv[0]).name
            args = sys.argv[1:]

            def record():
                with Path(os.environ["FAKE_CALL_LOG"]).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps([command, *args]) + "\\n")

            if command == "pveversion":
                print(f"pve-manager/{os.environ['FAKE_PVE_MAJOR']}.0.0/fixture")
            elif command == "mktemp":
                image_dir = Path(os.environ["FAKE_IMAGE_DIR"])
                image_dir.mkdir(parents=True, exist_ok=True)
                print(image_dir)
            elif command == "wget":
                Path(args[args.index("-O") + 1]).touch()
            elif command == "qm":
                record()
                if args[0] == "status":
                    raise SystemExit(1)
                if args[0] == "destroy":
                    raise SystemExit(int(os.environ["FAKE_DESTROY_STATUS"]))
                if args[0] == "config" and (volume := os.environ.get("FAKE_IMPORTED_VOLUME")):
                    print(f"unused0: {volume},size=2G")
            elif command == "pvesh":
                record()
                if args[0] == "get":
                    raise SystemExit(1)
            elif command == "pveum":
                record()
                if args[:2] in (["role", "list"], ["user", "list"]):
                    print("[]")
                elif args[:3] == ["user", "token", "add"]:
                    print(json.dumps({"value": "secret", "full-tokenid": "agentworks@pam!agentworks"}))
            elif command == "hostname":
                print("pve.example.test" if args == ["-f"] else "pve")
            elif command == "apt-get":
                record()
                if args[0] == "update":
                    raise SystemExit(int(os.environ["FAKE_APT_UPDATE_STATUS"]))
            elif command == "virt-customize":
                record()
            else:
                raise SystemExit(127)
            """
        ),
        encoding="utf-8",
    )
    dispatcher.chmod(0o700)
    fake_names = ["apt-get", "hostname", "mktemp", "pvesh", "pveum", "pveversion", "qm", "wget"]
    if include_virt_customize:
        fake_names.append("virt-customize")
    for name in fake_names:
        (bin_dir / name).symlink_to(dispatcher.name)


def _run_setup(
    tmp_path: Path,
    *,
    pve_major: str,
    imported_volume: str = "local:9001/vm-9001-disk-0.qcow2",
    include_virt_customize: bool = True,
    apt_update_status: int = 0,
    destroy_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, ...]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_system_commands(bin_dir)
    _install_fakes(bin_dir, include_virt_customize=include_virt_customize)
    call_log = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "FAKE_APT_UPDATE_STATUS": str(apt_update_status),
        "FAKE_CALL_LOG": str(call_log),
        "FAKE_DESTROY_STATUS": str(destroy_status),
        "FAKE_IMAGE_DIR": str(tmp_path / "image"),
        "FAKE_IMPORTED_VOLUME": imported_volume,
        "FAKE_PVE_MAJOR": pve_major,
    }

    result = subprocess.run(
        ["/bin/bash", str(_SCRIPT), "9001", "local", "vmbr0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    calls = [tuple(json.loads(line)) for line in call_log.read_text().splitlines()] if call_log.exists() else []
    return result, calls


@pytest.mark.parametrize(
    ("major", "volume", "guest_agent_privileges"),
    [
        ("8", "local:9001/vm-9001-disk-0.qcow2", {"VM.Monitor"}),
        (
            "9",
            "local-lvm:vm-9001-disk-0",
            {"VM.GuestAgent.Unrestricted"},
        ),
    ],
)
def test_setup_attaches_discovered_volume_and_selects_version_privileges(
    tmp_path: Path,
    major: str,
    volume: str,
    guest_agent_privileges: set[str],
) -> None:
    result, calls = _run_setup(tmp_path, pve_major=major, imported_volume=volume)

    assert result.returncode == 0, result.stderr
    assert ("qm", "set", "9001", "--scsi0", volume) in calls
    assert not any(call[:2] == ("qm", "destroy") for call in calls)
    vm_role = next(call for call in calls if call[:4] == ("pveum", "role", "add", "AgentworksVM"))
    privileges = set(vm_role[vm_role.index("--privs") + 1].split())
    assert privileges == _LIFECYCLE_PRIVILEGES | guest_agent_privileges


def test_setup_refuses_import_without_recorded_volume(tmp_path: Path) -> None:
    result, calls = _run_setup(tmp_path, pve_major="9", imported_volume="", destroy_status=42)

    assert result.returncode == 1
    assert ("qm", "destroy", "9001", "--purge", "1") in calls
    assert not any(call[:4] == ("qm", "set", "9001", "--scsi0") for call in calls)


def test_setup_refuses_unknown_pve_major_before_mutation(tmp_path: Path) -> None:
    result, calls = _run_setup(tmp_path, pve_major="10")

    assert result.returncode != 0
    assert calls == []


def test_setup_stops_when_package_metadata_cannot_be_loaded(tmp_path: Path) -> None:
    result, calls = _run_setup(
        tmp_path,
        pve_major="9",
        include_virt_customize=False,
        apt_update_status=100,
    )

    assert result.returncode != 0
    assert ("apt-get", "update", "-qq") in calls
    assert not any(call[:2] == ("apt-get", "install") for call in calls)
    assert not any(call[:2] == ("qm", "create") for call in calls)
