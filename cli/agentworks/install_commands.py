"""System and user install commands: two install-command declarable kinds.

Two first-class Registry kinds live here next to the code that loads them:

- ``system-install-command`` (``SystemInstallCommandEntry``): a
  VM-wide install command run as the VM admin during VM init.
- ``user-install-command`` (``UserInstallCommandEntry``): a per-user
  install command run during admin/agent init.

System scope does not imply root execution. A system install command must
explicitly use ``sudo`` for each step that needs root privileges.
Install commands are one logical shell invocation, written as a plain scalar.
Package-manager and maintained vendor entry points belong here; embedded
scripts and multi-step installer machinery do not.

Both are ``declarable`` kinds under the ``error`` miss policy: a typo'd
reference (an unknown command named by a vm-template, admin-template, or
agent-template) surfaces as a framework ``ConfigError`` at
``build_registry`` time citing the reference's source. Built-in entries
ship as bundled manifests under ``manifests/builtin/``; operators may add
or override entries via YAML manifests. Manifest decoders use the loading
helpers below.

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import Field, model_validator

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import _require_field, _require_list
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


# -- Rows --------------------------------------------------------------


class _InstallCommandEntry(DeclaredResource):
    """The spec both install-command kinds declare.

    The two kinds differ in WHO runs the command (the VM admin at VM init,
    the agent or admin user at user init) and in nothing else, so the
    fields are authored once. Each kind is a named subclass rather than an
    alias, because the Registry keys rows by type and the two are separate
    kinds with separate miss policies.
    """

    # The example is what a generated sample writes on the one line an
    # operator MUST fill in here: one maintained entry point, not an embedded
    # installer implementation.
    command: str = Field(examples=["tool-manager install my-tool"])
    """One logical shell invocation, authored as a plain scalar. It must be
    repeat-safe itself or paired with completion checks that reliably skip it
    after success because init and reinit may invoke the resource again."""

    path: list[str] = Field(default_factory=list)
    """Directories prepended to ``PATH`` for the duration of the command."""

    test_exec: str | None = Field(default=None, examples=["my-tool"])
    """Optional completion check for whether this command is already on
    ``PATH``. When multiple non-empty ``test_*`` fields are set, all must pass
    to skip the install."""

    test_file: str | None = None
    """Optional completion check for whether this file exists. A leading
    ``~`` resolves to the target user's home."""

    test_dir: str | None = None
    """Optional completion check for whether this directory exists. A leading
    ``~`` resolves to the target user's home."""

    @model_validator(mode="before")
    @classmethod
    def _steer_bare_test(cls, data: Any) -> Any:
        """``test`` is the mistake operators actually make, so it keeps its
        own steer: as a plain unknown key it would lose the remedy."""
        if isinstance(data, dict) and "test" in data:
            raise ValueError("'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
        return data


class SystemInstallCommandEntry(_InstallCommandEntry):
    """A VM-wide install command run as the VM admin during VM init."""


class UserInstallCommandEntry(_InstallCommandEntry):
    """A per-user install command run during admin or agent init."""


# -- Loading -------------------------------------------------------------------


class _TestFields(TypedDict):
    test_exec: str | None
    test_file: str | None
    test_dir: str | None


def _load_test_fields(data: dict[str, object], ctx: str) -> _TestFields:
    """Load the optional test_exec/test_file/test_dir fields."""
    if "test" in data:
        raise ConfigError(f"{ctx}: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
    fields: _TestFields = {"test_exec": None, "test_file": None, "test_dir": None}
    for key in ("test_exec", "test_file", "test_dir"):
        raw = str(data[key]).strip() if key in data else None
        fields[key] = raw if raw else None  # type: ignore[literal-required,unused-ignore]
    return fields


def _load_system_commands(
    raw: dict[str, object],
) -> dict[str, SystemInstallCommandEntry]:
    entries: dict[str, SystemInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"system_install_commands.{name} must be a table")
        ctx = f"system_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = SystemInstallCommandEntry(
            name=name,
            description=str(data["description"]) if "description" in data else None,
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            **tests,
        )
    return entries


def _load_user_commands(
    raw: dict[str, object],
) -> dict[str, UserInstallCommandEntry]:
    entries: dict[str, UserInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"user_install_commands.{name} must be a table")
        ctx = f"user_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = UserInstallCommandEntry(
            name=name,
            description=str(data["description"]) if "description" in data else None,
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            **tests,
        )
    return entries


# -- Framework kind strategies -------------------------------------------------
#
# Both kinds use the **error miss policy**: a typo in a template's
# ``system_install_commands`` / ``user_install_commands`` list surfaces as
# a framework miss-policy error at ``build_registry`` time, citing the
# reference's source. There is no auto-declare path: entries are built-in
# (bundled manifests) or operator-declared, and references must resolve to
# a known name.


@dataclass(frozen=True)
class _SystemInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"system-install-command"``."""

    kind: str = "system-install-command"
    description: str = "VM-wide install commands run as the VM admin during VM init"
    prose: TopicProse = TopicProse(
        title="System install commands",
        overview="""
        A system-install-command is the fallback for VM-wide tooling that cannot use a
        template's `apt_packages` or `snap` fields. Prefer those fields first, then a
        maintained package-manager or vendor entry point. The command must be one
        logical shell invocation written as a plain scalar. Embedded scripts, block
        scalars, here-documents, multi-step installers, state machines, signature pipelines,
        and cleanup routines do not belong in a manifest.

        It runs as the VM admin user, not root, during `agw vm create` and again on
        `agw vm reinit`. Commands that need root privileges must explicitly use `sudo`.
        The invocation must be repeat-safe itself or declare completion checks that
        reliably skip it after success.

        A vm-template refers to it by name through `system_install_commands`. Declare
        any combination of `test_exec`, `test_file`, and `test_dir` as completion
        checks. Init skips the command only when every non-empty declared test passes.
        With no non-empty tests, the command always runs.
        """,
    )
    model: type[DeclaredResource] = SystemInstallCommandEntry
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


@dataclass(frozen=True)
class _UserInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"user-install-command"``."""

    kind: str = "user-install-command"
    description: str = "Per-user install commands for admin/agent init"
    prose: TopicProse = TopicProse(
        title="User install commands",
        overview="""
        A user-install-command is the fallback for per-user tooling that cannot use a
        template's `mise_packages` field. Prefer a maintained package-manager or vendor entry
        point. The command must be one logical shell invocation written as a plain
        scalar. Embedded scripts, block scalars, here-documents, multi-step installers, state
        machines, signature pipelines, and cleanup routines do not belong in a manifest.

        It runs unprivileged, once for the admin user and once for each agent user whose
        template names it, and it runs again on reinit. The invocation must be
        repeat-safe itself or declare completion checks that reliably skip it after
        success.

        An admin-template or agent-template refers to it by name through
        `user_install_commands`. Declare any combination of `test_exec`, `test_file`,
        and `test_dir` as completion checks. Init skips the command only when every
        non-empty declared test passes. With no non-empty tests, it always runs. In
        `test_file` and `test_dir`, `~` is the target user's home, not the operator's.
        """,
    )
    model: type[DeclaredResource] = UserInstallCommandEntry
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["system-install-command"] = _SystemInstallCommandKind()
KIND_REGISTRY["user-install-command"] = _UserInstallCommandKind()
