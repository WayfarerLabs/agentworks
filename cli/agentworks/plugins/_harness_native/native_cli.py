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
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from agentworks import output
from agentworks.capabilities.harness_integration.settings import parse_settings
from agentworks.errors import ConfigError, ExternalError, StateError
from agentworks.execution.files import guest_path
from agentworks.execution.requests import CaptureOutput, Command, DiscardOutput, PythonProgram, Script, Shell
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from agentworks.capabilities.harness_integration.settings import SettingsObject
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation
    from agentworks.execution.requests import OutputSink
    from agentworks.execution.results import Result

type NativeTool = Literal["codex", "claude"]


_RESOLVED_PREFIX = "AGW_NATIVE_EXECUTABLE="

_EXPLICIT_PATH = "AGW_NATIVE_EXPLICIT_PATH"
"""The setup environment's own PATH, when it sets one, for resolution to search instead of the login PATH."""

# Run after the user's login startup, so PATH is the login PATH; an explicit
# PATH from the setup environment wins. The tool is the one argument, the PATH
# an environment value, so nothing reaches the program on stdin, which login
# startup could read first, and the log records the tool but no value.
_LOGIN_RESOLVE_PROGRAM = r"""
import json, os, shutil, sys
path = os.environ.get('AGW_NATIVE_EXPLICIT_PATH', os.environ.get('PATH', ''))
executable = shutil.which(sys.argv[1], path=path)
if executable is None:
    sys.exit(1)
observed = {'executable': os.path.abspath(executable), 'path': path}
sys.stdout.write('\nAGW_NATIVE_EXECUTABLE=' + json.dumps(observed) + '\n')
"""

_CAPTURE = CaptureOutput()
_DISCARD = DiscardOutput()
_STDERR_FILE = "native-stderr"
"""A failed command's stderr, in the scratch directory that is its working directory."""

_DIAGNOSTIC_BYTES = 8192
"""How much of a failed native command's stderr is read back, to find a named marketplace in it."""

_STRUCTURED_OUTPUT_LIMIT = 16 * 1024 * 1024
"""The largest JSON reply a native inventory command may give."""

_MARKETPLACE_ERROR = re.compile(r"\bmarketplaces\.([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?=[`'\s:]|$)")
_CODEX_MARKETPLACE_LOAD_ERROR = re.compile(
    r"^- `([A-Za-z0-9][A-Za-z0-9_.-]{0,127})` at <invalid source>:", re.MULTILINE
)
_CLAUDE_MARKETPLACE_ERROR = re.compile(
    r"Marketplace configuration file is corrupted: ([A-Za-z0-9][A-Za-z0-9_.-]{0,127}?)\.source(?:\.[a-zA-Z_]+)*:"
)


@dataclass(frozen=True)
class NativeMarket:
    name: str
    source: str | None
    unavailable_reason: str | None = None
    # Rewriting settings from this cached registration can undo a user edit.
    settings_conflict: bool = False


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


def _claude_market(name: str, source: Mapping[str, object], declaration: object) -> NativeMarket:
    """Keep a named declaration gap local without guessing its full association."""

    def unavailable(reason: str) -> NativeMarket:
        return NativeMarket(name, None, reason, settings_conflict=True)

    kind = source.get("source")
    location_key = (
        {"directory": "path", "file": "path", "github": "repo", "git": "url", "url": "url"}.get(kind)
        if isinstance(kind, str)
        else None
    )
    if location_key is None:
        return unavailable("its native source type cannot be compared safely")
    if not isinstance(source.get(location_key), str) or not source[location_key]:
        return unavailable(f"its native registration has no usable {location_key!r}")
    if "ref" in source and (not isinstance(source["ref"], str) or not source["ref"]):
        return unavailable("its native registration has an invalid source ref")
    if not isinstance(declaration, dict):
        return unavailable("its extraKnownMarketplaces user declaration is not an object")
    if "source" not in declaration:
        return unavailable("its source is unavailable without a usable user declaration")
    declared_source = declaration["source"]
    if not isinstance(declared_source, dict):
        return unavailable("its user declaration has no object-valued source")
    observed = dict(source)
    # Only omitted optional fields come from the current native declaration.
    # A differing source never establishes ownership or permission to remove it.
    for key in ("ref", "sparsePaths"):
        if key in declared_source and key not in observed:
            observed[key] = declared_source[key]
    if declared_source != observed:
        return unavailable("its user declaration source differs from the observed native registration")
    if "ref" in observed and not isinstance(observed["ref"], str):
        return unavailable("its user declaration has an invalid source ref")
    sparse = observed.get("sparsePaths", [])
    if not isinstance(sparse, list) or any(not isinstance(item, str) or not item for item in sparse):
        return unavailable("its user declaration has invalid sparse paths")
    return NativeMarket(name, identity({"source": observed}))


