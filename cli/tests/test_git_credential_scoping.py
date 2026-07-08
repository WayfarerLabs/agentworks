"""Fine-grained PAT scoping for github credentials (issue #166).

Selection rides git's own machinery, empirically verified against git
2.39: provisioned ``[credential "<url>"]`` context sections inject a
per-credential username (longest-prefix match on slash boundaries) and
the username-tagged, path-less store line supplies the token. No
``credential.useHttpPath`` anywhere -- with it on, path-less store
lines stop matching path-carrying queries, which would break every
unscoped credential.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.git_credentials import build_credential_materials
from agentworks.git_credentials.github import GitHubCredentialProvider
from agentworks.manifests import load_manifests
from agentworks.vms.initializer import resolve_git_credential_providers

# -- provider_config validation ----------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [{}, {"repo": "acme/widgets"}, {"owner": "acme"}],
)
def test_valid_scopes_accepted(blob: dict[str, object]) -> None:
    assert GitHubCredentialProvider.validate_config("t", blob) == ()


@pytest.mark.parametrize(
    ("blob", "match"),
    [
        ({"repo": "acme/widgets", "owner": "acme"}, "mutually exclusive"),
        ({"repo": "no-slash"}, '"owner/name"'),
        ({"repo": "a/b/c"}, '"owner/name"'),
        ({"repo": "/leading"}, '"owner/name"'),
        ({"owner": "acme/"}, "no slash"),
        ({"owner": ""}, "no slash"),
        ({"org": "acme"}, "unknown github provider field"),
    ],
)
def test_invalid_scopes_rejected(blob: dict[str, object], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        GitHubCredentialProvider.validate_config("t", blob)


# -- per-credential emission --------------------------------------------------


def test_unscoped_store_line_unchanged() -> None:
    """Loads-today: unscoped credentials keep the released host-level
    line verbatim (x-access-token username)."""
    p = GitHubCredentialProvider(config_name="gh")
    assert p.credential_lines("tok") == ["https://x-access-token:tok@github.com"]
    assert p.gitconfig_sections() == []


def test_repo_scope_covers_both_remote_spellings() -> None:
    """Context matching is slash-boundary-exact, so ``repo`` does not
    prefix-match ``repo.git``; both spellings get a section."""
    p = GitHubCredentialProvider(config_name="widgets-bot", repo="acme/widgets")
    assert p.credential_lines("tok") == ["https://widgets-bot:tok@github.com"]
    assert p.gitconfig_sections() == [
        ("https://github.com/acme/widgets", "widgets-bot"),
        ("https://github.com/acme/widgets.git", "widgets-bot"),
    ]


def test_owner_scope_uses_trailing_slash_prefix() -> None:
    p = GitHubCredentialProvider(config_name="acme-bot", owner="acme")
    assert p.gitconfig_sections() == [("https://github.com/acme/", "acme-bot")]


# -- cross-credential materials ----------------------------------------------


def test_unscoped_lines_precede_scoped() -> None:
    """The ordering contract: a username-less query takes the FIRST
    matching store line, so the host-level fallback must come before
    username-tagged scoped lines -- regardless of provider dict order."""
    providers = {
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme"),
        "gh": GitHubCredentialProvider(config_name="gh"),
    }
    m = build_credential_materials(providers, {"acme-bot": "tokA", "gh": "tokB"})
    lines = m.store_content.splitlines()
    assert lines == [
        "https://x-access-token:tokB@github.com",
        "https://acme-bot:tokA@github.com",
    ]
    assert '[credential "https://github.com/acme/"]' in m.gitconfig_content
    assert "\tusername = acme-bot" in m.gitconfig_content


def test_scope_collision_is_loud() -> None:
    providers = {
        "bot-a": GitHubCredentialProvider(config_name="bot-a", owner="acme"),
        "bot-b": GitHubCredentialProvider(config_name="bot-b", owner="acme"),
    }
    with pytest.raises(ConfigError, match="both claim scope"):
        build_credential_materials(providers, {"bot-a": "x", "bot-b": "y"})


def test_no_scopes_yields_header_only_include() -> None:
    """The include file is written even with no scoped credentials so
    re-provisioning after REMOVING scopes wipes stale sections."""
    providers = {"gh": GitHubCredentialProvider(config_name="gh")}
    m = build_credential_materials(providers, {"gh": "tok"})
    assert m.gitconfig_content.startswith("# Managed by agentworks")
    assert "[credential" not in m.gitconfig_content


# -- registry -> provider threading -------------------------------------------


def _registry_with_scoped_cred(tmp_path: Path):  # noqa: ANN202
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "creds.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: widgets-bot
        spec:
          provider: github
          provider_config:
            repo: acme/widgets
        """)
    )
    return build_registry(load_config(cfg, warn_issues=False))


def test_resolve_threads_scope_from_manifest(tmp_path: Path) -> None:
    """The manifest blob's scope reaches the provider instance the
    initializer builds (scoping is manifest-only: the flat TOML shape
    has no github blob columns)."""
    registry = _registry_with_scoped_cred(tmp_path)
    providers = resolve_git_credential_providers(registry, ["widgets-bot"])
    sections = providers["widgets-bot"].gitconfig_sections()
    assert ("https://github.com/acme/widgets", "widgets-bot") in sections


def test_manifest_scope_validation_has_file_line(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "creds.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: bad
        spec:
          provider: github
          provider_config:
            repo: not-a-repo
        """)
    )
    with pytest.raises(ConfigError, match='"owner/name"') as exc:
        load_manifests(resources)
    assert "creds.yaml" in str(exc.value)
