"""Exercise the native npm resolver and executables against a local registry."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import unquote, urlsplit

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.vms.initializer.npm import run_npm_install

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.name != "posix", reason="Linux guest npm flow")]


def _tarball(name: str, version: str, *, dependency: bool = False) -> bytes:
    manifest: dict[str, object] = {"name": name, "version": version}
    if name == "agw-npm-incompatible":
        manifest["engines"] = {"node": ">=999.0.0"}
    files = {}
    if name == "npm":
        manifest["bin"] = {"npm": "cli.js"}
        files["cli.js"] = (
            "#!/usr/bin/env node\n"
            f"if (process.argv[2] === '--version') console.log('{version}'); else process.exit(86);\n"
        )
    if dependency:
        manifest["dependencies"] = {"agw-npm-dep": "*"}
        manifest["bin"] = {"agw-npm-probe": "cli.js"}
        files["cli.js"] = (
            "#!/usr/bin/env node\n"
            "console.log(require('./package.json').version + ':' + require('agw-npm-dep/package.json').version);\n"
        )
    files["package.json"] = json.dumps(manifest)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, content in files.items():
            data = content.encode()
            entry = tarfile.TarInfo(f"package/{path}")
            entry.size = len(data)
            entry.mode = 0o755 if path == "cli.js" else 0o644
            archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


@pytest.fixture
def registry():
    tarballs = {
        (name, version): _tarball(name, version, dependency=name == "agw-npm-tool")
        for name in ("agw-npm-tool", "agw-npm-dep", "npm", "agw-npm-incompatible")
        for version in ("1.0.0", "2.0.0")
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path).strip("/")
            name = path.split("/", 1)[0]
            if name not in {"agw-npm-tool", "agw-npm-dep", "npm", "agw-npm-incompatible"}:
                self.send_error(404)
                return
            if "/-/" in path:
                version = path.rsplit("-", 1)[-1].removesuffix(".tgz")
                body = tarballs[(name, version)]
                content_type = "application/octet-stream"
            else:
                versions = {}
                for version in ("1.0.0", "2.0.0"):
                    archive = tarballs[(name, version)]
                    metadata: dict[str, object] = {
                        "name": name,
                        "version": version,
                        "dist": {
                            "tarball": f"http://127.0.0.1:{server.server_port}/{name}/-/{name}-{version}.tgz",
                            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode(),
                        },
                    }
                    if name == "agw-npm-tool":
                        metadata["dependencies"] = {"agw-npm-dep": "*"}
                        metadata["bin"] = {"agw-npm-probe": "cli.js"}
                    elif name == "npm":
                        metadata["bin"] = {"npm": "cli.js"}
                    elif name == "agw-npm-incompatible":
                        metadata["engines"] = {"node": ">=999.0.0"}
                    versions[version] = metadata
                body = json.dumps(
                    {
                        "name": name,
                        "dist-tags": {"latest": "2.0.0"},
                        "versions": versions,
                        "time": {
                            "1.0.0": "2000-01-01T00:00:00.000Z",
                            "2.0.0": datetime.now(UTC).isoformat(),
                        },
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def native_target(tmp_path: Path, registry: str):
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        pytest.skip("native npm and Node.js are required")
    version = subprocess.run([npm, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if tuple(int(part) for part in version.split(".")) < (11, 10, 0):
        pytest.skip("minimum release age requires npm >= 11.10.0")
    provisioned = tmp_path / "runtime"
    provisioned.mkdir()
    # Resolve through any host runtime-manager shim before isolating HOME.
    # The actual probe then has no runtime-manager executable or configuration.
    node_binary = subprocess.run(
        [node, "-p", "process.execPath"], capture_output=True, text=True, check=True
    ).stdout.strip()
    npm_script = Path(npm).resolve()
    if npm_script.name in {"mise", "asdf"}:
        global_root = subprocess.run(
            [npm, "root", "--global"], capture_output=True, text=True, check=True
        ).stdout.strip()
        npm_script = Path(global_root) / "npm/bin/npm-cli.js"
    (provisioned / "npm").symlink_to(npm_script)
    (provisioned / "node").symlink_to(node_binary)
    npmrc = tmp_path / ".npmrc"
    npmrc.write_text(f"registry={registry}\ncache={tmp_path / 'cache'}\naudit=false\nfund=false\n")
    (tmp_path / ".profile").write_text(
        f'export PATH={shlex.quote(str(tmp_path / ".local/bin"))}:{shlex.quote(str(provisioned))}:"$PATH"\n'
        f"export NPM_CONFIG_USERCONFIG={shlex.quote(str(npmrc))}\n"
    )
    child_env = {**os.environ, "HOME": str(tmp_path)}
    target = MagicMock()
    target.npm_version = tuple(int(part) for part in version.split("."))

    def run(command: str, *, check: bool = True, timeout: int = 60, **kwargs: object) -> SSHResult:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode:
            raise SSHError(result.stderr)
        return SSHResult(result.returncode, result.stdout, result.stderr)

    target.run.side_effect = run
    return target


def test_native_npm_filters_direct_and_transitive_releases_and_reinitializes(
    native_target: MagicMock, tmp_path: Path
) -> None:
    for _ in range(2):
        run_npm_install(native_target, ["agw-npm-tool@latest"], "sh", str(tmp_path), 7)
        installed = native_target.run(f"sh -lc {shlex.quote(str(tmp_path / '.local/bin/agw-npm-probe'))}")
        assert installed.stdout.strip() == "1.0.0:1.0.0"


def test_native_npm_refuses_new_explicit_pin(native_target: MagicMock, tmp_path: Path) -> None:
    logger = MagicMock()
    run_npm_install(native_target, ["agw-npm-tool@2.0.0"], "sh", str(tmp_path), 7, logger)
    logger.warning.assert_called()
    assert not (tmp_path / ".local/bin/agw-npm-probe").exists()


def test_native_npm_zero_age_allows_current_release(native_target: MagicMock, tmp_path: Path) -> None:
    run_npm_install(native_target, ["agw-npm-tool@2.0.0"], "sh", str(tmp_path), 0)
    installed = native_target.run(f"sh -lc {shlex.quote(str(tmp_path / '.local/bin/agw-npm-probe'))}")
    assert installed.stdout.strip() == "2.0.0:2.0.0"


def test_native_npm_reinit_with_stricter_age_reconciles_existing_install(
    native_target: MagicMock, tmp_path: Path
) -> None:
    run_npm_install(native_target, ["agw-npm-tool@latest"], "sh", str(tmp_path), 0)
    run_npm_install(native_target, ["agw-npm-tool@latest"], "sh", str(tmp_path), 7)
    installed = native_target.run(f"sh -lc {shlex.quote(str(tmp_path / '.local/bin/agw-npm-probe'))}")
    assert installed.stdout.strip() == "1.0.0:1.0.0"


def test_native_npm_age_limit_overrides_inherited_exclusions(native_target: MagicMock, tmp_path: Path) -> None:
    if native_target.npm_version < (11, 17, 0):
        pytest.skip("this npm does not support age exclusions")
    with (tmp_path / ".npmrc").open("a") as stream:
        stream.write("min-release-age-exclude[]=*\nbefore=2999-01-01\n")
    run_npm_install(native_target, ["agw-npm-tool@latest"], "sh", str(tmp_path), 7)
    installed = native_target.run(f"sh -lc {shlex.quote(str(tmp_path / '.local/bin/agw-npm-probe'))}")
    assert installed.stdout.strip() == "1.0.0:1.0.0"


def test_native_npm_replacement_without_age_support_stops_remaining_installs(
    native_target: MagicMock, tmp_path: Path
) -> None:
    logger = MagicMock()
    run_npm_install(native_target, ["npm@1.0.0", "agw-npm-tool@latest"], "sh", str(tmp_path), 7, logger)
    assert native_target.run("sh -lc 'npm --version'").stdout.strip() == "1.0.0"
    assert not (tmp_path / ".local/bin/agw-npm-probe").exists()
    logger.warning.assert_called_once()
    installs = [call for call in native_target.run.call_args_list if " install " in call.args[0]]
    assert len(installs) == 1


def test_native_npm_rejects_incompatible_node_even_with_user_force_enabled(
    native_target: MagicMock, tmp_path: Path
) -> None:
    with (tmp_path / ".npmrc").open("a") as stream:
        stream.write("force=true\nengine-strict=false\n")
    logger = MagicMock()
    run_npm_install(native_target, ["agw-npm-incompatible@1.0.0"], "sh", str(tmp_path), 7, logger)
    logger.warning.assert_called_once()
    assert "EBADENGINE" in logger.warning.call_args.args[0]
    assert not (tmp_path / ".local/lib/node_modules/agw-npm-incompatible").exists()
