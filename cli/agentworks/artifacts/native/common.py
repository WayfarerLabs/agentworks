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
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from agentworks.artifacts.application import OwnedArtifactFile, SessionArtifactContext
    from agentworks.artifacts.model import ArtifactFacet, ArtifactInput
    from agentworks.capabilities.harness_integration.setup import SetupInvocation
    from agentworks.execution.access import VMAccess


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
    return select_inputs(inputs, selected), tuple(deferred)


def select_inputs(inputs: ArtifactInputs, selected: set[str]) -> ArtifactInputs:
    return ArtifactInputs(
        local=inputs.local.select(selected) if inputs.local is not None else None,
        deferred={owner: group.select(selected) for owner, group in inputs.deferred.items()},
    )


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
            identities: dict[str, tuple[str, ...]] = {}
            for file in application.files:
                if file.native_identity is not None:
                    prior = identities.setdefault(file.native_identity, file.origins)
                    if set(prior) != set(file.origins):
                        raise NativeArtifactConflictError(
                            f"multiple artifacts claim native identity '{file.native_identity}'",
                            (file.native_identity,),
                            hint="Rename the conflicting artifact before retrying setup.",
                        )
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
                    destination=_setup_deferral_destination(invocation),
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


def _name_conflicts(
    inputs: ArtifactInputs, types: tuple[ArtifactType, ...] = (ArtifactType.SKILL, ArtifactType.AGENT)
) -> tuple[tuple[ArtifactInput, ...], ...]:
    names: dict[tuple[ArtifactType, str], list[ArtifactInput]] = {}
    for item in inputs.items():
        if item.content.type in types:
            names.setdefault((item.content.type, item.content.name), []).append(item)
    return tuple(tuple(items) for items in names.values() if len(items) > 1)


def _name_conflict_reason(items: tuple[ArtifactInput, ...]) -> str:
    content = items[0].content
    owners = ", ".join(f"{item.origin.component}:{item.origin.resource_name}" for item in items)
    return f"multiple artifacts claim native {content.type.value} name '{content.name}': {owners}"


def render_setup_artifacts(
    invocation: SetupInvocation,
    render: Callable[[ArtifactInputs], ArtifactApplication],
) -> ArtifactApplication:
    """Defer every colliding input origin before rendering independent setup files."""
    rejected: dict[str, str] = {}
    for items in _name_conflicts(invocation.artifacts):
        reason = _name_conflict_reason(items)
        invocation.report_incomplete(reason)
        rejected.update((item.identity, reason) for item in items)
    selected = {item.identity for item in invocation.artifacts.items() if item.identity not in rejected}
    application = render(select_inputs(invocation.artifacts, selected))
    return replace(
        application,
        deferred=(
            *application.deferred,
            *(
                ArtifactDeferral(input_id=identity, destination=_setup_deferral_destination(invocation), reason=reason)
                for identity, reason in rejected.items()
            ),
        ),
    )


def _setup_deferral_destination(invocation: SetupInvocation) -> ArtifactFacet:
    from agentworks.capabilities.harness_integration.setup import VMSetupInvocation

    return "user" if isinstance(invocation, VMSetupInvocation) else "session"


def validate_names(
    inputs: ArtifactInputs, *, types: tuple[ArtifactType, ...] = (ArtifactType.SKILL, ArtifactType.AGENT)
) -> None:
    """Reject colliding native names at the integration input boundary."""
    from agentworks.artifacts.native.probe import NativeArtifactConflictError

    conflicts = _name_conflicts(inputs, types)
    if conflicts:
        raise NativeArtifactConflictError(
            "; ".join(_name_conflict_reason(items) for items in conflicts),
            tuple(f"{items[0].content.type.value}:{items[0].content.name}" for items in conflicts),
            hint="Rename the conflicting artifacts or use an integration with separate native identities.",
        )


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


def reject_flags(extra_args: Sequence[str], forbidden: set[str], integration: str) -> None:
    """Reject raw CLI switches that replace or disable an artifact carrier."""
    for token in extra_args:
        if token.split("=", 1)[0] in forbidden or token == "--":
            raise ConfigError(f"{integration} extra_args conflicts with artifact delivery: {token.split('=', 1)[0]}")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


@contextmanager
def ancestor_diagnostics(integration: str, *, redactions: Iterable[str] = ()) -> Iterator[None]:
    """Keep native checks of existing upstream placement advisory at session start."""
    try:
        yield
    except AgentworksError as error:
        diagnostic = sanitize_setup_diagnostic(
            " ".join(part for part in (str(error), error.hint) if part),
            redactions=redactions,
        )
        output.warn(f"Existing ancestor artifacts via {integration}: {diagnostic} Continuing with existing placement.")


