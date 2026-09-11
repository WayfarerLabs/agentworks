"""Reconcile native setup owned by one user or workspace integration activation.

Native command success is followed by identity observation before a checkpoint.
Matching registrations without a prior claim remain unowned and are refused.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.harness_integration.settings import (
    PreparedSettings,
    SettingsFormat,
    SettingsObject,
    SettingsValue,
    parse_settings,
    prepare_settings,
    serialize_settings,
)
from agentworks.errors import ConfigError, StateError
from agentworks.harness_setup.model import NativeClaim
from agentworks.plugins._harness_native.native_cli import NativeCLI, NativeMarket, NativeTool
from agentworks.plugins._harness_native.native_files import NativeFiles, native_path

if TYPE_CHECKING:
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
    from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig


def _format(tool: NativeTool) -> SettingsFormat:
    return "json" if tool == "claude" else "toml"


def _filename(tool: NativeTool) -> str:
    return "settings.json" if tool == "claude" else "config.toml"


def _hash(content: bytes | None) -> str | None:
    return None if content is None else hashlib.sha256(content).hexdigest()


def _native_key(tool: NativeTool, role: str, name: str) -> tuple[str, str]:
    return (
        ("enabledPlugins" if tool == "claude" else "plugins", name)
        if role == "plugin"
        else ("extraKnownMarketplaces" if tool == "claude" else "marketplaces", name)
    )


def _table(document: SettingsObject, key: str) -> SettingsObject:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError("native plugin settings must use object or table values")
    return value


def _market_fields(market: NativeMarket) -> SettingsObject:
    if market.source is None:
        raise ConfigError("native marketplace source identity is unavailable; register an explicit source first")
    return parse_settings(market.source.encode(), format="json")


def _market_location(tool: NativeTool, market: NativeMarket) -> SettingsValue:
    fields = _market_fields(market)
    if tool == "codex":
        return fields.get("source")
    source = fields.get("source")
    if not isinstance(source, dict):
        return None
    kind = source.get("source")
    if not isinstance(kind, str):
        return None
    location_key = {"directory": "path", "github": "repo", "git": "url", "url": "url"}.get(kind)
    return source.get(location_key) if location_key else None


def _overlay_fields(existing: SettingsValue, desired: SettingsValue) -> SettingsValue:
    if isinstance(existing, dict) and isinstance(desired, dict):
        return {**existing, **desired}
    return desired


@dataclass
class _MappingPlan:
    prepared: PreparedSettings
    skipped: bool
    contributions: SettingsObject

    @classmethod
    def build(cls, prepared: PreparedSettings, initial: bytes | None) -> _MappingPlan:
        result = prepared.apply(initial)
        return cls(prepared, result.skipped, prepared.contributions(initial))

    def publish(
        self,
        files: NativeFiles,
        destination: str,
        *,
        current: bytes | None,
        group: str = "",
        overlays: tuple[tuple[tuple[str, str], SettingsValue], ...] = (),
        removed: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if self.skipped:
            return
        # Keep the initial skip decision even when plugin commands create the file.
        result = self.prepared.apply(None if self.prepared.strategy == "skip-existing" else current)
        document = parse_settings(result.content, format=self.prepared.format)
        for key, name in removed:
            _table(document, key).pop(name, None)
        native = {} if current is None or not overlays else parse_settings(current, format=self.prepared.format)
        for (key, name), value in overlays:
            if key not in document:
                document[key] = {}
            table = _table(document, key)
            baseline = _table(native, key).get(name)
            table[name] = _overlay_fields(_overlay_fields(baseline, table.get(name, baseline)), value)
        content = serialize_settings(document, format=self.prepared.format)
        if content != current:
            files.publish(destination, content, expected=_hash(current), group=group)


def setup_workspace(
    tool: NativeTool, config: NativeWorkspaceConfig | None, invocation: WorkspaceSetupInvocation
) -> None:
    """Publish one fixed project role; workspace setup never installs plugins."""
    claims = list(invocation.prior.claims if invocation.prior else ())
    if any(claim.role != "settings" for claim in claims):
        raise StateError("workspace native setup contains unsupported ownership claims")
    mapping = None if config is None else config.settings
    if mapping is None:
        if claims:
            output.info("Removed settings mapping; native settings and their current values are retained.")
        invocation.checkpoint(())
        return
    prepared = prepare_settings(mapping, format=_format(tool))
    root = native_path(invocation.root) + ("/.claude" if tool == "claude" else "/.codex")
    destination = root + "/" + _filename(tool)
    with NativeFiles(invocation.runner) as files:
        initial = files.read(destination)
        plan = _MappingPlan.build(prepared, initial)
        plan.publish(files, destination, current=initial, group=invocation.linux_group)
        invocation.checkpoint(())


def _require_owned(claim: NativeClaim | None, source: str | None, *, operation: str) -> None:
    if claim is None:
        raise ConfigError(
            f"native {operation} already exists without an Agentworks claim; remove it with the native CLI first"
        )
    if source is None or claim.source != source:
        raise StateError(
            f"native {operation} differs from its ownership receipt; resolve the drift with the native CLI"
        )


def _check_contributions(
    tool: NativeTool,
    plan: _MappingPlan | None,
    desired: tuple[NativeMarket, ...],
    plugins: tuple[str, ...],
    obsolete: tuple[NativeClaim, ...],
) -> None:
    if plan is None or plan.skipped:
        return
    source = plan.contributions
    for selector in plugins:
        key, name = _native_key(tool, "plugin", selector)
        table = _table(source, key)
        if name in table:
            value = table[name]
            enabled = value if tool == "claude" else value.get("enabled", True) if isinstance(value, dict) else None
            if enabled is not True:
                raise ConfigError("mapped settings conflict with an explicitly requested plugin installation")
    for market in desired:
        key, name = _native_key(tool, "marketplace", market.name)
        table = _table(source, key)
        if name in table:
            value = table[name]
            expected = _market_fields(market)
            identity_fields = ("source",) if tool == "claude" else ("source_type", "source", "ref", "sparse_paths")
            if not isinstance(value, dict) or any(
                field in value and (field not in expected or value[field] != expected[field])
                for field in identity_fields
            ):
                raise ConfigError("mapped settings conflict with an explicitly requested marketplace source")
    for claim in obsolete:
        key, name = _native_key(tool, claim.role, claim.identifier)
        if name in _table(source, key):
            raise ConfigError("mapped settings still request a native association removed from explicit setup")


def setup_user(tool: NativeTool, config: NativeUserConfig | None, invocation: UserSetupInvocation) -> None:
    """Reconcile actual-user native associations using confirmed prior claims."""
    mapping = None if config is None else config.settings
    prepared = None if mapping is None else prepare_settings(mapping, format=_format(tool))
    claims = list(invocation.prior.claims if invocation.prior else ())
    if any(claim.role not in ("settings", "plugin", "marketplace") for claim in claims):
        raise StateError("user native setup contains unsupported ownership claims")
    override = "CLAUDE_CONFIG_DIR" if tool == "claude" else "CODEX_HOME"
    root = native_path(
        invocation.environment.get(override) or (invocation.home + ("/.claude" if tool == "claude" else "/.codex"))
    )
    if mapping is None and any(claim.role == "settings" for claim in claims):
        output.info("Removed settings mapping; native settings and their current values are retained.")
    native_claims = [claim for claim in claims if claim.role in ("plugin", "marketplace")]
    if native_claims != claims:
        invocation.checkpoint(tuple(native_claims))
    claims = native_claims.copy()
    if config is None and native_claims:
        roots = {native_path(claim.destination) for claim in native_claims}
        if len(roots) != 1:
            raise StateError("native retirement requires one recorded config home; ownership evidence was retained")
        root = roots.pop()
    destination = root + "/" + _filename(tool)
    if any(claim.destination != root for claim in native_claims):
        raise StateError(
            "native config home differs from existing ownership receipts; clean up the previous home first"
        )
    sources = [] if config is None else config.marketplaces
    requested = [] if config is None else config.plugins
    if not native_claims and not sources and not requested:
        if prepared is None or mapping is None:
            invocation.checkpoint(())
            return
        with NativeFiles(invocation.runner) as files:
            initial = files.read(destination)
            settings_plan = _MappingPlan.build(prepared, initial)
            settings_plan.publish(files, destination, current=initial)
            invocation.checkpoint(())
        return
    with NativeFiles(invocation.runner) as files:
        initial = files.read(destination)
        plan = None if mapping is None or prepared is None else _MappingPlan.build(prepared, initial)
        # Native inventory must be trustworthy before mutation. A settings-only
        # replacement can repair malformed input; plugin reconciliation refuses
        # it until that separate repair makes ownership observable again.
        initial_document = {} if initial is None else parse_settings(initial, format=_format(tool))
        cli = NativeCLI(tool, files, home=invocation.home, config_root=root, environment=invocation.environment)
        root_exists = files.directory(root)
        markets = cli.markets() if root_exists else ()
        installed = cli.plugins(markets) if root_exists else ()
        prior = {(claim.role, claim.identifier): claim for claim in native_claims}
        by_market = {market.name: market for market in markets}
        by_plugin = {plugin.identifier: plugin for plugin in installed if plugin.scope == "user"}
        desired: list[NativeMarket] = []
        source_for: dict[str, str] = {}
        available = set(plugin.identifier for plugin in installed)
        for source in sources:
            known = next(
                (
                    market
                    for market in markets
                    if market.source is not None and source == _market_location(tool, market)
                ),
                None,
            )
            if known is None:
                market, catalog = cli.discover(source)
                available.update(catalog)
            else:
                market = known
            if market.name in source_for:
                raise ConfigError("multiple marketplace sources resolve to the same native name")
            desired.append(market)
            source_for[market.name] = source
        if root_exists and requested:
            available.update(cli.available())
        selectors = []
        for selector in requested:
            matches = [
                item
                for item in available
                if item == selector or ("@" not in selector and item.split("@")[0] == selector)
            ]
            if len(matches) != 1:
                raise ConfigError("requested plugin is absent or ambiguous in the native marketplaces")
            selectors.append(matches[0])
        if len(selectors) != len(set(selectors)):
            raise ConfigError("multiple plugin names resolve to the same native installation")
        desired_names = {market.name for market in desired}
        obsolete = tuple(
            claim
            for claim in native_claims
            if claim.identifier not in (set(selectors) if claim.role == "plugin" else desired_names)
        )
        _check_contributions(tool, plan, tuple(desired), tuple(selectors), obsolete)
        # Validate all existing ownership and all removal dependencies before the
        # first actual native registration, plugin command, or settings write.
        for market in desired:
            if market.name in by_market:
                _require_owned(
                    prior.get(("marketplace", market.name)), by_market[market.name].source, operation="marketplace"
                )
                if market.source != by_market[market.name].source:
                    raise ConfigError("desired marketplace source differs from the existing native source")
        planned_markets = {**by_market, **{market.name: market for market in desired}}
        for selector in selectors:
            parent_market = planned_markets.get(selector.rsplit("@", 1)[1])
            if parent_market is None or parent_market.source is None:
                raise ConfigError(
                    "requested plugin has no observable marketplace source; register an explicit source first"
                )
            if selector in by_plugin:
                _require_owned(prior.get(("plugin", selector)), by_plugin[selector].source, operation="plugin")
        removing = {claim.identifier for claim in obsolete if claim.role == "plugin"}
        for claim in obsolete:
            if claim.role == "marketplace":
                if claim.identifier in by_market:
                    _require_owned(claim, by_market[claim.identifier].source, operation="marketplace")
                if any(
                    plugin.identifier.endswith("@" + claim.identifier)
                    and (plugin.scope != "user" or plugin.identifier not in removing)
                    for plugin in installed
                ):
                    raise StateError(
                        "marketplace cleanup would affect an unowned or project plugin; remove that dependency first"
                    )
            elif claim.identifier in by_plugin:
                _require_owned(claim, by_plugin[claim.identifier].source, operation="plugin")
            else:
                key, name = _native_key(tool, "plugin", claim.identifier)
                if name in _table(initial_document, key):
                    raise StateError(
                        "plugin ownership cannot be observed after marketplace drift; repair it with the native CLI"
                    )

        def checkpoint(claim: NativeClaim | None, role: str, identifier: str) -> None:
            nonlocal claims
            updated = [old for old in claims if (old.role, old.identifier) != (role, identifier)]
            if claim is not None:
                updated.append(claim)
            invocation.checkpoint(tuple(updated))
            claims = updated

        if sources or requested:
            files.directory(root, create=True)
        for claim in sorted(obsolete, key=lambda item: item.role == "marketplace"):
            if claim.role == "plugin" and claim.identifier in by_plugin:
                cli.uninstall(claim.identifier)
                observed = cli.plugins(cli.markets())
                if any(plugin.identifier == claim.identifier and plugin.scope == "user" for plugin in observed):
                    raise StateError("native plugin removal could not be confirmed")
            elif claim.role == "marketplace" and claim.identifier in by_market:
                cli.remove_market(claim.identifier)
                if any(market.name == claim.identifier for market in cli.markets()):
                    raise StateError("native marketplace removal could not be confirmed")
            checkpoint(None, claim.role, claim.identifier)
        for market in desired:
            if market.name not in by_market:
                cli.add_market(source_for[market.name])
                observed_market = next((item for item in cli.markets() if item.name == market.name), None)
                if observed_market is None or observed_market.source != market.source:
                    raise StateError("native marketplace registration identity could not be confirmed")
                checkpoint(
                    NativeClaim(role="marketplace", identifier=market.name, destination=root, source=market.source),
                    "marketplace",
                    market.name,
                )
        for selector in selectors:
            existing = by_plugin.get(selector)
            if existing is None or not existing.enabled:
                cli.install(selector)
                observed_plugin = next(
                    (
                        item
                        for item in cli.plugins(cli.markets())
                        if item.identifier == selector and item.scope == "user"
                    ),
                    None,
                )
                if observed_plugin is None or not observed_plugin.enabled or observed_plugin.source is None:
                    raise StateError("native user plugin installation could not be confirmed")
                checkpoint(
                    NativeClaim(role="plugin", identifier=selector, destination=root, source=observed_plugin.source),
                    "plugin",
                    selector,
                )
        if plan is not None:
            current = files.read(destination)
            current_document = {} if current is None else parse_settings(current, format=_format(tool))
            overlays: tuple[tuple[tuple[str, str], SettingsValue], ...] = tuple(
                (_native_key(tool, "marketplace", market.name), _market_fields(market)) for market in desired
            ) + tuple(
                (_native_key(tool, "plugin", selector), True if tool == "claude" else {"enabled": True})
                for selector in selectors
            )
            # Confirm the native command's own keys before overlaying them. Other
            # mapped fields under these tables remain source-controlled values.
            for (key, name), _value in overlays:
                if name not in _table(current_document, key):
                    raise StateError("native plugin settings association could not be confirmed")
            plan.publish(
                files,
                destination,
                current=current,
                overlays=overlays,
                removed=tuple(_native_key(tool, old.role, old.identifier) for old in obsolete),
            )
            invocation.checkpoint(tuple(claims))
