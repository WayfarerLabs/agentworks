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
from agentworks.config.loaders_secrets import _load_plugins, _load_secret_config
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
    (``_warn_deprecated_resource_sections``); it reuses the shared
    ``KIND_SECTIONS`` presence sweep and the same grep-able display shapes.

    The rewrite is the operator's, so the error carries every part of it
    they need: which sections are the problem, which kind each becomes
    where the section name does not say, the two commands that print the
    target shape, and the guide section that walks it through. There is no
    tool to defer to (operator ruling, 2026-08-07).

    ``[secret_backends.*]`` is swept here like every other section (it IS a
    resource-declaring section: it is in ``KIND_SECTIONS``, keyed by the
    ``secret-backend`` kind), but it is the one section whose remediation is
    not "rewrite it as a manifest". It never carried configuration, only the
    backend NAME, so there is nothing to move and no manifest to write:
    delete it, and activate the backend in ``[secret_config].backends``
    instead. Sending an operator to write a ``secret-backend`` manifest would
    send them to a command that errors, since ``secret-backend`` is a
    capability kind with no declarable form. Hence the two clauses below
    rather than one message: a config carrying both kinds of section still
    gets the whole job in one read.

    The escape hatch is ``load_config(resources=False)``: the commands that
    ARE the remediation (``resource sample --write``, ``resource edit``'s
    fallback) load that way and so still read a config that carries
    resource sections, which is what lets an operator author the
    replacement manifests before deleting the sections. Folding
    ``[secret_backends.*]`` into this sweep is what finally puts it behind
    that hatch too: it used to be refused by the settings load, which no
    escape hatch covers, so a section carrying no configuration could break
    the very commands the rewrite depends on.

    RELEASE-SCOPED, and paired with its guide. This check exists only to
    carry hosts across the 0.14 TOML sunset, so it retires on the same
    schedule as ``docs/guides/upgrading-to-0.14.md``, which its hint names.
    Delete the two together, or the error outlives the document it sends
    the operator to. The retirement is this function, the ``resources=``
    escape hatch it is gated by, and the retired section names in
    ``EXPECTED_TOP_LEVEL_KEYS`` (they sit in that set only so this error
    fires instead of the generic unexpected-key one; leaving them there
    would make ``[secrets.*]`` load silently again).
    """
    from agentworks.manifests.decode import KIND_SECTIONS

    present: list[str] = []
    for _kind, sections in KIND_SECTIONS.items():
        for section in sections:
            if section not in data:
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
    rewritable = [s for s in present if s != "[secret_backends.*]"]

    message = (
        f"config.toml declares resources, which config.toml no longer supports "
        f"(it is settings only now): {', '.join(present)}."
    )
    hint_parts: list[str] = []
    if rewritable:
        rewrite_noun = "section" if len(rewritable) == 1 else "sections"
        # The [azure]/[proxmox] sections become vm-site, a kind name nothing
        # on screen would suggest; name it only when one is present.
        site_hint = (
            " (the [azure]/[proxmox] sections become vm-site manifests)"
            if any(s in ("[azure]", "[proxmox]") for s in rewritable)
            else ""
        )
        message += (
            f" Rewrite {', '.join(rewritable)} as YAML manifests{site_hint}, "
            f"then remove the {rewrite_noun} from config.toml."
        )
        hint_parts.append(
            "`agw resource sample <kind> --write <kind>s.yaml` writes a commented starter to edit, "
            "and `agw resource describe-kind <kind>` lists every field with its type."
        )
    if "[secret_backends.*]" in present:
        message += " [secret_backends.*] carries no configuration, so there is nothing to rewrite: delete it."
        hint_parts.append(
            "If you meant to ACTIVATE that backend, list its name in [secret_config].backends, "
            "which is a setting and stays in config.toml."
        )
    hint_parts.append(
        'The "TOML resource sections: removed" section of docs/guides/upgrading-to-0.14.md '
        f"walks through the {noun} one by one."
    )
    raise ConfigError(message, hint=" ".join(hint_parts))


def load_config(
    path: Path | None = None,
    *,
    warn_issues: bool = True,
    warn_deprecations: bool = True,
    resources: bool = True,
    raise_errors: bool = False,
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
        raise_errors: Raise typed errors for early file failures instead of
            using the legacy stderr and ``SystemExit`` path.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the config is missing, invalid, or declares resources
            (with ``resources=True``).
        SystemExit: If an early file failure occurs and ``raise_errors`` is false.
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
        if raise_errors:
            raise ConfigError(
                f"configuration file not found: {config_path}",
                hint="Create it to get started. See the documentation for the schema.",
            )
        print(f"Configuration file not found: {config_path}", file=sys.stderr)
        print("Run `agw config init` to create one from the commented sample.", file=sys.stderr)
        raise SystemExit(1)

    try:
        raw_text = config_path.read_text()
    except (OSError, UnicodeError) as error:
        if raise_errors:
            raise ConfigError(f"cannot read configuration file {config_path}: {error}") from None
        raise
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        if raise_errors:
            raise ConfigError(f"invalid config file {config_path}: {e}") from None
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

    # No settings loader produces a deprecation today (the last producer,
    # the ``[secret_backends.*]`` no-op nudge, became part of the
    # resource-section hard error above). The channel stays wired because it
    # is generic machinery with an operator-facing flag (--no-deprecations),
    # not because anything currently rides it.
    deprecations: list[str] = []
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
