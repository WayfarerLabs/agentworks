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
    install_dir = tmp_path / "managed" / "aws-cli"
    bin_dir = tmp_path / "managed" / "bin"
    return install_dir, bin_dir


def _aws_command_for_test(tmp_path: Path) -> str:
    install_dir, bin_dir = _aws_paths(tmp_path)
    return (
        _command("aws")
        .replace('install_dir="/usr/local/aws-cli"', f'install_dir="{install_dir}"')
        .replace('bin_dir="/usr/local/bin"', f'bin_dir="{bin_dir}"')
    )


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(0o755)


def _run(command: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    if "awscli-exe-linux" in command:
        assert "/usr/local/aws-cli" not in command
        assert 'bin_dir="/usr/local/bin"' not in command
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
mkdir -p "$AGW_TEST_AWS_INSTALL_DIR" "$AGW_TEST_AWS_BIN_DIR"
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
    assert install_dir.is_dir()
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


def test_aws_installer_updates_partial_managed_layout(tmp_path: Path) -> None:
    install_dir, bin_dir = _aws_paths(tmp_path)
    install_dir.mkdir(parents=True)
    (install_dir / "partial-download").write_text("incomplete")

    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_AWS_VERSION="aws-cli/2.99.0 Python/test")

    assert result.returncode == 0, result.stderr
    assert "--update" in (tmp_path / "install-args").read_text()
    assert not (tmp_path / "aws-probe-log").exists()
    assert f"--install-dir {install_dir} --bin-dir {bin_dir} --update" in (tmp_path / "sudo-log").read_text()


def test_provider_installer_commands_have_no_test_path_overrides() -> None:
    gcloud = _command("gcp")
    aws = _command("aws")

    assert "AGW_GCLOUD_" not in gcloud
    assert "AGW_AWS_" not in aws
    assert "/usr/share/keyrings/cloud.google.gpg" in gcloud
    assert "/etc/apt/sources.list.d/google-cloud-sdk.list" in gcloud
    assert "/usr/local/aws-cli" in aws
    assert "/usr/local/bin" in aws


def test_failed_aws_installer_cleans_private_temp(tmp_path: Path) -> None:
    result = _run(_aws_command_for_test(tmp_path), tmp_path, AGW_TEST_INSTALL_STATUS="41")

    assert result.returncode == 41
    assert not (tmp_path / "private-temp").exists()
    assert not (tmp_path / "aws-probe-log").exists()


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
