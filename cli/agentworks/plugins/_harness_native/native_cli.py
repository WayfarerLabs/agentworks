"""Native plugin observations and commands, with no raw output in logs.

Payloads and scope behavior were measured with Codex 0.166.0 and Claude 2.1.316.
Provider additions are tolerated; the identity fields used below must remain
well formed. Source references are declared non-secret configuration.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from agentworks.capabilities.harness_integration.settings import parse_settings
from agentworks.errors import ConfigError, ExternalError
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.native_files import NativeFiles

type NativeTool = Literal["codex", "claude"]


# The login shell writes only PATH, never its full environment. Both profile
# noise and subprocess failures stay private; prepared values arrive on stdin.
_LOGIN_RESOLVE_PROGRAM = r"""
import json, os, shlex, shutil, subprocess, sys
request = json.load(sys.stdin)
output_path = sys.argv[1]
profile_path = output_path + '.profile'
os.chdir(request['home'])
shell = os.environ.get('SHELL')
if not shell:
    sys.exit(1)
capture = (
    "import json, os, sys; "
    "fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); "
    "json.dump(os.environ.get('PATH', ''), os.fdopen(fd, 'w'))"
)
subprocess.run(
    [shell, '-lc', shlex.join([sys.executable, '-c', capture, profile_path])],
    env={**os.environ, 'HOME': request['home']},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
)
with open(profile_path) as profile:
    login_path = json.load(profile)
path = login_path if request['path'] is None else request['path']
executable = shutil.which(request['tool'], path=path)
if executable is None:
    sys.exit(1)
