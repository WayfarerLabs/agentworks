"""Offline shell behavior for the optional cloud-provider guest installers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _command(provider: str) -> str:
    manifest = Path(__file__).parents[2] / "agentworks" / "plugins" / provider / "manifests" / "install-commands.yaml"
    return str(yaml.safe_load(manifest.read_text())["spec"]["command"])


def _aws_paths(tmp_path: Path) -> tuple[Path, Path]:
    install_dir = tmp_path / "managed" / "lib" / "agentworks" / "aws-cli"
    bin_dir = tmp_path / "managed" / "bin"
    return install_dir, bin_dir


def _aws_command_for_test(tmp_path: Path) -> str:
    install_dir, bin_dir = _aws_paths(tmp_path)
    return (
        _command("aws")
        .replace('install_dir="/usr/local/lib/agentworks/aws-cli"', f'install_dir="{install_dir}"')
        .replace('bin_dir="/usr/local/bin"', f'bin_dir="{bin_dir}"')
    )


def _aws_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    install_dir, bin_dir = _aws_paths(tmp_path)
    aws_target = install_dir / "v2" / "current" / "bin" / "aws"
    completer_target = install_dir / "v2" / "current" / "bin" / "aws_completer"
    return aws_target, completer_target, bin_dir / "aws", bin_dir / "aws_completer"


def _executable(path: Path, content: str = "managed\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _complete_aws_layout(tmp_path: Path) -> None:
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    _executable(aws_target)
    _executable(completer_target)
    aws_link.parent.mkdir(parents=True, exist_ok=True)
    aws_link.symlink_to(aws_target)
    completer_link.symlink_to(completer_target)


def _assert_complete_aws_layout(tmp_path: Path) -> None:
    install_dir, _ = _aws_paths(tmp_path)
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    assert install_dir.is_dir()
    assert not install_dir.is_symlink()
    assert aws_link.is_symlink()
    assert aws_link.readlink() == aws_target
    assert completer_link.is_symlink()
    assert completer_link.readlink() == completer_target
    assert aws_target.is_file()
    assert completer_target.is_file()
    assert os.access(aws_target, os.X_OK)
    assert os.access(completer_target, os.X_OK)


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(0o755)


def _run(command: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    if "awscli-exe-linux" in command:
        assert "/usr/local/lib/agentworks/aws-cli" not in command
        assert 'bin_dir="/usr/local/bin"' not in command
        assert "/usr/local/bin/aws" not in command
        assert "/usr/local/bin/aws_completer" not in command
    tmp_path.mkdir(exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _tool(
        fake_bin,
        "curl",
        """
output=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then output="$argument"; fi
  previous="$argument"
done
printf '%s\\n' "$*" >> "$AGW_TEST_LOG"
if [ -n "$output" ]; then : > "$output"; else printf fake-key; fi
""",
    )
    _tool(
        fake_bin,
        "aws",
        'printf "probe\\n" >> "$AGW_TEST_AWS_PROBE_LOG"\nprintf "%s\\n" "$AGW_TEST_AWS_VERSION"',
    )
    _tool(
        fake_bin,
        "sudo",
        """
printf 'sudo %s\\n' "$*" >> "$AGW_TEST_SUDO_LOG"
case "$1" in
  install)
    case " $* " in
      *" $AGW_TEST_AWS_INSTALL_DIR/"*|*" $AGW_TEST_AWS_BIN_DIR "*|*" $AGW_TEST_AWS_BIN_DIR/"*) exec "$@" ;;
    esac
    if [ "$2" = "-d" ]; then
      mkdir -p "$AGW_TEST_SYSTEM_ROOT/usr/share/keyrings" "$AGW_TEST_SYSTEM_ROOT/etc/apt/sources.list.d"
    else
      mkdir -p "$(dirname "$AGW_TEST_SYSTEM_ROOT$5")"
      cp "$4" "$AGW_TEST_SYSTEM_ROOT$5"
    fi
    ;;
  tee)
    mkdir -p "$(dirname "$AGW_TEST_SYSTEM_ROOT$2")"
    tee "$AGW_TEST_SYSTEM_ROOT$2"
    ;;
  chmod) : ;;
  *) exec "$@" ;;
