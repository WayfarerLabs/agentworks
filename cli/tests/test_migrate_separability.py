"""The migrator is SEPARABLE, and stays that way until it is deleted.

``agw resource migrate`` is runway, not capability: it exists to carry
operators across the breaking changes this effort ships (TOML resource
declarations, the retired sibling capability shape), and it retires a
release or two afterwards like every other compatibility surface here.
Operator direction, 2026-08-06.

Deleting it then must be a clean excision: remove ``agentworks/migrate/``,
its CLI command, and its tests, and nothing else changes. That is only true
if the dependency arrow points one way. The migrator may reach into core
freely, because it is the thing going away; core may not reach into the
migrator, because every such import is a line someone has to unpick under
time pressure on removal day.

The one sanctioned consumer is the CLI command that fronts it. It is named
here rather than pattern-matched so that adding a second is a deliberate
edit to this list with a reviewer looking at it.
"""

from __future__ import annotations

from pathlib import Path

_AGENTWORKS = Path(__file__).resolve().parents[1] / "agentworks"

#: The only module outside the package that may import it: the CLI command
#: that fronts ``agw resource migrate``. It goes away with the migrator.
_SANCTIONED_CONSUMERS = frozenset({"cli/commands/resource.py"})


def _imports_migrate(source: str) -> bool:
    """Whether ``source`` imports the migrator, at any indentation.

    Function-local imports count. The migrator's own consumer uses them
    deliberately (to keep ruamel off the startup path), so a scan that only
    read module-level lines would miss precisely the spelling in use.
    """
    return any(
        line.strip().startswith(("from agentworks.migrate", "import agentworks.migrate"))
        or line.strip().startswith("from agentworks import migrate")
        for line in source.splitlines()
    )


def test_nothing_outside_the_migrator_imports_it_but_its_own_command() -> None:
    """Core never reaches into the migrator, so removal is a deletion.

    A violation here is not a bug today; it is a line that will have to be
    unpicked on removal day, which is when nobody wants to be discovering
    what depended on the thing they are deleting.
    """
    offenders = sorted(
        path.relative_to(_AGENTWORKS).as_posix()
        for path in _AGENTWORKS.rglob("*.py")
        if not path.relative_to(_AGENTWORKS).as_posix().startswith("migrate/")
        and _imports_migrate(path.read_text())
        and path.relative_to(_AGENTWORKS).as_posix() not in _SANCTIONED_CONSUMERS
    )
    assert not offenders, (
        "these modules import the migrator, which is scheduled for removal:\n"
        + "\n".join(offenders)
        + "\nThe migrator may import core; core may not import the migrator. "
        "If a consumer is genuinely sanctioned, add it to _SANCTIONED_CONSUMERS "
        "and say in the commit why it is worth unpicking on removal day."
    )


def test_the_sanctioned_consumer_still_exists() -> None:
    """Non-vacuity: the allow-list names a real file that really imports it.

    Without this, deleting or renaming the command would leave the guard
    passing over an allow-list describing nothing, and the next real
    violation would be the first thing it ever caught.
    """
    for relative in _SANCTIONED_CONSUMERS:
        path = _AGENTWORKS / relative
        assert path.exists(), f"_SANCTIONED_CONSUMERS names a file that is gone: {relative}"
        assert _imports_migrate(path.read_text()), (
            f"{relative} no longer imports the migrator; drop it from _SANCTIONED_CONSUMERS"
        )
