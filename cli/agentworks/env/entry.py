"""EnvEntry: one env var declaration, either a plaintext value or a secret reference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources.reference import SecretReference


@dataclass(frozen=True)
class EnvEntry:
    """One env var declaration.

    Exactly one of ``value`` or ``secret`` is set. ``value`` carries a plaintext
    value to export; ``secret`` carries the name of a declared secret
    (``[secrets.<name>]``) resolved through the active backends at command time.

    ``key`` is the env var name; validation lives in the config loader so this
    type can stay pure data with no regex coupling.
    """

    key: str
    value: str | None = None
    secret: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and self.secret is None:
            raise ValueError(
                f"EnvEntry for {self.key!r} must set exactly one of value or secret",
            )
        if self.value is not None and self.secret is not None:
            raise ValueError(
                f"EnvEntry for {self.key!r} cannot set both value and secret",
            )

    def referenced_resources(self, source: tuple[str, str]) -> list[SecretReference]:
        """Emit a ``SecretReference`` for this entry's secret reference,
        or an empty list for plaintext entries.

        Called by the Resource that owns this env entry's table (admin,
        the four template kinds, named_console). ``source`` is the
        declaring Resource's ``(kind, name)`` identity. The usage text is
        derived from the env-var key, so a typo'd KEY surfaces in
        diagnostics with the actual variable name.

        The import of ``SecretReference`` is ``TYPE_CHECKING``-only to
        keep ``EnvEntry`` framework-ignorant at runtime; constructed
        lazily inside the method.
        """
        if self.secret is None:
            return []
        from agentworks.resources.reference import SecretReference
        from agentworks.secrets.kinds import SECRET_KIND_NAME

        return [
            SecretReference(
                name=self.secret,
                kind=SECRET_KIND_NAME,
                usage=f"the {self.key} env var",
                source=source,
            )
        ]


def env_references(
    env: dict[str, EnvEntry] | None,
    source: tuple[str, str],
) -> list[SecretReference]:
    """Aggregate ``EnvEntry.referenced_resources(source)`` across an env table.

    Module-level helper shared by every env-bearing Resource type's
    ``referenced_resources()`` method so the per-type method body stays
    one line. ``env`` may be ``None`` (``SessionTemplate.env`` is
    optional) or empty, in which case the result is an empty list.

    The env package owns this helper because it aggregates
    ``EnvEntry.referenced_resources``; the template dataclasses that
    live in the domain packages import it from here.
    """
    if not env:
        return []
    out: list[SecretReference] = []
    for entry in env.values():
        out.extend(entry.referenced_resources(source))
    return out