esac
""",
    )
    _tool(
        fake_bin,
        "apt-get",
        'printf "%s %s\\n" "${CLOUDSDK_SKIP_PY_COMPILATION:-}" "$*" >> "$AGW_TEST_LOG"',
    )
    _tool(fake_bin, "uname", 'printf "%s\\n" "$AGW_TEST_ARCH"')
    _tool(
        fake_bin,
        "gpg",
        """
case " $* " in
  *" --with-colons "*)
    printf 'pub:::::::::\\n'
    printf 'fpr:::::::::%s:\\n' "$AGW_TEST_FINGERPRINT"
    printf 'sub:::::::::\\n'
    printf 'fpr:::::::::subkey-fingerprint:\\n'
    ;;
  *" --verify "*) exit "$AGW_TEST_VERIFY_STATUS" ;;
  *" --output "*)
    previous=""
    for argument in "$@"; do
      if [ "$previous" = "--output" ]; then : > "$argument"; fi
      previous="$argument"
    done
    ;;
esac
""",
    )
    _tool(
        fake_bin,
        "unzip",
        """
for argument in "$@"; do destination="$argument"; done
mkdir -p "$destination/aws"
cat > "$destination/aws/install" <<'INSTALL'
#!/bin/sh
set -eu
printf '%s\\n' "$*" > "$AGW_TEST_INSTALL_ARGS"
if [ "$AGW_TEST_INSTALL_STATUS" -ne 0 ]; then
  exit "$AGW_TEST_INSTALL_STATUS"
fi
case " $* " in
  *" --update "*) update=1 ;;
  *) update=0 ;;
esac
if [ "$update" -eq 0 ] && [ "$AGW_TEST_SAME_VERSION_EARLY_EXIT" -eq 1 ] \
  && [ -d "$AGW_TEST_AWS_INSTALL_DIR/v2/2.99.0" ]; then
  exit 0
fi
aws_target="$AGW_TEST_AWS_INSTALL_DIR/v2/current/bin/aws"
completer_target="$AGW_TEST_AWS_INSTALL_DIR/v2/current/bin/aws_completer"
mkdir -p "$(dirname "$aws_target")" "$AGW_TEST_AWS_BIN_DIR"
printf managed > "$aws_target"
printf managed > "$completer_target"
chmod +x "$aws_target" "$completer_target"
if [ "$AGW_TEST_INSTALL_LAYOUT" != "missing-aws-link" ]; then
  ln -sfn "$aws_target" "$AGW_TEST_AWS_BIN_DIR/aws"
fi
if [ "$AGW_TEST_INSTALL_LAYOUT" != "missing-completer-link" ]; then
  ln -sfn "$completer_target" "$AGW_TEST_AWS_BIN_DIR/aws_completer"
