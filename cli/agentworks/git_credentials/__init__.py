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
    from agentworks.git_credentials.base import GitCredentialProvider
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
    """Assemble the git credential store and context sections.

    Ordering contract (empirically pinned, git 2.39): UNSCOPED
    credentials' store lines come first -- a username-less query takes
    the first matching line, so the host-level fallback must precede
    username-tagged scoped lines; scoped queries carry the
    context-injected username, which filters lines, so their relative
    order is irrelevant.

    Scope collisions (two credentials claiming the same context URL)
    are a hard error: git would silently let the later section win,
    which is exactly the dead-config ambiguity we reject loudly.
    """
    store_scoped: list[str] = []
    store_unscoped: list[str] = []
    sections: list[tuple[str, str]] = []
    claimed: dict[str, str] = {}
    for name, provider in providers.items():
        provider_sections = provider.gitconfig_sections()
        for url, _username in provider_sections:
            if url in claimed:
                raise ConfigError(
                    f"git credentials {claimed[url]!r} and {name!r} both "
                    f"claim scope {url}; scopes must be unambiguous"
                )
            claimed[url] = name
        lines = provider.credential_lines(tokens[name])
        if provider_sections:
            sections.extend(provider_sections)
            store_scoped.extend(lines)
        else:
            store_unscoped.extend(lines)

    rendered = [
        f'[credential "{url}"]\n\tusername = {username}'
        for url, username in sections
    ]
    header = (
        "# Managed by agentworks (git credential scoping); do not edit.\n"
    )
    diag = [
        (provider.store_username, name, provider.secret_name)
        for name, provider in providers.items()
    ]
    return CredentialMaterials(
        store_content="\n".join(store_unscoped + store_scoped) + "\n",
        gitconfig_content=header + "\n".join(rendered) + ("\n" if rendered else ""),
        helper_script=_helper_script(diag, warn_foreign=bool(sections)),
    )



def _helper_script(
    diag: list[tuple[str, str, str]], *, warn_foreign: bool
) -> str:
    """Render THE git credential helper (POSIX sh).

    ``get`` serves the first matching line (host + username-if-given)
    from the agentworks-managed store file -- credential-store's GET
    semantics, minus its erase. ``erase`` deliberately deletes nothing:
    git invokes it when the remote REJECTED the credential, so it
    prints a diagnosis naming the credential and its secret instead of
    destroying provisioned state. ``diag`` is (store_username,
    credential_name, secret_name) per credential; ``warn_foreign``
    gates the embedded-username warning on scoped credentials existing
    (without scoping there is nothing to bypass).
    """
    known = " ".join(sorted({u for u, _c, _s in diag}))
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
    if warn_foreign:
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
proto=""; host=""; username=""
while IFS='=' read -r key value; do
    case "$key" in
        protocol) proto="$value" ;;
        host) host="$value" ;;
        username) username="$value" ;;
    esac
done

creds="$HOME/.git-credentials"

case "$op" in
get)@WARN@
    [ -r "$creds" ] || exit 0
    while IFS= read -r line; do
        case "$line" in *://*@*) : ;; *) continue ;; esac
        rest="${line#*://}"
        userinfo="${rest%%@*}"
        hostpath="${rest#*@}"
        lhost="${hostpath%%/*}"
        luser="${userinfo%%:*}"
        lpass="${userinfo#*:}"
        [ "$lhost" = "$host" ] || continue
        if [ -n "$username" ] && [ "$luser" != "$username" ]; then
            continue
        fi
        printf 'protocol=%s\n' "${proto:-https}"
        printf 'host=%s\n' "$host"
        printf 'username=%s\n' "$luser"
        printf 'password=%s\n' "$lpass"
        exit 0
    done < "$creds"
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
    return (
        template.replace("@WARN@", warn_block).replace(
            "@CASES@", "\n".join(cases) if cases else "        _none_) : ;;"
        )
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
