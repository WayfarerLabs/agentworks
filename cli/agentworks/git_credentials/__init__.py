"""Git credential providers.

Each provider implementation (``GitHubCredentialProvider``,
``AzDOCredentialProvider``) is the code-side handle for one
``[git_credentials.<name>].provider = "..."`` value (``type`` is the
accepted legacy alias). The framework's ``git-credential-provider``
kind (Phase 2b.1) holds one row per known provider so a typo in the
operator's ``provider`` field surfaces as a clean miss-policy error at
``build_registry`` time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.git_credentials.azdo import AzDOCredentialProvider
from agentworks.git_credentials.github import GitHubCredentialProvider

if TYPE_CHECKING:
    from agentworks.git_credentials.base import GitCredentialProvider, HelperEntry
    from agentworks.resources import Registry


@dataclass(frozen=True)
class CredentialMaterials:
    """Everything the initialization flow writes for git auth.

    ``store_content`` is the full ``~/.git-credentials`` body.
    ``gitconfig_content`` is the body of the agentworks-owned gitconfig
    include file (credential-context sections selecting per-credential
    usernames); present even when empty so re-initialization after
    removing scopes is idempotent (while at least one credential remains).
    """

    store_content: str
    gitconfig_content: str
    # THE git credential helper (POSIX sh) -- replaces git's
    # ``credential-store`` in the chain. ``get`` serves from the
    # agentworks-managed store file; ``erase`` never deletes (git
    # invokes it after a rejected auth, which is exactly when the
    # operator needs a diagnosis, not state destruction -- with
    # credential-store, every failed auth silently deleted the
    # provisioned line); a foreign embedded username on github.com
    # draws a scoping warning when scoped credentials exist.
    helper_script: str


# The credential helper's on-VM path, registered as "!<path>" in the
# user's global ``credential.helper`` slot (replacing the old
# ``store``): git only shell-executes helper values starting with "!"
# or an absolute path, and the shell is what expands the tilde --
# per-user, which is what makes the same content work for admin and
# agents. The old warn-only script (~/.agentworks-git-cred-warn.sh) is
# orphaned harmlessly on VMs initialized by earlier builds.
GIT_CRED_HELPER_PATH = "~/.agentworks-git-cred-helper.sh"

# The agentworks-owned gitconfig include carrying the credential-context
# sections. Referenced from the user's global gitconfig via include.path
# (tilde-literal, expanded per-user by git); overwritten wholesale on
# every init, so removing scopes degrades cleanly while at least one
# credential remains (removing ALL credentials leaves both files stale
# -- a pre-existing gap shared with the store itself).
GIT_SCOPES_INCLUDE_PATH = "~/.agentworks-git-scopes.gitconfig"



def build_credential_materials(
    providers: dict[str, GitCredentialProvider],
    tokens: dict[str, str],
) -> CredentialMaterials:
    """Assemble the git credential store, the gitconfig include, and
    the selecting helper.

    Selection lives entirely in the generated helper: the include sets
    ``useHttpPath`` (safe -- credential-store left the chain, and only
    our helper consumes the queries), so every query carries the remote
    path and the helper picks the most specific credential (exact repo,
    then owner, then the host's default, then the first store line for
    the host -- the legacy semantics that keep ``add-git-credential``
    additions serving).

    Store ordering: unscoped credentials' lines first, so the legacy
    first-host-line fallback finds the default. Scope collisions (two
    credentials claiming the same repo or owner on one host) are a hard
    error -- silent shadowing is dead-config ambiguity.
    """
    store_scoped: list[str] = []
    store_unscoped: list[str] = []
    entries: list[tuple[str, HelperEntry]] = []
    claimed: dict[tuple[str, str, str], str] = {}
    for name, provider in providers.items():
        entry = provider.helper_entry()
        for kind_, value in (
            *(("repo", repo) for repo in entry.repos),
            *((("owner", entry.owner),) if entry.owner else ()),
        ):
            key = (entry.host, kind_, value)
            if key in claimed:
                raise ConfigError(
                    f"git credentials {claimed[key]!r} and {name!r} both "
                    f"claim scope {kind_} {value!r} on {entry.host}; "
                    f"scopes must be unambiguous"
                )
            claimed[key] = name
        entries.append((name, entry))
        lines = provider.credential_lines(tokens[name])
        if entry.repos or entry.owner:
            store_scoped.extend(lines)
        else:
            store_unscoped.extend(lines)

    header = "# Managed by agentworks (git credential selection); do not edit.\n"
    diag = [
        (provider.store_username, name, provider.secret_name)
        for name, provider in providers.items()
    ]
    return CredentialMaterials(
        store_content="\n".join(store_unscoped + store_scoped) + "\n",
        # useHttpPath makes git send the remote path to the helper --
        # the whole selection mechanism. Harmless globally now that our
        # helper is the only consumer (the old hazard was
        # credential-store's path matching, and store is gone).
        gitconfig_content=header + "[credential]\n\tuseHttpPath = true\n",
        helper_script=_helper_script(entries, diag),
    )


def _selection_block(entries: list[tuple[str, HelperEntry]]) -> str:
    """The per-host selection: exact repo, then owner, then default."""
    by_host: dict[str, list[tuple[str, HelperEntry]]] = {}
    for name, entry in entries:
        by_host.setdefault(entry.host, []).append((name, entry))
    host_cases = []
    for host, host_entries in sorted(by_host.items()):
        lines = [f"    {host})"]
        repo_cases = [
            '        case "$1" in\n'
            + "\n".join(
                f'        {"|".join(entry.repos)}) echo {entry.username}; return ;;'
                for _n, entry in host_entries
                if entry.repos
            )
            + "\n        esac"
        ] if any(entry.repos for _n, entry in host_entries) else []
        owner_cases = [
            '        case "${1%%/*}" in\n'
            + "\n".join(
                f"        {entry.owner}) echo {entry.username}; return ;;"
                for _n, entry in host_entries
                if entry.owner
            )
            + "\n        esac"
        ] if any(entry.owner for _n, entry in host_entries) else []
        default = next(
            (
                entry.username
                for _n, entry in host_entries
                if not entry.repos and not entry.owner
            ),
            "x-access-token" if host == "github.com" else None,
        )
        lines.extend(repo_cases)
        lines.extend(owner_cases)
        if default:
            lines.append(f"        echo {default}")
        lines.append("        ;;")
        host_cases.append("\n".join(lines))
    return "\n".join(host_cases)


def _helper_script(
    entries: list[tuple[str, HelperEntry]],
    diag: list[tuple[str, str, str]],
) -> str:
    """Render THE git credential helper (POSIX sh).

    ``get``: an explicit query username short-circuits to a direct
    line match (plus the bypasses-scoping warning when scoped
    credentials exist); otherwise selection runs on (host, path) --
    normalized by stripping the leading slash and a ``.git`` suffix --
    and the chosen username keys back into the store file; if nothing
    matches, the first store line for the host is served (legacy
    credential-store semantics). ``erase`` deletes nothing -- it fires
    when the remote rejected the credential, so it prints a diagnosis
    naming the credential and its secret. ``store`` is a no-op.
    """
    known = " ".join(sorted({u for u, _c, _s in diag}))
    scoped = any(e.repos or e.owner for _n, e in entries)
    scoped_hosts = "|".join(
        sorted({e.host for _n, e in entries if e.repos or e.owner})
    )
    cases = []
    seen: set[str] = set()
    for username, cred, secret in diag:
        if username in seen:
            continue  # first credential wins, matching store-line order
        seen.add(username)
        cases.append(
            f"""        {username})
            echo "agentworks: the remote rejected git credential '{cred}'."
            echo "The token in secret '{secret}' is likely invalid, expired, or lacks access."
            echo "Fix the secret, then re-run 'agw agent reinit <agent>' or 'agw vm reinit <vm>'."
            ;;"""
        )
    warn_block = ""
    if scoped:
        warn_block = """
    if [ "$host" = "github.com" ] && [ -n "$username" ]; then
        case " @KNOWN@ " in
            *" $username "*) : ;;
            *)
                {
                    echo "agentworks: this remote embeds username '$username', which"
                    echo "bypasses git credential scoping for github.com; use a plain"
                    echo "https remote (scoping selects the credential automatically)"
                } >&2
                ;;
        esac
    fi""".replace("@KNOWN@", known)
    template = """#!/bin/sh
