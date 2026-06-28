"""Validation helper for the ``exec`` commands.

``agw vm exec`` and ``agw agent exec`` use Click's
``allow_interspersed_args=False`` so an operator can pass through
arguments that look like options (``ls -la``, ``tail -f /path``). The
trade-off is that an agentworks flag (e.g. ``--workspace``) placed
after the VM / agent positional ends up in the passthrough argv rather
than being consumed by Click. sshd then executes
``$SHELL -c '--workspace ws1 pwd'`` (or ``$SHELL -lc '...'`` when the
transport's ``login_shell`` is set), and the remote shell -- both zsh
and bash do this -- parses the script's leading ``-`` as further
shell options, producing cryptic errors:

- zsh: ``no such option: workspace ws1 pwd``
- bash: ``--: invalid option`` (with the usage screen)

We catch this case at the manager layer with a clean
``ValidationError`` whose hint points at the right invocation shape.
"""

from __future__ import annotations

from agentworks.errors import ValidationError


def reject_dash_prefixed_command(
    command: list[str], *, kind: str, name: str,
) -> None:
    """Reject ``exec`` commands whose first token starts with ``-``.

    Only call from exec paths (``exec_vm`` / ``exec_agent``). The shell
    commands accept ``--workspace`` in any position because they don't
    set ``allow_interspersed_args=False`` on the Click context.

    No legitimate remote command starts with ``-`` (files with such
    names are addressable via ``./-name``), so we reject upfront. The
    hint covers the common case -- a misplaced agentworks flag -- and
    operators with a genuine ``-``-prefixed need can read past it.
    """
    if not command or not command[0].startswith("-"):
        return
    raise ValidationError(
        f"remote command cannot start with '-' (got: {command[0]!r})",
        entity_kind=kind,
        entity_name=name,
        hint=(
            "agentworks args must come before the first positional "
            "argument for this command."
        ),
    )
