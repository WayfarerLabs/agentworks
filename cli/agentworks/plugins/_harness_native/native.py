"""Reconcile native setup owned by one user or workspace integration activation.

Native command success is followed by identity observation before a checkpoint.
Identical registrations can be adopted; differing unowned entries are retained
and reported without preventing independent entries from converging.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactInputs
from agentworks.artifacts.native.mcp import apply_mcp
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
from agentworks.execution.errors import FileConflict, PathRefusal, PathRefused
from agentworks.execution.files import Absent, Directory, guest_path
from agentworks.harness_setup.model import NativeClaim
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.plugins._harness_native.native_cli import (
    NativeCLI,
    NativeMarket,
    NativePlugin,
    NativeTool,
    identity,
    unreadable_marketplace_settings,
)

if TYPE_CHECKING:
    from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
    from agentworks.execution.files import Files
    from agentworks.plugins._harness_native.native_config import NativeUserConfig, NativeWorkspaceConfig


def _format(tool: NativeTool) -> SettingsFormat:
    return "json" if tool == "claude" else "toml"


def _filename(tool: NativeTool) -> str:
    return "settings.json" if tool == "claude" else "config.toml"


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


def _simple_market_location(tool: NativeTool, market: NativeMarket) -> SettingsValue:
    # Optional refs and sparse paths need isolated native discovery to establish
    # the full requested identity; a location alone cannot establish a match.
    fields = _market_fields(market)
    if tool == "codex":
        return fields.get("source") if set(fields) == {"source_type", "source"} else None
    source = fields.get("source")
    if not isinstance(source, dict):
        return None
    kind = source.get("source")
    if not isinstance(kind, str):
        return None
    location_key = {"directory": "path", "file": "path", "github": "repo", "git": "url", "url": "url"}.get(kind)
    return source.get(location_key) if location_key and set(source) == {"source", location_key} else None


def _overlay_fields(existing: SettingsValue, desired: SettingsValue) -> SettingsValue:
    if isinstance(existing, dict) and isinstance(desired, dict):
        return {**existing, **desired}
    return desired


def _preserve_entries(
    document: SettingsObject, current: SettingsObject, protected: tuple[tuple[str, str], ...]
) -> None:
    """Preserve entry-local skips without deriving an unobservable association."""
    for key, name in protected:
        baseline = _table(current, key)
        observed = _table(document, key)
        if name in baseline:
            if key not in document:
                document[key] = {}
            _table(document, key)[name] = baseline[name]
        else:
            observed.pop(name, None)


@dataclass
class _MappingPlan:
    prepared: PreparedSettings
    skipped: bool
    contributions: SettingsObject

    @classmethod
    def build(cls, prepared: PreparedSettings, initial: bytes | None, destination: str) -> _MappingPlan:
        result = prepared.apply(initial, destination_path=destination)
        return cls(prepared, result.skipped, prepared.contributions(initial, destination_path=destination))

    def publish(
        self,
        files: Files,
        destination: str,
        *,
        current: bytes | None,
        group: str | None = None,
        overlays: tuple[tuple[tuple[str, str], SettingsValue], ...] = (),
        removed: tuple[tuple[str, str], ...] = (),
        protected: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Publish the planned settings over ``current``, what was last read, through :meth:`Files.update`.

        User files are 0600 under 0700 parents; workspace files, with
        ``group``, 0660 under setgid 2770 parents. Settings equal to
        ``current`` write nothing, so the file keeps its permissions. A file
        that changed since ``current`` was read is ``FileConflict``, and
        nothing is written.
        """
        content, _ = self.render(destination, current=current, overlays=overlays, removed=removed, protected=protected)
        if content is None:
            return
        try:
            files.update(
                destination,
                content,
                observed=current,
                mode=0o600 if group is None else 0o660,
                parent_mode=0o700 if group is None else 0o2770,
                group=group,
            )
        except FileConflict as conflict:
            raise FileConflict(
                conflict.vm_name,
                "settings publication",
                destination,
                hint="Another writer changed the file between setup's read and its write.",
            ) from None

    def render(
        self,
        destination: str,
        *,
        current: bytes | None,
        overlays: tuple[tuple[tuple[str, str], SettingsValue], ...] = (),
        removed: tuple[tuple[str, str], ...] = (),
        protected: tuple[tuple[str, str], ...] = (),
    ) -> tuple[bytes | None, SettingsObject]:
        """The planned settings over ``current``, and the mapping's policy-winning contributions."""
        if self.skipped:
            return current, {}
        # Keep the initial skip decision even when plugin commands create the file.
        result = self.prepared.apply(
            None if self.prepared.strategy == "skip-existing" else current, destination_path=destination
        )
        document = parse_settings(result.content, format=self.prepared.format)
        for key, name in removed:
            _table(document, key).pop(name, None)
        native = (
            {}
            if current is None or not (overlays or protected)
            else parse_settings(current, format=self.prepared.format, location=f"guest destination {destination!r}")
        )
        for (key, name), value in overlays:
            if key not in document:
                document[key] = {}
            table = _table(document, key)
            baseline = _table(native, key).get(name)
            table[name] = _overlay_fields(_overlay_fields(baseline, table.get(name, baseline)), value)
        # A skipped native association is not permission to undo an explicit
        # settings mapping. Preserve only values the selected policy leaves alone.
        contributions = self.prepared.contributions(current, destination_path=destination)
        retained = tuple((key, name) for key, name in protected if name not in _table(contributions, key))
        _preserve_entries(document, native, retained)
        return serialize_settings(document, format=self.prepared.format), contributions


