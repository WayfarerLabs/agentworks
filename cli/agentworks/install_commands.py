"""System and user install commands: two install-command declarable kinds.

Two first-class Registry kinds live here next to the code that loads them:

- ``system-install-command`` (``SystemInstallCommandEntry``): a
  system-level (root) install command run during VM init.
- ``user-install-command`` (``UserInstallCommandEntry``): a per-user
  install command run during admin/agent init.

Both are ``declarable`` kinds under the ``error`` miss policy: a typo'd
reference (an unknown command named by a vm-template, admin-template, or
agent-template) surfaces as a framework ``ConfigError`` at
``build_registry`` time citing the reference's source. Built-in entries
ship as bundled manifests under ``manifests/builtin/``; operators may add
or override entries via YAML manifests. The ``_load_system_commands`` /
``_load_user_commands`` helpers below belong to the migrator's frozen TOML
oracle (``agentworks.migrate.toml_resources``), which is written
independently of the rows' own models on purpose, so its
registry-equivalence check stays a real test of the emission mapping.

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import Field, model_validator

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import (
    _SYNTHESIZED_DECLS,
    _require_field,
    _require_list,
)
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import _SectionLineMap
    from agentworks.resources.reference import ResourceReference


# -- Rows --------------------------------------------------------------


class _InstallCommandEntry(DeclaredResource):
    """The spec both install-command kinds declare.

    The two kinds differ in WHO runs the command (root at VM init, the
    agent or admin user at user init) and in nothing else, so the fields
    are authored once. Each kind is a named subclass rather than an alias,
    because the Registry keys rows by type and the two are separate kinds
    with separate miss policies.
    """

    # The example is what a generated sample writes on the one line an
    # operator MUST fill in here, and it is the shape worth teaching: a
    # fetch piped to a shell, which is what most vendor installers are.
    command: str = Field(examples=["curl -fsSL https://example.com/install.sh | bash"])
    """The shell command to run.

    Run at VM init and again at reinit, so write it to be idempotent."""

    path: list[str] = Field(default_factory=list)
    """Directories prepended to ``PATH`` for the duration of the command."""

    test_exec: str | None = Field(default=None, examples=["my-tool"])
    """Skip the install when this command is already on ``PATH``. At most
    one of the three ``test_*`` fields may be set."""

    test_file: str | None = None
    """Skip the install when this file already exists. ``~`` resolves to
    the target user's home."""

    test_dir: str | None = None
    """Skip the install when this directory already exists. ``~`` resolves
    to the target user's home."""

    @model_validator(mode="before")
    @classmethod
    def _steer_bare_test(cls, data: Any) -> Any:
        """``test`` is the mistake operators actually make, so it keeps its
        own steer: as a plain unknown key it would lose the remedy."""
        if isinstance(data, dict) and "test" in data:
            raise ValueError("'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
        return data

    @model_validator(mode="after")
    def _at_most_one_test(self) -> _InstallCommandEntry:
        """At most one of the three may be set.

        Counted on ``is not None``, so an EMPTY string counts as set. The
        loader this replaces normalized ``""`` to ``None`` before counting,
        which made ``test_exec: ""`` beside a real ``test_file`` legal; it
        is an error now. That follows from strict mode dropping the
        normalization, but it does not follow from it automatically, so it
        is said here rather than left to be discovered.

        Which is also why the message names the fields it FOUND set, and
        calls out an empty one. "at most one of test_exec, test_file,
        test_dir may be set" gives an operator whose file says
        ``test_exec: ""`` beside a real ``test_file`` no reason to think
        the empty string is the newly-counted one, so deleting the
        meaningful field is exactly as plausible a reading.
        """
        found = [
            (name, value)
            for name, value in (
                ("test_exec", self.test_exec),
                ("test_file", self.test_file),
                ("test_dir", self.test_dir),
            )
            if value is not None
        ]
        if len(found) <= 1:
            return self
        listed = ", ".join(f"{name} (empty string)" if value == "" else name for name, value in found)
        message = f"at most one of test_exec, test_file, test_dir may be set; this one sets {listed}"
        empty = [name for name, value in found if value == ""]
        if len(empty) < len(found):
            # An empty one beside a real one: say which to delete, because
            # that is the whole ambiguity. All-empty has no such answer.
            message += (
                f". An empty string counts as set, so delete {' and '.join(empty)} rather than blanking it"
                if empty
                else ""
            )
        raise ValueError(message)


class SystemInstallCommandEntry(_InstallCommandEntry):
    """A system-level (root) install command run during VM init."""


class UserInstallCommandEntry(_InstallCommandEntry):
    """A per-user install command run during admin or agent init."""


# -- Loading -------------------------------------------------------------------


class _TestFields(TypedDict):
    test_exec: str | None
    test_file: str | None
    test_dir: str | None


def _load_test_fields(data: dict[str, object], ctx: str) -> _TestFields:
    """Load and validate test_exec/test_file/test_dir fields. At most one may be set."""
    if "test" in data:
        raise ConfigError(f"{ctx}: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
    fields: _TestFields = {"test_exec": None, "test_file": None, "test_dir": None}
    for key in ("test_exec", "test_file", "test_dir"):
        raw = str(data[key]).strip() if key in data else None
        fields[key] = raw if raw else None  # type: ignore[literal-required,unused-ignore]
    set_count = sum(1 for v in fields.values() if v is not None)
    if set_count > 1:
        raise ConfigError(f"{ctx}: at most one of test_exec, test_file, test_dir may be set")
    return fields


def _load_system_commands(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
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
            declared_at=decls.lookup("system_install_commands", name),
            **tests,
        )
    return entries


def _load_user_commands(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
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
            declared_at=decls.lookup("user_install_commands", name),
            **tests,
        )
    return entries


# The operator install-command publisher was deleted with the TOML resource
# surface (ADR 0022): built-in install commands ship as bundled YAML
# manifests (via ``builtin_manifests.publish_to``), and operator
# install-command entries are YAML manifests too. ``_load_system_commands``
# / ``_load_user_commands`` above survive because the manifest install
# decoders still delegate to them.


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
    description: str = "System-level (root) install commands for VM init"
    prose: TopicProse = TopicProse(
        title="System install commands",
        overview="""
        A system-install-command installs system-wide tooling that apt cannot: a vendor
        install script, a binary release, anything that ends up outside a package. It
        runs as root during `agw vm create` and again on `agw vm reinit`, so write it to
        be safe to run twice.

        A vm-template refers to it by name through `system_install_commands`. Give it
        exactly one of `test_exec`, `test_file`, or `test_dir` and init skips the
        command when the tool is already there.
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
        A user-install-command installs per-user tooling: something that belongs in one
        user's home rather than on the whole machine. It runs unprivileged, once for the
        admin user and once for each agent user whose template names it, and it re-runs
        on reinit, so write it to be safe to run twice.

        An admin-template or agent-template refers to it by name through
        `user_install_commands`. Give it exactly one of `test_exec`, `test_file`, or
        `test_dir` to make it skippable; in `test_file` and `test_dir`, `~` is the
        target user's home, not the operator's.
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
