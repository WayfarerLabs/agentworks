#!/usr/bin/env python3
"""Drive the real Secret Sources CLI against isolated, value-free fixtures."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class _Result:
    returncode: int
    output: str


@dataclass(frozen=True)
class _CliLayout:
    root: Path
    executable: Path
    interpreter: Path


_SENTINEL = f"agw-secret-drive-{secrets.token_hex(16)}"
_UNSUPPORTED_HOST_MESSAGE = "secret sources drive supports POSIX hosts only; no child command or provider was attempted"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _select_cli_layout() -> _CliLayout:
    override = os.environ.get("AGW_CLI_DIR")
    root = Path(override).resolve() if override else Path(sys.prefix).resolve().parent
    executable = root / ".venv" / "bin" / "agw"
    interpreter = root / ".venv" / "bin" / "python"
    _require(root.joinpath("pyproject.toml").is_file(), "AGW_CLI_DIR does not name an agentworks CLI tree")
    _require(root.joinpath("agentworks").is_dir(), "AGW_CLI_DIR does not contain the agentworks package")
    _require(executable.is_file(), "selected CLI tree has no agw console script in .venv")
    _require(interpreter.is_file(), "selected CLI tree has no Python interpreter in .venv")
    _require(executable.resolve().is_relative_to(root), "selected agw console script escapes AGW_CLI_DIR")
    _require(interpreter.is_relative_to(root), "selected Python launcher escapes AGW_CLI_DIR")
    return _CliLayout(root, executable, interpreter)


def _validate_cli_import(layout: _CliLayout, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        [
            str(layout.interpreter),
            "-I",
            "-c",
            "from pathlib import Path; import agentworks; print(Path(agentworks.__file__).resolve())",
        ],
        cwd=layout.root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    _require(completed.returncode == 0, "selected CLI interpreter cannot import agentworks")
    imported = Path(completed.stdout.strip())
    _require(imported.is_relative_to(layout.root / "agentworks"), "selected CLI imports agentworks from another tree")


@contextmanager
def _drive_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="agw-secret-sources-drive-") as raw_root:
        yield Path(raw_root)


def _write_isolation_shim(root: Path) -> Path:
    shim = root / "python-shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        """\
import os
from pathlib import Path


def _isolated_home(cls):
    del cls
    return Path(os.environ["AGW_SECRET_DRIVE_HOME"])


Path.home = classmethod(_isolated_home)
"""
    )
    return shim


def _write_fixture(home: Path, *, settings: str, manifests: str) -> None:
    config_dir = home / ".config" / "agentworks"
    resources_dir = config_dir / "resources"
    resources_dir.mkdir(parents=True)
    public_key = home / "fixture-key.pub"
    private_key = home / "fixture-key"
    public_key.write_text("isolated fixture\n")
    private_key.write_text("isolated fixture\n")
    config_dir.joinpath("config.toml").write_text(
        f"""\
[operator]
ssh_public_key = "{public_key}"
ssh_private_key = "{private_key}"

{settings.strip()}
"""
    )
    resources_dir.joinpath("secrets.yaml").write_text(manifests)


def _child_environment(
    *,
    home: Path,
    shim: Path,
    path_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    platform_keys = ("LANG", "LC_ALL", "TEMP", "TMP", "TMPDIR")
    environment = {key: os.environ[key] for key in platform_keys if key in os.environ}
    environment.update(
        {
            "AGW_SECRET_DRIVE_HOME": str(home),
            "NO_COLOR": "1",
            "PATH": str(path_dir),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(shim),
            "TERM": "dumb",
        }
    )
    if extra_env:
        reserved = {"HOME", "PATH", "PYTHONHOME", "PYTHONPATH", "USERPROFILE"} & extra_env.keys()
        _require(not reserved, "extra harness environment tried to replace an isolation boundary")
        environment.update(extra_env)
    return environment


def _run(
    executable: Path,
    shim: Path,
    home: Path,
    *args: str,
    path_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> _Result:
    environment = _child_environment(home=home, shim=shim, path_dir=path_dir, extra_env=extra_env)
    completed = subprocess.run(
        [str(executable), *args],
        cwd=executable.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    _require(_SENTINEL not in completed.stdout, "resolved secret reached CLI output")
    return _Result(completed.returncode, completed.stdout)


def _secret_manifest(name: str, mapping: str = "") -> str:
    mapping_block = f"\n  backend_mappings:\n{mapping}" if mapping else ""
    return f"""\