def _map_settings(
    prepared: PreparedSettings, destination: str, current: bytes | None
) -> tuple[bytes | None, SettingsObject]:
    """Plan against the same snapshot the MCP entry updater will publish."""
    return _MappingPlan.build(prepared, current, destination).render(destination, current=current)


def setup_workspace(
    tool: NativeTool, config: NativeWorkspaceConfig | None, invocation: WorkspaceSetupInvocation
) -> ArtifactApplication:
    """Publish one fixed project role; workspace setup never installs plugins."""
    claims = list(invocation.prior.claims if invocation.prior else ())
    preserved = tuple(claim for claim in claims if claim.role != "settings")
    if config is None:
        invocation = replace(invocation, artifacts=ArtifactInputs())
    mapping = None if config is None else config.settings
    prepared = None if mapping is None else prepare_settings(mapping, format=_format(tool))
    root = guest_path(invocation.root) + ("/.claude" if tool == "claude" else "/.codex")
    destination = root + "/" + _filename(tool)
    if len(preserved) != len(claims):
        if mapping is None:
            output.info("Removed settings mapping; native settings and their current values are retained.")
        invocation.checkpoint(preserved)
    if tool == "claude" and prepared is not None:
        files = invocation.files
        initial = files.read(destination)
        plan = _MappingPlan.build(prepared, initial, destination)
        plan.publish(files, destination, current=initial, group=invocation.linux_group)
    return apply_mcp(
        tool,
        invocation,
        destination=guest_path(invocation.root) + "/.mcp.json" if tool == "claude" else destination,
        claims=preserved,
        map_document=(lambda current: _map_settings(prepared, destination, current))
        if tool == "codex" and prepared is not None
        else None,
    )


def _is_directory(files: Files, path: str) -> bool:
    """Whether the native home ``path`` exists; anything but a directory there is refused."""
    state = files.stat(path)
    if isinstance(state, Directory):
        return True
    if isinstance(state, Absent):
        return False
    raise PathRefused(files.access.vm_name, "stat", path, PathRefusal.UNSUITABLE)


def _owned(claim: NativeClaim | None, source: str | None) -> bool:
    if claim is None or claim.source is None or source is None:
        return False
    if claim.role != "plugin":
        return claim.source == source
    # Older claims included the mutable native version. Association ownership
    # survives native updates while retaining the full parent marketplace identity.
    try:
        recorded = parse_settings(claim.source.encode(), format="json")
    except ConfigError:
        return False
    recorded.pop("version", None)
    return identity(recorded) == source


def _plugin_market(plugin: NativePlugin) -> str | None:
    return None if plugin.source is None else str(parse_settings(plugin.source.encode(), format="json")["marketplace"])


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


