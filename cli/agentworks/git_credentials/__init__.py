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
    # A warn-only git credential helper (POSIX sh): when a github.com
    # query carries a username outside the known set (an agent added a
    # remote with an embedded username, bypassing scoping), it prints a
    # diagnosis to stderr -- which git surfaces above the auth failure
    # -- and returns nothing so the chain proceeds. No-op body when no
    # scoped credentials exist.
    warn_helper_script: str


# The warn-only helper's on-VM path. A shell-executed helper value
# (contains a slash), so the shell expands the tilde -- per-user, which
# is what makes the same include content work for admin and agents.
GIT_CRED_WARN_HELPER_PATH = "~/.agentworks-git-cred-warn.sh"

# The agentworks-owned gitconfig include carrying the credential-context
# sections. Referenced from the user's global gitconfig via include.path
# (tilde-literal, expanded per-user by git); overwritten wholesale on
# every init, so removing scopes degrades cleanly while at least one
# credential remains (removing ALL credentials leaves both files stale
# -- a pre-existing gap shared with the store itself).
GIT_SCOPES_INCLUDE_PATH = "~/.agentworks-git-scopes.gitconfig"

_WARN_HELPER_HEADER = """\
#!/bin/sh
# Managed by agentworks (git credential scoping); do not edit.
"""


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
    if sections:
        rendered.insert(
            0, f'[credential]\n\thelper = {GIT_CRED_WARN_HELPER_PATH}'
        )
    return CredentialMaterials(
        store_content="\n".join(store_unscoped + store_scoped) + "\n",
        gitconfig_content=header + "\n".join(rendered) + ("\n" if rendered else ""),
        warn_helper_script=_warn_helper_script(sections),
    )



def _warn_helper_script(sections: list[tuple[str, str]]) -> str:
    """Render the warn-only credential helper.

    The allowlist is every scoped username plus the released unscoped
    username. Only github.com queries are inspected (azdo's host is
    dev.azure.com and azdo has no scoping). Without scoped credentials
    the script is a no-op body -- still written, so re-initialization
    after removing scopes degrades cleanly.
    """
    if not sections:
        return _WARN_HELPER_HEADER + "exit 0\n"
    allow = " ".join(
        sorted({username for _url, username in sections} | {"x-access-token"})
    )
    return (
        _WARN_HELPER_HEADER
        + f'''[ "$1" = "get" ] || exit 0
host=""; username=""
while IFS='=' read -r key value; do
    case "$key" in
        host) host="$value" ;;
        username) username="$value" ;;
    esac
done
[ "$host" = "github.com" ] || exit 0
[ -n "$username" ] || exit 0
case " {allow} " in
    *" $username "*) exit 0 ;;
esac
{{
    echo "agentworks: this remote embeds username '$username', which"
    echo "bypasses git credential scoping for github.com; use a plain https"
    echo "remote (scoping selects the credential automatically)"
}} >&2
exit 0
'''
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
