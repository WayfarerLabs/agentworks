"""Native plugin observations and commands, with no raw output in logs.

Payloads and scope behavior were measured with Codex 0.153.4 and Claude 2.1.263.
Provider additions are tolerated; the identity fields used below must remain
well formed. Source references are declared non-secret configuration.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.capabilities.harness_integration.settings import parse_settings
from agentworks.errors import ConfigError, ExternalError

if TYPE_CHECKING:
    from agentworks.capabilities.harness_integration.native_files import NativeFiles

type NativeTool = Literal["codex", "claude"]


@dataclass(frozen=True)
class NativeMarket:
    name: str
    source: str | None


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
    """Canonical, non-secret native source identity for compact receipts."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class NativeCLI:
    """Run one harness as the invoking user, optionally with an isolated home."""

    def __init__(self, tool: NativeTool, files: NativeFiles, *, home: str, config_root: str, isolated: bool = False):
        self.tool = tool
        self.files = files
        self.home = home
        self.config_root = config_root
        self.isolated = isolated

    def command(self, args: list[str], *, structured: bool = False) -> object:
        remote, local = self.files.slot()
        override = "CODEX_HOME" if self.tool == "codex" else "CLAUDE_CONFIG_DIR"
        env = {
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
        # transport logger. Only the private stdout file is subsequently read.
        script = (
            f"cd {shlex.quote(self.files.remote)} && "
            f"{shlex.join([self.tool, *args])} > {shlex.quote(remote)} 2>/dev/null"
        )
        result = self.files.runner.run("bash", input_text=script, env=env, check=False, timeout=180)
        if not result.ok:
            raise ExternalError(f"{self.tool} native plugin operation failed; inspect its setup with the native CLI")
        if not structured:
            return None
        try:
            self.files.runner.copy_from(remote, local)
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
        settings = self.files.read(active_root + ("/config.toml" if self.tool == "codex" else "/settings.json"))
        document = (
            {} if settings is None else parse_settings(settings, format="toml" if self.tool == "codex" else "json")
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
                declaration = _object(configured.get(name, {}))
                if self.tool == "codex":
                    native_source = {"source_type": source["sourceType"], "source": source["source"]}
                    for key in ("ref", "sparse_paths"):
                        if key in declaration:
                            native_source[key] = declaration[key]
                    if any(key in declaration and declaration[key] != value for key, value in native_source.items()):
                        raise ValueError
                else:
                    native_source = {"source": declaration.get("source", source)}
                    if native_source["source"] != source:
                        raise ValueError
                result.append(NativeMarket(name, identity(native_source)))
            if len({market.name for market in result}) != len(result):
                raise ValueError
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
                    identity({"marketplace": market.source, "version": _text(entry["version"])})
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
        staged = NativeCLI(self.tool, self.files, home=self.home, config_root=root, isolated=True)
        staged.add_market(self.home + source[1:] if source.startswith("~/") else source)
        markets = staged.markets()
        if len(markets) != 1 or markets[0].source is None:
            raise ConfigError("marketplace source must resolve to one named native marketplace")
        return markets[0], staged.available()