apiVersion: agentworks/v1
kind: secret
metadata:
  name: {name}
  description: isolated acceptance fixture
spec:{mapping_block}
"""


def _case_implied_env(executable: Path, shim: Path, root: Path, closed_bin: Path) -> None:
    home = root / "implied-env"
    _write_fixture(home, settings='[secret_config]\nsources = ["env-var"]', manifests=_secret_manifest("implied-env"))
    result = _run(
        executable,
        shim,
        home,
        "secret",
        "verify",
        "implied-env",
        path_dir=closed_bin,
        extra_env={"AW_SECRET_IMPLIED_ENV": _SENTINEL},
    )
    _require(result.returncode == 0, "implied environment verification did not succeed")
    _require("resolved" in result.output, "implied environment outcome was not resolved")
    _require("env-var" in result.output, "implied environment outcome omitted its source")
    _require("AW_SECRET_IMPLIED_ENV" in result.output, "implied environment outcome omitted its identifier")
    print("[ok] implied env-var resolution: value-free resolved outcome")


def _case_prompt_refusal(executable: Path, shim: Path, root: Path, closed_bin: Path) -> None:
    home = root / "prompt-refusal"
    _write_fixture(home, settings='[secret_config]\nsources = ["prompt"]', manifests=_secret_manifest("prompt-only"))
    result = _run(executable, shim, home, "secret", "verify", "prompt-only", path_dir=closed_bin)
    _require(result.returncode == 1, "prompt refusal did not return the aggregate failure exit")
    _require("refused-interaction" in result.output, "prompt refusal outcome was not typed")
    _require("prompt" in result.output, "prompt refusal outcome omitted its source")
    print("[ok] prompt refusal: no prompt and value-free refused-interaction outcome")


def _case_variadic_mixed(executable: Path, shim: Path, root: Path, closed_bin: Path) -> None:
    home = root / "variadic-mixed"
    manifests = "---\n".join((_secret_manifest("implied-env"), _secret_manifest("missing-env")))
    _write_fixture(home, settings='[secret_config]\nsources = ["env-var"]', manifests=manifests)
    result = _run(
        executable,
        shim,
        home,
        "secret",
        "verify",
        "implied-env",
        "missing-env",
        path_dir=closed_bin,
        extra_env={"AW_SECRET_IMPLIED_ENV": _SENTINEL},
    )
    _require(result.returncode == 1, "mixed variadic verification did not aggregate failure")
    _require("implied-env" in result.output and "missing-env" in result.output, "variadic output omitted a name")
    _require("resolved" in result.output and "unavailable" in result.output, "mixed outcomes were not preserved")
    print("[ok] variadic verify: mixed resolved and unavailable outcomes, aggregate exit 1")


def _case_direct_backend_remediation(executable: Path, shim: Path, root: Path, closed_bin: Path) -> None:
    home = root / "direct-backend"
    settings = '[plugins]\nsystem = ["onepassword"]\n\n[secret_config]\nsources = ["onepassword"]'
    _write_fixture(home, settings=settings, manifests=_secret_manifest("op-token"))
    result = _run(executable, shim, home, "secret", "list", path_dir=closed_bin)
    _require(result.returncode == 1, "direct onepassword source name was not rejected")
    _require("not a configured secret-source" in result.output, "direct backend error omitted source distinction")
    _require("kind: secret-source" in result.output, "direct backend error omitted the source manifest rewrite")
    _require("<source-name>" in result.output, "direct backend error omitted the reference rewrite")
    print("[ok] direct onepassword migration: exact source declaration and reference rewrite shown")


def _write_fake_op(root: Path, interpreter: Path) -> tuple[Path, Path, Path]:
    bin_dir = root / "fake-bin"
    bin_dir.mkdir()
    marker = root / "fake-op-invoked"
    implementation = root / "fake-op.py"
    implementation.write_text(
        """\
import os
import sys
from pathlib import Path

expected = ["read", "--no-newline", "--account", "work.example.com", "op://Work/item/password"]
if sys.argv[1:] != expected:
    raise SystemExit(64)
