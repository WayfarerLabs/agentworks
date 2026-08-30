"""Structural, routing, runtime-helper, and reconciliation coverage."""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.git_credential.base import (
    HttpsCredentialScope,
    ManagedHelper,
    StoredCredential,
)
from agentworks.capabilities.git_credential.github import (
    GitHubCliSource,
    GitHubCredentialProvider,
    GitHubSecretSource,
)
from agentworks.errors import ConfigError
from agentworks.git_credentials.reconcile import (
    _RECONCILE_SCRIPT,
    _state_archive,
    reconcile_user_git_credentials,
)
from agentworks.git_credentials.state import (
    UserCredentialState,
    build_user_credential_state,
    validate_credential_scope_claims,
)
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.plugins.azure.azdo import (
    AzDOCredentialProvider,
    AzDOSecretSource,
    AzureCliSource,
)
from agentworks.ssh import SSHError, SSHResult


def _ctx(values: dict[str, str], allowed: tuple[str, ...]) -> RunContext:
    return RunContext(secrets=ScopedSecrets(values, allowed))


def _install(home: Path, state: UserCredentialState) -> subprocess.CompletedProcess[str]:
    desired = "present" if state.has_credentials else "empty"
    payload = _state_archive(state) if state.has_credentials else ""
    env = os.environ | {"HOME": str(home)}
    return subprocess.run(
        ["bash", "-c", _RECONCILE_SCRIPT.replace("@DESIRED@", desired)],
        input=payload,
        text=True,
        env=env,
        capture_output=True,
        check=False,
    )


def _query(
    home: Path,
    request: str,
    *,
    operation: str = "get",
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"HOME": str(home)}
    if path is not None:
        env["PATH"] = f"{path}:{env['PATH']}"
    return subprocess.run(
        [str(home / ".agentworks/git-credentials/launch"), operation],
        input=request,
        text=True,
        env=env,
        capture_output=True,
        check=False,
    )


def _request(host: str, path: str = "", *, username: str = "") -> str:
    fields = ["protocol=https", f"host={host}"]
    if path:
        fields.append(f"path={path}")
    if username:
        fields.append(f"username={username}")
    return "\n".join(fields) + "\n\n"


def _shorten_managed_helper_timeouts(home: Path) -> None:
    dispatch = (home / ".agentworks/git-credentials/current/dispatch").resolve()
    script = dispatch.read_text()
    timeout = "timeout --signal=TERM --kill-after=1s 10s"
    assert script.count(timeout) == 2
    dispatch.write_text(script.replace(timeout, "timeout --signal=TERM --kill-after=1s 0.1s"))


def test_provider_sources_are_required_closed_and_provider_owned() -> None:
    with pytest.raises(ConfigError):
        GitHubCredentialProvider("gh", {})
    with pytest.raises(ConfigError):
        GitHubCredentialProvider("gh", {"source": "secret"})
    with pytest.raises(ConfigError):
        GitHubCredentialProvider("gh", {"token": "old"})
    with pytest.raises(ConfigError):
        GitHubCredentialProvider("gh", {"source": {"mode": "az-cli"}})
    with pytest.raises(ConfigError):
        AzDOCredentialProvider("ado", {"org": "acme", "source": {"mode": "gh-cli"}})

    github_secret = GitHubCredentialProvider("gh", {"source": {"mode": "secret"}})
    github_cli = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}})
    azdo_secret = AzDOCredentialProvider("ado", {"org": "acme", "source": {"mode": "secret"}})
    azdo_cli = AzDOCredentialProvider("ado", {"org": "acme", "source": {"mode": "az-cli"}})

    assert github_secret.config.source == GitHubSecretSource(mode="secret", secret="git-token-gh")
    assert github_cli.config.source == GitHubCliSource(mode="gh-cli")
    assert azdo_secret.config.source == AzDOSecretSource(mode="secret", secret="git-token-ado")
    assert azdo_cli.config.source == AzureCliSource(mode="az-cli")


def test_secret_and_cli_arms_return_final_material_with_identical_scope_translation() -> None:
    github_config = {"owner": "acme", "repos": ["acme/repo"], "source": {"mode": "secret"}}
    github_secret_provider = GitHubCredentialProvider("gh", github_config)
    github_secret = github_secret_provider.credential_material(
        _ctx({"git-token-gh": "secret-token"}, ("git-token-gh",))
    )
    github_config["source"] = {"mode": "gh-cli"}
    github_cli_provider = GitHubCredentialProvider("gh", github_config)
    github_cli = github_cli_provider.credential_material(_ctx({}, ()))
    assert (
        github_secret_provider.credential_scopes()
        == github_cli_provider.credential_scopes()
        == (
            HttpsCredentialScope("github.com", ("acme", "repo")),
            HttpsCredentialScope("github.com", ("acme",)),
        )
    )
    assert github_secret == StoredCredential("gh", "secret-token")
    assert isinstance(github_cli, ManagedHelper)

    azdo_config = {"org": "acme", "source": {"mode": "secret"}}
    azdo_secret_provider = AzDOCredentialProvider("ado", azdo_config)
    azdo_secret = azdo_secret_provider.credential_material(_ctx({"git-token-ado": "secret-token"}, ("git-token-ado",)))
    azdo_config["source"] = {"mode": "az-cli"}
    azdo_cli_provider = AzDOCredentialProvider("ado", azdo_config)
    azdo_cli = azdo_cli_provider.credential_material(_ctx({}, ()))
    assert (
        azdo_secret_provider.credential_scopes()
        == azdo_cli_provider.credential_scopes()
        == (HttpsCredentialScope("dev.azure.com", ("acme",)),)
    )
    assert azdo_secret == StoredCredential("acme", "secret-token")
    assert isinstance(azdo_cli, ManagedHelper)


