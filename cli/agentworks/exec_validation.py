"""Validation helpers for the ``exec`` commands.

``agw vm exec`` and ``agw agent exec`` use Click's
``allow_interspersed_args=False`` so an operator can pass through
arguments that look like options (``ls -la``, ``tail -f /path``). The
trade-off is that any agentworks long option (e.g. ``--workspace``)
placed after the VM / agent positional ends up in the passthrough argv
rather than being consumed by Click. sshd would then execute
``$SHELL -c '--workspace ws1 pwd'`` (or ``$SHELL -lc '...'`` when the
transport's ``login_shell`` is set), and the remote shell -- both zsh
and bash do this -- parses the script's leading ``-`` as further shell
options, producing cryptic errors:

- zsh: ``no such option: workspace ws1 pwd``
- bash: ``--: invalid option`` (with the usage screen)

We catch this case at the manager layer with a clean ``ValidationError``
that tells the operator how to invoke the command correctly.
"""

from __future__ import annotations

from functools import lru_cache

from agentworks.errors import ValidationError


@lru_cache(maxsize=1)
def _exec_agw_long_options() -> frozenset[str]:
    """Return the long-form (``--name``) agentworks options on the
    exec commands, derived from Typer's own param metadata.

    Self-maintaining: adding a new option to ``vm exec`` or
    ``agent exec`` automatically extends the set. Walks the Typer
    tree via ``typer.main.get_command``; cached because the result is
    stable for the life of the process.

    The import is deferred to call time -- by the time this fires
    (from inside an ``exec`` command handler) the CLI has already
    wired every subapp onto the root ``app``, so the lookup is
    cycle-free.
    """
    import click
    import typer

    from agentworks.cli._app import app

    click_app = typer.main.get_command(app)
    if not isinstance(click_app, click.Group):
        return frozenset()

    flags: set[str] = set()
    for resource in ("vm", "agent"):
        group = click_app.get_command(click.Context(click_app), resource)
        if not isinstance(group, click.Group):
            continue
        exec_cmd = group.get_command(click.Context(group), "exec")
        if exec_cmd is None:
            continue
        for param in exec_cmd.params:
            if isinstance(param, click.Option):
                # Include secondary_opts so bool flags declared as
                # ``--foo/--no-foo`` register both forms; today every
                # exec option is value-bearing, but the cost of being
                # future-proof here is one extra iteration.
                for opt in (*param.opts, *param.secondary_opts):
                    if opt.startswith("--"):
                        flags.add(opt)
    return frozenset(flags)


def reject_dash_prefixed_command(
    command: list[str], *, kind: str, name: str,
) -> None:
    """Reject ``exec`` commands whose first token starts with ``-``.

    Only call from ``exec`` paths (``exec_vm`` / ``exec_agent``). The
    shell paths (``shell_vm`` / ``shell_agent``) accept ``--workspace``
    in any position because they don't set
    ``allow_interspersed_args=False`` on the Click context, so the
    "args must come before the first positional" hint would be false
    for them.

    The hint is tailored by detection:

    - When the passthrough argv contains a recognized agentworks long
      option (from :func:`_exec_agw_long_options`), we emit the
      ordering hint -- this is the misplaced-flag case.
    - Otherwise we emit the generic ``./-name`` workaround hint for
      operators who really do mean to address a file whose name starts
      with ``-``.
    """
    if not command or not command[0].startswith("-"):
        return
    known_flags = _exec_agw_long_options()
    if any(arg in known_flags for arg in command):
        hint = (
            "agentworks args must come before the first positional "
            "argument for this command."
        )
    else:
        hint = (
            "Remote commands cannot start with '-'. For a file whose "
            "name starts with '-', address it as './-name'."
        )
    raise ValidationError(
        f"remote command cannot start with '-' (got: {command[0]!r})",
        entity_kind=kind,
        entity_name=name,
        hint=hint,
    )
