"""Top-level ``load_config`` entry point: reads the TOML file, pre-scans it
for section-header line numbers, and drives every settings-section loader
to compose a validated ``Config``.

config.toml is settings only now (ADR 0022): a resource-declaring section
is a hard error (``_raise_for_resource_sections``), gated on ``resources``
so the remediation commands can still read a config that carries them via
the ``resources=False`` escape hatch.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

import sys
import tomllib
from typing import TYPE_CHECKING

from agentworks.config.loaders_core import _load_defaults, _load_operator, _load_paths
from agentworks.config.loaders_secrets import (
    _load_plugins,
    _load_secret_backends,
    _load_secret_config,
)
from agentworks.config.loaders_sessions import _load_session_config
from agentworks.config.models import Config, _SectionLineMap
from agentworks.errors import ConfigError
from agentworks.source_location import scan_section_lines

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED_TOP_LEVEL_KEYS = {
    "operator",
    "paths",
    "defaults",
    "named_console",
    "vm_templates",
    "admin",
    "agent_templates",
    "session",
    "session_templates",
    "apt_sources",
    "apt_packages",
    "system_install_commands",
    "user_install_commands",
    "workspace_templates",
    "git_credentials",
    "azure",
    "proxmox",
    "secrets",
    "secret_backends",
    "secret_config",
    "plugins",
}


# Doubly-legacy singleton spellings: these were renamed to the
# ``[vm_templates.default]`` / ``[agent_templates.default]`` resource shape
# before this effort, and those shapes are themselves now resources (ADR 0022).
# The pointed rename error used to live in the vm/agent template loaders, which
# were deleted with the TOML resource surface; on a normal load these keys now
# fall through to the generic unexpected-key path, so give them a targeted hint
# at the modern destination (a YAML manifest) rather than the stale rename
# target.
_LEGACY_SINGLETON_HINTS = {
    "vm": "[vm.config] is a legacy spelling of the vm-template resource; declare it as a YAML "
    "manifest (`agw resource sample vm-template`).",
    "agent": "[agent.config] is a legacy spelling of the agent-template resource; declare it as a "
    "YAML manifest (`agw resource sample agent-template`).",
}


def _raise_unexpected_top_level_keys(data: dict[str, object]) -> None:
    """Reject unexpected top-level keys.

    This catches a common TOML pitfall: uncommenting a key without its section
    header causes the key to land in the wrong (or top-level) section. Known
    legacy singleton spellings retain their targeted migration errors instead
    of receiving the generic message.
    """
    unexpected = sorted(set(data.keys()) - EXPECTED_TOP_LEVEL_KEYS)
    if not unexpected:
        return
    messages = [_LEGACY_SINGLETON_HINTS[key] for key in unexpected if key in _LEGACY_SINGLETON_HINTS]
    generic = [key for key in unexpected if key not in _LEGACY_SINGLETON_HINTS]
    if generic:
        messages.append(f"unexpected top-level keys in config: {', '.join(generic)}")
    raise ConfigError(" ".join(messages))


def _raise_for_resource_sections(data: dict[str, object]) -> None:
    """Hard-error when config.toml declares resources (ADR 0022).

    config.toml is settings only now: the resource-declaration path moved to
    YAML manifests. This is the replacement for the old deprecation nudge
    (``_warn_deprecated_resource_sections``); it reuses the same shared
    ``KIND_SECTIONS`` presence sweep (minus ``secret_backends``, which has
    its own no-op warning) and the same grep-able display shapes.

    The rewrite is the operator's, so the error carries every part of it
    they need: which sections are the problem, which kind each becomes
    where the section name does not say, the two commands that print the
    target shape, and the guide section that walks it through. There is no
    tool to defer to (operator ruling, 2026-08-07).

    The escape hatch is ``load_config(resources=False)``: the commands that
    ARE the remediation (``resource sample --write``, ``resource edit``'s
    fallback) load that way and so still read a config that carries
    resource sections, which is what lets an operator author the
    replacement manifests before deleting the sections.
    """
    from agentworks.manifests.decode import KIND_SECTIONS

    present: list[str] = []
    for _kind, sections in KIND_SECTIONS.items():
        for section in sections:
            if section == "secret_backends" or section not in data:
                continue
            # The header shape operators can grep for: [admin.config],
            # [named_console], and the legacy vm-site sections ([azure] /
            # [proxmox]) are non-family sections; everything else nests
            # names ([secrets.<name>]).
            if section == "admin":
                present.append("[admin.config]")
            elif section in ("named_console", "azure", "proxmox"):
                present.append(f"[{section}]")
            else:
                present.append(f"[{section}.*]")
    if not present:
        return
    noun = "section" if len(present) == 1 else "sections"
    # The [azure]/[proxmox] sections become vm-site, a kind name nothing on
    # screen would suggest; name it only when one is present.
    site_hint = (
        " (the [azure]/[proxmox] sections become vm-site manifests)"
        if any(s in ("[azure]", "[proxmox]") for s in present)
        else ""
    )
    raise ConfigError(
        f"config.toml declares resources, which config.toml no longer supports "
        f"(it is settings only now): {', '.join(present)}. Rewrite the {noun} as "
        f"YAML manifests{site_hint}, then remove the {noun} from config.toml.",
        hint=(
            "`agw resource sample <kind> --write <kind>s.yaml` writes a commented starter to edit, "
            "and `agw resource describe-kind <kind>` lists every field with its type. "
            'The "TOML resource sections: removed" section of docs/guides/resources.md '
            "walks through it section by section."
        ),
    )


def load_config(
    path: Path | None = None,
    *,
    warn_issues: bool = True,
    warn_deprecations: bool = True,
    resources: bool = True,
) -> Config:
    """Load and validate the agentworks configuration.

    Args:
        path: Override config file path (default: ~/.config/agentworks/config.toml).
        warn_issues: Emit config issues as warnings to stderr (default: True).
            Set to False when the caller handles issues itself (e.g. doctor).
        warn_deprecations: Emit deprecation nudges (default: True; also
            silenceable per-invocation via --no-deprecations).
        resources: Enforce the resource-section hard error (default: True).
            config.toml is settings only now; a resource-declaring section is
            a hard error. The commands that ARE the remediation (``resource
            sample --write``, ``resource edit``'s fallback) pass False to read
            a config that still carries such sections. Settings load
            identically either way.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing, invalid, or declares resources
            (with ``resources=True``).
        SystemExit: If the config file does not exist.
    """
    # Re-imported here (rather than bound at module load) so that tests'
    # ``monkeypatch.setattr("agentworks.config.CONFIG_PATH", ...)``, which
    # patches the attribute on the public ``agentworks.config`` package, is
    # observed. A module-top `from ... import CONFIG_PATH` would instead
    # bind this module's own copy of the name at import time, permanently
    # deaf to a later monkeypatch.
    from agentworks.config import CONFIG_PATH

    config_path = path or CONFIG_PATH
    if not config_path.exists():
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        print("Run `agw config init` to create one from the commented sample.", file=sys.stderr)
        raise SystemExit(1)

    raw_text = config_path.read_text()
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: invalid config file {config_path}: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    # Pre-scan the raw text for section-header line numbers so we can attach
    # ``declared_at: SourceLocation`` to the settings singletons that carry
    # one. tomllib loses this info on parse; the scanner is a small regex
    # pre-pass.
    decls = _SectionLineMap(
        config_path=config_path,
        section_lines=scan_section_lines(raw_text),
    )

    issues: list[str] = []

    if "dotfiles" in data:
        raise ConfigError(
            "[dotfiles] section has been removed. Move dotfiles settings into "
            "[admin.config] (dotfiles_source, dotfiles_destination, dotfiles_install_cmd)."
        )

    _raise_unexpected_top_level_keys(data)

    # config.toml is settings only: reject resource-declaring sections before
    # the settings loaders run, unless the caller is the remediation itself
    # (resources=False).
    if resources:
        _raise_for_resource_sections(data)

    session_config = _load_session_config(data, issues)

    deprecations: list[str] = []
    noop_backend_sections = _load_secret_backends(data, deprecations)
    secret_config_data = _load_secret_config(data, issues, decls)
    enabled_system_plugins = _load_plugins(data, issues, decls)

    config = Config(
        operator=_load_operator(data, issues),
        paths=_load_paths(data),
        defaults=_load_defaults(data),
        source_path=config_path,
        session=session_config,
        secret_config_data=secret_config_data,
        enabled_system_plugins=enabled_system_plugins,
        config_issues=tuple(issues),
        deprecation_issues=tuple(deprecations),
        noop_secret_backend_sections=noop_backend_sections,
    )

    if warn_issues and config.config_issues:
        from agentworks.output import warn

        for issue in config.config_issues:
            warn(f"Config: {issue}")
    if warn_issues and warn_deprecations and config.deprecation_issues:
        from agentworks.output import deprecations_suppressed, warn

        if not deprecations_suppressed():
            for issue in config.deprecation_issues:
                warn(f"Config: {issue}")

    return config
