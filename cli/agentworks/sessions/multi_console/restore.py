"""Reconcile a console session window's live tmux state against its
configured shell list.

``_prepare_vm_target_for_attach``, ``_console_tmux_exists``,
``_restore_session_secret_targets``, and ``_add_session_window`` are
monkeypatched by tests directly on the ``agentworks.sessions.multi_console``
package object (so a test can drive ``restore_session`` against a fake
target without a live VM). A patch on the package object only rebinds the
package's own attribute, not the attribute of the module that actually
defines the function, so every call site below goes through the package
object at call time (``_mc.<name>(...)``) rather than a direct reference.
"""

from __future__ import annotations

import shlex
from collections import Counter
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.errors import ExternalError, NotFoundError, StateError
from agentworks.resources.access import named_console_template
from agentworks.sessions.multi_console_layout import (
    _apply_layout,
    _focus_session_pane,
    _list_shell_panes,
    _reorder_shell_panes,
)

from ._helpers import _require_console, tmux_session_name
from .attach import _session_linux_user
from .tmux_build import PreserveEnvMemo, _resolve_workspace_path, _split_shell_pane

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database


def restore_session(
    db: Database,
    config: Config,
    *,
    console_name: str,
    session_name: str,
) -> None:
    """Reconcile a single session window's live tmux state against its configured
    shell list. Additive only: rebuilds the window if missing, fills in any
    panes the user accidentally killed (in their correct config positions), but
    refuses to remove panes if the user has more live than configured.

    Strict on untagged panes: a window built before pane-tagging was added has
    no way to determine which configured shell is missing, so we refuse and
    direct the operator to `attach --recreate`.
    """
    from agentworks.bootstrap import build_registry

    console = _require_console(db, console_name)
    registry = build_registry(config)
    member = db.get_console_session(console_name, session_name)
    if member is None:
        raise NotFoundError(
            f"session '{session_name}' is not a member of console '{console_name}'",
            entity_kind="console-member",
            entity_name=session_name,
        )

    # restore_session raises StateError/ExternalError on failure, so it's
    # not a best-effort op (those are exempted from the keepalive sweep by
    # base.VMPlatform.vm_active's docstring). The gate's held-active span
    # wraps the SSH-heavy body so a freshly booted WSL2 distro doesn't
    # idle out between the window probe and the pane reconciliation.
    with _mc._prepare_vm_target_for_attach(db, config, console.vm_name, registry=registry) as (vm, target):
        if not _mc._console_tmux_exists(target, console_name):
            raise StateError(
                f"console '{console_name}' has no live tmux session on VM '{console.vm_name}'.",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name}` to build it; "
                    f"restore-session only repairs an already-running console."
                ),
            )

        q_con = shlex.quote(tmux_session_name(console_name))
        q_win = shlex.quote(session_name)
        layout = named_console_template(registry).tmux_layout
        configured_count = len(member.shells)

        # Window present?
        res = target.run(
            f"tmux list-windows -t {q_con} -F '#{{window_name}}'",
            check=False,
        )
        if not res.ok:
            raise ExternalError(
                f"failed to list windows for console '{console_name}': {res.stderr.strip()}",
                entity_kind="console",
                entity_name=console_name,
            )
        windows = res.stdout.strip().splitlines()
        if session_name not in windows:
            output.info(f"window '{session_name}' is missing; rebuilding from config...")
            # Eager-prompting orchestration:
            # the window-rebuild path also opens new shells (one per
            # configured shell entry, via _add_session_window ->
            # _split_shell_pane). Resolve every referenced secret BEFORE
            # any pane is opened. Targets cover ALL configured shells in
            # this case (the window is missing, so every pane is new).
            # Conditional-need exception to the one-boundary-resolve
            # contract: whether the window is missing is only knowable
            # from live tmux state, post-bind (same class as the
            # Tailscale rejoin).
            from agentworks.secrets import resolve_for_command

            all_indices = list(range(configured_count))
            secret_values: dict[str, str] = {}
            if all_indices:
                secret_values = resolve_for_command(
                    _mc._restore_session_secret_targets(
                        db,
                        registry,
                        vm=vm,
                        member=member,
                        indices=all_indices,
                    ),
                    config,
                    registry,
                )
            _mc._add_session_window(
                target,
                db,
                registry,
                values=secret_values,
                console_name=console_name,
                member=member,
                vm=vm,
                layout=layout,
                preserve_memo={},
            )
            output.result(f"Rebuilt window '{session_name}' in console '{console_name}'.")
            return

        # Window exists. Enumerate shell panes (skipping pane_index 0, the session
        # pane). The session pane is created via tmux new-window and intentionally
        # left untagged; every shell pane is created via _split_shell_pane and
        # gets an @agentworks-shell-index tag.
        shell_panes = _list_shell_panes(target, q_con, q_win)
        if shell_panes is None:
            raise ExternalError(
                f"failed to list panes for window '{session_name}'",
                entity_kind="console",
                entity_name=console_name,
            )

        untagged = [pid for pid, _pidx, cidx in shell_panes if cidx is None]
        if untagged:
            # Untagged shell panes happen for two reasons: (a) the window predates
            # the pane-tagging feature, or (b) the operator manually split a pane
            # via `tmux split-window` instead of `console add-shell`. Either way,
            # restore-session can't map the live pane back to a configured shell
            # index, so we refuse and direct the operator to rebuild.
            raise StateError(
                f"window '{session_name}' has {len(untagged)} shell pane(s) with no agentworks tag.",
                entity_kind="console",
                entity_name=console_name,
                hint=(f"Run `agw console attach {console_name} --recreate` to rebuild and retag from scratch."),
            )

        # Validate that the tag values form a subset of 0..configured_count-1 with
        # no duplicates. Three corruptions are caught here, all of which restore-
        # session can't safely repair:
        #   - duplicates: two panes claim the same config index
        #   - out-of-range: a pane references a config index that no longer exists
        #     (e.g. config shrank or DB was edited)
        #   - implied "too many panes": pigeonhole says any live_count >
        #     configured_count must trigger one of the two above (since untagged
        #     panes are already rejected by the strict check earlier)
        tag_values = [cidx for _pid, _pidx, cidx in shell_panes if cidx is not None]
        # Single-pass O(n) duplicate + out-of-range detection. The naive
        # tag_values.count(v) in a comprehension would be O(n^2); not a concern at
        # typical shell counts (1-5) but free to do correctly.
        counts = Counter(tag_values)
        duplicates = sorted(v for v, n in counts.items() if n > 1)
        out_of_range = sorted(v for v in counts if v < 0 or v >= configured_count)
        if duplicates or out_of_range:
            parts: list[str] = []
            if duplicates:
                parts.append(f"duplicate tags {duplicates}")
            if out_of_range:
                if configured_count == 0:
                    parts.append(f"{len(out_of_range)} tagged shell pane(s) but session has no configured shells")
                else:
                    parts.append(f"tags {out_of_range} point past the configured range (0..{configured_count - 1})")
            raise StateError(
                f"window '{session_name}' has shell panes with inconsistent tags ({'; '.join(parts)}).",
                entity_kind="console",
                entity_name=console_name,
                hint=(f"Run `agw console attach {console_name} --recreate` to rebuild and retag from scratch."),
            )

        # tag_values is now a subset of 0..configured_count-1 with no duplicates,
        # so len(tag_values) <= configured_count.
        if len(tag_values) == configured_count:
            output.info(
                f"session '{session_name}' already matches config ({len(tag_values)} shell pane(s)); nothing to do."
            )
            # Still focus the session pane on this no-op path so post-restore
            # landing focus is consistent whether or not repairs were needed.
            _focus_session_pane(target, q_con, q_win)
            return

        # Strict subset: figure out which config indices are missing.
        missing = sorted(set(range(configured_count)) - set(tag_values))

        session = db.get_session(session_name)
        if session is None:
            raise StateError(
                f"session '{session_name}' no longer exists in the database",
                entity_kind="session",
                entity_name=session_name,
                hint="Remove the session from the console first.",
            )
        workspace_path = _resolve_workspace_path(db, session)
        if workspace_path is None:
            raise StateError(
                f"workspace for session '{session_name}' is missing; cannot restore.",
                entity_kind="session",
                entity_name=session_name,
            )
        session_user = _session_linux_user(db, session, vm)

        # Eager-prompting orchestration: restore_session
        # opens new shells for the missing pane indices. Conditional-need
        # exception to the one-boundary-resolve contract: which panes are
        # missing is only knowable from live tmux state, post-bind.
        # Resolve secrets
        # NOW -- after all the validation guards (untagged-panes /
        # duplicate-tags / out-of-range / "already matches config" no-op)
        # so an operator with a tag-corruption gets the actionable
        # validation error instead of being prompted for credentials they
        # would never end up using. Targets are scoped precisely to the
        # missing config indices (not all configured shells) so non-
        # interactive runs only fail on secrets that actually would be
        # consumed.
        from agentworks.secrets import resolve_for_command

        secret_values = resolve_for_command(
            _mc._restore_session_secret_targets(
                db,
                registry,
                vm=vm,
                member=member,
                indices=missing,
            ),
            config,
            registry,
        )

        output.info(f"Restoring {len(missing)} shell pane(s) in '{session_name}': config indices {missing}.")
        # Collect each split's outcome so a partial failure becomes a loud error
        # rather than a silent exit-0 leaving panes missing or untagged.
        failed: list[int] = []
        preserve_memo: PreserveEnvMemo = {}
        for cidx in missing:
            pane_id = _split_shell_pane(
                target,
                db,
                registry,
                values=secret_values,
                console_name=console_name,
                window_name=session_name,
                workspace_path=workspace_path,
                shell=member.shells[cidx],
                session=session,
                vm=vm,
                session_user=session_user,
                admin_user=vm.admin_username,
                config_index=cidx,
                preserve_memo=preserve_memo,
            )
            if pane_id is None:
                failed.append(cidx)
        if failed:
            raise ExternalError(
                f"restore-session left '{session_name}' incomplete: failed to "
                f"create/tag config indices {failed} (see warnings above).",
                entity_kind="console",
                entity_name=console_name,
                hint=(f"Run `agw console attach {console_name} --recreate` to rebuild from scratch."),
            )

        # New panes land at the tail; reorder so visual pane_index matches
        # config_index for every shell pane.
        _reorder_shell_panes(target, q_con, q_win, configured_count)

        # Re-apply the layout to redistribute geometry after the splits and
        # swaps, then land the operator on the session pane (matches attach /
        # recreate behavior; restore-session is a repair, not an attach, but
        # we still want consistent landing focus).
        _apply_layout(target, q_con, q_win, layout)
        _focus_session_pane(target, q_con, q_win)
        output.result(f"Restored {output.count(len(missing), 'shell pane')} in '{session_name}'.")
