"""``agw vm import-host-keys``: matching is OpenSSH's own (``ssh-keygen -F``), so
these tests run the real tool over sample files instead of re-testing its
matcher; what they pin is what the import does with the answer."""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import ConfigError, NotFoundError, StateError
from agentworks.execution import trust
from agentworks.execution.trust import HostKey
from agentworks.vms.manager.host_keys import ImportStatus, import_host_keys, render_import

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(
        shutil.which("ssh-keygen") is None,
        reason="the import matches with the installed OpenSSH's ssh-keygen",
    ),
]


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> list[HostKey]:
    """Four real public keys of two types."""
    directory = tmp_path_factory.mktemp("keys")
    generated: list[HostKey] = []
    for index, key_type in enumerate(("ed25519", "ed25519", "ecdsa", "ecdsa")):
        path = directory / f"k{index}"
        subprocess.run(["ssh-keygen", "-q", "-t", key_type, "-N", "", "-f", str(path)], check=True, timeout=30)
        fields = path.with_name(f"k{index}.pub").read_text().split()
        generated.append(HostKey(fields[0], fields[1]))
    return generated


def _line(patterns: str, key: HostKey, *, marker: str = "") -> str:
    return f"{marker}{patterns} {key.key_type} {key.blob}\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose ``~/.ssh`` exists, so the default source is findable."""
    from tests.conftest import windows_home_env

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    for key, value in windows_home_env(home).items():
        monkeypatch.setenv(key, value)
    return home


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: calls.append(a))
    return calls


def _vm(db: Database, name: str, address: str | None = None) -> None:
    db.insert_vm(name, site="proxmox", hostname=name)
    if address is not None:
        db.update_vm_tailscale(name, address)


def _run(db: Database, *names: str, all_vms: bool = False, known_hosts: Path | None = None) -> Any:
    outcomes = import_host_keys(db, SimpleNamespace(), list(names), all_vms=all_vms, known_hosts=known_hosts)  # type: ignore[arg-type]
    return {o.vm_name: o for o in outcomes}


def _status(outcomes: Any, name: str) -> ImportStatus:
    return outcomes[name].status  # type: ignore[no-any-return]


# -- What gets imported ------------------------------------------------------------------


def test_plain_patterned_listed_and_hashed_records_import_under_the_alias(
    db: Database, tmp_path: Path, keys: list[HostKey], synced: list[object]
) -> None:
    for name, address in (
        ("plain", "100.64.0.1"),
        ("wild", "100.64.1.7"),
        ("listed", "100.64.2.2"),
        ("hashed", "100.64.3.3"),
    ):
        _vm(db, name, address)
    source = tmp_path / "known_hosts"
    source.write_text(
        _line("100.64.0.1", keys[0])
        + _line("100.64.1.*,!100.64.1.9", keys[1])
        + _line("other.example,100.64.2.2,more.example", keys[2])
        + _line("100.64.3.3", keys[3])
    )
    subprocess.run(["ssh-keygen", "-H", "-f", str(source)], check=True, capture_output=True, timeout=30)
    assert "100.64.0.1" not in source.read_text()  # the sample really is hashed

    outcomes = _run(db, all_vms=True, known_hosts=source)

    assert {name: o.status for name, o in outcomes.items()} == dict.fromkeys(outcomes, ImportStatus.IMPORTED)
    assert trust.stored_keys("plain") == (keys[0],)
    assert trust.stored_keys("wild") == (keys[1],)
    assert trust.stored_keys("listed") == (keys[2],)
    assert trust.stored_keys("hashed") == (keys[3],)
    assert render_import(list(outcomes.values())) is True
    assert len(synced) == 1


def test_records_are_rewritten_as_plain_lines_under_the_alias(
    db: Database, tmp_path: Path, keys: list[HostKey]
) -> None:
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(f"100.64.0.1 {keys[0].key_type} {keys[0].blob} operator comment\n")

    _run(db, "web", known_hosts=source)

    assert trust.store_file("web").read_text() == f"agentworks-vm-web {keys[0].key_type} {keys[0].blob}\n"
    found = subprocess.run(
        ["ssh-keygen", "-F", "agentworks-vm-web", "-f", str(trust.store_file("web"))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert found.returncode == 0


def test_several_keys_for_one_address_are_all_imported(db: Database, tmp_path: Path, keys: list[HostKey]) -> None:
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]) + _line("100.64.0.*", keys[2]))

    outcomes = _run(db, "web", known_hosts=source)

    assert outcomes["web"].keys_written == 2
    assert set(trust.stored_keys("web")) == {keys[0], keys[2]}