def validate_ancestor_names(context: SessionArtifactContext, application: ArtifactApplication) -> None:
    """Report old branch collisions; refuse conflicts introduced by new delivery."""
    from agentworks.artifacts.native.probe import NativeArtifactConflictError

    files_by_name: dict[str, OwnedArtifactFile | ArtifactFile] = {}
    conflicting_ancestors: dict[str, OwnedArtifactFile] = {}
    for ancestor in context.ancestor_files:
        if ancestor.native_identity is None:
            continue
        prior = files_by_name.setdefault(ancestor.native_identity, ancestor)
        if set(prior.origins) != set(ancestor.origins) and ancestor.native_identity not in conflicting_ancestors:
            conflicting_ancestors[ancestor.native_identity] = ancestor
            output.warn(
                f"Existing scopes share native artifact identity '{ancestor.native_identity}': "
                f"{prior.path!r} and {ancestor.path!r}. Continuing with existing placement."
            )
    for file in application.files:
        if file.native_identity is None:
            continue
        prior = files_by_name.setdefault(file.native_identity, file)
        competing = conflicting_ancestors.get(file.native_identity)
        if competing is not None or set(prior.origins) != set(file.origins):
            paths = {prior.path, file.path}
            if competing is not None:
                paths.add(competing.path)
            raise NativeArtifactConflictError(
                f"multiple scopes claim native artifact identity '{file.native_identity}': "
                + ", ".join(repr(path) for path in sorted(paths)),
                (file.native_identity,),
                hint="Rename the conflicting artifacts or retire the competing native entry.",
            )


def has_artifacts(context: SessionArtifactContext | None) -> bool:
    return context is not None and bool(context.inputs or context.ancestor_files)


def recognized_ancestor_files(
    context: SessionArtifactContext, application: ArtifactApplication
) -> tuple[OwnedArtifactFile, ...]:
    """Recognize earlier placements only for the same native identity and origin.

    A removed activation can forward an artifact whose old native file remains.
    New delivery recognizes that logical artifact without widening policy checks
    to unrelated ancestor content.
    """
    proposed = {
        (file.native_identity, frozenset(file.origins))
        for file in application.files
        if file.native_identity is not None
    }
    return tuple(file for file in context.ancestor_files if (file.native_identity, frozenset(file.origins)) in proposed)


def prepare_session_delivery(
    access: VMAccess | None,
    *,
    tool: str,
    integration: str,
    context: SessionArtifactContext | None,
    render: Callable[[SessionArtifactContext | None], NativeSessionArtifacts],
    workspace: str,
) -> tuple[NativeSessionArtifacts, str | None]:
    """Rerender around native name conflicts; keep other new-delivery failures strict."""
    from agentworks.artifacts.native.probe import NativeArtifactConflictError, probe_native

    if context is None:
        return render(None), None
    root: str | None = None
    rejected: dict[str, str] = {}
    plan: NativeSessionArtifacts | None = None
    while True:
        selected = {item.identity for item in context.inputs.items() if item.identity not in rejected}
        current = replace(context, inputs=select_inputs(context.inputs, selected))
        plan = None
        try:
            plan = render(current)
            validate_ancestor_names(current, plan.application)
            # MCP uses entry-specific config checks, not skill/persona discovery.
            deferred = {item.input_id for item in plan.application.deferred}
            legacy_delivery = not current.inputs or any(
                item.content.type is not ArtifactType.MCP_SERVER and item.identity not in deferred
                for item in current.inputs.items()
            )
            if legacy_delivery and (plan.application.files or plan.argv):
                if access is None:
                    raise StateError("artifact delivery requires the actual native launch target")
                root = probe_native(
                    access,
                    tool=tool,
                    environment=current.environment,
                    redactions=current.redactions,
                    home=current.home,
                    workspace=workspace,
                    files=plan.application.files,
                    recognized_files=recognized_ancestor_files(current, plan.application),
                    session_plugin="--plugin-dir" in plan.argv,
                )
        except NativeArtifactConflictError as error:
            for identity in error.identities:
                parts = identity.split(":")
                origins = {
                    origin
                    for file in (() if plan is None else plan.application.files)
                    if file.native_identity == identity
                    for origin in file.origins
                }
                matching = tuple(
                    item
                    for item in current.inputs.items()
                    if item.origin_identity in origins
                    or (item.content.type.value == parts[0] and item.content.name == parts[-1])
                )
                if not matching:
                    raise StateError("native name conflict does not identify a selected session input") from error
                reason = " ".join(value for value in (str(error), error.hint) if value)
                rejected.update((item.identity, reason) for item in matching)
            if not error.identities:
                raise StateError("native name conflict does not identify a selected session input") from error
            continue
        break
    assert plan is not None
    plan = replace(
        plan,
        application=replace(
            plan.application,
            deferred=(
                *plan.application.deferred,
                *(
                    ArtifactDeferral(input_id=identity, destination="session", reason=reason)
                    for identity, reason in rejected.items()
                ),
            ),
        ),
    )
    if context.ancestor_files:
        with ancestor_diagnostics(integration, redactions=context.redactions):
            if access is None:
                raise StateError("artifact delivery requires the actual native launch target")
            ancestor_root = probe_native(
                access,
                tool=tool,
                environment=context.environment,
                redactions=context.redactions,
                home=context.home,
                workspace=workspace,
                files=context.ancestor_files,
            )
            root = root or ancestor_root
    return plan, root


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
