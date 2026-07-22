"""Secrets-related loaders: ``[secrets.*]`` declarations, the deprecated
``[secret_backends.*]`` no-op sections, the aggregated deprecated-TOML-
resource-section warning, and ``[secret_config]``.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from agentworks.config.loaders_core import _warn_unexpected_keys
from agentworks.config.validation import validate_name
from agentworks.errors import ConfigError
from agentworks.secrets import SecretConfig, SecretDecl

if TYPE_CHECKING:
    from agentworks.config.models import _SectionLineMap


def _load_secrets(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> dict[str, SecretDecl]:
    """Load [secrets.*] declarations into SecretDecls keyed by name."""
    raw = data.get("secrets", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secrets] must be a table")

    expected = {"description", "hint", "backend_mappings"}
    secret_decls: dict[str, SecretDecl] = {}
    for name, sdata in raw.items():
        name_str = str(name)
        if not isinstance(sdata, dict):
            raise ConfigError(f"secrets.{name_str} must be a table")
        validate_name(name_str)
        _warn_unexpected_keys(sdata, expected, f"secrets.{name_str}", issues)

        description = sdata.get("description")
        if not isinstance(description, str) or not description:
            raise ConfigError(f"secrets.{name_str}.description is required and must be a non-empty string")
        hint = sdata.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise ConfigError(f"secrets.{name_str}.hint must be a string")

        raw_mappings = sdata.get("backend_mappings", {})
        if not isinstance(raw_mappings, dict):
            raise ConfigError(f"secrets.{name_str}.backend_mappings must be a table")
        backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = {}
        for kind, mapping in raw_mappings.items():
            kind_str = str(kind)
            if isinstance(mapping, bool):
                if mapping is True:
                    raise ConfigError(
                        f"secrets.{name_str}.backend_mappings.{kind_str}: "
                        "boolean must be `false` (opt-out); `true` is not a valid value"
                    )
                backend_mappings[kind_str] = False
            elif isinstance(mapping, str):
                backend_mappings[kind_str] = mapping
            elif isinstance(mapping, dict):
                backend_mappings[kind_str] = dict(mapping)
            else:
                raise ConfigError(
                    f"secrets.{name_str}.backend_mappings.{kind_str}: must be a string, inline table, or false"
                )

        secret_decls[name_str] = SecretDecl(
            name=name_str,
            description=description,
            hint=hint,
            backend_mappings=backend_mappings,
            declared_at=decls.lookup("secrets", name_str),
        )
    return secret_decls


def _load_secret_backends(
    data: dict[str, object],
    deprecations: list[str],
) -> tuple[str, ...]:
    """Warn ``[secret_backends.*]`` sections as deprecated no-ops.

    The backend-keyed TOML sections never carried configuration (only
    the backend name itself), and the backends are registered code
    capabilities -- so a section here is semantically empty. Known
    backends warn as deprecated; unknown ones (typo ``envvar`` for
    ``env-var``) stay a hard ``ConfigError`` for typo protection.
    Nothing is stored and nothing publishes.

    Returns the display shapes of the sections found (facts for
    surfaces that render their own tidy rows, mirroring
    ``_warn_deprecated_resource_sections``).
    """
    raw = data.get("secret_backends", {})
    if not isinstance(raw, dict):
        raise ConfigError("[secret_backends] must be a table")

    from agentworks.secrets.backends import SECRET_BACKEND_REGISTRY

    known_backends = set(SECRET_BACKEND_REGISTRY)
    found: list[str] = []
    for key, bdata in raw.items():
        backend_str = str(key)
        if not isinstance(bdata, dict):
            raise ConfigError(f"secret_backends.{backend_str} must be a table")
        if backend_str not in known_backends:
            raise ConfigError(
                f"[secret_backends.{backend_str}] names an unknown secret backend; supported: {sorted(known_backends)}"
            )
        found.append(f"[secret_backends.{backend_str}]")
        deprecations.append(
            f"[secret_backends.{backend_str}] is deprecated and has no effect: "
            f"the built-in backends ship with agentworks, and activation is "
            f"[secret_config].backends. Remove the section, or run "
            f"`agw resource migrate --all` to drop it."
        )
    return tuple(found)


def _warn_deprecated_resource_sections(
    data: dict[str, object],
    deprecations: list[str],
) -> tuple[str, ...]:
    """ONE aggregated deprecation issue for the TOML resource sections
    present (aggregated at maintainer direction; a warning
    per section was obnoxious on real configs).

    Dual-path is permanent policy short of a future major release: these
    sections keep loading with exactly today's semantics. The warning is
    the nudge toward the YAML manifest surface. ``[secret_backends.*]``
    is excluded -- it has its own no-op message above -- and
    ``[secret_config]`` is config, not a resource section.

    Returns the display shapes of the sections found, so surfaces with
    their own rendering (doctor's tidy one-line row) can compose from
    the fact instead of reusing this ambient teaching text.
    """
    from agentworks.manifests.decode import KIND_SECTIONS

    present: list[str] = []
    for _kind, sections in KIND_SECTIONS.items():
        for section in sections:
            if section == "secret_backends" or section not in data:
                continue
            # Display the header shape operators can actually grep for:
            # [admin.config], [named_console], and the legacy vm-site
            # sections ([azure] / [proxmox]) are non-family sections;
            # everything else nests names ([secrets.<name>]).
            if section == "admin":
                present.append("[admin.config]")
            elif section in ("named_console", "azure", "proxmox"):
                present.append(f"[{section}]")
            else:
                present.append(f"[{section}.*]")
    if not present:
        return ()
    noun = "section" if len(present) == 1 else "sections"
    # Selectors are KIND names, not section names: [azure]/[proxmox]
    # migrate as `vm-site`, which nothing on screen would suggest.
    site_hint = (
        " (the [azure]/[proxmox] sections migrate as `vm-site`)"
        if any(s in ("[azure]", "[proxmox]") for s in present)
        else ""
    )
    deprecations.append(
        f"deprecated TOML resource {noun}: {', '.join(present)}. Move "
        f"these with `agw resource migrate <kind>` or `--all`{site_hint}, "
        f"declare new resources as YAML manifests "
        f"(`agw resource sample <kind>`), or silence this warning with "
        f"--no-deprecations. TOML resource support will likely be removed "
        f"in a future major release."
    )
    return tuple(present)


def _load_secret_config(
    data: dict[str, object],
    issues: list[str],
    decls: _SectionLineMap,
) -> SecretConfig:
    """Load [secret_config] with the enabled-backends precedence list.

    Absence of the [secret_config] table OR absence of the ``backends`` key
    within it falls back to ``SecretConfig()``'s default chain
    (``DEFAULT_BACKEND_CHAIN``). An explicit ``backends = []`` is respected
    as "no backends" (operator opts out of resolution entirely).
    """
    declared_at = decls.lookup("secret_config")
    if "secret_config" not in data:
        return SecretConfig(declared_at=declared_at)
    raw = data["secret_config"]
    if not isinstance(raw, dict):
        raise ConfigError("[secret_config] must be a table")
    _warn_unexpected_keys(raw, {"backends"}, "secret_config", issues)
    if "backends" not in raw:
        return SecretConfig(declared_at=declared_at)
    backends_raw = raw["backends"]
    if not isinstance(backends_raw, list) or not all(isinstance(b, str) for b in backends_raw):
        raise ConfigError("[secret_config].backends must be a list of strings")
    return SecretConfig(backends=tuple(backends_raw), declared_at=declared_at)


# Secret resolution lives in ``agentworks.secrets.resolve`` (ADR 0016):
# the chain can name manifest-declared backends, which are unknowable at
# config-load time, so the chain-name and unreachable-secret checks run
# at the composition boundary instead of here.
