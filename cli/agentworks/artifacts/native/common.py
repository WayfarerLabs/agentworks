"""Small rendering primitives shared by the native artifact adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile, SessionArtifactContext
    from agentworks.artifacts.model import ArtifactFacet, ArtifactInput


@dataclass(frozen=True)
class NativeSessionArtifacts:
    """Files and literal argv tokens for one native launch, including resume."""

    application: ArtifactApplication = ArtifactApplication()
    argv: tuple[str, ...] = ()


def defer(inputs: tuple[ArtifactInput, ...], destination: ArtifactFacet, reason: str) -> ArtifactApplication:
    return ArtifactApplication(
        deferred=tuple(
            ArtifactDeferral(input_id=item.identity, destination=destination, reason=reason) for item in inputs
        )
    )


def native_home(home: str, environment: Mapping[str, str], variable: str, default: str) -> str:
    """Validate a native-home override supplied by the effective launch environment."""
    path = environment.get(variable) or f"{home}/{default}"
    parts = path.split("/")
    if not path.startswith("/") or any(part in (".", "..", "") for part in parts[1:]) or "\x00" in path:
        raise ConfigError(f"{variable} must be an absolute normalized directory for artifact delivery")
    return path


def artifact_file(
    path: str, text: str, inputs: tuple[ArtifactInput, ...], *, identity: str | None = None
) -> ArtifactFile:
    return ArtifactFile(
        path, text.encode("utf-8"), tuple(item.origin.identity for item in inputs), native_identity=identity
    )


def skill_files(root: str, item: ArtifactInput, *, namespace: str = "") -> tuple[ArtifactFile, ...]:
    identity = f"skill:{namespace}{item.content.name}"
    return tuple(
        ArtifactFile(
            f"{root}/{item.content.name}/{member.path}",
            member.data,
            (item.origin.identity,),
            executable=member.executable,
            native_identity=identity,
        )
        for member in item.content.members
    )


def validate_names(inputs: tuple[ArtifactInput, ...]) -> None:
    """Reject colliding native names at the integration input boundary."""
    seen: set[tuple[ArtifactType, str]] = set()
    for item in inputs:
        name = item.content.name
        if not name or PurePosixPath(name).name != name or name in (".", "..") or "\x00" in name:
            raise ConfigError("artifact native names must be single path components")
        key = item.content.type, name
        if key in seen:
            raise ConfigError(f"multiple artifacts claim native {item.content.type.value} name '{name}'")
        seen.add(key)


def persona_options(item: ArtifactInput, integration: str, allowed: Mapping[str, type]) -> dict[str, object]:
    """Validate integration-specific persona options, excluding executable extensions."""
    options = item.content.native_options.get(integration, {})
    if not isinstance(options, dict):
        raise ConfigError(f"agent '{item.content.name}': native options for {integration} must be an object")
    for key, value in options.items():
        expected = allowed.get(key)
        if expected is None or type(value) is not expected:
            raise ConfigError(f"agent '{item.content.name}': unsupported {integration} native option '{key}'")
        if isinstance(value, list) and any(not isinstance(element, str) for element in value):
            raise ConfigError(f"agent '{item.content.name}': native option '{key}' must contain strings")
    return dict(options)


def context_text(inputs: tuple[ArtifactInput, ...], configured: str | None = None) -> str:
    parts = [configured] if configured is not None else []
    parts.extend(item.content.text for item in inputs if item.content.type in (ArtifactType.HINT, ArtifactType.RULE))
    return "\n\n".join(parts)


def reject_flags(extra_args: Sequence[str], forbidden: set[str], integration: str) -> None:
    """Reject raw CLI switches that replace or disable an artifact carrier."""
    for token in extra_args:
        if token.split("=", 1)[0] in forbidden or token == "--":
            raise ConfigError(f"{integration} extra_args conflicts with artifact delivery: {token.split('=', 1)[0]}")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def validate_ancestor_names(context: SessionArtifactContext, application: ArtifactApplication) -> None:
    """Reject native shadowing between independently applied ancestor branches and this run."""
    origins_by_name: dict[str, set[str]] = {}
    files: tuple[OwnedArtifactFile | ArtifactFile, ...] = (*context.ancestor_files, *application.files)
    for file in files:
        if file.native_identity is None:
            continue
        origins = set(file.origins)
        prior = origins_by_name.setdefault(file.native_identity, origins)
        if prior != origins:
            raise ConfigError(f"multiple scopes claim native artifact identity '{file.native_identity}'")


def has_artifacts(context: SessionArtifactContext | None) -> bool:
    return context is not None and bool(context.inputs or context.ancestor_files)
