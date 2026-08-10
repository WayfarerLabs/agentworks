"""Settings values that NAME resource rows, and the one check that they
resolve.

A handful of settings are references: ``defaults.site`` names a ``vm-site``,
``[secret_config].backends`` names ``secret-source`` rows in precedence
order. They are settings, not resources, and are never published as
pseudo-resources (ADR 0016), so the framework's own reference machinery does
not see them: a ``ResourceReference`` is sourced from a declaring ROW, and
inventing a ``("config", "defaults")`` row to carry one would put a fake
resource into ``agw resource describe``'s "Referenced by:" and into the
``REFS`` / ``USED BY`` columns. What they share with a manifest reference is
not a type, it is the OBLIGATION: a name that resolves to nothing is a hard
error (operator ruling, 2026-08-07), with the same shape as the dangling
manifest reference at ``Registry._resolve_misses``.

So this module carries the obligation without the type. :data:`_SETTING_REF_SOURCES`
is the whole list of settings that name rows; adding a setting that names one
means adding a row there, and :func:`validate_setting_references` then covers
it with no new per-field check. That is deliberately the opposite of what it
replaced: one bespoke validator per subsystem
(``vms.validate_sites``, plus an ``active_sources`` call made purely for its
side effect inside ``secrets.validate_chain``), each with its own error
wording for the same operator mistake.

**Timing.** The registry does not exist at settings-load time, so a
reference cannot be checked there; the loaders validate SHAPE only
(``defaults.site`` is a non-empty string, ``backends`` is a list of strings).
Existence is checked after the resource graph is finalized, at the
composition boundary that holds both worlds
(``bootstrap.build_registry``). The Registry is deliberately NOT the place:
it is config-agnostic by construction, and handing it settings to finalize
against would invert the dependency for the sake of two fields.

**Presence, not availability.** A name resolves iff a row exists. A row that
exists but is DISABLED (an opt-out) or NOT-READY (unusable on this host)
resolves fine here; those are the subsystems' own answers at use time, and
failing config load over them would break every command on a host that
merely lacks an optional requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config.models import Config
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class _SettingRefSource:
    """One SETTING that names resource rows.

    ``setting`` is the operator-facing spelling, verbatim as it appears in
    ``config.toml`` (``defaults.site``, ``[secret_config].backends``), because
    it is what the error tells the operator to go and edit. ``kind`` is the
    resource kind the names must resolve in. ``read`` pulls the named values
    off a ``Config`` as a tuple, so a scalar setting and a list setting are
    one shape here (an unset scalar reads as ``()``).
    """

    setting: str
    kind: str
    read: Callable[[Config], tuple[str, ...]]


def _default_site(config: Config) -> tuple[str, ...]:
    """``defaults.site``, as zero or one name (unset means zero)."""
    site = config.defaults.site
    return () if site is None else (site,)


def _secret_chain(config: Config) -> tuple[str, ...]:
    """``[secret_config].backends``, the active-source precedence list."""
    return config.secret_config_data.backends


#: Every settings value that names a resource row. A new one is added here,
#: not as another bespoke validator.
#:
#: ``defaults.runup_git_credentials`` is deliberately NOT here: despite the
#: name it is a bool that switches the git-credential runup stage on and off,
#: not a name of anything.
_SETTING_REF_SOURCES: tuple[_SettingRefSource, ...] = (
    _SettingRefSource(setting="defaults.site", kind="vm-site", read=_default_site),
    _SettingRefSource(setting="[secret_config].backends", kind="secret-source", read=_secret_chain),
)


@dataclass(frozen=True)
class SettingReference:
    """One resolved-or-not reference: this setting names this row.

    A list-valued setting yields one of these per name, so every caller sees
    a flat sequence of (setting, kind, name) regardless of the setting's
    shape.
    """

    setting: str
    kind: str
    name: str


def setting_references(config: Config) -> tuple[SettingReference, ...]:
    """Every resource name this ``Config``'s settings currently carry.

    Public because "which settings name resources" is a question worth asking
    from outside the check itself, and because it is the seam a test uses to
    pin that :data:`_SETTING_REF_SOURCES` covers every such setting rather
    than merely the ones someone remembered to write a case for.
    """
    return tuple(
        SettingReference(setting=source.setting, kind=source.kind, name=name)
        for source in _SETTING_REF_SOURCES
        for name in source.read(config)
    )


def _dangling_hint(registry: Registry, ref: SettingReference) -> str:
    """The remediation for one dangling settings reference.

    Always enumerates what the kind DOES have, because for both settings that
    exist today the mistake is overwhelmingly a typo and the declared set is
    the whole fix. What follows differs by the kind's CATEGORY, on the reasoning
    already written at ``resources/inspect.py``'s edit path: a capability kind
    has no declarable form, so pointing at ``agw resource sample`` would send
    the operator to a command that errors.
    """
    from agentworks.resources import KIND_REGISTRY

    declared = sorted(name for name, _row in registry.iter_kind_items(ref.kind))
    hint = f"declared {ref.kind} resources: {declared}. Point {ref.setting} at one of them"
    handler = KIND_REGISTRY.get(ref.kind)
    if handler is not None and handler.category == "declarable":
        return f"{hint}, or declare {ref.name!r} (`agw resource sample {ref.kind} --write {ref.kind}s.yaml`)."
    return f"{hint}; {ref.kind} rows are provided by the app and its plugins, so there is none to declare."


def validate_setting_references(config: Config, registry: Registry) -> None:
    """Hard-error on any settings value naming a row the finalized registry
    does not have.

    Run by ``bootstrap.build_registry`` immediately after ``finalize``, and
    BEFORE the subsystems' semantic checks: a bogus name must be reported as
    the bogus name it is, not as the downstream consequence of one. (Left to
    run first, ``secrets.validate_chain`` would report a misspelled backend as
    an "unreachable secret", because a name that matches no edge simply drops
    out of its intersection.)

    The message deliberately matches the dangling-manifest-reference error at
    ``Registry._resolve_misses`` ("X references unknown KIND 'name'"), because
    it IS the same mistake; what differs is only that the referrer is a
    setting rather than a row, so the setting's own spelling stands where the
    row's ``kind/name`` would.
    """
    for ref in setting_references(config):
        try:
            registry.lookup(ref.kind, ref.name)
        except KeyError:
            if ref.kind == "secret-source":
                from agentworks.secrets.sources import direct_backend_source_error

                error = direct_backend_source_error(name=ref.name, registry=registry, referrer=ref)
                if error is not None:
                    raise error from None
            raise ConfigError(
                f"{ref.setting} references unknown {ref.kind} {ref.name!r}",
                hint=_dangling_hint(registry, ref),
            ) from None


__all__ = ["SettingReference", "setting_references", "validate_setting_references"]
