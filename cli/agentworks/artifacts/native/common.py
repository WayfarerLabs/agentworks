"""Small rendering primitives shared by the native artifact adapters."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.model import ArtifactInputs, ArtifactType
from agentworks.errors import AgentworksError, ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile, SessionArtifactContext
    from agentworks.artifacts.model import ArtifactFacet, ArtifactInput
    from agentworks.capabilities.harness_integration.setup import SetupInvocation


# Agentworks budget for a rendered persona TOML file, including its instruction body.
MAX_CODEX_PERSONA_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class NativeSessionArtifacts:
    """Files and literal argv tokens for one native launch, including resume."""

    application: ArtifactApplication = ArtifactApplication()
    argv: tuple[str, ...] = ()


def defer(inputs: ArtifactInputs, destination: ArtifactFacet, reason: str) -> ArtifactApplication:
    return ArtifactApplication(
        deferred=tuple(
            ArtifactDeferral(input_id=item.identity, destination=destination, reason=reason) for item in inputs.items()
        )
    )


def select_session_artifacts(
    inputs: ArtifactInputs,
    enabled_workarounds: Sequence[str],
    workarounds: Mapping[ArtifactType, str],
) -> tuple[ArtifactInputs, tuple[ArtifactDeferral, ...]]:
    """Select explicitly enabled session carriers before validating native delivery."""
    selected = set()
    deferred = []
    for item in inputs.items():
        workaround = workarounds.get(item.content.type)
        if workaround is not None and workaround in enabled_workarounds:
            selected.add(item.identity)
        else:
            reason = (
                f"Session delivery requires enabled_workarounds: [{workaround}]"
                if workaround is not None
                else f"No supported session workaround for artifact type {item.content.type.value}"
            )
            deferred.append(ArtifactDeferral(input_id=item.identity, destination="session", reason=reason))
    return ArtifactInputs(
        local=inputs.local.select(selected) if inputs.local is not None else None,
        deferred={owner: group.select(selected) for owner, group in inputs.deferred.items()},
    ), tuple(deferred)


def native_home(home: str, environment: Mapping[str, str], variable: str, default: str) -> str:
    """Validate a native-home override supplied by the effective launch environment."""
    path = environment.get(variable) or f"{home}/{default}"
    parts = path.split("/")
    if not path.startswith("/") or any(part in (".", "..", "") for part in parts[1:]) or "\x00" in path:
        raise ConfigError(f"{variable} must be an absolute normalized directory for artifact delivery")
    return path


def validate_user_placement(application: ArtifactApplication, home: str) -> None:
    """Refuse unsupported native-home placement before other user setup mutations."""
    if any(not file.path.startswith(home.rstrip("/") + "/") for file in application.files):
        raise ConfigError(
            "user artifact placement outside HOME is unsupported",
            hint="Choose a native-home override beneath this user's HOME and retry the owning setup.",
        )


def artifact_file(
    path: str, text: str, inputs: tuple[ArtifactInput, ...], *, identity: str | None = None
) -> ArtifactFile:
    return ArtifactFile(
        path, text.encode("utf-8"), tuple(item.origin_identity for item in inputs), native_identity=identity
    )


def skill_files(root: str, item: ArtifactInput, *, namespace: str = "") -> tuple[ArtifactFile, ...]:
    identity = f"skill:{namespace}{item.content.name}"
    return tuple(
        ArtifactFile(
            f"{root}/{item.content.name}/{member.path}",
            member.data,
            (item.origin_identity,),
            executable=member.executable,
            native_identity=identity,
            package_root=f"{root}/{item.content.name}",
        )
        for member in item.content.members
    )


def defer_setup_conflicts(
    application: ArtifactApplication,
    invocation: SetupInvocation,
    destination: ArtifactFacet,
    probe: Callable[[tuple[ArtifactFile, ...]], object],
) -> ArtifactApplication:
    """Defer conflicting native entries while validating independent setup outputs.

    Probe identity conflicts concern particular artifacts. Other native, policy,
    transport and malformed-input failures still fail this facet. Removing a
    placement from the plan allows ordinary safe retirement of its owned files.
    """
    from agentworks.artifacts.native.probe import NativeArtifactConflictError

    while application.files:
        try:
            probe(application.files)
            return application
        except NativeArtifactConflictError as error:
            rejected = {
                origin
                for file in application.files
                if file.native_identity in error.identities
                for origin in file.origins
            }
            if not rejected:
                raise StateError("native artifact conflict does not identify a proposed setup entry") from error
            invocation.report_incomplete(str(error))
            existing = {item.input_id for item in application.deferred}
            deferred = tuple(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination=destination,
                    reason="Native identity conflicts with existing content; repair that entry and rerun owning setup",
                )
                for item in invocation.artifacts.items()
                if item.origin_identity in rejected and item.identity not in existing
            )
            application = replace(
                application,
                files=tuple(file for file in application.files if not rejected.intersection(file.origins)),
                deferred=(*application.deferred, *deferred),
            )
    return application


def validate_names(inputs: ArtifactInputs) -> None:
    """Reject colliding native names at the integration input boundary."""
    seen: dict[tuple[ArtifactType, str], ArtifactInput] = {}
    for item in inputs.items():
        name = item.content.name
        key = item.content.type, name
        if item.content.type in (ArtifactType.SKILL, ArtifactType.AGENT) and key in seen:
            previous = seen[key].origin.owner
            current = item.origin.owner
            raise ConfigError(
                f"multiple artifacts claim native {item.content.type.value} name '{name}': "
                f"{previous.component}:{previous.resource_name} and {current.component}:{current.resource_name}"
            )
        seen[key] = item


def persona_options(item: ArtifactInput, integration: str, allowed: Mapping[str, type]) -> dict[str, object]:
    """Validate integration-specific persona options, excluding executable extensions."""
    options = item.content.native_options.get(integration, {})
    if not isinstance(options, dict):
        raise ConfigError(f"agent '{item.content.name}': native options for {integration} must be an object")
    for key, value in options.items():
        expected = allowed.get(key)
        if expected is None or type(value) is not expected:
            raise ConfigError(f"agent '{item.content.name}': unsupported {integration} native option '{key}'")
        if isinstance(value, int) and value < 1:
            raise ConfigError(f"agent '{item.content.name}': native option '{key}' must be positive")
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


@contextmanager
def ancestor_diagnostics(integration: str) -> Iterator[None]:
    """Keep native checks of existing upstream placement advisory at session start."""
    try:
        yield
    except AgentworksError as error:
        hint = f" {error.hint}" if error.hint else ""
        output.warn(
            f"Existing ancestor artifacts via {integration}: {error}.{hint} Continuing with existing placement."
        )


def validate_ancestor_names(context: SessionArtifactContext, application: ArtifactApplication) -> None:
    """Report old branch collisions; refuse conflicts introduced by new delivery."""
    files_by_name: dict[str, OwnedArtifactFile | ArtifactFile] = {}
    for ancestor in context.ancestor_files:
        if ancestor.native_identity is None:
            continue
        prior = files_by_name.setdefault(ancestor.native_identity, ancestor)
        if set(prior.origins) != set(ancestor.origins):
            output.warn(
                f"Existing scopes share native artifact identity '{ancestor.native_identity}': "
                f"{prior.path!r} and {ancestor.path!r}. Continuing with existing placement."
            )
    for file in application.files:
        if file.native_identity is None:
            continue
        prior = files_by_name.setdefault(file.native_identity, file)
        if set(prior.origins) != set(file.origins):
            raise ConfigError(
                f"multiple scopes claim native artifact identity '{file.native_identity}': "
                f"{prior.path!r} and {file.path!r}"
            )


def has_artifacts(context: SessionArtifactContext | None) -> bool:
    return context is not None and bool(context.inputs or context.ancestor_files)


def validate_discovery_paths(
    context: SessionArtifactContext, roots: tuple[str, ...], *, files: tuple[str, ...] = ()
) -> None:
    """Previously applied files must remain inside directories this launch discovers."""
    for file in context.ancestor_files:
        if file.path not in files and not any(file.path.startswith(root + "/") for root in roots):
            output.warn(
                f"Native discovery no longer includes ancestor artifact '{file.path}'. "
                "Restore the native home override or refresh its owning facet; continuing with existing placement."
            )


# Leave room for the launcher's shell/tmux wrapping below Linux's per-argument limit.
_NATIVE_COMMAND_BYTES = 32 * 1024


def validate_native_argv(argv: tuple[str, ...]) -> None:
    """Reject oversized or NUL-containing native carriers before session publication."""
    if (
        any("\x00" in token for token in argv)
        or sum(len(token.encode("utf-8")) + 1 for token in argv) > _NATIVE_COMMAND_BYTES
    ):
        raise ConfigError(
            "artifact content exceeds the native command carrier limit",
            hint="Reduce session artifact text or activate a user/workspace facet with native file delivery.",
        )


def validate_native_command(command: str) -> None:
    """Check the quoted command too: shell escaping can expand literal artifact text."""
    if len(command.encode("utf-8")) > _NATIVE_COMMAND_BYTES:
        raise ConfigError(
            "quoted artifact command exceeds the native launch limit",
            hint="Reduce session artifact text or use an outer facet's native file delivery.",
        )
