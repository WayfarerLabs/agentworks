"""Publish a config file an owner shares with no one, or with its workspace group."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution.errors import FileConflict

if TYPE_CHECKING:
    from agentworks.execution.files import Files


def publish_config(
    files: Files,
    path: str,
    content: bytes,
    *,
    observed: bytes | None,
    group: str | None,
    operation: str,
    preserve_metadata: bool = False,
) -> bool:
    """Publish ``content`` over ``observed``, what the caller read at ``path``; whether it wrote.

    Native settings, native MCP entries and Git exclusions share one mode
    rule. Without a group (``None`` or empty) a new file is 0600 under 0700
    parents; with one it is 0660 under setgid 2770 parents, in that group.
    ``preserve_metadata`` keeps an existing file's owner, group, mode and
    extended attributes instead. Content equal to ``observed`` writes
    nothing. A file changed since the read is ``FileConflict`` naming
    ``operation``, and nothing is written.
    """
    group = group or None
    try:
        return files.update(
            path,
            content,
            observed=observed,
            mode=0o600 if group is None else 0o660,
            parent_mode=0o700 if group is None else 0o2770,
            group=group,
            preserve_metadata=preserve_metadata,
        )
    except FileConflict as conflict:
        raise FileConflict(
            conflict.vm_name,
            operation,
            path,
            hint="Another writer changed the file between this operation's read and its write; rerun it.",
        ) from None
