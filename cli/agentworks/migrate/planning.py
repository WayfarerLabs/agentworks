"""Plan a migration run: selectors, emission, layout, and the TOML edit.

Planning is pure: it reads the config file text and produces a
``MigrationPlan`` carrying everything ``execute_plan`` needs (rendered
YAML documents grouped by target file, the rewritten TOML text, and the
normalized pre-migration registry rows for verification). ``--dry-run``
is therefore just "plan and print".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit
import yaml
from tomlkit import items as toml_items

from agentworks.errors import ConfigError, ValidationError
from agentworks.manifests.decode import KIND_SECTIONS
from agentworks.manifests.loader import RESOURCES_DIRNAME
from agentworks.migrate.toml_edit import apply_toml_edits, key_name
from agentworks.migrate.verify import normalized_rows

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry


# Kinds that exist in TOML as one singleton section rather than a named
# family; they emit as <kind>/default per the envelope restriction.
_SINGLETON_KINDS = {"admin-template", "named-console-template"}

# secret-backend is a capability kind (not declarable); its
# TOML sections are warned no-ops with no manifest successor -- they are
# dropped, never migrated. vm-site is the one multi-section kind: its
# legacy sections ([azure] / [proxmox]) are FLAT, the section name IS
# the resource name, and emission nests the platform-owned keys under
# spec.platform_config. Every remaining kind maps to exactly one
# section, with the section's inner tables as the named resources.
_MIGRATABLE_KINDS = {k for k in KIND_SECTIONS if k != "secret-backend"}

# section -> kind, covering vm-site's two legacy sections.
_SECTION_KINDS = {
    section: kind for kind, sections in KIND_SECTIONS.items() if kind != "secret-backend" for section in sections
}

# Kinds whose whole top-level section is the unit (rather than a family
# of inner tables): the true singletons plus vm-site's flat sections.
_WHOLE_SECTION_KINDS = _SINGLETON_KINDS | {"vm-site"}

_SECRET_BACKENDS_SECTION = "secret_backends"

# Conservative filename-safe set for the per-resource layout. '/' is
# already banned at Registry.add, but non-secret names are otherwise
# pass-through (spaces, backslashes, leading dots can appear); unsafe
# names are refused (not sanitized) with a pointer at per-kind.
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class MigrationUnit:
    """One resource moving from TOML to YAML."""

    kind: str
    name: str  # "default" for singleton kinds
    section: str


@dataclass
class FileWrite:
    """One target manifest file and the documents headed into it."""

    path: Path
    documents: list[str]
    exists: bool  # target existed at plan time -> append


@dataclass
class MigrationPlan:
    """Everything a run needs; produced by ``plan_migration``."""

    config_path: Path
    resources_dir: Path
    units: list[MigrationUnit]
    writes: list[FileWrite]
    toml_mode: str  # validated: "comment" | "delete"
    old_toml_text: str
    new_toml_text: str
    drops_secret_backends: bool
    # (kind, name) -> target path relative to the config dir (e.g.
    # "resources/vm-templates.yaml"); feeds the preview and the
    # "migrated to" markers.
    targets: dict[tuple[str, str], str] = field(default_factory=dict)
    # Normalized pre-migration registry rows, keyed by (kind, name);
    # ``execute_plan`` compares the post-migration rebuild against this.
    pre_rows: dict[tuple[str, str], Any] = field(repr=False, default_factory=dict)

    @property
    def nothing_to_do(self) -> bool:
        return not self.units and not self.drops_secret_backends


def plan_migration(
    config: Config,
    registry: Registry,
    selectors: list[str],
    *,
    all_resources: bool = False,
    layout: str = "per-kind",
    toml_mode: str = "comment",
) -> MigrationPlan:
    """Resolve selectors against the config's TOML and build the plan.

    Migrating everything requires the explicit ``all_resources`` opt-in
    (``--all``); an empty selection without it is an error, so a bare
    ``agw resource migrate`` can never rewrite the whole config by
    accident (maintainer ruling, 2026-07-05).

    Raises ``ValidationError`` for selector errors (unknown kind,
    explicit selector matching nothing) and ``ConfigError`` for TOML
    shapes the tool refuses (dotted-key / inline-table declarations,
    filename-unsafe names under the per-resource layout).
    """
    if all_resources and selectors:
        raise ValidationError(
            "pass selectors or --all, not both",
            hint="Selectors scope the run; --all migrates everything.",
        )
    if not all_resources and not selectors:
        raise ValidationError(
            "indicate resources to migrate, or pass --all",
            hint=(
                "Examples: `agw resource migrate secret`, "
                "`agw resource migrate vm-template/dev`, "
                "`agw resource migrate --all`."
            ),
        )
    if layout not in ("per-kind", "single", "per-resource"):
        raise ValidationError(
            f"unknown layout {layout!r}",
            hint="Choose per-kind (default), single, or per-resource.",
        )
    if toml_mode not in ("comment", "delete"):
        raise ValidationError(
            f"unknown --toml mode {toml_mode!r}",
            hint="Choose comment (default) or delete.",
        )
    config_path = config.source_path
    old_text = config_path.read_text(encoding="utf-8")
    doc = tomlkit.parse(old_text)

    available = _discover_units(doc)
    selected = _resolve_selectors(selectors, available)
    _check_declaration_shapes(doc, selected, registry, old_text, config_path)

    resources_dir = config_path.parent / RESOURCES_DIRNAME
    targets = _targets(selected, layout)
    writes = _build_writes(doc, selected, layout, resources_dir)

    drops = any(key is not None and key_name(key) == _SECRET_BACKENDS_SECTION for key, _item in doc.body)
    markers = {(u.section, u.name): targets[(u.kind, u.name)] for u in selected}
    # vm-site sections rewrite whole (like singletons); the editor's
    # singleton path looks markers up under the "default" name.
    for u in selected:
        if u.kind == "vm-site":
            markers[(u.section, "default")] = targets[(u.kind, u.name)]
    if selected or drops:
        new_text = apply_toml_edits(
            old_text,
            units={(u.section, u.name) for u in selected},
            singleton_sections={u.section for u in selected if u.kind in _WHOLE_SECTION_KINDS},
            mode=toml_mode,
            markers=markers,
            drop_sections={_SECRET_BACKENDS_SECTION} if drops else set(),
        )
    else:
        new_text = old_text

    return MigrationPlan(
        config_path=config_path,
        resources_dir=resources_dir,
        units=selected,
        writes=writes,
        toml_mode=toml_mode,
        old_toml_text=old_text,
        new_toml_text=new_text,
        drops_secret_backends=drops,
        targets=targets,
        pre_rows=normalized_rows(registry),
    )


def _discover_units(doc: tomlkit.TOMLDocument) -> list[MigrationUnit]:
    """Every TOML-declared resource, in declaration order."""
    units: list[MigrationUnit] = []
    seen: set[tuple[str, str]] = set()
    section_kinds = _SECTION_KINDS
    for key, item in doc.body:
        if key is None:
            continue
        section = key_name(key)
        kind = section_kinds.get(section)
        if kind is None:
            continue
        if kind in _SINGLETON_KINDS:
            unit = MigrationUnit(kind=kind, name="default", section=section)
            if (section, "default") not in seen:
                seen.add((section, "default"))
                units.append(unit)
            continue
        if kind == "vm-site":
            # Flat legacy sections: the section name IS the resource name.
            if (section, section) not in seen:
                seen.add((section, section))
                units.append(MigrationUnit(kind=kind, name=section, section=section))
            continue
        if not isinstance(item, toml_items.Table):
            # A top-level assignment shape (`secrets = { npm-token = ... }`).
            # Its children are still discoverable, so a bare run reaches
            # them and the shape check refuses loudly -- silently skipping
            # would report a "complete" migration that left rows behind.
            child_names = _mapping_child_names(item)
            for name in child_names:
                if (section, name) not in seen:
                    seen.add((section, name))
                    units.append(MigrationUnit(kind=kind, name=name, section=section))
            continue
        for inner_key, _inner in item.value.body:
            if inner_key is None:
                continue
            name = key_name(inner_key)
            if (section, name) not in seen:
                seen.add((section, name))
                units.append(MigrationUnit(kind=kind, name=name, section=section))
    return units


def _mapping_child_names(item: toml_items.Item) -> list[str]:
    try:
        value = item.unwrap()
    except AttributeError:
        return []
    if isinstance(value, dict):
        return [str(k) for k in value]
    return []


def _resolve_selectors(selectors: list[str], available: list[MigrationUnit]) -> list[MigrationUnit]:
    if not selectors:
        return list(available)

    by_kind: dict[str, list[MigrationUnit]] = {}
    by_key: dict[tuple[str, str], MigrationUnit] = {}
    for unit in available:
        by_kind.setdefault(unit.kind, []).append(unit)
        by_key[(unit.kind, unit.name)] = unit

    picked: dict[tuple[str, str], MigrationUnit] = {}
    for raw in selectors:
        kind, _, name = raw.partition("/")
        if kind == "secret-backend":
            raise ValidationError(
                "secret-backend TOML sections are deprecated no-ops with no "
                "manifest successor; there is nothing to migrate.",
                hint=("Run `agw resource migrate --all` to drop the [secret_backends.*] sections from config.toml."),
            )
        if kind not in _MIGRATABLE_KINDS:
            known = ", ".join(sorted(_MIGRATABLE_KINDS))
            raise ValidationError(
                f"unknown kind in selector {raw!r}",
                hint=f"migratable kinds: {known}",
            )
        if name:
            wanted = by_key.get((kind, name))
            if wanted is None:
                raise ValidationError(
                    f"no TOML-declared {kind} named {name!r}",
                    hint=(
                        "The resource may already be YAML-declared or "
                        "auto-declared; only TOML-declared resources migrate. "
                        "See `agw resource list`."
                    ),
                )
            picked[(wanted.kind, wanted.name)] = wanted
        else:
            matches = by_kind.get(kind, [])
            if not matches:
                raise ValidationError(
                    f"no TOML-declared resources of kind {kind!r}",
                    hint=("They may already be YAML-declared or auto-declared; only TOML-declared resources migrate."),
                )
            for unit in matches:
                picked[(unit.kind, unit.name)] = unit

    # Preserve declaration order regardless of selector order.
    return [u for u in available if (u.kind, u.name) in picked]


def _check_declaration_shapes(
    doc: tomlkit.TOMLDocument,
    selected: list[MigrationUnit],
    registry: Registry,
    old_text: str,
    config_path: Path,
) -> None:
    """Refuse dotted-key / inline-table declarations for selected units.

    "Commented out in place" has no faithful rendering for a key buried
    in a shared table; the operator migrates those by hand. Errors carry
    the declaration's file:line (from the registry row where one exists,
    else a text scan for the section).
    """
    wanted: dict[str, set[str]] = {}
    singleton_sections: dict[str, MigrationUnit] = {}
    for unit in selected:
        if unit.kind in _WHOLE_SECTION_KINDS:
            # Whole-section units (true singletons and vm-site's flat
            # sections): the shape requirement is section-is-a-table.
            singleton_sections[unit.section] = unit
        else:
            wanted.setdefault(unit.section, set()).add(unit.name)
    for key, item in doc.body:
        if key is None:
            continue
        section = key_name(key)
        if section in singleton_sections and not isinstance(item, toml_items.Table):
            where = _section_location(old_text, config_path, section)
            raise ConfigError(
                f"{where}: [{section}] is not declared as standard TOML tables; the migrate tool cannot rewrite it",
                hint="Migrate this section by hand (dotted-key/inline shapes).",
            )
        if section not in wanted:
            continue
        if not isinstance(item, toml_items.Table):
            where = _section_location(old_text, config_path, section)
            raise ConfigError(
                f"{where}: [{section}] is not declared as standard TOML tables; the migrate tool cannot rewrite it",
                hint="Migrate this section by hand (dotted-key/inline shapes).",
            )
        for inner_key, inner in item.value.body:
            if inner_key is None or key_name(inner_key) not in wanted[section]:
                continue
            if not isinstance(inner, toml_items.Table):
                child = f"{section}.{key_name(inner_key)}"
                unit = next(u for u in selected if u.section == section and u.name == key_name(inner_key))
                where = _declared_at(registry, unit) or _section_location(old_text, config_path, section)
                raise ConfigError(
                    f"{where}: [{child}] is declared as a dotted key or "
                    f"inline table; the migrate tool only rewrites standard "
                    f"[{child}] header tables",
                    hint="Migrate this resource by hand.",
                )


def _declared_at(registry: Registry, unit: MigrationUnit) -> str | None:
    try:
        resource = registry.lookup(unit.kind, unit.name)
    except Exception:  # noqa: BLE001 - location is best-effort decoration
        return None
    location = getattr(resource, "declared_at", None)
    if location is None or not getattr(location, "line", 0):
        return None
    return f"{location.file}:{location.line}"


def _section_location(old_text: str, config_path: Path, section: str) -> str:
    """Best-effort file:line of a section's first appearance.

    Headers may be indented; assignment patterns are anchored to column
    zero, since a top-level assignment cannot be indented -- otherwise a
    same-named key inside an earlier table (``secrets = [...]`` under a
    template) would match first.
    """
    for number, line in enumerate(old_text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith((f"[{section}]", f"[{section}.")) or line.startswith((f"{section} =", f"{section}=")):
            return f"{config_path}:{number}"
    return str(config_path)


def _targets(selected: list[MigrationUnit], layout: str) -> dict[tuple[str, str], str]:
    """Per-unit target paths relative to the config dir."""
    return {(u.kind, u.name): f"{RESOURCES_DIRNAME}/{_relative_target(u, layout).as_posix()}" for u in selected}


def _build_writes(
    doc: tomlkit.TOMLDocument,
    selected: list[MigrationUnit],
    layout: str,
    resources_dir: Path,
) -> list[FileWrite]:
    writes: dict[Path, FileWrite] = {}
    for unit in selected:
        target = resources_dir / _relative_target(unit, layout)
        write = writes.get(target)
        if write is None:
            write = FileWrite(path=target, documents=[], exists=target.exists())
            writes[target] = write
        write.documents.append(_emit_document(doc, unit))
    return list(writes.values())


def _relative_target(unit: MigrationUnit, layout: str) -> Path:
    if layout == "single":
        return Path("resources.yaml")
    if layout == "per-kind":
        return Path(f"{unit.kind}s.yaml")
    if not _SAFE_FILENAME.fullmatch(unit.name):
        raise ConfigError(
            f"{unit.kind}/{unit.name}: name is not filename-safe for the per-resource layout",
            hint="Use --layout per-kind for this resource.",
        )
    return Path(unit.kind) / f"{unit.name}.yaml"


def _emit_document(doc: tomlkit.TOMLDocument, unit: MigrationUnit) -> str:
    """Render one unit as a YAML manifest document."""
    spec = _spec_data(doc, unit)
    metadata: dict[str, Any] = {"name": unit.name}

    # Description moves to metadata BEFORE any kind-specific rebuild --
    # the git-credential branch below sweeps "everything left" into
    # provider_config, and description is kind-owned, not provider-owned.
    # Every declarable kind carries a description field now, so the only
    # exclusion is vm-site: its flat legacy sections never supported the
    # key (the TOML loader silently drops it), so popping it here would
    # smuggle a description past the pre-write stray-key refusal and
    # into a manifest the pre-rows can't match; verification would
    # fail AFTER writing. Left in place, it falls into platform_config
    # and hits the clean pre-write refusal below.
    if unit.kind != "vm-site" and "description" in spec:
        metadata["description"] = spec.pop("description")

    if unit.kind == "vm-site":
        # Flat legacy [azure] / [proxmox] sections nest under
        # spec.platform_config; the section name becomes the resource
        # name, and the platform comes from the legacy loader's own
        # mapping (one source of truth: the [azure] section's
        # platform is azure-vm, so the emitted manifest must match
        # what the loader publishes or verification fails). Validate
        # the blob pre-write in the operator's TOML vocabulary,
        # mirroring the git-credential branch: an unvalidated emission
        # would only fail the post-run verification after files were
        # written.
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
        from agentworks.config import _LEGACY_SITE_SECTIONS

        platform = _LEGACY_SITE_SECTIONS[unit.section][0]
        rebuilt_site: dict[str, Any] = {"platform": platform}
        if spec:
            rebuilt_site["platform_config"] = dict(spec)
        platform_cls = VM_PLATFORM_REGISTRY.get(platform)
        if platform_cls is not None and "platform_config" in rebuilt_site:
            try:
                platform_cls.validate_config(f"[{unit.section}]", rebuilt_site["platform_config"])
            except ConfigError as exc:
                raise ConfigError(
                    f"cannot migrate vm-site/{unit.name}: {exc}",
                    hint=(
                        "The flat TOML section carries key(s) its platform "
                        "does not accept (silently ignored by the TOML "
                        "loader). Remove them from config.toml, then re-run."
                    ),
                ) from exc
        spec = rebuilt_site

    if unit.kind == "git-credential":
        # TOML accepts type (legacy) or provider (alias); the manifest
        # surface only ever has spec.provider. Pop BOTH before
        # rebuilding so the precedence (provider wins, matching the TOML
        # loader) is explicit rather than an artifact of dict-literal
        # ordering. Kind-owned token stays top-level; everything else
        # (azdo's org) is provider-owned and nests under
        # provider_config -- the YAML shape diverges from flat TOML by
        # design, and the post-run registry-equivalence verification
        # proves the divergence is shape-only.
        legacy = spec.pop("type", None)
        provider = spec.pop("provider", None) or legacy
        # token is provider config now: it nests with everything else
        # provider-owned (org, ...) under provider_config, no longer a
        # top-level field.
        rebuilt: dict[str, Any] = {"provider": provider}
        if spec:
            rebuilt["provider_config"] = dict(spec)
        # The sweep above nests EVERYTHING the flat section carried
        # beyond the kind-owned fields -- including stray keys the TOML
        # loader silently ignores. The manifest loader validates blobs
        # strictly, so an unvalidated emission would fail the post-run
        # verification AFTER writing (rollback fires and the error cites
        # a rolled-back file). Validate here instead: fail before
        # anything is written, in the operator's TOML vocabulary.
        from agentworks.capabilities.git_credential import (
            GIT_CREDENTIAL_PROVIDER_REGISTRY,
        )

        capability = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(str(provider))
        if capability is not None and "provider_config" in rebuilt:
            try:
                capability.validate_config(f"git-credential/{unit.name}", rebuilt["provider_config"])
            except ConfigError as exc:
                raise ConfigError(
                    f"cannot migrate git-credential/{unit.name}: {exc}",
                    hint=(
                        "The flat TOML section carries key(s) its provider "
                        "does not accept (silently ignored by the TOML "
                        "loader). Remove them from config.toml, then re-run."
                    ),
                ) from exc
        spec = rebuilt

    if unit.kind == "session-template":
        # The legacy flat command fields nest under harness_config on
        # the 'shell' harness (mirroring the git-credential
        # provider_config nesting); a declared harness / harness_config
        # passes through. env and inherits are kind-owned and stay at
        # the spec top level. The TOML loader's hoist (config.py) and
        # this emission land on the identical internal value, which the
        # post-run registry-equivalence verification proves; validate
        # the rebuilt blob pre-write so a bad blob fails BEFORE anything
        # is written, in the operator's TOML vocabulary, rather than
        # failing verification after the write.
        flat = {key: spec.pop(key) for key in ("command", "restart_command", "required_commands") if key in spec}
        harness = spec.pop("harness", None)
        harness_config = spec.pop("harness_config", None)
        if flat:
            # The loader guarantees flat fields never coexist with a
            # non-shell harness or an explicit harness_config, so this
            # is unambiguously the shell-hoist case.
            harness = "shell"
            harness_config = dict(flat)
        rebuilt_session: dict[str, Any] = {}
        if "inherits" in spec:
            rebuilt_session["inherits"] = spec.pop("inherits")
        if harness is not None:
            rebuilt_session["harness"] = harness
        if harness_config is not None:
            rebuilt_session["harness_config"] = dict(harness_config)
        rebuilt_session.update(spec)  # env and any remaining kind-owned keys
        if isinstance(harness, str) and harness_config is not None:
            from agentworks.capabilities.harness import HARNESS_REGISTRY

            harness_cap = HARNESS_REGISTRY.get(harness)
            if harness_cap is not None:
                try:
                    harness_cap.validate_config(f"session-template/{unit.name}", harness_config)
                except ConfigError as exc:
                    raise ConfigError(
                        f"cannot migrate session-template/{unit.name}: {exc}",
                        hint=(
                            "The flat TOML section carries key(s) its harness "
                            "does not accept (silently ignored by the TOML "
                            "loader). Remove them from config.toml, then re-run."
                        ),
                    ) from exc
        spec = rebuilt_session

    envelope: dict[str, Any] = {
        "apiVersion": "agentworks/v1",
        "kind": unit.kind,
        "metadata": metadata,
        "spec": spec,
    }
    return yaml.safe_dump(envelope, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _spec_data(doc: tomlkit.TOMLDocument, unit: MigrationUnit) -> dict[str, Any]:
    """The unit's merged TOML data (tomlkit folds split sections)."""
    section = doc[unit.section]
    if unit.kind == "admin-template":
        data = dict(section.unwrap())
        env = data.pop("env", None)
        config_body = data.pop("config", {})
        if data:
            extras = ", ".join(sorted(data))
            raise ConfigError(
                f"[admin.{extras}]: unexpected admin sub-section; the migrate "
                "tool only rewrites [admin.config] and [admin.env]",
                hint="Migrate this section by hand.",
            )
        spec = dict(config_body)
        if env:
            spec["env"] = env
        return spec
    if unit.kind == "named-console-template":
        return dict(section.unwrap())
    if unit.kind == "vm-site":
        # Flat section: the section body IS the resource data.
        return dict(section.unwrap())
    return dict(section[unit.name].unwrap())
