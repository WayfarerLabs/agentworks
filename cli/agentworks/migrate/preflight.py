"""The precondition every migration run has: the tree has to load.

A run proves itself correct by rebuilding the registry from the migrated
config and comparing it against the pre-migration one (``verify.py``).
Rebuilding loads the WHOLE resources directory, so a document with
nothing to do with the migration stops the run just as surely as a broken
emission does: the migrator cannot check its own work over a tree that
will not load.

That is a precondition, not a verification result, and it belongs here,
before anything is written, for two reasons.

**A dry run has to reach the same verdict as the real run.** The
execute half is what loaded the tree, and a dry run does not reach it, so
a dry run over a tree with a bad manifest in it printed the complete
correct diff and ended "Dry run: nothing was written." while the real run
refused. A dry run that reports success where the real run fails is worse
than no dry run, because the operator believes it. Checking here puts the
identical refusal on both paths.

**The refusal explains the ORDER.** Rolling back after the fact is
correct and left nothing behind, but the operator was still told about a
file the migration does not touch by a command that had already said
"Applying migration...", with nothing saying which to do first. The whole
answer is: hand edits first, migrator last.

The check reads the tree AS THIS RUN WOULD LEAVE IT (``overlay``), never
as it is now. As it is now it carries the retired sibling shape this run
exists to remove, which is the one problem an operator must NOT go fix by
hand, and reporting it would refuse every run forever.

It goes as far as ``build_registry``, not just ``load_manifests``,
because both are things the operator's config can fail at and neither is
reachable from a dry run today: a name carrying a ``/`` passes the loader
and is rejected when the row is added. Everything up to a built registry
is "does the operator's config work?", and that is the whole question a
dry run is asked.

**This costs ``plan_migration`` its purity, deliberately.** Planning used
to be pure over the config text, with the first registry build happening
after the writes. The property that actually mattered is not purity: it
is that the migrator works on a config no other command can load, and
that survives, because the two things that stop those configs loading are
exactly the two this neutralizes (TOML resource sections, via the
settings-only load the command already does, and the retired sibling
shape, via the overlay). A build that fails here fails ``_verify`` too;
all that changes is that it fails before the writes instead of after.

What stays in ``execute._verify``: the registry-EQUIVALENCE comparison
against the pre-migration rows. It answers a different question ("did
this run drop something?", a tool bug, rolled back) from the one here
("does the operator's config work?", theirs to fix), and it has to run
against what actually landed on disk rather than a prediction of it. The
dry run says which of the two it ran rather than implying both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.migrate.planning import MigrationPlan

_ORDERING = (
    "The whole resources directory has to load before a migration can be "
    "verified, so a problem in a document this run does not touch stops it "
    "all the same. Fix what is reported above (`agw resource describe-kind "
    "<kind>` documents what each field accepts, with no working config "
    "needed), then re-run `agw resource migrate`. Nothing has been written."
)


def require_loadable_tree(plan: MigrationPlan, config: Config) -> None:
    """Refuse the run unless the config it would produce loads.

    Raises ``ConfigError`` naming what fails and saying to fix that
    first. Called from ``plan_migration``, so the dry run and the real
    run are held to it through the one path both already take, and a real
    run that trips it has written nothing to roll back.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.manifests import load_manifests

    try:
        build_registry(config, load_manifests(plan.resources_dir, overlay=plan.post_migration_texts))
    except ConfigError as exc:
        hint = f"{exc.hint} {_ORDERING}" if exc.hint else _ORDERING
        raise ConfigError(
            f"cannot migrate: {plan.resources_dir} does not load, so this run could not be verified.\n{exc}",
            hint=hint,
        ) from exc
