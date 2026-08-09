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
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy
from agentworks.sessions.multi_console_layout import (
    _apply_layout,
    _focus_session_pane,
    _list_panes_with_tags,
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
    interaction: InteractionPolicy,
) -> None:
    """Reconcile a single session window's live tmux state against its configured
    shell list. Additive only: it rebuilds the window if it is gone entirely and
    fills in any shell panes the operator accidentally killed (each back in its
    configured position), but it never destroys a live pane or window. Where the
    only repair would be destructive, it raises and points the operator at
    `attach --recreate` so the call is theirs. Three states take that path:

    - More live panes than configured: removing one is not ours to choose.
    - Shell panes that can't be mapped back to the config (untagged, duplicated,
      or out of range). Untagged panes come from windows built before
      pane-tagging existed, or from a hand-run `tmux split-window`.
    - A window whose session-attach pane was killed. tmux renumbers the
      remaining panes when one dies, so a shell ends up in the lowest slot and
      the console shows a shell where the session should be. The attach pane
      cannot be reinserted additively, and recreating the window would take
      every live shell pane in it with it (plus, for a single-member console,
      the console's last window, and with it the whole tmux session).
    """
    interaction = validate_interaction_policy(interaction)
    from agentworks.bootstrap import load_request_registry

    console = _require_console(db, console_name)
    registry = load_request_registry(config)
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
    with _mc._prepare_vm_target_for_attach(
        db,
        config,
        console.vm_name,
        registry=registry,
        interaction=interaction,
    ) as (vm, target):
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
            # Nothing of the operator's is live for this session, so rebuilding
            # the window from config destroys nothing. This is the only path
            # that (re)creates a window; a window that exists but is broken is
            # never torn down here (see the session-pane check below).
            #
            # Check the session and its workspace up front: _add_session_window
            # only warns and skips when either is gone, so without these the
            # operator would get a generic "failed to rebuild" instead of the
            # specific reason. Mirrors the additive path's checks further down.
            session = db.get_session(session_name)
            if session is None:
                raise StateError(
                    f"session '{session_name}' no longer exists in the database",
                    entity_kind="session",
                    entity_name=session_name,
                    hint="Remove the session from the console first.",
                )
            if _resolve_workspace_path(db, session) is None:
                raise StateError(
                    f"workspace for session '{session_name}' is missing; cannot restore.",
                    entity_kind="session",
                    entity_name=session_name,
                )

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
                    interaction=interaction,
                )
            result = _mc._add_session_window(
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
            if not result.built:
                # _add_session_window warns and skips rather than raising, so
                # without this check restore-session would print "Rebuilt
                # window ..." and exit 0 over a window that was never built.
                raise ExternalError(
                    f"failed to rebuild window '{session_name}' in console '{console_name}' (see warnings above).",
                    entity_kind="console",
                    entity_name=console_name,
                    hint=(f"Run `agw console attach {console_name} --recreate` to rebuild the console from scratch."),
                )
            if result.failed_shells:
                # The window is up but a shell pane failed to split or, worse,
                # split without getting its @agentworks-shell-index tag. An
                # untagged pane would make the next restore-session hit the
                # untagged-pane refusal, converting a repairable window into a
                # --recreate-only one. Escalate now, symmetric with the additive
                # repair path below, rather than reporting a clean rebuild.
                raise ExternalError(
                    f"restore-session rebuilt window '{session_name}' but failed to "
                    f"create/tag config indices {result.failed_shells} (see warnings above).",
                    entity_kind="console",
                    entity_name=console_name,
                    hint=(f"Run `agw console attach {console_name} --recreate` to rebuild from scratch."),
                )
            output.result(f"Rebuilt window '{session_name}' in console '{console_name}'.")
            return

        # The window exists. Its session-attach pane is the lowest-indexed pane:
        # `tmux new-window` creates it first and leaves it untagged, and every
        # shell pane added afterwards by _split_shell_pane carries an
        # @agentworks-shell-index tag. We key off the lowest live index rather
        # than a literal 0 because `pane-base-index 1` in the admin user's
        # ~/.tmux.conf makes the first pane report index 1 (the console tmux
        # server is started without -f, so it inherits that config).
        all_panes = _list_panes_with_tags(target, q_con, q_win)
        if all_panes is None:
            raise ExternalError(
                f"failed to list panes for window '{session_name}'",
                entity_kind="console",
                entity_name=console_name,
            )
        if not all_panes:
            # A live window always has at least one pane, so an empty list means
            # tmux told us nothing we could parse; we can't tell a healthy
            # window from a broken one.
            raise ExternalError(
                f"tmux listed no panes for window '{session_name}'",
                entity_kind="console",
                entity_name=console_name,
            )
        base_pidx = min(pidx for _pid, pidx, _cidx in all_panes)
        session_pane = next(p for p in all_panes if p[1] == base_pidx)
        if session_pane[2] is not None:
            # The lowest-indexed pane carries a shell tag, so the session-attach
            # pane was killed and tmux renumbered a shell into its slot: the
            # console now shows a shell where the session should be. Repairing
            # this means recreating the window, which would destroy every live
            # shell pane in it (and, for a single-member console, the console's
            # last window, which takes the whole tmux session with it). That is
            # the operator's call to make, not ours.
            raise StateError(
                f"window '{session_name}' has lost its session-attach pane "
                f"(pane {base_pidx} is a shell pane, not the session).",
                entity_kind="console",
                entity_name=console_name,
                hint=(
                    f"Run `agw console attach {console_name} --recreate` to rebuild the console; "
                    f"restore-session is additive and will not kill the live panes in this window."
                ),
            )

        # Shell panes are every pane except the session pane.
        shell_panes = [p for p in all_panes if p[1] != base_pidx]

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
            _focus_session_pane(target, q_con, q_win, base_pidx)
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
            interaction=interaction,
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
        _focus_session_pane(target, q_con, q_win, base_pidx)
        output.result(f"Restored {output.count(len(missing), 'shell pane')} in '{session_name}'.")