def test_a_bracketed_port_entry_is_not_consulted_for_the_default_port(
    db: Database, tmp_path: Path, keys: list[HostKey]
) -> None:
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(_line("[100.64.0.1]:2222", keys[0]))

    outcomes = _run(db, "web", known_hosts=source)

    assert _status(outcomes, "web") is ImportStatus.NO_MATCH
    assert not trust.has_record("web")


def test_the_source_is_never_modified(db: Database, tmp_path: Path, keys: list[HostKey]) -> None:
    _vm(db, "web", "100.64.0.1")
    (tmp_path / "ssh-dir").mkdir()
    source = tmp_path / "ssh-dir" / "known_hosts"
    source.write_bytes(
        _line("100.64.0.1", keys[0]).encode() + b"# a comment\n" + _line("elsewhere", keys[1]).encode() + b"\n\n"
    )
    before = source.read_bytes()

    _run(db, "web", known_hosts=source)

    assert source.read_bytes() == before
    assert [p.name for p in source.parent.iterdir()] == ["known_hosts"]


# -- Markers ---------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["@cert-authority ", "@revoked "])
def test_a_marker_line_for_the_vm_blocks_it_even_beside_a_plain_key(
    db: Database, tmp_path: Path, keys: list[HostKey], marker: str
) -> None:
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]) + _line("100.64.0.*", keys[1], marker=marker))

    outcomes = _run(db, "web", known_hosts=source)

    assert _status(outcomes, "web") is ImportStatus.MARKER
    assert not trust.has_record("web")
    assert render_import([outcomes["web"]]) is False


def test_a_marker_for_another_address_does_not_block(db: Database, tmp_path: Path, keys: list[HostKey]) -> None:
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]) + _line("10.0.0.*", keys[1], marker="@cert-authority "))

    assert _status(_run(db, "web", known_hosts=source), "web") is ImportStatus.IMPORTED


# -- An existing record is never written ------------------------------------------------------


def test_a_matching_subset_of_an_existing_record_is_already_current(
    db: Database, tmp_path: Path, keys: list[HostKey], synced: list[object]
) -> None:
    _vm(db, "web", "100.64.0.1")
    trust.write_records("web", [keys[0], keys[1]])
    stored = trust.store_file("web").read_bytes()
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]))

    outcomes = _run(db, "web", known_hosts=source)

    assert _status(outcomes, "web") is ImportStatus.ALREADY_CURRENT
    assert trust.store_file("web").read_bytes() == stored
    assert synced == []


def test_a_differing_key_conflicts_and_is_neither_replaced_nor_unioned(
    db: Database, tmp_path: Path, keys: list[HostKey]
) -> None:
    _vm(db, "web", "100.64.0.1")
    trust.write_records("web", [keys[0]])
    stored = trust.store_file("web").read_bytes()
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]) + _line("100.64.0.1", keys[1]))

    outcomes = _run(db, "web", known_hosts=source)

    assert _status(outcomes, "web") is ImportStatus.CONFLICT
    assert trust.store_file("web").read_bytes() == stored


def test_vms_imported_before_a_later_failure_still_get_their_ssh_config_entries(
    db: Database, tmp_path: Path, keys: list[HostKey], synced: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    _vm(db, "first", "100.64.0.1")
    _vm(db, "second", "100.64.0.2")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]) + _line("100.64.0.2", keys[1]))
    real_write = trust.write_records

    def _fail_for_second(vm_name: str, written: list[HostKey]) -> None:
        if vm_name == "second":
            raise StateError("disk full")
        real_write(vm_name, written)

    monkeypatch.setattr(trust, "write_records", _fail_for_second)

    with pytest.raises(StateError):
        _run(db, all_vms=True, known_hosts=source)

    assert trust.has_record("first")
    assert not trust.has_record("second")
    assert len(synced) == 1