def setup_user(
    tool: NativeTool,
    config: NativeUserConfig | None,
    invocation: UserSetupInvocation,
    *,
    native_root: str | None,
    mcp_destination: str | None = None,
    skip_mcp: bool = False,
) -> ArtifactApplication:
    """Reconcile actual-user native associations using confirmed prior claims."""
    mapping = None if config is None else config.settings
    if config is None:
        invocation = replace(invocation, artifacts=ArtifactInputs())
    prepared = None if mapping is None else prepare_settings(mapping, format=_format(tool))
    claims = list(invocation.prior.claims if invocation.prior else ())
    preserved = tuple(claim for claim in claims if claim.role not in ("settings", "plugin", "marketplace"))
    root = native_root
    if mapping is None and any(claim.role == "settings" for claim in claims):
        output.info("Removed settings mapping; native settings and their current values are retained.")
    native_claims = [claim for claim in claims if claim.role in ("plugin", "marketplace")]
    if len(native_claims) + len(preserved) != len(claims):
        invocation.checkpoint((*preserved, *native_claims))
    claims = native_claims.copy()
    if config is None and native_claims:
        roots = {guest_path(claim.destination) for claim in native_claims}
        if len(roots) != 1:
            raise StateError("native retirement requires one recorded config home; ownership evidence was retained")
        root = roots.pop()
    if root is None:
        return ArtifactApplication()
    root = guest_path(root)
    destination = root + "/" + _filename(tool)
    if not skip_mcp and mcp_destination is None:
        raise StateError("native MCP setup requires the selected current config role")
    joint_mapping = tool == "codex" and mcp_destination == destination and not skip_mcp
    if any(claim.destination != root for claim in native_claims):
        raise StateError("native config home differs from existing ownership claims; clean up the previous home first")
    sources = [] if config is None else config.marketplaces
    requested = [] if config is None else config.plugins
    if not native_claims and not sources and not requested:
        if prepared is not None and not joint_mapping:
            initial = invocation.files.read(destination)
            settings_plan = _MappingPlan.build(prepared, initial, destination)
            settings_plan.publish(invocation.files, destination, current=initial)
        if skip_mcp:
            return ArtifactApplication()
        assert mcp_destination is not None
        return apply_mcp(
            tool,
            invocation,
            destination=mcp_destination,
            claims=(*preserved, *claims),
            map_document=(lambda current: _map_settings(prepared, destination, current))
            if joint_mapping and prepared is not None
            else None,
        )
    files = invocation.files
    with NativeCLI(tool, invocation, config_root=root) as cli:
        initial = files.read(destination)
        plan = None if mapping is None or prepared is None else _MappingPlan.build(prepared, initial, destination)
        # Native inventory must be trustworthy before mutation. A settings-only
        # replacement can repair malformed input; plugin reconciliation refuses
        # it until that separate repair makes ownership observable again.
        initial_document = (
            {}
            if initial is None
            else parse_settings(initial, format=_format(tool), location=f"guest destination {destination!r}")
        )
        root_exists = _is_directory(files, root)
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
                    if market.source is not None and source == _simple_market_location(tool, market)
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
        ambiguous: dict[str, list[str]] = {}
        for selector in requested:
            matches = [
                item
                for item in available
                if item == selector or ("@" not in selector and item.split("@")[0] == selector)
            ]
            if not matches:
                raise ConfigError(
                    f"requested plugin {sanitize_setup_diagnostic(selector)!r} is absent in the native marketplaces"
                )
            if len(matches) != 1:
                ambiguous[selector] = matches
                continue
            selectors.append(matches[0])
        if len(selectors) != len(set(selectors)):
            raise ConfigError("multiple plugin names resolve to the same native installation")
        retained_plugins = {item for matches in ambiguous.values() for item in matches}
        retained_markets = {item.rsplit("@", 1)[1] for item in retained_plugins}
        desired_names = {market.name for market in desired} | retained_markets
        obsolete = tuple(
            claim
            for claim in native_claims
            if claim.identifier not in (set(selectors) | retained_plugins if claim.role == "plugin" else desired_names)
        )
        _check_contributions(tool, plan, tuple(desired), tuple(selectors), obsolete)

        def unsafe_settings(observed: tuple[NativeMarket, ...], document: SettingsObject) -> set[str]:
            marketplace_key, _ = _native_key(tool, "marketplace", "")
            declarations = _table(document, marketplace_key)
            return set(unreadable_marketplace_settings(tool, document)) | {
                market.name
                for market in observed
                if (market.settings_conflict and market.name in declarations)
                or (market.source is None and (market.name in desired_names or ("marketplace", market.name) in prior))
            }

        unsafe_markets = unsafe_settings(markets, initial_document)
        protected: set[tuple[str, str]] = set()
        blocked_markets: set[str] = set()
        blocked_plugins: set[str] = set()
        removed: list[NativeClaim] = []

        def checkpoint(claim: NativeClaim | None, role: str, identifier: str, *, force: bool = False) -> None:
            nonlocal claims
            old = next((item for item in claims if (item.role, item.identifier) == (role, identifier)), None)
            if old == claim and not force:
                return
            updated = [item for item in claims if (item.role, item.identifier) != (role, identifier)]
            if claim is not None:
                updated.append(claim)
            invocation.checkpoint((*preserved, *updated))
            claims = updated
            if force and claim is not None:
                # These calls follow validated native mutations, not adoption.
                # An initial absence cannot undo a registration just confirmed.
                protected.discard(_native_key(tool, role, identifier))

        def skip(role: str, identifier: str, reason: str) -> None:
            protected.add(_native_key(tool, role, identifier))
            invocation.report_incomplete(
                f"Retained {role} '{identifier}': {reason}. "
                "Review the listed native entries before reinitializing their owning setup."
            )
            if role == "marketplace":
                blocked_markets.add(identifier)
                protected.update(
                    _native_key(tool, "plugin", item.identifier)
                    for item in installed
                    if item.identifier.endswith("@" + identifier)
                )
            else:
                blocked_plugins.add(identifier)

        def can_mutate(role: str, identifier: str, operation: str) -> bool:
            # Native commands can rewrite their entire settings document. Do not
            # assume preservation of entries our comparator cannot recognize.
            current = files.read(destination)
            document = (
                {}
                if current is None
                else parse_settings(current, format=_format(tool), location=f"guest destination {destination!r}")
            )
            unavailable = unsafe_markets | unsafe_settings(cli.markets(), document)
            if unavailable:
                skip(
                    role,
                    identifier,
                    f"Agentworks cannot verify that '{tool} plugin {operation}' preserves settings entries "
                    + ", ".join(repr(name) for name in sorted(unavailable))
                    + "; review the native operation or explicitly map those entries before retrying",
                )
                return False
            return True

        def remove_plugin(claim: NativeClaim) -> bool:
            if claim.identifier.rpartition("@")[2] in blocked_markets:
                skip("plugin", claim.identifier, "its marketplace could not be retired or replaced safely")
                return False
            existing = by_plugin.get(claim.identifier)
            if existing is not None:
                if not _owned(claim, existing.source):
                    skip("plugin", claim.identifier, "the observed installation differs from its recorded ownership")
                    return False
                if not can_mutate("plugin", claim.identifier, "remove" if tool == "codex" else "uninstall"):
                    return False
                cli.uninstall(claim.identifier)
                observed = cli.plugins(cli.markets())
                if any(item.identifier == claim.identifier and item.scope == "user" for item in observed):
                    raise StateError("native plugin removal could not be confirmed")
                by_plugin.pop(claim.identifier, None)
                output.info(f"Removed native plugin '{claim.identifier}' via {tool}.")
            else:
                key, name = _native_key(tool, "plugin", claim.identifier)
                if name in _table(initial_document, key):
                    skip("plugin", claim.identifier, "its installation identity cannot be observed")
                    return False
            checkpoint(None, "plugin", claim.identifier)
            removed.append(claim)
            return True

        for selector, matches in ambiguous.items():
            invocation.report_incomplete(
                f"Skipped ambiguous plugin '{selector}'; select a qualified plugin@marketplace name."
            )
            protected.update(_native_key(tool, "plugin", item) for item in matches)
        for name in retained_markets:
            protected.add(_native_key(tool, "marketplace", name))

        # Claude's user-scoped removal requires the current user declaration,
        # even when list still reports a registration. Check before uninstalling
        # any dependencies; never fall back to removal across all scopes.
        for market in markets:
            if market.source is None and (market.name in desired_names or ("marketplace", market.name) in prior):
                skip("marketplace", market.name, market.unavailable_reason or "its native source cannot be observed")

        retiring_markets = {
            claim.identifier
            for claim in obsolete
            if claim.role == "marketplace"
            and claim.identifier in by_market
            and _owned(claim, by_market[claim.identifier].source)
        }
        replacing_markets = {
            market.name
            for market in desired
            if market.name in by_market
            and by_market[market.name].source != market.source
            and _owned(prior.get(("marketplace", market.name)), by_market[market.name].source)
        }
        for name in sorted(replacing_markets & retained_markets):
            skip("marketplace", name, "replacement would affect an ambiguous plugin selection")
        if tool == "claude":
            declarations = _table(initial_document, "extraKnownMarketplaces")
            for name in sorted((retiring_markets | replacing_markets) & by_market.keys()):
                declaration = declarations.get(name)
                if not isinstance(declaration, dict) or "source" not in declaration:
                    skip(
                        "marketplace",
                        name,
                        "user-scoped removal requires restoring its extraKnownMarketplaces user declaration first",
                    )

        if sources or requested:
            files.ensure_directory(root, mode=0o700)
        # Retirement is entry-local: preserve unsafe associations and their claims,
        # while unrelated removals and desired entries continue.
        for claim in obsolete:
            if claim.role == "plugin":
                remove_plugin(claim)
        for claim in obsolete:
            if claim.role != "marketplace":
                continue
            if claim.identifier in blocked_markets:
                continue
            existing = by_market.get(claim.identifier)
            if existing is not None:
                if not _owned(claim, existing.source):
                    skip("marketplace", claim.identifier, "the observed source differs from its recorded ownership")
                    continue
                if any(item.identifier.endswith("@" + claim.identifier) for item in cli.plugins(cli.markets())):
                    skip("marketplace", claim.identifier, "other user or project plugins still depend on it")
                    continue
                if not can_mutate("marketplace", claim.identifier, "marketplace remove"):
                    continue
                cli.remove_market(claim.identifier)
                if any(item.name == claim.identifier for item in cli.markets()):
                    raise StateError("native marketplace removal could not be confirmed")
                by_market.pop(claim.identifier, None)
                output.info(f"Removed native marketplace '{claim.identifier}' via {tool}.")
            checkpoint(None, "marketplace", claim.identifier)
            removed.append(claim)

        for market in desired:
            if market.name in blocked_markets:
                continue
            existing_market = by_market.get(market.name)
            if existing_market is not None and existing_market.source != market.source:
                if not _owned(prior.get(("marketplace", market.name)), existing_market.source):
                    skip(
                        "marketplace",
                        market.name,
                        "its full source cannot be observed"
                        if existing_market.source is None
                        else "the existing unowned source differs from the declaration",
                    )
                    continue
                dependents = tuple(
                    item for item in cli.plugins(cli.markets()) if item.identifier.endswith("@" + market.name)
                )
                if any(
                    item.scope != "user" or not _owned(prior.get(("plugin", item.identifier)), item.source)
                    for item in dependents
                ):
                    skip("marketplace", market.name, "replacement would affect an unowned or project plugin")
                    continue
                for plugin in dependents:
                    if not remove_plugin(prior[("plugin", plugin.identifier)]):
                        break
                else:
                    if not can_mutate("marketplace", market.name, "marketplace remove"):
                        continue
                    cli.remove_market(market.name)
                    if any(item.name == market.name for item in cli.markets()):
                        raise StateError("native marketplace removal could not be confirmed")
                    output.info(f"Removed native marketplace '{market.name}' via {tool}.")
                    checkpoint(None, "marketplace", market.name)
                    by_market.pop(market.name, None)
                    existing_market = None
                if market.name in by_market:
                    skip("marketplace", market.name, "a dependent plugin could not be retired")
                    continue
            if existing_market is None:
                if not can_mutate("marketplace", market.name, "marketplace add"):
                    continue
                cli.add_market(source_for[market.name])
                observed_market = next((item for item in cli.markets() if item.name == market.name), None)
                if observed_market is None or observed_market.source != market.source:
                    raise StateError("native marketplace registration identity could not be confirmed")
                by_market[market.name] = observed_market
                output.info(f"Registered native marketplace '{market.name}' via {tool}.")
            elif market.source is None:
                skip("marketplace", market.name, "its source cannot be observed")
                continue
            claim = NativeClaim(role="marketplace", identifier=market.name, destination=root, source=market.source)
            if existing_market is not None and prior.get(("marketplace", market.name)) != claim:
                output.info(f"Adopting matching native marketplace '{market.name}' via {tool}.")
            checkpoint(claim, "marketplace", market.name, force=existing_market is None)

        # Marketplace replacement may have removed an installation that existed in
        # the initial inventory. Observe the current native state before installing.
        markets = cli.markets()
        by_market = {item.name: item for item in markets}
        by_plugin = {item.identifier: item for item in cli.plugins(markets) if item.scope == "user"}
        for selector in selectors:
            parent = selector.rsplit("@", 1)[1]
            if parent in blocked_markets:
                skip("plugin", selector, "its requested marketplace could not converge")
                continue
            parent_market = by_market.get(parent)
            if parent_market is None or parent_market.source is None:
                skip("plugin", selector, "its marketplace source cannot be observed")
                continue
            existing_plugin = by_plugin.get(selector)
            previous_plugin_claim = prior.get(("plugin", selector))
            if (
                existing_plugin is not None
                and not _owned(previous_plugin_claim, existing_plugin.source)
                and (_plugin_market(existing_plugin) != parent_market.source or not existing_plugin.enabled)
            ):
                skip("plugin", selector, "the unowned installation differs from the declaration")
                continue
            changed = existing_plugin is None or not existing_plugin.enabled
            if existing_plugin is None or not existing_plugin.enabled:
                action = "Installed" if existing_plugin is None else "Re-enabled"
                if not can_mutate("plugin", selector, "add" if tool == "codex" else "install"):
                    continue
                cli.install(selector)
                existing_plugin = next(
                    (
                        item
                        for item in cli.plugins(cli.markets())
                        if item.identifier == selector and item.scope == "user"
                    ),
                    None,
                )
                if (
                    existing_plugin is None
                    or not existing_plugin.enabled
                    or existing_plugin.source is None
                    or _plugin_market(existing_plugin) != parent_market.source
                ):
                    raise StateError("native user plugin installation could not be confirmed")
                output.info(f"{action} native plugin '{selector}' via {tool}.")
            elif existing_plugin.source is None:
                skip("plugin", selector, "its installation source cannot be observed")
                continue
            adopted = NativeClaim(role="plugin", identifier=selector, destination=root, source=existing_plugin.source)
            if not _owned(previous_plugin_claim, existing_plugin.source) and selector in by_plugin:
                output.info(f"Adopting matching native plugin '{selector}' via {tool}.")
            checkpoint(adopted, "plugin", selector, force=changed)
        removed_keys = tuple(_native_key(tool, old.role, old.identifier) for old in removed)
        # Confirmed removals take precedence over an earlier inventory snapshot.
        protected.difference_update(removed_keys)
        if plan is not None:
            overlays: tuple[tuple[tuple[str, str], SettingsValue], ...] = tuple(
                (_native_key(tool, "marketplace", market.name), _market_fields(market))
                for market in desired
                if market.name not in blocked_markets
            ) + tuple(
                (_native_key(tool, "plugin", selector), True if tool == "claude" else {"enabled": True})
                for selector in selectors
                if selector not in blocked_plugins
            )
            if not joint_mapping:
                current = files.read(destination)
                plan.publish(
                    files,
                    destination,
                    current=current,
                    overlays=overlays,
                    removed=removed_keys,
                    protected=tuple(sorted(protected)),
                )
        else:
            overlays = ()
        if skip_mcp:
            return ArtifactApplication()
        assert mcp_destination is not None
        return apply_mcp(
            tool,
            invocation,
            destination=mcp_destination,
            claims=(*preserved, *claims),
            map_document=(
                lambda current: plan.render(
                    destination,
                    current=current,
                    overlays=overlays,
                    removed=removed_keys,
                    protected=tuple(sorted(protected)),
                )
            )
            if joint_mapping and plan is not None
            else None,
        )
