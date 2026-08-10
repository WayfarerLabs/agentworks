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


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n")
    path.chmod(0o755)


def _run(command: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
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
    _tool(fake_bin, "aws", 'printf "%s\\n" "$AGW_TEST_AWS_VERSION"')
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
printf '%s\\n' "$*" > "$AGW_TEST_INSTALL_ARGS"
INSTALL
chmod +x "$destination/aws/install"
""",
    )
    _tool(fake_bin, "mktemp", 'mkdir -p "$AGW_TEST_TEMP_ROOT" && printf "%s\\n" "$AGW_TEST_TEMP_ROOT"')
    values = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGW_TEST_LOG": str(tmp_path / "log"),
        "AGW_TEST_ARCH": "x86_64",
        "AGW_TEST_FINGERPRINT": "FB5DB77FD5C118B80511ADA8A6310ACC4672475C",
        "AGW_TEST_VERIFY_STATUS": "0",
        "AGW_TEST_AWS_VERSION": "",
        "AGW_TEST_TEMP_ROOT": str(tmp_path / "private-temp"),
        "AGW_TEST_INSTALL_ARGS": str(tmp_path / "install-args"),
        "AGW_GCLOUD_KEYRING": str(tmp_path / "keyrings" / "cloud.google.gpg"),
        "AGW_GCLOUD_SOURCE_FILE": str(tmp_path / "sources" / "google-cloud-sdk.list"),
        "AGW_AWS_INSTALL_DIR": str(tmp_path / "aws-cli"),
        "AGW_AWS_BIN_DIR": str(tmp_path / "bin-dir"),
    }
    values.update(env)
    return subprocess.run(["bash", "-c", command], text=True, capture_output=True, env=values, check=False)


def test_gcloud_installer_reconciles_partial_key_and_source_setup(tmp_path: Path) -> None:
    source_file = tmp_path / "sources" / "google-cloud-sdk.list"
    source_file.parent.mkdir()
    source_file.write_text("deb stale source\n")
    result = _run(_command("gcp"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "keyrings" / "cloud.google.gpg").exists()
    assert (tmp_path / "sources" / "google-cloud-sdk.list").read_text() == (
        f"deb [signed-by={tmp_path}/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main\n"
    )
    log = (tmp_path / "log").read_text()
    assert "1 install -y google-cloud-cli" in log


@pytest.mark.parametrize("arch, expected_archive", [("x86_64", "x86_64"), ("aarch64", "aarch64")])
def test_aws_installer_selects_architecture_and_cleans_private_temp(
    tmp_path: Path, arch: str, expected_archive: str
) -> None:
    result = _run(_command("aws"), tmp_path, AGW_TEST_ARCH=arch)

    assert result.returncode == 0, result.stderr
    assert f"awscli-exe-linux-{expected_archive}.zip" in (tmp_path / "log").read_text()
    assert "--update" not in (tmp_path / "install-args").read_text()
    assert not (tmp_path / "private-temp").exists()


def test_aws_installer_updates_an_existing_explicit_installation(tmp_path: Path) -> None:
    (tmp_path / "aws-cli").mkdir()
    result = _run(_command("aws"), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "--update" in (tmp_path / "install-args").read_text()


def test_aws_installer_upgrades_v1_but_skips_existing_v2(tmp_path: Path) -> None:
    original = _command("aws")
    v1 = _run(original, tmp_path, AGW_TEST_AWS_VERSION="aws-cli/1.32.0 Python/test")
    assert v1.returncode == 0, v1.stderr

    result = _run(original, tmp_path / "v2", AGW_TEST_AWS_VERSION="aws-cli/2.1.0 Python/test")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "v2" / "log").exists()


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
    result = _run(_command("aws"), tmp_path, **extra)

    assert result.returncode != 0
    assert needle in result.stderr
    if extra.get("AGW_TEST_ARCH") != "ppc64le":
        assert not (tmp_path / "private-temp").exists()