# Managed by agentworks (git credential helper); do not edit.
# Serves the agentworks-owned ~/.git-credentials and never deletes it:
# git invokes 'erase' after a rejected auth, which is exactly when the
# operator needs a diagnosis, not state destruction.
op="$1"
proto=""; host=""; username=""; qpath=""
while IFS='=' read -r key value; do
    case "$key" in
        protocol) proto="$value" ;;
        host) host="$value" ;;
        username) username="$value" ;;
        path) qpath="$value" ;;
    esac
done

creds="$HOME/.git-credentials"

# Most-specific credential for (host, path): exact repo, then owner
# (first path segment), then the host default.
select_username() {
    case "$host" in
@SELECT@
    esac
}

serve() {
    # $1 = required username ("" = first line for the host)
    [ -r "$creds" ] || return 1
    while IFS= read -r line; do
        case "$line" in *://*@*) : ;; *) continue ;; esac
        rest="${line#*://}"
        userinfo="${rest%%@*}"
        hostpath="${rest#*@}"
        [ "${hostpath%%/*}" = "$host" ] || continue
        luser="${userinfo%%:*}"
        if [ -n "$1" ] && [ "$luser" != "$1" ]; then
            continue
        fi
        printf 'protocol=%s\n' "${proto:-https}"
        printf 'host=%s\n' "$host"
        printf 'username=%s\n' "$luser"
        printf 'password=%s\n' "${userinfo#*:}"
        return 0
    done < "$creds"
    return 1
}