# -- Selection, sources and exit status --------------------------------------------------------


def test_named_vms_must_exist(db: Database, tmp_path: Path) -> None:
    source = tmp_path / "known_hosts"
    source.write_text("")

    with pytest.raises(NotFoundError):
        _run(db, "ghost", known_hosts=source)


@pytest.mark.parametrize("argv", [["vm", "import-host-keys"], ["vm", "import-host-keys", "a", "--all"]])
def test_the_command_needs_names_or_all_but_not_both(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    def _service(*_a: object, **_k: object) -> list[object]:
        raise AssertionError("the service ran for a malformed invocation")

    monkeypatch.setattr("agentworks.vms.manager.host_keys.import_host_keys", _service)

    assert CliRunner().invoke(app, argv).exit_code == 2


def test_a_missing_named_source_is_a_config_error(db: Database, tmp_path: Path) -> None:
    _vm(db, "web", "100.64.0.1")

    with pytest.raises(ConfigError):
        _run(db, "web", known_hosts=tmp_path / "absent")


def test_a_missing_default_source_means_nothing_to_import(db: Database, home: Path, synced: list[object]) -> None:
    _vm(db, "web", "100.64.0.1")

    outcomes = _run(db, "web")

    assert _status(outcomes, "web") is ImportStatus.NO_MATCH
    assert synced == []


def test_the_default_source_is_the_operators_known_hosts(db: Database, home: Path, keys: list[HostKey]) -> None:
    _vm(db, "web", "100.64.0.1")
    (home / ".ssh" / "known_hosts").write_text(_line("100.64.0.1", keys[0]))

    assert _status(_run(db, "web"), "web") is ImportStatus.IMPORTED


def test_a_vm_without_a_tailnet_address_is_reported_and_skipped(
    db: Database, tmp_path: Path, keys: list[HostKey]
) -> None:
    _vm(db, "bare")
    _vm(db, "web", "100.64.0.1")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]))

    outcomes = _run(db, all_vms=True, known_hosts=source)

    assert _status(outcomes, "bare") is ImportStatus.NO_ADDRESS
    assert _status(outcomes, "web") is ImportStatus.IMPORTED
    assert render_import(list(outcomes.values())) is False


def test_exit_status_follows_whether_each_selected_vm_ends_with_a_record(
    db: Database, tmp_path: Path, keys: list[HostKey]
) -> None:
    _vm(db, "has", "100.64.0.1")
    _vm(db, "lacks", "100.64.0.2")
    source = tmp_path / "known_hosts"
    source.write_text(_line("100.64.0.1", keys[0]))

    assert render_import(list(_run(db, "has", known_hosts=source).values())) is True
    assert render_import(list(_run(db, "lacks", known_hosts=source).values())) is False
    assert render_import(list(_run(db, "has", "lacks", known_hosts=source).values())) is False
    assert render_import(list(_run(db, all_vms=True, known_hosts=source).values())) is False

    trust.write_records("lacks", [keys[1]])
    assert render_import(list(_run(db, all_vms=True, known_hosts=source).values())) is True


# -- The command -----------------------------------------------------------------------------


def test_the_command_forwards_its_arguments_and_exits_with_the_report_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _service(db: object, config: object, names: list[str], **kwargs: object) -> list[object]:
        calls.append((names, kwargs))
        return []

    monkeypatch.setattr("agentworks.vms.manager.host_keys.import_host_keys", _service)
    monkeypatch.setattr("agentworks.vms.manager.host_keys.render_import", lambda outcomes: False)
    monkeypatch.setattr("agentworks.cli.commands.vm.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda *_a, **_k: object())
    source = tmp_path / "kh"

    named = CliRunner().invoke(app, ["vm", "import-host-keys", "a", "b", "--known-hosts", str(source)])
    everything = CliRunner().invoke(app, ["vm", "import-host-keys", "--all"])

    assert (named.exit_code, everything.exit_code) == (1, 1)
    assert calls == [
        (["a", "b"], {"all_vms": False, "known_hosts": source}),
        ([], {"all_vms": True, "known_hosts": None}),
    ]
