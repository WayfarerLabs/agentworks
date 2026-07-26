"""Argument normalization for the ``exec`` commands.

``agw vm exec`` and ``agw agent exec`` use Click's
``allow_interspersed_args=False`` so an operator can pass through
arguments that look like options (``ls -la``, ``tail -f /path``). The
trade-off is that an agentworks flag (e.g. ``--workspace``) placed
after the VM / agent positional ends up in the passthrough argv rather
than being consumed by Click. sshd then executes
``$SHELL -c '--workspace ws1 pwd'`` (or ``$SHELL -lc '...'`` when the
transport's ``login_shell`` is set), and the remote shell (both zsh
and bash do this) parses the script's leading ``-`` as further
shell options, producing cryptic errors:

- zsh: ``no such option: workspace ws1 pwd``
- bash: ``--: invalid option`` (with the usage screen)

The conventional escape hatch is the ``--`` end-of-options separator:
``agw vm exec box -- free -m`` runs ``free -m`` even though ``-m`` (and,
for other commands, the first token itself) starts with ``-``. Click
does NOT strip that leading ``--`` under ``allow_interspersed_args=False``
(it lands in ``ctx.args`` verbatim), so we consume it here. When the
separator is present the operator has explicitly stated where the remote
command begins, so the leading-dash guard steps aside; without it, a
leading-dash first token is almost certainly a misplaced agentworks flag
and we reject it with a hint at the real fix.
"""

from __future__ import annotations

from agentworks.errors import ValidationError


def normalize_exec_command(
    command: list[str],
    *,
    kind: str,
    name: str,
) -> list[str]:
    """Consume a leading ``--`` separator and guard misplaced flags.

    Only call from exec paths (``exec_vm`` / ``exec_agent``). The shell
    commands accept ``--workspace`` in any position because they don't
    set ``allow_interspersed_args=False`` on the Click context.

    Returns the remote-command tokens the transport should run:

    - If ``command`` begins with a single ``--`` end-of-options
      separator, that ``--`` is consumed and everything after it is
      returned verbatim (a dash-led first token included, a later
      ``--`` preserved). An empty remainder is a missing command.
    - Otherwise, if the first token starts with ``-`` the guard cannot
      tell a misplaced agentworks flag from a dash-led remote command
      that forgot its ``--``; we reject it with a hint naming both
      recoveries (flag before the positional, or the ``--`` separator /
      ``sh -c`` fallback).
    - Otherwise ``command`` is returned unchanged.
    """
    if command and command[0] == "--":
        remainder = command[1:]
        if not remainder:
            raise ValidationError(
                "missing command after '--'",
                entity_kind=kind,
                entity_name=name,
            )
        return remainder

    if command and command[0].startswith("-"):
        raise ValidationError(
            f"remote command cannot start with '-' (got: {command[0]!r})",
            entity_kind=kind,
            entity_name=name,
            hint=(
                f"if {command[0]!r} is an agentworks option, put it before the name "
                f"(e.g. agw {kind} exec {command[0]} ... {name} <cmd>); if it is part "
                f"of the remote command, put '--' before the command so agentworks "
                f"stops reading options (e.g. agw {kind} exec {name} -- {command[0]} ...), "
                f"or wrap it in a shell (e.g. agw {kind} exec {name} sh -c '...')."
            ),
        )

    return command
