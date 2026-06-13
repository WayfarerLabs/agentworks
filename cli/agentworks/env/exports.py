"""Shell-prelude helpers.

``build_export_block`` returns a quoted ``export K=v; ...`` string ready to
prepend to any shell command. ``build_prefixed_command`` composes the export
block with a command, transparently handling the empty-env case so call
sites can drop their own conditionals.

Both helpers expect a fully resolved dict[str, str] (no EnvEntry, no
SecretDecl). The SecretResolver's ``render`` method produces this shape;
callers that have only plaintext can pass it directly.
"""

from __future__ import annotations

import shlex


def build_export_block(env: dict[str, str]) -> str:
    """Return ``export K1=v1; export K2=v2; ...`` for ``env``.

    Empty input returns the empty string. Keys are emitted in iteration order
    so the caller controls precedence (effective_env already produced a
    properly merged dict).

    Values are shell-quoted via shlex.quote, so plaintext values with shell
    metacharacters are safe to include.
    """
    if not env:
        return ""
    return "; ".join(f"export {key}={shlex.quote(value)}" for key, value in env.items())


def build_prefixed_command(env: dict[str, str], command: str) -> str:
    """Return ``<exports>; <command>`` or just ``<command>`` for empty env.

    The empty-env case keeps call sites simple: a caller with no env to
    inject does not need to special-case the prefix, just pass an empty
    dict and the original command comes back unchanged.
    """
    block = build_export_block(env)
    if not block:
        return command
    return f"{block}; {command}"