case "$op" in
get)@WARN@
    if [ -n "$username" ]; then
        serve "$username"
        exit 0
    fi@NOPATH@
    p="${qpath#/}"
    p="${p%.git}"
    wanted=$(select_username "$p")
    if [ -n "$wanted" ] && serve "$wanted"; then
        exit 0
    fi
    serve ""
    exit 0
    ;;
erase)
    # git calls erase when the remote rejected the credential.
    case "$username" in
@CASES@
        *) : ;;
    esac >&2
    exit 0
    ;;
*)
    exit 0
    ;;
esac
"""
    nopath_block = ""
    if scoped_hosts:
        nopath_block = """
    if [ -z "$qpath" ]; then
        case "$host" in
        @SCOPED_HOSTS@)
            {
                echo "agentworks: git sent no repository path for $host, so"
                echo "credential scoping cannot select per-repo/per-owner; check that"
                echo "credential.useHttpPath is still true (agentworks sets it, but a"
                echo "local git config may override it); serving the host default"
            } >&2
            ;;
        esac
    fi""".replace("@SCOPED_HOSTS@", scoped_hosts)
    return (
        template.replace("@WARN@", warn_block)
        .replace("@NOPATH@", nopath_block)
        .replace("@SELECT@", _selection_block(entries))
        .replace("@CASES@", "\n".join(cases) if cases else "        _none_) : ;;")
    )


# The capability registry (the canonical provider list): provider name
# -> implementation class. ``validate_config`` (blob validation +
# implied references) is invoked through this dict at each source's
# blob boundary and at finalize; descriptor rows publish from it.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
    "azdo": AzDOCredentialProvider,
    "github": GitHubCredentialProvider,
}


def publish_to(registry: Registry) -> None:
    """Publish the known git credential provider types into the registry.

    Each entry lands as a ``GitCredentialProviderEntry`` row, built-in
    with source ``"agentworks.git_credentials"``. Phase 2b.1.

    Unlike the catalog kinds, this kind has no
    operator-override path today: ``Config.publish_to`` publishes
    ``git_credentials`` entries (the per-credential config), not
    ``git-credential-provider`` rows. The kind is read-only from the
    operator's perspective; a future SDD that wants to let operators
    register new provider types would add an operator-publish path.
    """
    from agentworks.git_credentials.kinds import (
        GitCredentialProviderEntry,
    )
    from agentworks.resources import Origin

    code_origin = Origin.built_in(source="agentworks.git_credentials")
    for type_name in sorted(GIT_CREDENTIAL_PROVIDER_REGISTRY):
        registry.add(
            "git-credential-provider",
            type_name,
            GitCredentialProviderEntry(name=type_name),
            code_origin,
        )