def test_builtin_cli_helpers_pin_external_commands_and_construct_complete_responses() -> None:
    github = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}}).credential_material(_ctx({}, ()))
    azdo = AzDOCredentialProvider("ado", {"org": "acme", "source": {"mode": "az-cli"}}).credential_material(
        _ctx({}, ())
    )
    assert isinstance(github, ManagedHelper)
    assert isinstance(azdo, ManagedHelper)
    assert b"gh auth token --hostname github.com" in github.program
    assert b"GH_PROMPT_DISABLED=1" in github.program
    assert b"az account get-access-token" in azdo.program
    assert b"499b84ac-1321-427f-aa17-267ca6975798" in azdo.program
    assert b"--query accessToken --output tsv" in azdo.program


def test_azure_cli_helper_constructs_the_provider_owned_git_response(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    az = fake_bin / "az"
    az.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "account get-access-token --resource '
        '499b84ac-1321-427f-aa17-267ca6975798 --query accessToken --output tsv" ] || exit 9\n'
        "printf 'azure-access-token\\n'\n"
    )
    az.chmod(0o700)
    provider = AzDOCredentialProvider(
        "ado",
        {"org": "acme", "source": {"mode": "az-cli"}},
    )
    material = provider.credential_material(_ctx({}, ()))
    state = build_user_credential_state([("ado", provider.credential_scopes(), material)])
    assert _install(tmp_path, state).returncode == 0

    response = _query(tmp_path, _request("dev.azure.com", "acme/repo"), path=str(fake_bin))
    assert response.returncode == 0
    assert response.stdout == "username=acme\npassword=azure-access-token\n\n"


