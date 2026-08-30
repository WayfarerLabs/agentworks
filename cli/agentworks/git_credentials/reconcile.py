"""Atomic target-user reconciliation for Agentworks Git credentials."""

from __future__ import annotations

import base64
import gzip
import io
import tarfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.git_credentials.state import UserCredentialState
    from agentworks.transports import Transport


_LAUNCHER = """#!/bin/sh
# Managed by Agentworks. Holds one credential generation for this request.
parent="$HOME/.agentworks"
root="$HOME/.agentworks/git-credentials"
if [ -L "$parent" ] || [ ! -d "$parent" ]; then
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
fi
exec 7<"$parent" || {
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
}
if ! flock -s -w 10 7; then
    echo "agentworks: Git credential state is busy; retry the Git operation" >&2
    exit 1
fi
if [ -L "$root" ] || [ ! -d "$root" ]; then
    exec 7<&-
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
fi
exec 6<"$root" || {
    exec 7<&-
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
}
if [ -L "$root/lock" ] || [ ! -f "$root/lock" ] || \
        [ "$(stat -c %h -- "$root/lock" 2>/dev/null)" != 1 ]; then
    exec 6<&- 7<&-
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
fi
exec 9<>"$root/lock" || {
    exec 6<&- 7<&-
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
}
if ! flock -s -w 10 9; then
    exec 6<&- 7<&-
    echo "agentworks: Git credential state is busy; retry the Git operation" >&2
    exit 1
fi
if [ -L "$parent" ] || [ ! "$parent" -ef /proc/self/fd/7 ] || \
        [ -L "$root" ] || [ ! "$root" -ef /proc/self/fd/6 ] || \
        [ -L "$root/lock" ] || [ ! "$root/lock" -ef /proc/self/fd/9 ] || \
        [ "$(stat -Lc %h -- /proc/self/fd/9 2>/dev/null)" != 1 ]; then
    exec 9>&- 6<&- 7<&-
    echo "agentworks: Git credential state is unavailable; reinitialize this user" >&2
    exit 1
fi
exec 6<&- 7<&-
if [ ! -x "$root/current/dispatch" ]; then
    exec 9>&-
    echo "agentworks: Git credential state is incomplete; reinitialize this user" >&2
    exit 1
fi
exec env -u BASH_ENV -u ENV -u SHELLOPTS -u BASH_XTRACEFD -u PS4 \
    "$root/current/dispatch" "$@"
"""