fi
if [ "$AGW_TEST_INSTALL_LAYOUT" = "missing-aws-executable" ]; then rm -f "$aws_target"; fi
if [ "$AGW_TEST_INSTALL_LAYOUT" = "missing-completer-executable" ]; then rm -f "$completer_target"; fi
if [ "$AGW_TEST_INSTALL_LAYOUT" = "nonexec-aws" ]; then chmod -x "$aws_target"; fi
if [ "$AGW_TEST_INSTALL_LAYOUT" = "nonexec-completer" ]; then chmod -x "$completer_target"; fi
INSTALL
chmod +x "$destination/aws/install"
""",
    )
    _tool(fake_bin, "mktemp", 'mkdir -p "$AGW_TEST_TEMP_ROOT" && printf "%s\\n" "$AGW_TEST_TEMP_ROOT"')
    install_dir, bin_dir = _aws_paths(tmp_path)
    values = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGW_TEST_LOG": str(tmp_path / "log"),
        "AGW_TEST_SUDO_LOG": str(tmp_path / "sudo-log"),
        "AGW_TEST_SYSTEM_ROOT": str(tmp_path / "system"),
        "AGW_TEST_ARCH": "x86_64",
        "AGW_TEST_FINGERPRINT": "FB5DB77FD5C118B80511ADA8A6310ACC4672475C",
        "AGW_TEST_VERIFY_STATUS": "0",
        "AGW_TEST_AWS_VERSION": "",
        "AGW_TEST_AWS_PROBE_LOG": str(tmp_path / "aws-probe-log"),
        "AGW_TEST_AWS_INSTALL_DIR": str(install_dir),
        "AGW_TEST_AWS_BIN_DIR": str(bin_dir),
        "AGW_TEST_INSTALL_STATUS": "0",
        "AGW_TEST_INSTALL_LAYOUT": "complete",
        "AGW_TEST_SAME_VERSION_EARLY_EXIT": "0",
        "AGW_TEST_TEMP_ROOT": str(tmp_path / "private-temp"),
        "AGW_TEST_INSTALL_ARGS": str(tmp_path / "install-args"),
    }
    values.update(env)
    return subprocess.run(["bash", "-c", command], text=True, capture_output=True, env=values, check=False)


def test_gcloud_installer_reconciles_partial_key_and_source_setup(tmp_path: Path) -> None:
    source_file = tmp_path / "system" / "etc" / "apt" / "sources.list.d" / "google-cloud-sdk.list"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("deb stale source\n")
    result = _run(_command("gcp"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "system" / "usr" / "share" / "keyrings" / "cloud.google.gpg").exists()
    assert source_file.read_text() == (
        "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main\n"
    )
    log = (tmp_path / "log").read_text()
    assert "1 install -y google-cloud-cli" in log
    sudo_log = (tmp_path / "sudo-log").read_text()
    assert "sudo install -d -m 0755" in sudo_log
    assert "sudo install -m 0644" in sudo_log
    assert "sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list" in sudo_log
    assert "sudo chmod 0644" in sudo_log
    assert "sudo apt-get update -y" in sudo_log
    assert "sudo env CLOUDSDK_SKIP_PY_COMPILATION=1 apt-get install -y google-cloud-cli" in sudo_log


@pytest.mark.parametrize("arch, expected_archive", [("x86_64", "x86_64"), ("aarch64", "aarch64")])
def test_aws_installer_selects_architecture_and_cleans_private_temp(
    tmp_path: Path, arch: str, expected_archive: str
) -> None:
    install_dir, bin_dir = _aws_paths(tmp_path)
    assert not install_dir.exists()
    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_ARCH=arch)

    assert result.returncode == 0, result.stderr
    assert f"awscli-exe-linux-{expected_archive}.zip" in (tmp_path / "log").read_text()
    assert "--update" not in (tmp_path / "install-args").read_text()
    sudo_log = (tmp_path / "sudo-log").read_text()
    assert sudo_log.splitlines() == [
        f"sudo {tmp_path}/private-temp/aws/install --install-dir {install_dir} --bin-dir {bin_dir}"
    ]
    _assert_complete_aws_layout(tmp_path)
    assert not (tmp_path / "aws-probe-log").exists()
    assert "sudo test" not in sudo_log
    assert "sudo gpg" not in sudo_log
    assert "sudo curl" not in sudo_log
    assert "sudo unzip" not in sudo_log
    assert not (tmp_path / "private-temp").exists()


def test_aws_installer_repeats_verified_recipe_and_updates_managed_install(tmp_path: Path) -> None:
    command = _aws_command_for_test(tmp_path)
    first = _run(command, tmp_path, AGW_TEST_AWS_VERSION="aws-cli/2.99.0 Python/test")

    assert first.returncode == 0, first.stderr
    assert "--update" not in (tmp_path / "install-args").read_text()
    assert not (tmp_path / "aws-probe-log").exists()

    second = _run(command, tmp_path, AGW_TEST_AWS_VERSION="aws-cli/2.99.0 Python/test")

    assert second.returncode == 0, second.stderr
    assert "--update" in (tmp_path / "install-args").read_text()
    assert len((tmp_path / "log").read_text().splitlines()) == 4
    install_dir, bin_dir = _aws_paths(tmp_path)
    assert (tmp_path / "sudo-log").read_text().splitlines() == [
        f"sudo {tmp_path}/private-temp/aws/install --install-dir {install_dir} --bin-dir {bin_dir}",
        f"sudo {tmp_path}/private-temp/aws/install --install-dir {install_dir} --bin-dir {bin_dir} --update",
    ]
    _assert_complete_aws_layout(tmp_path)
    assert not (tmp_path / "aws-probe-log").exists()


@pytest.mark.parametrize(
    "version",
    ["aws-cli/1.32.0 Python/test", "aws-cli/2.99.0 Python/test"],
    ids=("path-v1", "path-v2"),
)
def test_path_aws_versions_never_short_circuit_verified_recipe(tmp_path: Path, version: str) -> None:
    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_AWS_VERSION=version)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "log").is_file()
    assert (tmp_path / "install-args").is_file()
    assert not (tmp_path / "aws-probe-log").exists()


@pytest.mark.parametrize(
    "state",
    [
        "partial-managed-directory",
        "missing-aws-link",
        "missing-completer-link",
        "missing-aws-executable",
        "missing-completer-executable",
        "aws-artifact-directory",
        "completer-artifact-directory",
        "missing-current-layout",
        "exact-dangling-links-only",
    ],
)
def test_aws_installer_replaces_incomplete_owned_layout(tmp_path: Path, state: str) -> None:
    install_dir, _ = _aws_paths(tmp_path)
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    if state == "partial-managed-directory":
        install_dir.mkdir(parents=True)
        (install_dir / "partial-download").write_text("incomplete")
    elif state == "exact-dangling-links-only":
        aws_link.parent.mkdir(parents=True)
        aws_link.symlink_to(aws_target)
        completer_link.symlink_to(completer_target)
    else:
        _complete_aws_layout(tmp_path)
        if state == "missing-aws-link":
            aws_link.unlink()
        elif state == "missing-completer-link":
            completer_link.unlink()
        elif state == "missing-aws-executable":
            aws_target.unlink()
        elif state == "missing-completer-executable":
            completer_target.unlink()
        elif state == "aws-artifact-directory":
            aws_target.unlink()
            aws_target.mkdir()
        elif state == "completer-artifact-directory":
            completer_target.unlink()
            completer_target.mkdir()
        else:
            aws_target.parent.parent.rename(install_dir / "v2" / "saved-current")

    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_AWS_VERSION="aws-cli/2.99.0 Python/test")

    assert result.returncode == 0, result.stderr
    assert "--update" not in (tmp_path / "install-args").read_text()
    assert not (tmp_path / "aws-probe-log").exists()
    sudo_log = (tmp_path / "sudo-log").read_text()
    assert f"sudo rm -rf -- {install_dir}" in sudo_log
    assert f"sudo rm -f -- {aws_link} {completer_link}" in sudo_log
    _assert_complete_aws_layout(tmp_path)


def test_incomplete_layout_is_reset_before_same_version_installer_runs(tmp_path: Path) -> None:
    install_dir, _ = _aws_paths(tmp_path)
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    version_dir = install_dir / "v2" / "2.99.0"
    version_dir.mkdir(parents=True)
    (version_dir / "same-version-marker").write_text("would make the official installer exit early")
    aws_link.parent.mkdir(parents=True)
    aws_link.symlink_to(aws_target)
    completer_link.symlink_to(completer_target)

    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_SAME_VERSION_EARLY_EXIT="1")

    assert result.returncode == 0, result.stderr
    assert "--update" not in (tmp_path / "install-args").read_text()
    assert not version_dir.exists()
    _assert_complete_aws_layout(tmp_path)


def test_reserved_managed_symlink_is_replaced_without_touching_its_target(tmp_path: Path) -> None:
    install_dir, _ = _aws_paths(tmp_path)
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    external = tmp_path / "external-managed-layout"
    external.mkdir()
    marker = external / "keep"
    marker.write_text("external bytes")
    install_dir.parent.mkdir(parents=True)
    install_dir.symlink_to(external)
    aws_link.parent.mkdir(parents=True)
    aws_link.symlink_to(aws_target)
    completer_link.symlink_to(completer_target)

    result = _run(_aws_command_for_test(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "external bytes"
    _assert_complete_aws_layout(tmp_path)


@pytest.mark.parametrize("launcher_name", ["aws", "aws_completer"])
@pytest.mark.parametrize(
    "collision_kind",
    ["regular-file", "directory", "other-symlink", "relative-equivalent-symlink", "target-with-trailing-newline"],
)
def test_launcher_collision_fails_before_download_without_mutation(
    tmp_path: Path, launcher_name: str, collision_kind: str
) -> None:
    install_dir, bin_dir = _aws_paths(tmp_path)
    _complete_aws_layout(tmp_path)
    marker = install_dir / "ownership-marker"
    marker.write_text("managed bytes")
    aws_target, completer_target, _, _ = _aws_layout(tmp_path)
    collision = bin_dir / launcher_name
    collision.unlink()
    collision_target: str | None = None
    if collision_kind == "regular-file":
        collision.write_text("collision bytes")
    elif collision_kind == "directory":
        collision.mkdir()
        (collision / "keep").write_text("directory bytes")
    else:
        expected = aws_target if launcher_name == "aws" else completer_target
        collision_target = str(tmp_path / "someone-elses-aws")
        if collision_kind == "relative-equivalent-symlink":
            collision_target = os.path.relpath(expected, collision.parent)
        elif collision_kind == "target-with-trailing-newline":
            collision_target = f"{expected}\n"
        collision.symlink_to(collision_target)
    other = bin_dir / ("aws_completer" if launcher_name == "aws" else "aws")
    other_target = other.readlink()

    result = _run(_aws_command_for_test(tmp_path), tmp_path)

    assert result.returncode != 0
    assert marker.read_text() == "managed bytes"
    assert aws_target.read_text() == "managed\n"
    assert completer_target.read_text() == "managed\n"
    assert other.is_symlink()
    assert other.readlink() == other_target
    if collision_kind == "regular-file":
        assert collision.read_text() == "collision bytes"
    elif collision_kind == "directory":
        assert (collision / "keep").read_text() == "directory bytes"
    else:
        assert os.readlink(collision) == collision_target
    assert not (tmp_path / "log").exists()
    assert not (tmp_path / "private-temp").exists()
    assert not (tmp_path / "install-args").exists()
    assert not (tmp_path / "sudo-log").exists()


def test_provider_installer_commands_have_no_test_path_overrides() -> None:
    gcloud = _command("gcp")
    aws = _command("aws")

    assert "AGW_GCLOUD_" not in gcloud
    assert "AGW_AWS_" not in aws
    assert "/usr/share/keyrings/cloud.google.gpg" in gcloud
    assert "/etc/apt/sources.list.d/google-cloud-sdk.list" in gcloud
    assert "/usr/local/lib/agentworks/aws-cli" in aws
    assert "/usr/local/bin" in aws


def test_failed_aws_installer_cleans_private_temp(tmp_path: Path) -> None:
    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_INSTALL_STATUS="41")

    assert result.returncode == 41
    assert not (tmp_path / "private-temp").exists()
    assert not (tmp_path / "aws-probe-log").exists()


@pytest.mark.parametrize(
    "layout",
    [
        "missing-aws-link",
        "missing-completer-link",
        "missing-aws-executable",
        "missing-completer-executable",
        "nonexec-aws",
        "nonexec-completer",
    ],
)
def test_successful_installer_must_produce_complete_managed_layout(tmp_path: Path, layout: str) -> None:
    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_INSTALL_LAYOUT=layout)

    assert result.returncode != 0
    assert not (tmp_path / "private-temp").exists()


def test_untrusted_archive_does_not_mutate_incomplete_owned_layout(tmp_path: Path) -> None:
    install_dir, _ = _aws_paths(tmp_path)
    aws_target, completer_target, aws_link, completer_link = _aws_layout(tmp_path)
    install_dir.mkdir(parents=True)
    marker = install_dir / "partial"
    marker.write_text("unchanged")
    aws_link.parent.mkdir(parents=True)
    aws_link.symlink_to(aws_target)
    completer_link.symlink_to(completer_target)

    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_VERIFY_STATUS="1")

    assert result.returncode != 0
    assert marker.read_text() == "unchanged"
    assert aws_link.readlink() == aws_target
    assert completer_link.readlink() == completer_target
    assert not (tmp_path / "sudo-log").exists()
    assert not (tmp_path / "install-args").exists()


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"AGW_TEST_ARCH": "ppc64le"}, "unsupported on architecture"),
        ({"AGW_TEST_FINGERPRINT": "wrong"}, ""),
        ({"AGW_TEST_VERIFY_STATUS": "1"}, ""),
    ],
)
def test_aws_installer_rejects_unsupported_architecture_or_untrusted_archive(
    tmp_path: Path, extra: dict[str, str], needle: str
) -> None:
    result = _run(_aws_command_for_test(tmp_path), tmp_path, **extra)

    assert result.returncode != 0
    assert needle in result.stderr
    assert not (tmp_path / "install-args").exists()
    if extra.get("AGW_TEST_ARCH") != "ppc64le":
        assert not (tmp_path / "private-temp").exists()
