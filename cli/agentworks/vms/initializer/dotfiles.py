"""Dotfiles: sync an account's checkout, then run its install, for the admin and each agent alike."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.requests import Command
from agentworks.sources import (
    DOTFILES_DEADLINE,
    DestinationError,
    expand_destination,
    fetch_dir,
    normalize_destination,
    parse_source_ref,
)

from .steps import STEP_FAILURES, warn_step

if TYPE_CHECKING:
    from agentworks.execution.files import Files

    from .steps import InitLog


def sync_dotfiles(
    files: Files,
    *,
    source: str,
    destination: str,
    install_cmd: str,
    shell: str,
    home: str,
    managed: tuple[str, ...],
    log: InitLog,
) -> None:
    """Sync the dotfiles ``source`` names to ``destination``, then run ``install_cmd`` in the checkout.

    Everything runs as the account ``files`` acts as, whose home is ``home``
    and whose managed paths (``vms.home_layout``) are ``managed``.
    ``destination`` is the operator's value as rendered for this account. It
    is normalized and checked here (``sources.normalize_destination``, then
    ``sources.expand_destination``) before any request; a refused one warns
    without naming it, and nothing is synced or installed. The sync is
    ``sources.fetch_dir``. The install runs as ``{shell} -lc`` with the
    checkout as its working directory, so it sees the account's login
    environment (its profile fragment, written earlier) and no operator
    environment. Sync and install warn separately, because a failed sync
    skips the install entirely; saying so is the difference between a
    warning an operator can act on and a later failure that looks unrelated
    to it.
    """
    log.step("Dotfiles")
    try:
        dest = expand_destination(normalize_destination(destination, managed=managed), home, managed=managed)
    except DestinationError as error:
        warn_step(log, f"dotfiles destination refused, so nothing synced and '{install_cmd}' did not run", error)
        return
    try:
        ref = parse_source_ref(source)
        output.info(f"Syncing dotfiles from {source}...")
        fetch_dir(ref, files, dest, log=log)
    except STEP_FAILURES as error:
        summary = (
            f"dotfiles sync failed, so '{install_cmd}' did not run and {dest} "
            "may hold its previous contents, the new ones, or a partial checkout"
        )
        warn_step(log, summary, error)
        return
    output.info(f"Running dotfiles install: {install_cmd}")
    try:
        files.access.run(Command((shell, "-lc", install_cmd), cwd=dest, deadline=DOTFILES_DEADLINE), check=True)
    except STEP_FAILURES as error:
        warn_step(log, f"dotfiles install '{install_cmd}' failed in {dest}", error)