fd = os.open(output_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump({'executable': os.path.abspath(executable), 'path': path}, output)
"""

_BOUND_STDERR = """import sys
with open(sys.argv[1], 'rb') as source:
    data = source.read(8192)
with open(sys.argv[2], 'xb') as destination:
    destination.write(data)
"""
_MARKETPLACE_ERROR = re.compile(r"\bmarketplaces\.([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?=[`'\s:]|$)")


@dataclass(frozen=True)
class NativeMarket:
    name: str
    source: str | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class NativePlugin:
    identifier: str
    scope: str
    enabled: bool
    source: str | None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def identity(value: object) -> str:
    """Canonical, non-secret native source identity for ownership claims."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _command_description(tool: NativeTool, args: list[str]) -> str:
    """Identify an operation without provider output or URL credentials."""
    count = 3 if args[:2] == ["plugin", "marketplace"] else 2
    operation = " ".join(args[:count])
    selector = args[count] if len(args) > count and not args[count].startswith("--") else None
    if selector is not None and args[:3] == ["plugin", "marketplace", "add"] and "://" in selector:
        try:
            parsed = urlsplit(selector)
            selector = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", parsed.fragment))
        except ValueError:
            selector = "<invalid URL>"
    return sanitize_setup_diagnostic(f"{tool} {operation}" + ("" if selector is None else f" {selector!r}"))


def _claude_market(name: str, source: dict[str, object], declaration: object) -> NativeMarket:
    """Keep a named declaration gap local without guessing its full association."""
    kind = source.get("source")
    location_key = (
        {"directory": "path", "file": "path", "github": "repo", "git": "url", "url": "url"}.get(kind)
        if isinstance(kind, str)
        else None
    )
    if location_key is None:
        return NativeMarket(name, None, "its native source type cannot be compared safely")
    if not isinstance(source.get(location_key), str) or not source[location_key]:
        return NativeMarket(name, None, f"its native registration has no usable {location_key!r}")
    if "ref" in source and (not isinstance(source["ref"], str) or not source["ref"]):
        return NativeMarket(name, None, "its native registration has an invalid source ref")
    if not isinstance(declaration, dict):
        return NativeMarket(name, None, "its extraKnownMarketplaces user declaration is not an object")
    if "source" not in declaration:
        if kind in ("git", "github"):
            return NativeMarket(name, None, "its full git source is unavailable without a usable user declaration")
        return NativeMarket(name, identity({"source": source}))
    declared_source = declaration["source"]
    if not isinstance(declared_source, dict):
        return NativeMarket(name, None, "its user declaration has no object-valued source")
    observed = dict(source)
    # Only omitted optional fields come from the current native declaration.
    # A differing source never establishes ownership or permission to remove it.
    for key in ("ref", "sparsePaths"):
        if key in declared_source and key not in observed:
            observed[key] = declared_source[key]
    if declared_source != observed:
        return NativeMarket(name, None, "its user declaration source differs from the observed native registration")
    if "ref" in observed and not isinstance(observed["ref"], str):
        return NativeMarket(name, None, "its user declaration has an invalid source ref")
    sparse = observed.get("sparsePaths", [])
    if not isinstance(sparse, list) or any(not isinstance(item, str) or not item for item in sparse):
        return NativeMarket(name, None, "its user declaration has invalid sparse paths")
    return NativeMarket(name, identity({"source": observed}))


class NativeCLI:
    """Run one harness as the invoking user, optionally with an isolated home."""

    def __init__(
        self,
        tool: NativeTool,
        files: NativeFiles,
        *,
        home: str,
        config_root: str,
        isolated: bool = False,
        environment: Mapping[str, str] | None = None,
    ):
        self.tool = tool
        self.files = files
        self.home = home
        self.config_root = config_root
        self.isolated = isolated
        self.environment = dict(environment or {})
        self._resolved: tuple[str, str] | None = None

    def _resolve_command(self) -> tuple[str, str]:
        """Capture the actual user's executable and login PATH once per setup."""
        if self._resolved is not None:
            return self._resolved
        remote, local = self.files.slot()
        command = shlex.join(["python3", "-c", _LOGIN_RESOLVE_PROGRAM, remote])
        request = {"home": self.home, "tool": self.tool, "path": self.environment.get("PATH")}
        result = self.files.runner.run(
            command,
            input_text=json.dumps(request),
            env={**self.environment, "HOME": self.home},
            check=False,
            timeout=30,
        )
        if not result.ok:
            raise ExternalError(f"could not resolve {self.tool} from the actual user's login environment")
        self.files.runner.copy_from(remote, local)
        try:
            observed = json.loads(local.read_bytes())
            executable = _text(observed["executable"])
            path = _text(observed["path"])
            if not executable.startswith("/") or "\x00" in executable or "\x00" in path:
                raise ValueError
        except Exception:
            raise ExternalError("invalid native executable discovery result") from None
        self._resolved = executable, path
        return self._resolved

    def command(self, args: list[str], *, structured: bool = False) -> object:
        executable, path = self._resolve_command()
        remote, local = self.files.slot()
        override = "CODEX_HOME" if self.tool == "codex" else "CLAUDE_CONFIG_DIR"
        env = {
            **self.environment,
            "PATH": path,
            override: self.config_root,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        if self.isolated:
            env.update(
                {
                    "HOME": self.config_root,
                    "CODEX_HOME": self.config_root + "/.codex",
                    "CLAUDE_CONFIG_DIR": self.config_root + "/.claude",
                    "XDG_CACHE_HOME": self.config_root + "/.cache",
                    "XDG_CONFIG_HOME": self.config_root + "/.config",
                    "XDG_DATA_HOME": self.config_root + "/.local/share",
                }
            )
        # Sensitive stdin keeps source URLs and all provider output out of the
        # transport logger. On failure only a bounded private stderr prefix is
        # inspected for a named marketplace, never rendered as provider prose.
        stderr = remote + ".stderr"
        diagnostic_path = remote + ".diagnostic"
        script = (
            f"cd {shlex.quote(self.files.remote)} || exit 1\n"
            f"{shlex.join([executable, *args])} > {shlex.quote(remote)} 2> {shlex.quote(stderr)}\n"
            "native_status=$?\n"
            'if [ "$native_status" -ne 0 ]; then\n'
            f"{shlex.join(['python3', '-c', _BOUND_STDERR, stderr, diagnostic_path])}\n"
            "fi\n"
            'exit "$native_status"'
        )
        result = self.files.runner.run("bash", input_text=script, env=env, check=False, timeout=180)
        if not result.ok:
            self.files.runner.copy_from(diagnostic_path, local)
            diagnostic = local.read_bytes().decode("utf-8", errors="replace")
            match = _MARKETPLACE_ERROR.search(diagnostic)
            entry = "" if match is None else f" at native setting {'marketplaces.' + match[1]!r}"
            raise ExternalError(
                f"{_command_description(self.tool, args)} failed{entry}; "
                f"inspect its setup in {self.config_root!r} with the native CLI"
            )
        if not structured:
            return None
        self.files.runner.copy_from(remote, local)
        try:
            return json.loads(local.read_bytes())
        except Exception:
            raise ExternalError(f"invalid {self.tool} native plugin response") from None

    def markets(self) -> tuple[NativeMarket, ...]:
        payload = self.command(["plugin", "marketplace", "list", "--json"], structured=True)
        active_root = (
            self.config_root + ("/.codex" if self.tool == "codex" else "/.claude")
            if self.isolated
            else self.config_root
        )
        settings_path = active_root + ("/config.toml" if self.tool == "codex" else "/settings.json")
        settings = self.files.read(settings_path)
        document = (
            {}
            if settings is None
            else parse_settings(
                settings,
                format="toml" if self.tool == "codex" else "json",
                location=f"guest destination {settings_path!r}",
            )
        )
        configured_raw = document.get("marketplaces" if self.tool == "codex" else "extraKnownMarketplaces", {})
        try:
            configured = _object(configured_raw)
            entries = _array(_object(payload)["marketplaces"] if self.tool == "codex" else payload)
            result = []
            for raw in entries:
                entry = _object(raw)
                name = _text(entry["name"])
                if self.tool == "codex":
                    if entry.get("marketplaceSource") is None:
                        result.append(NativeMarket(name, None))
                        continue
                    source = _object(entry["marketplaceSource"])
                else:
                    source = {
                        key: entry[key]
                        for key in ("source", "repo", "url", "path", "ref", "sparsePaths")
                        if key in entry
                    }
                if self.tool == "codex":
                    declaration = _object(configured.get(name, {}))
                    native_source = {"source_type": source["sourceType"], "source": source["source"]}
                    for key in ("ref", "sparse_paths"):
                        if key in declaration:
                            native_source[key] = declaration[key]
                    if any(key in declaration and declaration[key] != value for key, value in native_source.items()):
                        raise ValueError
                else:
                    result.append(_claude_market(name, source, configured.get(name, {})))
                    continue
                result.append(NativeMarket(name, identity(native_source)))
            observed_names = {market.name for market in result}
            if len(observed_names) != len(result):
                raise ValueError
            if self.tool == "claude":
                # A bad current declaration can hide a still-registered entry
                # from list output. Its name is evidence, never its full source.
                for name in configured:
                    if name in observed_names:
                        continue
                    result.append(
                        NativeMarket(name, None, "its user declaration has no matching native inventory entry")
                    )
            return tuple(result)
        except (KeyError, ValueError, TypeError):
            raise ExternalError(f"invalid {self.tool} native marketplace inventory") from None

    def plugins(self, markets: tuple[NativeMarket, ...]) -> tuple[NativePlugin, ...]:
        payload = self.command(["plugin", "list", "--json"], structured=True)
        by_name = {market.name: market for market in markets}
        try:
            entries = _array(_object(payload)["installed"] if self.tool == "codex" else payload)
            result = []
            for raw in entries:
                entry = _object(raw)
                selector = _text(entry["pluginId" if self.tool == "codex" else "id"])
                market_name = selector.rsplit("@", 1)[1]
                enabled = entry["enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError
                market = by_name.get(market_name)
                source = (
                    identity({"marketplace": market.source})
                    if market is not None and market.source is not None
                    else None
                )
                scope = "user" if self.tool == "codex" else _text(entry["scope"])
                result.append(NativePlugin(selector, scope, enabled, source))
            return tuple(result)
        except (KeyError, IndexError, ValueError, TypeError):
            raise ExternalError(f"invalid {self.tool} native installed-plugin inventory") from None

    def available(self) -> tuple[str, ...]:
        payload = self.command(["plugin", "list", "--available", "--json"], structured=True)
        try:
            entries = _array(_object(payload)["available"])
            return tuple(_text(_object(entry)["pluginId"]) for entry in entries)
        except (KeyError, ValueError, TypeError):
            raise ExternalError(f"invalid {self.tool} native plugin catalog") from None

    def add_market(self, source: str) -> None:
        if source.startswith("~/"):
            source = self.home + source[1:]
        elif not source.startswith("/"):
            candidate = posixpath.normpath(self.home + "/" + source)
            exists = self.files.runner.run("bash", input_text="test -e " + shlex.quote(candidate), check=False)
            if exists.ok:
                source = candidate
        self.command(["plugin", "marketplace", "add", source, *([] if self.tool == "codex" else ["--scope", "user"])])

    def remove_market(self, name: str) -> None:
        self.command(["plugin", "marketplace", "remove", name, *([] if self.tool == "codex" else ["--scope", "user"])])

    def install(self, selector: str) -> None:
        self.command(
            [
                "plugin",
                "add" if self.tool == "codex" else "install",
                selector,
                *([] if self.tool == "codex" else ["--scope", "user"]),
            ]
        )

    def uninstall(self, selector: str) -> None:
        self.command(
            [
                "plugin",
                "remove" if self.tool == "codex" else "uninstall",
                selector,
                *([] if self.tool == "codex" else ["--scope", "user", "--keep-data"]),
            ]
        )

    def discover(self, source: str) -> tuple[NativeMarket, tuple[str, ...]]:
        """Resolve a new source without registering it in the actual native home.

        Native source grammar remains the harness's. Git sources may be fetched
        twice: once here to validate the plan and once for actual registration.
        """
        root, _ = self.files.slot()
        result = self.files.runner.run(
            shlex.join(["mkdir", "-p", "-m", "700", root + "/.codex", root + "/.claude"]),
            check=False,
            discard_output=True,
        )
        if not result.ok:
            raise ExternalError("could not create isolated native marketplace discovery directory")
        staged = NativeCLI(
            self.tool, self.files, home=self.home, config_root=root, isolated=True, environment=self.environment
        )
        staged._resolved = self._resolve_command()
        staged.add_market(self.home + source[1:] if source.startswith("~/") else source)
        markets = staged.markets()
        if len(markets) != 1 or markets[0].source is None:
            raise ConfigError("marketplace source must resolve to one named native marketplace")
        return markets[0], staged.available()