def test_builder_routes_longest_prefix(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [
            (
                "default-first",
                (HttpsCredentialScope("github.com"),),
                StoredCredential("default", "one"),
            ),
            (
                "owner",
                (HttpsCredentialScope("github.com", ("acme",)),),
                StoredCredential("owner", "three"),
            ),
            (
                "repo",
                (HttpsCredentialScope("github.com", ("acme", "repo")),),
                StoredCredential("repo", "four"),
            ),
        ]
    )
    result = _install(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert _query(tmp_path, _request("github.com", "acme/repo.git")).stdout == "username=repo\npassword=four\n\n"
    assert _query(tmp_path, _request("github.com", "acme/other")).stdout == "username=owner\npassword=three\n\n"
    assert _query(tmp_path, _request("github.com", "other/repo")).stdout == "username=default\npassword=one\n\n"
    assert len(state.stored_credential_files) == 3


def test_duplicate_nonempty_scope_is_rejected() -> None:
    scope = HttpsCredentialScope("github.com", ("acme",))
    with pytest.raises(ConfigError):
        validate_credential_scope_claims([("one", (scope,)), ("two", (scope,))])


def test_duplicate_host_default_scope_is_rejected() -> None:
    scope = HttpsCredentialScope("github.com")
    with pytest.raises(ConfigError):
        validate_credential_scope_claims([("one", (scope,)), ("two", (scope,))])


@pytest.mark.parametrize(
    "scopes",
    [
        (),
        (HttpsCredentialScope("UPPER.example"),),
        (HttpsCredentialScope("example.com", ("..",)),),
    ],
)
def test_preparation_rejects_malformed_provider_scopes(scopes: tuple[HttpsCredentialScope, ...]) -> None:
    with pytest.raises(ConfigError):
        validate_credential_scope_claims([("bad", scopes)])


@pytest.mark.parametrize("password", ["a:b", "a@b", "a/b", "a%b", "a?b", "a#b", "a=b", r"a\b"])
def test_stored_protocol_record_round_trips_git_and_url_delimiters(tmp_path: Path, password: str) -> None:
    state = build_user_credential_state(
        [
            (
                "credential",
                (HttpsCredentialScope("example.com"),),
                StoredCredential("user", password),
            )
        ]
    )
    assert _install(tmp_path, state).returncode == 0
    response = _query(tmp_path, _request("example.com"))
    assert response.returncode == 0
    assert response.stdout == f"username=user\npassword={password}\n\n"


@pytest.mark.parametrize("value", ["", "line\nfeed", "carriage\rreturn", "nul\0byte", "control\x1fbyte"])
def test_stored_protocol_fields_reject_empty_or_control_values(value: str) -> None:
    with pytest.raises(ConfigError):
        build_user_credential_state(
            [
                (
                    "credential",
                    (HttpsCredentialScope("example.com"),),
                    StoredCredential("user", value),
                )
            ]
        )


def test_stored_protocol_fields_must_fit_the_total_runtime_envelope() -> None:
    with pytest.raises(ConfigError):
        build_user_credential_state(
            [
                (
                    "credential",
                    (HttpsCredentialScope("example.com"),),
                    StoredCredential("u" * 8192, "p" * 8192),
                )
            ]
        )


def test_embedded_username_does_not_override_generic_path_selection(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [
            (
                "repo",
                (HttpsCredentialScope("github.com", ("acme", "repo")),),
                StoredCredential("selected", "token"),
            )
        ]
    )
    assert _install(tmp_path, state).returncode == 0
    response = _query(tmp_path, _request("github.com", "acme/repo", username="foreign"))
    assert response.stdout == "username=selected\npassword=token\n\n"


def test_managed_helper_acquires_on_each_get_and_suppresses_upstream_failure(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "counter"
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        f"echo $n > {counter}\n"
        "printf 'token-%s\\n' \"$n\"\n"
    )
    gh.chmod(0o700)
    provider = GitHubCredentialProvider("gh", {"source": {"mode": "gh-cli"}})
    material = provider.credential_material(_ctx({}, ()))
    state = build_user_credential_state([("gh", provider.credential_scopes(), material)])
    assert _install(tmp_path, state).returncode == 0

    first = _query(tmp_path, _request("github.com"), path=str(fake_bin))
    second = _query(tmp_path, _request("github.com"), path=str(fake_bin))
    assert first.stdout == "username=x-access-token\npassword=token-1\n\n"
    assert second.stdout == "username=x-access-token\npassword=token-2\n\n"

    gh.write_text("#!/bin/sh\necho leaked-upstream-value >&2\nexit 9\n")
    gh.chmod(0o700)
    failed = _query(tmp_path, _request("github.com"), path=str(fake_bin))
    assert failed.returncode != 0
    assert "leaked-upstream-value" not in failed.stderr
    assert "token-1" not in failed.stderr
    assert isinstance(material, ManagedHelper)
    assert failed.stderr.strip() == material.failure_hint


@pytest.mark.parametrize(
    "program",
    [
        b"#!/bin/sh\nprintf 'username=user\\nsecret=leaked-value\\n\\n'\n",
        b"#!/bin/sh\nprintf 'username=user\\npassword=bad\\rvalue\\n\\n'\n",
        b"#!/bin/sh\nprintf 'username=user\\nusername=again\\npassword=value\\n\\n'\n",
    ],
)
def test_runtime_rejects_malformed_managed_response_without_relaying_it(tmp_path: Path, program: bytes) -> None:
    helper = ManagedHelper(program, "fixed failure")
    state = build_user_credential_state([("bad", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    response = _query(tmp_path, _request("example.com"))
    assert response.returncode != 0
    assert response.stdout == ""
    assert response.stderr == "fixed failure\n"


def test_runtime_bounds_managed_helper_execution(tmp_path: Path) -> None:
    helper = ManagedHelper(b"#!/bin/sh\nsleep 20\n", "bounded failure")
    state = build_user_credential_state([("slow", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    _shorten_managed_helper_timeouts(tmp_path)
    started = time.monotonic()
    response = _query(tmp_path, _request("example.com"))
    elapsed = time.monotonic() - started
    assert response.returncode != 0
    assert response.stderr == "bounded failure\n"
    assert elapsed < 1


def test_dispatcher_has_two_production_managed_helper_timeouts() -> None:
    helper = ManagedHelper(b"#!/bin/sh\nexit 1\n", "fixed failure")
    state = build_user_credential_state([("runtime", (HttpsCredentialScope("example.com"),), helper)])
    assert state.dispatcher_script.count("timeout --signal=TERM --kill-after=1s 10s") == 2


def test_signal_exited_producer_does_not_override_a_valid_helper_response(tmp_path: Path) -> None:
    helper = ManagedHelper(
        b"#!/bin/sh\nprintf 'username=runtime\\npassword=value\\n\\n'\n",
        "fixed failure",
    )
    state = build_user_credential_state([("runtime", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    dispatch = (tmp_path / ".agentworks/git-credentials/current/dispatch").resolve()
    script = dispatch.read_text()
    producer = "printf '%s\\n\\n' \"$helper_request\""
    assert script.count(producer) == 1
    dispatch.write_text(script.replace(producer, "exit 141"))

    response = _query(tmp_path, _request("example.com"))
    assert response.returncode == 0
    assert response.stdout == "username=runtime\npassword=value\n\n"
    assert response.stderr == ""


def test_runtime_missing_helper_command_uses_only_fixed_failure_hint(tmp_path: Path) -> None:
    helper = ManagedHelper(
        b"#!/bin/sh\ncommand -v agw-command-that-does-not-exist >/dev/null 2>&1 || exit 1\n",
        "install and authenticate the required command",
    )
    state = build_user_credential_state([("missing", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    response = _query(tmp_path, _request("example.com"))
    assert response.returncode != 0
    assert response.stdout == ""
    assert response.stderr == "install and authenticate the required command\n"


def test_runtime_no_match_and_non_get_operations_serve_no_value(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "secret"))]
    )
    assert _install(tmp_path, state).returncode == 0
    assert _query(tmp_path, _request("other.example")).stdout == ""
    store = _query(tmp_path, _request("example.com"), operation="store")
    assert store.returncode == 0
    assert store.stdout == ""
    assert store.stderr == ""
    erase = _query(tmp_path, _request("example.com"), operation="erase")
    assert erase.returncode == 0
    assert erase.stdout == ""
    assert "one" in erase.stderr
    assert "secret" not in erase.stderr
    started = time.monotonic()
    unknown = _query(tmp_path, "x" * 100_000, operation="unknown")
    assert time.monotonic() - started < 1
    assert unknown.returncode == 0
    assert unknown.stdout == ""
    assert unknown.stderr == ""


@pytest.mark.parametrize("payload_kind", ["stored", "helper"])
def test_inherited_bash_tracing_cannot_expose_credential_values(
    tmp_path: Path,
    payload_kind: str,
) -> None:
    marker = f"{payload_kind}-trace-secret"
    payload = (
        StoredCredential("user", marker)
        if payload_kind == "stored"
        else ManagedHelper(
            f"#!/bin/bash\nvalue={marker}\nprintf 'username=user\\npassword=%s\\n\\n' \"$value\"\n".encode(),
            "fixed failure",
        )
    )
    state = build_user_credential_state([("one", (HttpsCredentialScope("example.com"),), payload)])
    assert _install(tmp_path, state).returncode == 0
    bash_env = tmp_path / "inherited-bash-env"
    bash_env.write_text("PS4='inherited-trace: '; set -x\n")
    result = subprocess.run(
        [str(tmp_path / ".agentworks/git-credentials/launch"), "get"],
        input=_request("example.com"),
        text=True,
        env=os.environ
        | {
            "HOME": str(tmp_path),
            "BASH_ENV": str(bash_env),
            "SHELLOPTS": "braceexpand:hashall:xtrace",
            "BASH_XTRACEFD": "2",
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert f"password={marker}\n" in result.stdout
    assert marker not in result.stderr


@pytest.mark.parametrize("failure_point", ["cleanup-after-disable", "stage", "activation"])
def test_reconciliation_failure_after_disable_is_unreachable_and_retry_converges(
    tmp_path: Path,
    failure_point: str,
) -> None:
    old = build_user_credential_state(
        [("old", (HttpsCredentialScope("example.com"),), StoredCredential("user", "old"))]
    )
    new = build_user_credential_state(
        [("new", (HttpsCredentialScope("example.com"),), StoredCredential("user", "new"))]
    )
    assert _install(tmp_path, old).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    old_current = os.readlink(root / "current")

    marker, replacement = {
        "cleanup-after-disable": (
            "remove_config_value include.path '~/.agentworks/git-credentials/current/config.gitconfig'\n",
            "remove_config_value include.path '~/.agentworks/git-credentials/current/config.gitconfig'\nfalse\n",
        ),
        "stage": (
            'chmod 700 "$stage" "$root/generations"\n',
            'chmod 700 "$stage" "$root/generations"\nfalse\n',
        ),
        "activation": (
            '    mv -Tf "$current_tmp" "$root/current"\n',
            '    false\n    mv -Tf "$current_tmp" "$root/current"\n',
        ),
    }[failure_point]
    failed_script = _RECONCILE_SCRIPT.replace(marker, replacement, 1)
    assert failed_script != _RECONCILE_SCRIPT
    failed = subprocess.run(
        ["bash", "-c", failed_script.replace("@DESIRED@", "present")],
        input=_state_archive(new),
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert os.readlink(root / "current") == old_current
    includes = subprocess.run(
        ["git", "config", "--global", "--get-all", "include.path"],
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    ).stdout.splitlines()
    assert "~/.agentworks/git-credentials/current/config.gitconfig" not in includes
    fill = subprocess.run(
        ["git", "credential", "fill"],
        input=_request("example.com"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )
    assert "password=old" not in fill.stdout
    assert "password=new" not in fill.stdout

    assert _install(tmp_path, new).returncode == 0
    converged = subprocess.run(
        ["git", "credential", "fill"],
        input=_request("example.com"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )
    assert converged.returncode == 0
    assert converged.stdout == "protocol=https\nhost=example.com\nusername=user\npassword=new\n"


@pytest.mark.parametrize("corrupt_as", ["directories", "external-symlinks"])
def test_reconciliation_repairs_corrupt_owned_activation_paths(tmp_path: Path, corrupt_as: str) -> None:
    old = build_user_credential_state(
        [("old", (HttpsCredentialScope("example.com"),), StoredCredential("user", "old"))]
    )
    new = build_user_credential_state(
        [("new", (HttpsCredentialScope("example.com"),), StoredCredential("user", "new"))]
    )
    assert _install(tmp_path, old).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    (root / "launch").unlink()
    (root / "current").unlink()
    if corrupt_as == "directories":
        (root / "launch").mkdir()
        (root / "current").mkdir()
    else:
        external = tmp_path / "external"
        external.mkdir()
        (external / "launch").write_text("outside")
        (root / "launch").symlink_to(external / "launch")
        (root / "current").symlink_to(external, target_is_directory=True)

    assert _install(tmp_path, new).returncode == 0
    assert (root / "launch").is_file() and not (root / "launch").is_symlink()
    assert (root / "current").is_symlink()
    if corrupt_as == "external-symlinks":
        assert (tmp_path / "external/launch").read_text() == "outside"
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=new\n\n"


def test_reconciliation_does_not_follow_corrupt_lock_or_generation_symlinks(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("outside-lock")
    external_generations = tmp_path / "external-generations"
    external_generations.mkdir()
    (external_generations / "sentinel").write_text("outside-generation")
    (root / "lock").unlink()
    (root / "lock").symlink_to(external_lock)
    current = root / "current"
    current.unlink()
    shutil.rmtree(root / "generations")
    (root / "generations").symlink_to(external_generations, target_is_directory=True)

    assert _install(tmp_path, state).returncode == 0
    assert external_lock.read_text() == "outside-lock"
    assert (external_generations / "sentinel").read_text() == "outside-generation"
    assert (root / "lock").is_file() and not (root / "lock").is_symlink()
    assert (root / "generations").is_dir() and not (root / "generations").is_symlink()


def test_hard_linked_lock_fails_closed_then_repairs_without_mutating_external_inode(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    lock = root / "lock"
    external = tmp_path / "external-lock"
    lock.write_text("external-content")
    lock.chmod(0o640)
    os.link(lock, external)
    assert lock.stat().st_nlink == external.stat().st_nlink == 2

    failed = _query(tmp_path, _request("example.com"))
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert external.read_text() == "external-content"
    assert stat.S_IMODE(external.stat().st_mode) == 0o640
    assert external.stat().st_nlink == 2

    assert _install(tmp_path, state).returncode == 0
    assert external.read_text() == "external-content"
    assert stat.S_IMODE(external.stat().st_mode) == 0o640
    assert external.stat().st_nlink == 1
    assert lock.read_text() == ""
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert lock.stat().st_nlink == 1
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=value\n\n"


def test_mode_zero_lock_recovers_on_reconciliation_and_retry(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    lock = tmp_path / ".agentworks/git-credentials/lock"
    inode = lock.stat().st_ino
    lock.chmod(0)

    failed = _query(tmp_path, _request("example.com"))
    assert failed.returncode != 0
    assert failed.stdout == ""

    recovered = _install(tmp_path, state)
    assert recovered.returncode == 0, recovered.stderr
    assert lock.stat().st_ino == inode
    assert lock.read_text() == ""
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert _install(tmp_path, state).returncode == 0
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=value\n\n"


def test_nonempty_single_link_lock_converges_empty_without_replacing_inode(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    lock = tmp_path / ".agentworks/git-credentials/lock"
    inode = lock.stat().st_ino
    lock.write_text("stale-owned-lock-content")

    assert _install(tmp_path, state).returncode == 0
    assert lock.stat().st_ino == inode
    assert lock.read_text() == ""
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_helper_and_reconciler_share_repaired_lock_identity(tmp_path: Path) -> None:
    critical = tmp_path / "reconcile-critical"
    overlap = tmp_path / "helper-overlap"
    old_helper = ManagedHelper(
        (
            "#!/bin/sh\n"
            f"[ ! -d {shlex.quote(str(critical))} ] || touch {shlex.quote(str(overlap))}\n"
            "printf 'username=user\\npassword=old\\n\\n'\n"
        ).encode(),
        "fixed failure",
    )
    old = build_user_credential_state([("old", (HttpsCredentialScope("example.com"),), old_helper)])
    new = build_user_credential_state(
        [("new", (HttpsCredentialScope("example.com"),), StoredCredential("user", "new"))]
    )
    assert _install(tmp_path, old).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("outside")
    (root / "lock").unlink()
    (root / "lock").symlink_to(external_lock)

    marker = "exec 6<&- 7<&-\n\nremove_config_value()"
    guarded_script = (
        _RECONCILE_SCRIPT.replace(
            marker,
            "exec 6<&- 7<&-\n" + f"mkdir {shlex.quote(str(critical))}\nsleep 0.3\n\nremove_config_value()",
            1,
        )
        + f"\nrmdir {shlex.quote(str(critical))}\n"
    )
    reconcile = subprocess.Popen(
        ["bash", "-c", guarded_script.replace("@DESIRED@", "present")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
    )
    assert reconcile.stdin is not None
    reconcile.stdin.write(_state_archive(new))
    reconcile.stdin.close()
    deadline = time.monotonic() + 2
    while not critical.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert critical.exists()

    helper = _query(tmp_path, _request("example.com"))
    assert reconcile.wait(timeout=2) == 0
    assert helper.returncode == 0
    assert helper.stdout == "username=user\npassword=new\n\n"
    assert not overlap.exists()
    assert external_lock.read_text() == "outside"


def test_concurrent_reconcilers_share_repaired_lock_identity(tmp_path: Path) -> None:
    old = build_user_credential_state(
        [("old", (HttpsCredentialScope("example.com"),), StoredCredential("user", "old"))]
    )
    first_state = build_user_credential_state(
        [("first", (HttpsCredentialScope("example.com"),), StoredCredential("user", "one"))]
    )
    second_state = build_user_credential_state(
        [("second", (HttpsCredentialScope("example.com"),), StoredCredential("user", "two"))]
    )
    assert _install(tmp_path, old).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("outside")
    (root / "lock").unlink()
    (root / "lock").symlink_to(external_lock)
    critical = tmp_path / "reconcile-critical"
    overlap = tmp_path / "reconcile-overlap"
    marker = "exec 6<&- 7<&-\n\nremove_config_value()"

    def instrument(script: str, *, delay: bool) -> str:
        body = (
            f"[ ! -d {shlex.quote(str(critical))} ] || touch {shlex.quote(str(overlap))}\n"
            f"mkdir {shlex.quote(str(critical))}\n"
        )
        if delay:
            body += "sleep 0.3\n"
        replacement = "exec 6<&- 7<&-\n" + body + "\nremove_config_value()"
        return script.replace(marker, replacement, 1) + f"\nrmdir {shlex.quote(str(critical))}\n"

    first = subprocess.Popen(
        ["bash", "-c", instrument(_RECONCILE_SCRIPT, delay=True).replace("@DESIRED@", "present")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
    )
    assert first.stdin is not None
    first.stdin.write(_state_archive(first_state))
    first.stdin.close()
    deadline = time.monotonic() + 2
    while not critical.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert critical.exists()
    second = subprocess.run(
        ["bash", "-c", instrument(_RECONCILE_SCRIPT, delay=False).replace("@DESIRED@", "present")],
        input=_state_archive(second_state),
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    )
    assert first.wait(timeout=2) == 0
    assert second.returncode == 0, second.stderr
    assert not overlap.exists()
    assert external_lock.read_text() == "outside"
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=two\n\n"


def test_reconciliation_replaces_a_symlinked_active_generation(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    active = (root / "current").resolve()
    external_generation = tmp_path / "external-generation"
    shutil.copytree(active, external_generation)
    shutil.rmtree(active)
    active.symlink_to(external_generation, target_is_directory=True)

    assert _install(tmp_path, state).returncode == 0
    repaired = (root / "current").resolve()
    assert repaired.is_relative_to(root / "generations")
    assert repaired.is_dir() and not repaired.is_symlink()
    assert (external_generation / "dispatch").exists()
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=value\n\n"


def test_launcher_fails_closed_and_reconcile_repairs_a_symlinked_root(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    saved_launcher = tmp_path / "saved-launch"
    saved_launcher.write_bytes((root / "launch").read_bytes())
    saved_launcher.chmod(0o700)
    shutil.rmtree(root)
    external = tmp_path / "external-root"
    external.mkdir()
    (external / "sentinel").write_text("outside")
    root.symlink_to(external, target_is_directory=True)

    failed = subprocess.run(
        [str(saved_launcher), "get"],
        input=_request("example.com"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert (external / "sentinel").read_text() == "outside"

    assert _install(tmp_path, state).returncode == 0
    assert root.is_dir() and not root.is_symlink()
    assert (external / "sentinel").read_text() == "outside"
    assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=value\n\n"


def test_helper_and_reconciler_lock_waits_are_bounded(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    lock_path = root / "lock"

    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        launcher_path = root / "launch"
        launcher = launcher_path.read_text()
        fast_launcher = launcher.replace("flock -s -w 10 9", "flock -s -w 0.1 9")
        assert fast_launcher != launcher
        launcher_path.write_text(fast_launcher)
        started = time.monotonic()
        blocked_helper = _query(tmp_path, _request("example.com"))
        elapsed = time.monotonic() - started
        assert blocked_helper.returncode != 0
        assert elapsed < 1

    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        fast_reconcile = _RECONCILE_SCRIPT.replace("flock -x -w 30 9", "flock -x -w 0.1 9")
        assert fast_reconcile != _RECONCILE_SCRIPT
        started = time.monotonic()
        blocked_reconcile = subprocess.run(
            ["bash", "-c", fast_reconcile.replace("@DESIRED@", "empty")],
            text=True,
            env=os.environ | {"HOME": str(tmp_path)},
            capture_output=True,
            check=False,
        )
        elapsed = time.monotonic() - started
        assert blocked_reconcile.returncode != 0
        assert elapsed < 1


def test_launchers_share_parent_handoff_while_reconciliation_is_excluded(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "value"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    launcher_path = root / "launch"
    lock_path = root / "lock"

    def reached_stable_lock(process: subprocess.Popen[str]) -> bool:
        descriptor = Path(f"/proc/{process.pid}/fd/9")
        try:
            return descriptor.samefile(lock_path)
        except FileNotFoundError:
            return False

    with lock_path.open("r+") as external_lock:
        fcntl.flock(external_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        launchers: list[subprocess.Popen[str]] = []
        for _ in range(2):
            launcher = subprocess.Popen(
                [str(launcher_path), "get"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=os.environ | {"HOME": str(tmp_path)},
            )
            assert launcher.stdin is not None
            launcher.stdin.write(_request("example.com"))
            launcher.stdin.close()
            launchers.append(launcher)

        deadline = time.monotonic() + 2
        while not all(reached_stable_lock(launcher) for launcher in launchers) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(reached_stable_lock(launcher) for launcher in launchers)

        reconcile = subprocess.Popen(
            ["bash", "-c", _RECONCILE_SCRIPT.replace("@DESIRED@", "empty")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ | {"HOME": str(tmp_path)},
        )
        time.sleep(0.1)
        assert reconcile.poll() is None

    for launcher in launchers:
        assert launcher.wait(timeout=2) == 0
        assert launcher.stdout is not None
        assert launcher.stdout.read() == "username=user\npassword=value\n\n"
    assert reconcile.wait(timeout=2) == 0


def test_desired_state_representations_and_scripts_do_not_contain_stored_values() -> None:
    value = "private-built-in-value"
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", value))]
    )
    assert value not in repr(state)
    assert value not in state.include_content
    assert value not in state.dispatcher_script


def test_reconciliation_is_idempotent_and_empty_removes_owned_state_only(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "config", "--global", "--add", "credential.helper", "operator-helper"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--add", "credential.helper", "!~/.agentworks-git-cred-helper.sh"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--add", "credential.helper", "operator-after"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--add", "include.path", "~/.operator-gitconfig"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    (tmp_path / ".git-credentials").write_text("unread-legacy-secret\n")
    (tmp_path / ".agentworks-git-cred-helper.sh").write_text("legacy")
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "secret"))]
    )
    assert _install(tmp_path, state).returncode == 0
    current = (tmp_path / ".agentworks/git-credentials/current").resolve()
    assert _install(tmp_path, state).returncode == 0
    assert (tmp_path / ".agentworks/git-credentials/current").resolve() == current
    managed_fill = subprocess.run(
        ["git", "credential", "fill"],
        input=_request("example.com"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )
    assert managed_fill.returncode == 0
    assert managed_fill.stdout == "protocol=https\nhost=example.com\nusername=user\npassword=secret\n"

    assert _install(tmp_path, build_user_credential_state([])).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    assert {path.name for path in root.iterdir()} == {"lock"}
    assert not (tmp_path / ".git-credentials").exists()
    assert not (tmp_path / ".agentworks-git-cred-helper.sh").exists()
    helpers = subprocess.run(
        ["git", "config", "--global", "--get-all", "credential.helper"],
        env=os.environ | {"HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert helpers == ["operator-helper", "operator-after"]
    includes = subprocess.run(
        ["git", "config", "--global", "--get-all", "include.path"],
        env=os.environ | {"HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    ).stdout.splitlines()
    assert "~/.agentworks/git-credentials/current/config.gitconfig" not in includes
    assert includes == ["~/.operator-gitconfig"]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_legacy_cleanup_runs_under_zsh_without_mutating_command_lookup(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "config", "--global", "--add", "credential.helper", "!~/.agentworks-git-cred-helper.sh"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    legacy_paths = (
        tmp_path / ".git-credentials",
        tmp_path / ".agentworks-git-cred-helper.sh",
        tmp_path / ".agentworks-git-scopes.gitconfig",
        tmp_path / ".agentworks-git-cred-warn.sh",
    )
    for legacy_path in legacy_paths:
        legacy_path.write_text("legacy")

    result = subprocess.run(
        ["zsh", "-c", _RECONCILE_SCRIPT.replace("@DESIRED@", "empty")],
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert all(not legacy_path.exists() for legacy_path in legacy_paths)


def test_empty_reconciliation_removes_directory_shaped_legacy_paths(tmp_path: Path) -> None:
    old = build_user_credential_state(
        [("old", (HttpsCredentialScope("example.com"),), StoredCredential("user", "old"))]
    )
    assert _install(tmp_path, old).returncode == 0
    legacy_paths = (
        tmp_path / ".git-credentials",
        tmp_path / ".agentworks-git-cred-helper.sh",
        tmp_path / ".agentworks-git-scopes.gitconfig",
        tmp_path / ".agentworks-git-cred-warn.sh",
    )
    for path in legacy_paths:
        if path.exists() or path.is_symlink():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        path.mkdir()
        (path / "old-value").write_text("old")
    subprocess.run(
        ["git", "config", "--global", "--add", "credential.helper", "store"],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "--add",
            "credential.helper",
            "!~/.agentworks-git-cred-helper.sh",
        ],
        env=os.environ | {"HOME": str(tmp_path)},
        check=True,
    )

    empty = build_user_credential_state([])
    assert _install(tmp_path, empty).returncode == 0
    assert _install(tmp_path, empty).returncode == 0
    assert all(not path.exists() for path in legacy_paths)
    fill = subprocess.run(
        ["git", "credential", "fill"],
        input=_request("example.com"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        check=False,
    )
    assert "password=old" not in fill.stdout
    assert {path.name for path in (tmp_path / ".agentworks/git-credentials").iterdir()} == {"lock"}


@pytest.mark.parametrize("directory_shaped", [False, True])
def test_reconciliation_preserves_unwitnessed_git_credentials_path(
    tmp_path: Path,
    *,
    directory_shaped: bool,
) -> None:
    legacy_store = tmp_path / ".git-credentials"
    if directory_shaped:
        legacy_store.mkdir()
        content = legacy_store / "operator-owned"
    else:
        content = legacy_store
    content.write_bytes(b"operator-owned-bytes\n")
    content.chmod(0o640)
    inode = content.stat().st_ino

    assert _install(tmp_path, build_user_credential_state([])).returncode == 0
    assert _install(tmp_path, build_user_credential_state([])).returncode == 0

    assert content.read_bytes() == b"operator-owned-bytes\n"
    assert content.stat().st_ino == inode
    assert stat.S_IMODE(content.stat().st_mode) == 0o640


@pytest.mark.parametrize("returncode", [20, 21, 22, 23, 24, 25, 29, 97])
def test_reconcile_maps_remote_failures_without_exposing_remote_data(returncode: int) -> None:
    sentinel = "remote-reflected-sensitive-payload"

    class _Target:
        kwargs: dict[str, object] | None = None

        def run(self, command: str, **kwargs: object) -> SSHResult:
            self.kwargs = kwargs
            return SSHResult(returncode, sentinel, sentinel)

    target = _Target()
    with pytest.raises(SSHError) as caught:
        reconcile_user_git_credentials(target, build_user_credential_state([]))  # type: ignore[arg-type]

    assert sentinel not in str(caught.value)
    assert target.kwargs is not None
    assert target.kwargs["check"] is False
    assert target.kwargs["timeout"] == 90
    assert target.kwargs["input_text"] == ""


def test_reconcile_sanitizes_transport_failure() -> None:
    sentinel = "transport-reflected-sensitive-payload"

    class _Target:
        def run(self, command: str, **kwargs: object) -> SSHResult:
            raise SSHError(f"{sentinel}: {command}: {kwargs['input_text']}")

    with pytest.raises(SSHError) as caught:
        reconcile_user_git_credentials(_Target(), build_user_credential_state([]))  # type: ignore[arg-type]

    assert sentinel not in str(caught.value)
    assert _RECONCILE_SCRIPT not in str(caught.value)


def test_reconciliation_replaces_complete_scope_and_payload_state(tmp_path: Path) -> None:
    default = ("default", (HttpsCredentialScope("example.com"),), StoredCredential("default", "one"))
    scoped = (
        "team",
        (HttpsCredentialScope("example.com", ("team",)),),
        StoredCredential("scoped", "two"),
    )
    helper = (
        "other",
        (HttpsCredentialScope("example.com", ("other",)),),
        ManagedHelper(
            b"#!/bin/sh\nprintf 'username=runtime\\npassword=three\\n\\n'\n",
            "fixed failure",
        ),
    )

    assert _install(tmp_path, build_user_credential_state([default])).returncode == 0
    assert _install(tmp_path, build_user_credential_state([default, scoped])).returncode == 0
    assert _query(tmp_path, _request("example.com", "team/repo")).stdout.startswith("username=scoped\n")

    assert _install(tmp_path, build_user_credential_state([scoped, helper])).returncode == 0
    assert _query(tmp_path, _request("example.com", "unscoped/repo")).stdout == ""
    assert _query(tmp_path, _request("example.com", "other/repo")).stdout.startswith("username=runtime\n")

    narrowed = (
        "team",
        (HttpsCredentialScope("example.com", ("team", "one")),),
        ManagedHelper(
            b"#!/bin/sh\nprintf 'username=runtime\\npassword=four\\n\\n'\n",
            "fixed failure",
        ),
    )
    assert _install(tmp_path, build_user_credential_state([narrowed])).returncode == 0
    assert _query(tmp_path, _request("example.com", "team/two")).stdout == ""
    assert _query(tmp_path, _request("example.com", "team/one/repo")).stdout.startswith("username=runtime\n")


def test_generation_modes_and_git_credential_fill_use_the_managed_include(tmp_path: Path) -> None:
    state = build_user_credential_state(
        [("one", (HttpsCredentialScope("example.com"),), StoredCredential("user", "secret"))]
    )
    assert _install(tmp_path, state).returncode == 0
    root = tmp_path / ".agentworks/git-credentials"
    assert stat.S_IMODE((root / "lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "launch").stat().st_mode) == 0o700
    current = (root / "current").resolve()
    assert stat.S_IMODE((current / "config.gitconfig").stat().st_mode) == 0o600
    assert stat.S_IMODE((current / "dispatch").stat().st_mode) == 0o700
    assert stat.S_IMODE(next((current / "stored").iterdir()).stat().st_mode) == 0o600

    result = subprocess.run(
        ["git", "credential", "fill"],
        input=_request("example.com", "owner/repo"),
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "username=user\n" in result.stdout
    assert "password=secret\n" in result.stdout


def test_killed_managed_helper_leaves_no_credential_bearing_runtime_file(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    runtime_tmp = tmp_path / "runtime-tmp"
    runtime_tmp.mkdir()
    helper = ManagedHelper(
        f'#!/bin/sh\nvalue=abnormal\nvalue="${{value}}-token"\n'
        f"printf 'username=user\\npassword=%s\\n\\n' \"$value\"\ntouch {marker}\nsleep 20\n".encode(),
        "fixed failure",
    )
    state = build_user_credential_state([("runtime", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    process = subprocess.Popen(
        [str(tmp_path / ".agentworks/git-credentials/launch"), "get"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"HOME": str(tmp_path), "TMPDIR": str(runtime_tmp)},
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(_request("example.com"))
    process.stdin.close()
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    os.killpg(process.pid, signal.SIGKILL)
    assert process.wait(timeout=2) < 0
    assert list(runtime_tmp.iterdir()) == []
    managed_root = tmp_path / ".agentworks/git-credentials"
    assert all(b"abnormal-token" not in path.read_bytes() for path in managed_root.rglob("*") if path.is_file())


def test_escaped_helper_descendant_cannot_hold_response_capture_open(tmp_path: Path) -> None:
    marker = "escaped-descendant-secret"
    escaped_pid = tmp_path / "escaped-pid"
    escaped_command = f"echo $$ > {shlex.quote(str(escaped_pid))}; printf {marker}; sleep 30"
    helper = ManagedHelper(
        (
            "#!/bin/sh\n"
            "printf 'username=user\\npassword=runtime-value\\n\\n'\n"
            f"setsid sh -c {shlex.quote(escaped_command)} &\n"
        ).encode(),
        "fixed bounded failure",
    )
    state = build_user_credential_state([("runtime", (HttpsCredentialScope("example.com"),), helper)])
    assert _install(tmp_path, state).returncode == 0
    _shorten_managed_helper_timeouts(tmp_path)
    started = time.monotonic()
    try:
        result = _query(tmp_path, _request("example.com"))
        elapsed = time.monotonic() - started
    finally:
        deadline = time.monotonic() + 1
        while not escaped_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if escaped_pid.exists():
            os.killpg(int(escaped_pid.read_text()), signal.SIGKILL)
    assert elapsed < 1
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "fixed bounded failure\n"
    assert marker not in result.stdout
    assert marker not in result.stderr


@pytest.mark.parametrize("replacement", ["stored", "empty"])
def test_managed_helper_open_descriptor_survives_reconciliation(tmp_path: Path, replacement: str) -> None:
    marker = tmp_path / "started"
    helper = ManagedHelper(
        f"#!/bin/sh\ntouch {marker}\nsleep 1\nprintf 'username=user\\npassword=old\\n\\n'\n".encode(),
        "fixed failure",
    )
    old_state = build_user_credential_state([("runtime", (HttpsCredentialScope("example.com"),), helper)])
    new_state = (
        build_user_credential_state([("new", (HttpsCredentialScope("example.com"),), StoredCredential("user", "new"))])
        if replacement == "stored"
        else build_user_credential_state([])
    )
    assert _install(tmp_path, old_state).returncode == 0
    process = subprocess.Popen(
        [str(tmp_path / ".agentworks/git-credentials/launch"), "get"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | {"HOME": str(tmp_path)},
    )
    assert process.stdin is not None
    process.stdin.write(_request("example.com"))
    process.stdin.close()
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    started = time.monotonic()
    assert _install(tmp_path, new_state).returncode == 0
    assert time.monotonic() - started < 0.8
    assert process.stdout is not None
    assert process.stdout.read() == "username=user\npassword=old\n\n"
    assert process.wait(timeout=2) == 0
    if replacement == "stored":
        assert _query(tmp_path, _request("example.com")).stdout == "username=user\npassword=new\n\n"
    else:
        assert {path.name for path in (tmp_path / ".agentworks/git-credentials").iterdir()} == {"lock"}
