"""The VM-bound host-key store and the OpenSSH options each trust mode selects."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing
from agentworks.execution.trust import HOST_TRUST, HostKey, TrustBinding, TrustMode

_KEY = HostKey("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
_OTHER_KEY = HostKey("ssh-rsa", "AAAAB3NzaC1yc2EAAAADAQABAAABAQC7")

needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")


def _effective(options: tuple[str, ...], tmp_path: Path, *, config_text: str = "") -> dict[str, list[str]]:
    """Resolve ``options`` with ``ssh -G``, which never connects, against a config file."""
    config = tmp_path / "ssh-config"
    config.write_text(config_text)
    ssh = shutil.which("ssh")
    assert ssh is not None
    out = subprocess.run(
        [ssh, "-G", "-F", str(config), *options, "-o", "BatchMode=yes", "vm.example"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    settings: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, []).append(value)
    return settings


# -- Option sets under the installed client ------------------------------------

_HOSTILE_CONFIG = (
    "HashKnownHosts yes\nUpdateHostKeys yes\nCheckHostIP yes\nGlobalKnownHostsFile /etc/ssh/ssh_known_hosts\n"
)


@needs_ssh
@pytest.mark.windows
@pytest.mark.parametrize("mode", [TrustMode.VM, TrustMode.ENROLL])
def test_store_modes_take_effect_over_configuration(mode: TrustMode, tmp_path: Path) -> None:
    trust.write_records("web", [_KEY])
    options = trust.ssh_options(TrustBinding(mode, "web"))

    settings = _effective(options, tmp_path, config_text=_HOSTILE_CONFIG)

    assert settings["userknownhostsfile"] == [trust.store_file("web").as_posix()]
    assert settings["hostkeyalias"] == ["agentworks-vm-web"]
    assert settings["stricthostkeychecking"] == (["true"] if mode is TrustMode.VM else ["accept-new"])
    assert settings["globalknownhostsfile"] == [trust._NULL_DEVICE]
    assert settings["updatehostkeys"] == ["false"]
    assert settings["hashknownhosts"] == ["no"]
    assert settings["checkhostip"] == ["no"]


@needs_ssh
@pytest.mark.windows
def test_unverified_mode_pins_the_provisioning_file_and_sets_no_alias(tmp_path: Path) -> None:
    with trust.provisioning_binding("web") as binding:
        settings = _effective(trust.ssh_options(binding), tmp_path, config_text=_HOSTILE_CONFIG)

        assert binding.provisioning_file is not None
        assert settings["userknownhostsfile"] == [binding.provisioning_file.as_posix()]
        assert "hostkeyalias" not in settings
        assert settings["stricthostkeychecking"] == ["accept-new"]
        assert settings["globalknownhostsfile"] == [trust._NULL_DEVICE]
        assert settings["updatehostkeys"] == ["false"]
        assert settings["hashknownhosts"] == ["no"]


@needs_ssh
@pytest.mark.windows
def test_store_path_with_a_space_survives_quoting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = tmp_path / "store with space"
    monkeypatch.setattr(trust, "STORE_DIR", store)
    trust.write_records("web", [_KEY])

    settings = _effective(trust.ssh_options(TrustBinding(TrustMode.VM, "web")), tmp_path)

    assert settings["userknownhostsfile"] == [(store / "vm-web").as_posix()]


@needs_ssh
@pytest.mark.windows
def test_host_trust_is_first_use_against_the_operators_configuration(tmp_path: Path) -> None:
    settings = _effective(trust.ssh_options(HOST_TRUST), tmp_path, config_text="HashKnownHosts yes\n")

    assert settings["stricthostkeychecking"] == ["accept-new"]
    assert settings["hashknownhosts"] == ["yes"]
    assert "hostkeyalias" not in settings


# -- Path rules ------------------------------------------------------------------


@pytest.mark.windows
def test_windows_paths_use_forward_slashes() -> None:
    path = PureWindowsPath("C:\\Users\\Op Erator\\.config\\agentworks\\known_hosts\\vm-web")

    assert trust._literal_path(path) == "C:/Users/Op Erator/.config/agentworks/known_hosts/vm-web"


@pytest.mark.windows
@pytest.mark.parametrize(
    "path",
    [
        PureWindowsPath("C:\\Users\\%USERPROFILE%\\vm-web"),
        PureWindowsPath("C:\\Users\\a$b\\vm-web"),
        PureWindowsPath('C:\\Users\\a"b\\vm-web'),
        PureWindowsPath("known_hosts\\vm-web"),
        PureWindowsPath("C:\\Users\\a\nb\\vm-web"),
    ],
)
def test_paths_ssh_would_expand_or_misread_are_refused_by_name(path: PureWindowsPath) -> None:
    with pytest.raises(ConfigError):
        trust._literal_path(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX path rules")
@pytest.mark.parametrize("bad", ["%h", "a$HOME", 'a"b', "a\\b"])
def test_a_store_directory_ssh_would_expand_is_a_config_error(
    bad: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(trust, "STORE_DIR", tmp_path / bad)
    trust.write_records("web", [_KEY])

    with pytest.raises(ConfigError):
        trust.ssh_options(TrustBinding(TrustMode.VM, "web"))


# -- Modes and the missing record --------------------------------------------------


def test_vm_mode_without_a_record_raises_instead_of_falling_back_to_first_use() -> None:
    with pytest.raises(TrustMissing) as caught:
        trust.ssh_options(TrustBinding(TrustMode.VM, "web"))

    assert caught.value.vm_name == "web"


def test_enroll_and_unverified_modes_need_no_record(tmp_path: Path) -> None:
    trust.ssh_options(TrustBinding(TrustMode.ENROLL, "web"))
    trust.ssh_options(TrustBinding(TrustMode.UNVERIFIED, "web", tmp_path / "provisioning"))


def test_a_binding_pairs_the_provisioning_file_with_unverified_mode_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TrustBinding(TrustMode.UNVERIFIED, "web")
    for mode in (TrustMode.VM, TrustMode.ENROLL):
        with pytest.raises(ValueError):
            TrustBinding(mode, "web", tmp_path / "provisioning")


def test_unsafe_vm_names_never_reach_a_path_or_an_option() -> None:
    for name in ("../escape", "a/b", "", "-leading", "has space", 'quo"te'):
        with pytest.raises(ValidationError):
            trust.store_file(name)
        with pytest.raises(ValidationError):
            trust.host_key_alias(name)


def test_windows_reserved_device_names_get_a_prefixed_file() -> None:
    assert trust.store_file("con").name == "vm-con"
    assert trust.store_file("nul").name == "vm-nul"


# -- The record ----------------------------------------------------------------------


def test_records_round_trip_and_the_file_is_private() -> None:
    trust.write_records("web", [_KEY, _OTHER_KEY])

    assert trust.stored_keys("web") == (_KEY, _OTHER_KEY)
    assert trust.has_record("web")
    if sys.platform != "win32":
        assert stat.S_IMODE(trust.store_file("web").stat().st_mode) == 0o600
        assert stat.S_IMODE(trust.STORE_DIR.stat().st_mode) == 0o700


def test_only_well_formed_lines_under_the_vms_alias_count_as_a_record() -> None:
    trust.write_records("web", [_KEY])
    path = trust.store_file("web")
    path.write_text(
        "# comment\n"
        "\n"
        f"agentworks-vm-other {_OTHER_KEY.key_type} {_OTHER_KEY.blob}\n"
        "agentworks-vm-web ssh-rsa\n"
        "agentworks-vm-web ssh-rsa not*base64\n"
        f"agentworks-vm-web {_KEY.key_type} {_KEY.blob} trailing comment\n"
    )

    assert trust.stored_keys("web") == (_KEY,)


def test_a_file_with_no_usable_line_is_no_record() -> None:
    trust.begin_enrollment("web")
    trust.store_file("web").write_text("garbage\n")

    assert not trust.has_record("web")
    with pytest.raises(TrustMissing):
        trust.require_record("web")


@pytest.mark.parametrize(
    "key",
    [
        HostKey("ssh-ed25519\nbad", "AAAA"),
        HostKey("ssh-ed25519", "not*base64"),
        HostKey("", "AAAA"),
        HostKey("ssh-ed25519", ""),
        HostKey("ssh-ed25519", "AAAA ssh-rsa BBBB"),
    ],
)
def test_a_malformed_key_is_refused_at_the_write_boundary_and_nothing_is_written(key: HostKey) -> None:
    trust.write_records("web", [_KEY])
    before = trust.store_file("web").read_bytes()

    with pytest.raises(ValidationError):
        trust.write_records("web", [_OTHER_KEY, key])

    assert trust.store_file("web").read_bytes() == before


def test_begin_enrollment_resets_a_stale_record_and_creates_the_directory() -> None:
    assert not trust.STORE_DIR.exists()
    trust.write_records("web", [_KEY])

    trust.begin_enrollment("web")

    assert trust.STORE_DIR.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(trust.STORE_DIR.stat().st_mode) == 0o700
    assert not trust.has_record("web")
    assert not trust.store_file("web").exists()


def test_remove_deletes_the_record_and_tolerates_none() -> None:
    trust.write_records("web", [_KEY])
    trust.write_records("db", [_OTHER_KEY])

    trust.remove("web")
    trust.remove("web")

    assert not trust.has_record("web")
    assert trust.has_record("db")


# -- The provisioning file -------------------------------------------------------------


def test_provisioning_file_is_private_inside_the_store_and_removed_afterwards() -> None:
    with trust.provisioning_binding("web") as binding:
        assert binding.mode is TrustMode.UNVERIFIED
        path = binding.provisioning_file
        assert path is not None
        assert path.parent == trust.STORE_DIR
        assert path.is_file()
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(trust.STORE_DIR.stat().st_mode) == 0o700

    assert not path.exists()


def test_provisioning_file_is_removed_when_the_create_fails() -> None:
    captured: list[Path] = []

    with pytest.raises(RuntimeError), trust.provisioning_binding("web") as binding:
        assert binding.provisioning_file is not None
        captured.append(binding.provisioning_file)
        raise RuntimeError("provisioning failed")

    assert captured
    assert not captured[0].exists()


def test_two_provisioning_files_for_one_name_do_not_collide() -> None:
    with trust.provisioning_binding("web") as first, trust.provisioning_binding("web") as second:
        assert first.provisioning_file != second.provisioning_file


# -- Managed entries ---------------------------------------------------------------------


def test_entry_options_exist_exactly_when_the_vm_has_a_record() -> None:
    assert trust.entry_options("web") == ()

    trust.write_records("web", [_KEY])
    options = trust.entry_options("web")

    assert "HostKeyAlias agentworks-vm-web" in options
    assert f'UserKnownHostsFile "{trust.store_file("web").as_posix()}"' in options
    assert "StrictHostKeyChecking yes" in options
    assert "UpdateHostKeys no" in options
    assert "HashKnownHosts no" in options

    trust.remove("web")
    assert trust.entry_options("web") == ()


# -- Store failures are state errors --------------------------------------------------------


@pytest.fixture
def unusable_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A store directory that cannot be created: its parent is a regular file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(trust, "STORE_DIR", blocker / "store")


def test_an_unusable_store_directory_is_a_state_error_for_every_writer(unusable_store: None) -> None:
    with pytest.raises(StateError):
        trust.write_records("web", [_KEY])
    with pytest.raises(StateError):
        trust.begin_enrollment("web")
    with pytest.raises(StateError), trust.provisioning_binding("web"):
        pass


def test_an_unreadable_record_is_a_state_error() -> None:
    trust.begin_enrollment("web")
    trust.store_file("web").mkdir()

    with pytest.raises(StateError):
        trust.stored_keys("web")