def unreadable_marketplace_settings(tool: NativeTool, document: SettingsObject) -> tuple[str, ...]:
    """Identify declarations whose preservation Agentworks cannot verify.

    The association comparator recognizes only supported source forms. An
    unrecognized declaration can be valid native configuration, but prevents
    automated whole-document native rewrites. A recognized declaration alone
    does not prove a registration or authorize adopting its association.
    """
    entries = document.get("marketplaces" if tool == "codex" else "extraKnownMarketplaces", {})
    if not isinstance(entries, dict):
        raise ConfigError("native marketplace settings must use object or table values")
    unsafe = []
    for name, declaration in entries.items():
        if not isinstance(declaration, dict):
            unsafe.append(name)
        elif tool == "claude":
            source = declaration.get("source")
            if not isinstance(source, dict) or _claude_market(name, source, declaration).source is None:
                unsafe.append(name)
        elif any(
            not isinstance(declaration.get(key), str) or not declaration[key] for key in ("source_type", "source")
        ):
            unsafe.append(name)
    return tuple(unsafe)


class NativeCLI(AbstractContextManager["NativeCLI"]):
    """Run one harness as the setup's user, optionally with an isolated home.

    Entering creates a private scratch directory on the guest, each command's
    working directory and the parent of isolated discovery homes; leaving
    removes it. Every request takes the setup's request environment, and
    neither a command line nor its output reaches the operation log.
    """

    def __init__(
        self,
        tool: NativeTool,
        invocation: UserSetupInvocation,
        *,
        config_root: str,
        isolated: bool = False,
        diagnostic_root: str | None = None,
    ):
        self.tool = tool
        self.invocation = invocation
        self.home = invocation.home
        self.config_root = config_root
        self.diagnostic_root = diagnostic_root or config_root
        self.isolated = isolated
        self.scratch: str | None = None
        self._discoveries = 0
        self._resolved: tuple[str, str] | None = None

    def __enter__(self) -> NativeCLI:
        result = self._run(Command(("mktemp", "-d", "-t", "agentworks-native-XXXXXXXXXX")))
        path = result.stdout.decode("utf-8", "replace").strip()
        if result.require_completed(self._vm) != 0 or not PurePosixPath(path).name.startswith("agentworks-native-"):
            raise ExternalError("could not create native setup scratch directory")
        self.scratch = guest_path(path)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self.scratch is None:
            return
        scratch, self.scratch = self.scratch, None
        try:
            if self._run(Command(("rm", "-rf", "--", scratch)), stdout=_DISCARD).require_completed(self._vm) != 0:
                raise ExternalError("could not remove native setup scratch directory")
        except Exception:
            if exc is None:
                raise
            output.warn("Native setup scratch cleanup failed; the original setup error is retained.")

    @property
    def _vm(self) -> str:
        return self.invocation.access.vm_name

    def _run(self, request: Command | Script, *, stdout: OutputSink = _CAPTURE) -> Result:
        """One request with the setup's request environment, bounded as setup's file operations are."""
        request = replace(request, env=self.invocation.request_environment(), deadline=self.invocation.files.deadline)
        return self.invocation.access.run(request, stdout=stdout, stderr=_DISCARD)

    def _scratch(self) -> str:
        if self.scratch is None:
            raise StateError("the native CLI's scratch directory is not open")
        return self.scratch

    def _resolve_command(self) -> tuple[str, str]:
        """Capture the actual user's executable and login PATH once per setup."""
        if self._resolved is not None:
            return self._resolved
        explicit = self.invocation.environment.get("PATH")
        result = self.invocation.access.run(
            PythonProgram(
                _LOGIN_RESOLVE_PROGRAM,
                args=(self.tool,),
                login=True,
                env=self.invocation.request_environment(None if explicit is None else {_EXPLICIT_PATH: explicit}),
                deadline=30,
                log_streams=False,
            ),
            stderr=_DISCARD,
        )
        if result.require_completed(self._vm) != 0:
            raise ExternalError(f"could not resolve {self.tool} from the actual user's login environment")
        try:
            # Login startup may print first, even without a trailing newline.
            (line,) = (
                line.removeprefix(_RESOLVED_PREFIX)
                for line in result.stdout.decode("utf-8").splitlines()
                if line.startswith(_RESOLVED_PREFIX)
            )
            observed = json.loads(line)
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
        override = "CODEX_HOME" if self.tool == "codex" else "CLAUDE_CONFIG_DIR"
        local = {
            "PATH": path,
            override: self.config_root,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        if self.isolated:
            local.update(
                {
                    "HOME": self.config_root,
                    "CODEX_HOME": self.config_root + "/.codex",
                    "CLAUDE_CONFIG_DIR": self.config_root + "/.claude",
                    "XDG_CACHE_HOME": self.config_root + "/.cache",
                    "XDG_CONFIG_HOME": self.config_root + "/.config",
                    "XDG_DATA_HOME": self.config_root + "/.local/share",
                }
            )
        # The command line, which can hold a source URL, is script text, and
        # neither stream is logged. On failure only a bounded stderr prefix
        # returns, inspected for a named marketplace and never shown as
        # provider prose.
        script = (
            f"{shlex.join([executable, *args])} 2>{_STDERR_FILE}\n"
            "native_status=$?\n"
            'if [ "$native_status" -ne 0 ]; then\n'
            f"  head -c {_DIAGNOSTIC_BYTES} -- {_STDERR_FILE} >&2\n"
            "fi\n"
            'exit "$native_status"'
        )
        result = self.invocation.access.run(
            Script(
                script,
                Shell.BASH,
                cwd=self._scratch(),
                env=self.invocation.request_environment(local),
                deadline=180,
                log_streams=False,
            ),
            stdout=CaptureOutput(_STRUCTURED_OUTPUT_LIMIT) if structured else _DISCARD,
            stderr=CaptureOutput(),
        )
        if result.require_completed(self._vm) != 0:
            diagnostic = result.stderr.decode("utf-8", errors="replace")
            match = _MARKETPLACE_ERROR.search(diagnostic)
            entry = "" if match is None else f" at native setting {'marketplaces.' + match[1]!r}"
            if match is None and self.tool == "codex":
                match = _CODEX_MARKETPLACE_LOAD_ERROR.search(diagnostic)
                if match is not None:
                    entry = f" at native setting {'marketplaces.' + match[1]!r}"
            elif match is None and self.tool == "claude":
                match = _CLAUDE_MARKETPLACE_ERROR.search(diagnostic)
                if match is not None:
                    entry = f" at native marketplace {match[1]!r} in plugins/known_marketplaces.json"
            raise ExternalError(
                f"{_command_description(self.tool, args)} failed{entry}; "
                f"inspect its setup in {self.diagnostic_root!r} with the native CLI"
            )
        if not structured:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise ExternalError(f"invalid {self.tool} native plugin response") from None

    def markets(self) -> tuple[NativeMarket, ...]:
        payload = self.command(["plugin", "marketplace", "list", "--json"], structured=True)
        active_root = (
            self.config_root + ("/.codex" if self.tool == "codex" else "/.claude")
            if self.isolated
            else self.config_root
        )
        settings_path = active_root + ("/config.toml" if self.tool == "codex" else "/settings.json")
        settings = self.invocation.files.read(settings_path)
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
            # Script text, not an argv, keeps a source URL's credentials out of the log.
            candidate = posixpath.normpath(self.home + "/" + source)
            if self._run(Script("test -e " + shlex.quote(candidate), Shell.SH)).require_completed(self._vm) == 0:
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
        self._discoveries += 1
        root = f"{self._scratch()}/discovery-{self._discoveries}"
        homes = Command(("mkdir", "-p", "-m", "700", "--", root + "/.codex", root + "/.claude"))
        if self._run(homes, stdout=_DISCARD).require_completed(self._vm) != 0:
            raise ExternalError("could not create isolated native marketplace discovery directory")
        staged = NativeCLI(
            self.tool,
            self.invocation,
            config_root=root,
            isolated=True,
            diagnostic_root=self.diagnostic_root,
        )
        staged.scratch = self.scratch
        staged._resolved = self._resolve_command()
        staged.add_market(self.home + source[1:] if source.startswith("~/") else source)
        markets = staged.markets()
        if len(markets) != 1 or markets[0].source is None:
            raise ConfigError("marketplace source must resolve to one named native marketplace")
        return markets[0], staged.available()