Path(os.environ["AGW_SECRET_DRIVE_MARKER"]).write_text("invoked")
sys.stdout.write(os.environ["AGW_SECRET_DRIVE_SENTINEL"])
"""
    )
    executable = bin_dir / "op"
    executable.write_text(f"#!{interpreter}\n{implementation.read_text()}")
    executable.chmod(0o755)
    resolved = shutil.which("op", path=str(bin_dir))
    _require(resolved is not None, "fake-only PATH does not resolve op")
    _require(Path(resolved).resolve() == executable.resolve(), "fake-only PATH resolved an unexpected op command")
    return bin_dir, marker, executable


def _case_declared_onepassword(executable: Path, interpreter: Path, shim: Path, root: Path) -> None:
    home = root / "declared-onepassword"
    settings = '[plugins]\nsystem = ["onepassword"]\n\n[secret_config]\nsources = ["work-op"]'
    manifests = """\
apiVersion: agentworks/v1
kind: secret-source
metadata:
  name: work-op
spec:
  backend:
    name: onepassword
    account: work.example.com
    timeout: 5
---
apiVersion: agentworks/v1
kind: secret
metadata:
  name: op-token
  description: isolated acceptance fixture
spec:
  backend_mappings:
    work-op: op://Work/item/password
"""
    _write_fixture(home, settings=settings, manifests=manifests)
    bin_dir, marker, fake_op = _write_fake_op(root, interpreter)
    _require(
        Path(shutil.which("op", path=str(bin_dir)) or "").resolve() == fake_op.resolve(),
        "provider lookup could fall through to a real op command",
    )
    result = _run(
        executable,
        shim,
        home,
        "secret",
        "verify",
        "op-token",
        "--allow-interaction",
        path_dir=bin_dir,
        extra_env={
            "AGW_SECRET_DRIVE_MARKER": str(marker),
            "AGW_SECRET_DRIVE_SENTINEL": _SENTINEL,
        },
    )
    _require(result.returncode == 0, "declared OnePassword verification did not succeed")
    _require(marker.read_text() == "invoked", "declared OnePassword did not use the fake provider boundary")
    _require("resolved" in result.output and "work-op" in result.output, "OnePassword outcome omitted source status")
    _require("op://Work/item/password" in result.output, "OnePassword outcome omitted its safe identifier")
    print("[ok] declared onepassword source: fake provider boundary invoked, value remained private")


def _case_doctor(executable: Path, shim: Path, root: Path, closed_bin: Path) -> None:
    home = root / "doctor"
    manifests = "---\n".join(
        (
            _secret_manifest("implied-env"),
            _secret_manifest("prompt-only", "    env-var: false"),
        )
    )
    _write_fixture(home, settings="", manifests=manifests)
    result = _run(
        executable,
        shim,
        home,
        "doctor",
        path_dir=closed_bin,
        extra_env={"AW_SECRET_IMPLIED_ENV": _SENTINEL},
    )
    _require(result.returncode == 1, "isolated doctor did not report its expected missing-tool failures")
    expected = (
        "env-var: backend env-var; active; enabled; synthesized default; ready",
        "prompt: backend prompt; active; enabled; synthesized default; ready",
        "Secret 'implied-env': would attempt via env-var",
        "Secret 'prompt-only': would attempt via prompt",
    )
    _require(all(fragment in result.output for fragment in expected), "doctor omitted synthesized source evidence")
    print("[ok] doctor: synthesized sources are ready and attemptability stays probe-free")


def _run_drive(*, platform_name: str = os.name) -> int:
    if platform_name != "posix":
        print(_UNSUPPORTED_HOST_MESSAGE, file=sys.stderr)
        return 2
    layout = _select_cli_layout()
    with _drive_root() as root:
        shim = _write_isolation_shim(root)
        closed_bin = root / "closed-bin"
        closed_bin.mkdir()
        validation_home = root / "validation-home"
        validation_home.mkdir()
        validation_environment = _child_environment(
            home=validation_home,
            shim=shim,
            path_dir=closed_bin,
        )
        _validate_cli_import(layout, validation_environment)
        _case_implied_env(layout.executable, shim, root, closed_bin)
        _case_prompt_refusal(layout.executable, shim, root, closed_bin)
        _case_variadic_mixed(layout.executable, shim, root, closed_bin)
        _case_direct_backend_remediation(layout.executable, shim, root, closed_bin)
        _case_declared_onepassword(layout.executable, layout.interpreter, shim, root)
        _case_doctor(layout.executable, shim, root, closed_bin)
    print("secret sources real-entry drive passed; no resolved value reached command output")
    return 0


def main() -> None:
    raise SystemExit(_run_drive())


if __name__ == "__main__":
    main()