def _add_tar_directory(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o700
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    archive.addfile(info)


def _add_tar_file(archive: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    archive.addfile(info, io.BytesIO(content))


def _state_archive(state: UserCredentialState) -> str:
    """Return a deterministic base64 tarball for one private staged state."""
    raw = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        _add_tar_file(archive, "launch", _LAUNCHER.encode(), 0o700)
        _add_tar_directory(archive, "generation")
        _add_tar_file(archive, "generation/config.gitconfig", state.include_content.encode(), 0o600)
        _add_tar_file(archive, "generation/dispatch", state.dispatcher_script.encode(), 0o700)
        if state.stored_credential_files:
            _add_tar_directory(archive, "generation/stored")
            for relative, content in state.stored_credential_files:
                _add_tar_file(archive, f"generation/{relative}", content, 0o600)
        if state.managed_helper_files:
            _add_tar_directory(archive, "generation/helpers")
            for relative, content in state.managed_helper_files:
                _add_tar_file(archive, f"generation/{relative}", content, 0o700)
    return base64.b64encode(raw.getvalue()).decode("ascii")


_RECONCILE_SCRIPT = r"""set -Eeu
trap 'exit 29' ERR
parent="$HOME/.agentworks"
root="$HOME/.agentworks/git-credentials"
stage="$root/.stage"
current_tmp="$root/.current.new"
desired='@DESIRED@'
umask 077
if [ -L "$parent" ] || { [ -e "$parent" ] && [ ! -d "$parent" ]; }; then
    exit 20
fi
mkdir -p "$parent"
exec 7<"$parent"
if ! flock -x -w 30 7; then
    exit 21
fi
if [ -L "$parent" ] || [ ! "$parent" -ef /proc/self/fd/7 ]; then
    exec 7<&-
    exit 20
fi
if [ -L "$root" ] || { [ -e "$root" ] && [ ! -d "$root" ]; }; then
    rm -f "$root"
fi
mkdir -p "$root"
chmod 700 "$root"
exec 6<"$root"
if [ -L "$root/lock" ] || [ -d "$root/lock" ]; then
    rm -rf "$root/lock"
elif [ -e "$root/lock" ] && [ ! -f "$root/lock" ]; then
    rm -f "$root/lock"
elif [ -f "$root/lock" ] && [ "$(stat -c %h -- "$root/lock")" != 1 ]; then
    rm -f "$root/lock"
fi
if [ ! -e "$root/lock" ]; then
    : >"$root/lock"
fi
chmod u+rw "$root/lock"
exec 9<>"$root/lock"
if ! flock -x -w 30 9; then
    exec 6<&- 7<&-
    exit 22
fi
if [ -L "$parent" ] || [ ! "$parent" -ef /proc/self/fd/7 ] || \
        [ -L "$root" ] || [ ! "$root" -ef /proc/self/fd/6 ] || \
        [ -L "$root/lock" ] || [ ! "$root/lock" -ef /proc/self/fd/9 ] || \
        [ "$(stat -Lc %h -- /proc/self/fd/9 2>/dev/null)" != 1 ]; then
    exec 9>&- 6<&- 7<&-
    exit 23
fi
: > /proc/self/fd/9
chmod 600 /proc/self/fd/9
exec 6<&- 7<&-

remove_config_value() {
    key=$1
    value=$2
    if git config --global --fixed-value --unset-all "$key" "$value" >/dev/null 2>&1; then
        return 0
    else
        rc=$?
        [ "$rc" -eq 5 ] || exit 25
    fi
}

remove_owned_path() {
    path=$1
    if [ -L "$path" ] || [ -d "$path" ]; then
        rm -rf -- "$path"
    else
        rm -f -- "$path"
    fi
}

# The released store is owned only when its exact Agentworks helper
# registration is present at the start of this reconciliation. Record that
# witness before cleanup because removing an absent config value succeeds.
legacy_store_owned=0
if git config --global --fixed-value --get-all credential.helper \
        '!~/.agentworks-git-cred-helper.sh' >/dev/null 2>&1; then
    legacy_store_owned=1
else
    rc=$?
    [ "$rc" -eq 1 ] || exit 25
fi

# Clean abandoned staging and disable every known Agentworks registration
# before changing material. A failure after this point leaves authentication
# absent, never an older credential newly reachable.
rm -rf "$stage" "$current_tmp"
if [ "$legacy_store_owned" -eq 1 ]; then
    remove_owned_path "$HOME/.git-credentials"
fi
remove_owned_path "$HOME/.agentworks-git-cred-helper.sh"
remove_owned_path "$HOME/.agentworks-git-scopes.gitconfig"
remove_owned_path "$HOME/.agentworks-git-cred-warn.sh"
remove_config_value credential.helper '!~/.agentworks-git-cred-helper.sh'
remove_config_value include.path '~/.agentworks-git-scopes.gitconfig'
remove_config_value include.path '~/.agentworks/git-credentials/current/config.gitconfig'

if [ "$desired" = "empty" ]; then
    rm -rf "$root/launch" "$root/current"
    rm -rf "$root/generations" "$stage"
    exit 0
fi

# Normalize the two exact Agentworks-owned activation paths before comparing
# or replacing them. Never follow a persisted symlink outside this root, and
# never let mv reinterpret an owned directory as a destination container.
if [ -L "$root/launch" ] || [ -d "$root/launch" ]; then
    rm -rf "$root/launch"
elif [ -e "$root/launch" ] && [ ! -f "$root/launch" ]; then
    rm -f "$root/launch"
fi
current_target=$(readlink "$root/current" 2>/dev/null || true)
case "$current_target" in
    generations/generation.??????)
        current_suffix=${current_target#generations/generation.}
        case "$current_suffix" in
            *[!A-Za-z0-9]*) rm -rf "$root/current" ;;
            *)
                if [ -L "$root/$current_target" ] || [ ! -d "$root/$current_target" ]; then
                    rm -rf "$root/current"
                fi
                ;;
        esac
        ;;
    *) rm -rf "$root/current" ;;
esac
if [ -L "$root/generations" ] || { [ -e "$root/generations" ] && [ ! -d "$root/generations" ]; }; then
    rm -rf "$root/generations"
fi

mkdir -p "$stage" "$root/generations"
chmod 700 "$stage" "$root/generations"
if ! base64 -d | tar -xzf - -C "$stage"; then
    rm -rf "$stage"
    exit 24
fi

if [ ! -f "$root/launch" ] || ! cmp -s "$stage/launch" "$root/launch"; then
    mv -Tf "$stage/launch" "$root/launch"
    chmod 700 "$root/launch"
else
    rm -f "$stage/launch"
fi

changed=1
if [ -d "$root/current" ] && diff -qr "$stage/generation" "$root/current" >/dev/null 2>&1; then
    changed=0
fi
if [ "$changed" -eq 1 ]; then
    generation=$(mktemp -d "$root/generations/generation.XXXXXX")
    rmdir "$generation"
    mv "$stage/generation" "$generation"
    chmod 700 "$generation"
    ln -s "generations/${generation##*/}" "$current_tmp"
    mv -Tf "$current_tmp" "$root/current"
else
    rm -rf "$stage/generation"
fi
rm -rf "$stage"

git config --global --add include.path '~/.agentworks/git-credentials/current/config.gitconfig' || exit 25

active=$(readlink -f "$root/current")
for candidate in "$root/generations"/*; do
    [ -d "$candidate" ] || continue
    [ "$candidate" = "$active" ] || rm -rf "$candidate"
done
"""


_RECONCILE_FAILURES = {
    20: "Git credential state could not be established safely; reinitialize this user after repairing ~/.agentworks",
    21: "timed out establishing Git credential state; retry user initialization",
    22: "timed out reconciling Git credential state; retry user initialization",
    23: "Git credential lock identity changed; retry user initialization",
    24: "the staged Git credential state was rejected; retry user initialization",
    25: (
        "Git credential configuration could not be updated; "
        "inspect this user's global Git config and retry initialization"
    ),
    29: "Git credential state could not be reconciled; retry user initialization",
}


def reconcile_user_git_credentials(target: Transport, state: UserCredentialState) -> None:
    """Converge all Agentworks-owned Git credential state for one user."""
    from agentworks.ssh import SSHError

    desired = "present" if state.has_credentials else "empty"
    payload = _state_archive(state) if state.has_credentials else ""
    try:
        result = target.run(
            _RECONCILE_SCRIPT.replace("@DESIRED@", desired),
            input_text=payload,
            timeout=90,
            check=False,
        )
    except SSHError:
        raise SSHError("Git credential reconciliation transport failed; retry user initialization") from None
    if not result.ok:
        message = _RECONCILE_FAILURES.get(
            result.returncode,
            "Git credential reconciliation failed unexpectedly; retry user initialization",
        )
        raise SSHError(message) from None
