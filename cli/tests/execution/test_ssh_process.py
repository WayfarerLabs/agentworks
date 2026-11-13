"""The client argv: what the VM policy says, checked against the installed ssh."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.errors import ConfigError, ValidationError
from agentworks.execution import ssh_process, trust
from agentworks.execution.client import SSHClient
from agentworks.execution.errors import TrustMissing
from agentworks.execution.requests import Forward
from agentworks.execution.ssh_process import ArgvMode, Destination, VMPolicy, build_argv, stage0_command
from agentworks.execution.trust import TrustBinding, TrustMode
from tests.conftest import seed_vm_trust

SSH = shutil.which("ssh")
needs_ssh = pytest.mark.skipif(SSH is None, reason="requires an installed OpenSSH client")


def policy_for(identity: Path, vm: str = "box") -> VMPolicy:
    return VMPolicy(identity, TrustBinding(TrustMode.VM, vm))


def argv_for(
    identity: Path,
    mode: ArgvMode = ArgvMode.PIPE,
    *,
    log: Path | None = None,
    forwards: tuple[Forward, ...] = (),
    verbose: bool = False,
) -> list[str]:
    seed_vm_trust("box")
    command = stage0_command() if mode is not ArgvMode.FORWARD else None
    return build_argv(
        SSHClient(SSH or "/usr/bin/ssh", (9, 2)),
        policy_for(identity),
        Destination("100.64.0.9", "agentworks"),
        mode,
        command,
        forwards,
        client_log=log,
        verbose=verbose,
    )


def effective_config(argv: list[str], home: Path | None = None) -> dict[str, list[str]]:
    """``ssh -G`` for an argv built by ``build_argv``: the options ssh would really use."""
    assert SSH is not None
    config_argv = [SSH, "-G", *argv[1:]]
    env = {**os.environ, **({"HOME": str(home)} if home else {})}
    output = subprocess.run(config_argv, capture_output=True, text=True, check=True, env=env).stdout
    config: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key, []).append(value)
    return config


# -- Argument shapes ---------------------------------------------------------------------


def test_a_pipe_argv_has_no_request_data_and_no_stdin_stealing_flag(tmp_path: Path) -> None:
    argv = argv_for(tmp_path / "key", log=tmp_path / "log")

    assert argv[1:4] == ["-F", "none", "-T"]
    assert "-n" not in argv  # it would replace the request's stdin with the null device
    assert argv[argv.index("-E") + 1] == str(tmp_path / "log")
    destination = argv.index("--")
    assert argv[destination - 2 : destination + 3] == ["-l", "agentworks", "--", "100.64.0.9", stage0_command()]


def test_a_terminal_argv_asks_for_a_pty_and_keeps_diagnostics_on_the_terminal(tmp_path: Path) -> None:
    argv = argv_for(tmp_path / "key", ArgvMode.TERMINAL, log=tmp_path / "log")

    assert argv[1:4] == ["-F", "none", "-t"]
    assert "-E" not in argv
    assert "-tt" not in argv


def test_a_forward_argv_carries_no_request_and_fails_when_a_forward_cannot_be_set_up(tmp_path: Path) -> None:
    forwards = (Forward(8080, 80), Forward(2222, 22, "0.0.0.0"))

    argv = argv_for(tmp_path / "key", ArgvMode.FORWARD, log=tmp_path / "log", forwards=forwards)

    assert argv[1:6] == ["-F", "none", "-N", "-n", "-E"]
    assert "ExitOnForwardFailure=yes" in argv
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "-L"] == [
        "localhost:8080:localhost:80",
        "0.0.0.0:2222:localhost:22",
    ]
    assert argv[-1] == "100.64.0.9"


def test_a_verbose_forward_inherits_the_operators_stderr(tmp_path: Path) -> None:
    argv = argv_for(tmp_path / "key", ArgvMode.FORWARD, log=tmp_path / "log", verbose=True)

    assert "-v" in argv
    assert "-E" not in argv


@pytest.mark.parametrize("mode", [ArgvMode.PIPE, ArgvMode.TERMINAL])
def test_only_a_forward_may_be_verbose(tmp_path: Path, mode: ArgvMode) -> None:
    with pytest.raises(ValueError):
        argv_for(tmp_path / "key", mode, verbose=True)


def test_the_client_log_is_read_from_its_end(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text("first\n" + "x" * (ssh_process.CLIENT_LOG_READ_BYTES * 2) + "\nlast line\n")

    text = ssh_process.client_log_text(log)

    assert len(text) <= ssh_process.CLIENT_LOG_READ_BYTES
    assert text.endswith("last line\n")
    assert "first" not in text
    assert ssh_process.client_log_text(tmp_path / "missing") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="signal statuses are POSIX")
def test_a_client_killed_by_a_signal_reports_128_plus_the_signal() -> None:
    program = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    assert ssh_process.run_terminal([sys.executable, "-c", program]) == 128 + signal.SIGKILL
    assert ssh_process.exit_status(3) == 3


def test_a_non_default_port_is_passed_and_the_default_is_not(tmp_path: Path) -> None:
    seed_vm_trust("box")
    client = SSHClient("/usr/bin/ssh", (9, 2))
    default = build_argv(client, policy_for(tmp_path / "k"), Destination("h", "u"), ArgvMode.PIPE, "cmd")
    custom = build_argv(client, policy_for(tmp_path / "k"), Destination("h", "u", 2222), ArgvMode.PIPE, "cmd")

    assert "-p" not in default
    assert custom[custom.index("-p") + 1] == "2222"


@pytest.mark.parametrize("host", ["", "-oProxyCommand=x", "a b", "a;b", "[::1]", "fe80::1%eth0", "a\nb", "x/y"])
def test_a_destination_that_could_become_an_option_or_a_second_host_is_refused(host: str) -> None:
    with pytest.raises(ValidationError):
        Destination(host, "agentworks")


@pytest.mark.parametrize("user", ["", "-x", "a b", "a@b", "a:b", "1abc"])
def test_a_user_that_is_not_a_posix_account_name_is_refused(user: str) -> None:
    with pytest.raises(ValidationError):
        Destination("100.64.0.9", user)


@pytest.mark.parametrize("host", ["100.64.0.9", "host.example", "fd7a:115c:a1e0::1"])
def test_literal_hosts_are_accepted(host: str) -> None:
    assert Destination(host, "agentworks").host == host


def test_a_missing_trust_record_raises_before_an_argv_exists(tmp_path: Path) -> None:
    client = SSHClient("/usr/bin/ssh", (9, 2))

    with pytest.raises(TrustMissing):
        build_argv(client, policy_for(tmp_path / "key", "never-enrolled"), Destination("h", "u"), ArgvMode.PIPE, "c")


def test_an_identity_file_ssh_would_expand_is_refused_by_name(tmp_path: Path) -> None:
    seed_vm_trust("box")
    client = SSHClient("/usr/bin/ssh", (9, 2))

    with pytest.raises(ConfigError, match="SSH identity file"):
        build_argv(client, policy_for(tmp_path / "%d" / "key"), Destination("h", "u"), ArgvMode.PIPE, "c")


# -- ssh -G: what the installed client would really do ---------------------------------


@pytest.mark.parametrize("key_dir", ["keys", "dir with space", "o'brien"])
def test_only_an_identity_path_ssh_would_split_or_unquote_is_quoted(key_dir: str, tmp_path: Path) -> None:
    """The ssh shipped with Git for Windows refuses a quoted value without whitespace (#898)."""
    key = (tmp_path / key_dir / "id_ed25519").as_posix()
    quote = '"' if key_dir != "keys" else ""

    argv = argv_for(Path(key))

    assert f"IdentityFile={quote}{key}{quote}" in argv
    assert f"CertificateFile={quote}{key}/disabled-certificate{quote}" in argv


@needs_ssh
@pytest.mark.parametrize("key_dir", ["keys", "dir with space", "o'brien"])
def test_the_policy_takes_effect(key_dir: str, tmp_path: Path) -> None:
    """The installed client may accept a quoted value without whitespace too, so this
    cannot reproduce #898 alone; ``test_only_an_identity_path_ssh_would_split_or_unquote_is_quoted`` guards that."""
    key = tmp_path / key_dir / "id_ed25519"

    config = effective_config(argv_for(key))

    assert config["identityfile"] == [str(key)]  # quoting held for a path with a space (I13)
    assert config["identitiesonly"] == ["yes"]
    assert config["batchmode"] == ["yes"]
    assert config["preferredauthentications"] == ["publickey"]
    assert config["user"] == ["agentworks"]
    assert config["hostkeyalias"] == [trust.host_key_alias("box")]
    assert config["userknownhostsfile"] == [str(trust.store_file("box"))]
    assert config["stricthostkeychecking"] == ["true"]
    assert config["globalknownhostsfile"] == ["/dev/null"]
    assert config["updatehostkeys"] == ["false"]
    assert config["hashknownhosts"] == ["no"]
    assert config["certificatefile"] == [f"{key}/disabled-certificate"]


@needs_ssh
def test_keepalives_and_connect_timeout_apply_to_every_connection(tmp_path: Path) -> None:
    for mode in ArgvMode:
        config = effective_config(argv_for(tmp_path / "key", mode))

        assert config["serveraliveinterval"] == ["15"], mode
        assert config["serveralivecountmax"] == ["4"], mode
        assert config["connecttimeout"] == ["30"], mode


@needs_ssh
def test_t_beats_a_request_tty_force_supplied_later(tmp_path: Path) -> None:
    argv = argv_for(tmp_path / "key")
    argv[argv.index("--") : argv.index("--")] = ["-o", "RequestTTY=force"]

    assert effective_config(argv)["requesttty"] == ["false"]  # I14


@needs_ssh
def test_no_configuration_file_is_read_so_the_defaults_stay_the_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(
        "Host *\n  RequestTTY force\n  ControlMaster yes\n  ControlPath /tmp/shared\n  ControlPersist yes\n"
        "  ForwardAgent yes\n  ForwardX11 yes\n  ProxyCommand false\n  HashKnownHosts yes\n"
        "  VerifyHostKeyDNS yes\n  CanonicalizeHostname yes\n  PermitLocalCommand yes\n"
        "  Tunnel yes\n  KnownHostsCommand false\n  HostbasedAuthentication yes\n  PKCS11Provider /x\n"
    )

    config = effective_config(argv_for(tmp_path / "key"), home)

    assert config["requesttty"] == ["false"]
    assert config["controlmaster"] == ["false"]  # I12: no connection sharing
    assert config["controlpersist"] == ["no"]
    assert "controlpath" not in config
    assert config["forwardagent"] == ["no"]
    assert config["forwardx11"] == ["no"]
    assert "proxycommand" not in config
    assert config["hashknownhosts"] == ["no"]
    assert config["verifyhostkeydns"] == ["false"]
    assert config["canonicalizehostname"] == ["false"]
    assert config["permitlocalcommand"] == ["no"]
    assert config["tunnel"] == ["false"]
    assert "knownhostscommand" not in config
    assert config["hostbasedauthentication"] == ["no"]
    assert "pkcs11provider" not in config
    assert config["connectionattempts"] == ["1"]


@needs_ssh
def test_a_forward_requests_no_remote_command_and_exits_on_a_failed_forward(tmp_path: Path) -> None:
    config = effective_config(
        argv_for(tmp_path / "key", ArgvMode.FORWARD, forwards=(Forward(8080, 80),)),
    )

    assert config["exitonforwardfailure"] == ["yes"]
    # The rendering of the address differs between OpenSSH versions; the ports do not.
    assert re.fullmatch(r".*:8080 .*:80", config["localforward"][0])
    assert config["clearallforwardings"] == ["no"]


def test_stage0_is_one_short_line_that_no_account_shell_rewrites() -> None:
    program = ssh_process.STAGE0_PROGRAM

    assert len(program) < 200
    assert not re.search(r"['\"!\\\n]", program)
    command = stage0_command()
    assert command.startswith("/usr/bin/python3 -I -S -c '")
    assert command.endswith(f"{program}'")
    assert stage0_command("/tmp/agw-handover-ab/s").endswith("' /tmp/agw-handover-ab/s")
