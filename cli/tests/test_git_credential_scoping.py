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
    [{}, {"repos": ["acme/widgets"]}, {"repos": ["acme/widgets", "acme/gadgets"]}, {"owner": "acme"}],
)
def test_valid_scopes_accepted(blob: dict[str, object]) -> None:
    assert GitHubCredentialProvider.validate_config("t", blob) == ()


@pytest.mark.parametrize(
    ("blob", "match"),
    [
        ({"repos": ["acme/widgets"], "owner": "acme"}, "mutually exclusive"),
        ({"repos": ["no-slash"]}, '"owner/name"'),
        ({"repos": []}, '"owner/name"'),
        ({"repos": "acme/widgets"}, '"owner/name"'),
        ({"repo": "acme/widgets"}, "the field is 'repos'"),
        ({"repos": ["a/b/c"]}, '"owner/name"'),
        ({"repos": ["/leading"]}, '"owner/name"'),
        ({"owner": "acme/"}, "no slash"),
        ({"owner": ""}, "no slash"),
        ({"org": "acme"}, "unknown github provider field"),
        ({"repos": [123]}, '"owner/name"'),
        ({"owner": 123}, "user/org name"),
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
    p = GitHubCredentialProvider(config_name="widgets-bot", repos=["acme/widgets"])
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
            repos: [acme/widgets]
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
            repos: [not-a-repo]
        """)
    )
    with pytest.raises(ConfigError, match='"owner/name"') as exc:
        load_manifests(resources)
    assert "creds.yaml" in str(exc.value)


# -- more-specific-wins is NOT a collision ------------------------------------


def test_repo_and_owner_scopes_on_same_org_coexist() -> None:
    """The guide promises: a repo under one credential and its org under
    another is fine -- git's longest-prefix match resolves it."""
    providers = {
        "widgets-bot": GitHubCredentialProvider(
            config_name="widgets-bot", repos=["acme/widgets"]
        ),
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme"),
    }
    m = build_credential_materials(
        providers, {"widgets-bot": "x", "acme-bot": "y"}
    )
    assert '[credential "https://github.com/acme/widgets"]' in m.gitconfig_content
    assert '[credential "https://github.com/acme/"]' in m.gitconfig_content


# -- the warn-only helper ------------------------------------------------------


def _run_helper(script: str, tmp_path: Path, op: str, query: str) -> str:
    import subprocess

    path = tmp_path / "helper.sh"
    path.write_text(script)
    path.chmod(0o700)
    result = subprocess.run(
        ["sh", str(path), op],
        input=query,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0  # the helper NEVER blocks the chain
    assert result.stdout == ""  # never answers, only warns
    return result.stderr


def _scoped_script(tmp_path: Path) -> str:
    providers = {
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme"),
        "gh": GitHubCredentialProvider(config_name="gh"),
    }
    return build_credential_materials(
        providers, {"acme-bot": "x", "gh": "y"}
    ).warn_helper_script


def test_helper_warns_on_foreign_username(tmp_path: Path) -> None:
    err = _run_helper(
        _scoped_script(tmp_path),
        tmp_path,
        "get",
        "protocol=https\nhost=github.com\nusername=alice\n",
    )
    assert "embeds username 'alice'" in err
    assert "bypasses git credential scoping" in err


@pytest.mark.parametrize(
    ("op", "query"),
    [
        ("get", "protocol=https\nhost=github.com\nusername=acme-bot\n"),
        ("get", "protocol=https\nhost=github.com\nusername=x-access-token\n"),
        ("get", "protocol=https\nhost=github.com\n"),
        ("get", "protocol=https\nhost=dev.azure.com\nusername=alice\n"),
        ("store", "protocol=https\nhost=github.com\nusername=alice\n"),
    ],
)
def test_helper_silent_when_appropriate(
    tmp_path: Path, op: str, query: str
) -> None:
    assert _run_helper(_scoped_script(tmp_path), tmp_path, op, query) == ""


def test_helper_is_noop_without_scopes(tmp_path: Path) -> None:
    providers = {"gh": GitHubCredentialProvider(config_name="gh")}
    script = build_credential_materials(providers, {"gh": "y"}).warn_helper_script
    err = _run_helper(
        script, tmp_path, "get", "protocol=https\nhost=github.com\nusername=alice\n"
    )
    assert err == ""


def test_include_registers_helper_only_when_scoped() -> None:
    scoped = {
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme")
    }
    m = build_credential_materials(scoped, {"acme-bot": "x"})
    assert "helper = !~/.agentworks-git-cred-warn.sh" in m.gitconfig_content
    unscoped = {"gh": GitHubCredentialProvider(config_name="gh")}
    m2 = build_credential_materials(unscoped, {"gh": "y"})
    assert "helper" not in m2.gitconfig_content


# -- initializer wiring --------------------------------------------------------


def test_initializer_writes_all_three_files() -> None:
    """The load-bearing shell: both config files + the helper written
    with the right modes, and the include.path add is grep-guarded."""
    from unittest.mock import MagicMock

    from agentworks.vms.initializer import _configure_git_credentials

    target = MagicMock()
    writes: list[tuple[str, str, str]] = []
    runs: list[str] = []
    target.write_file.side_effect = (
        lambda path, content, mode="600", **kw: writes.append(
            (path, content, mode)
        )
    )
    target.run.side_effect = lambda cmd, **kw: runs.append(cmd)

    providers = {
        "gh": GitHubCredentialProvider(config_name="gh"),
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme"),
    }
    _configure_git_credentials(
        "vm1", target, providers, MagicMock(), git_tokens={"gh": "t1", "acme-bot": "t2"}
    )

    by_path = {path: (content, mode) for path, content, mode in writes}
    store, store_mode = by_path["~/.git-credentials"]
    assert store.splitlines()[0] == "https://x-access-token:t1@github.com"
    assert store_mode == "600"
    include, include_mode = by_path["~/.agentworks-git-scopes.gitconfig"]
    assert '[credential "https://github.com/acme/"]' in include
    assert include_mode == "600"
    helper, helper_mode = by_path["~/.agentworks-git-cred-warn.sh"]
    assert helper.startswith("#!/bin/sh")
    assert helper_mode == "700"
    (cmd,) = runs
    assert "credential.helper store" in cmd
    assert "grep -qxF '~/.agentworks-git-scopes.gitconfig'" in cmd
    assert "--add include.path '~/.agentworks-git-scopes.gitconfig'" in cmd


# -- vm add-git-credential guard -----------------------------------------------


def test_add_git_credential_line_key_preserves_scoped_lines() -> None:
    """The merge key is (username, host/path): adding the unscoped
    fallback must not evict scoped github lines already on the VM."""
    from agentworks.vms.manager import _credential_line_key

    scoped = "https://acme-bot:tok@github.com"
    fallback_old = "https://x-access-token:old@github.com"
    fallback_new = "https://x-access-token:new@github.com"
    assert _credential_line_key(scoped) != _credential_line_key(fallback_new)
    assert _credential_line_key(fallback_old) == _credential_line_key(fallback_new)


# -- TOML path: ignored scope keys warn ----------------------------------------


def test_toml_github_scope_keys_warn_and_unscope(tmp_path: Path) -> None:
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

        [git_credentials.gh]
        provider = "github"
        repos = ["acme/widgets"]
        """)
    )
    config = load_config(cfg, warn_issues=False)
    assert any(
        "manifest-only" in issue and "IGNORED" in issue
        for issue in config.config_issues
    )
    assert config.git_credentials["gh"].provider_config == {}


# -- workspace repo userinfo warning -------------------------------------------


def test_workspace_repo_userinfo_warns(tmp_path: Path) -> None:
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

        [workspace_templates.w]
        repo = "https://alice@github.com/acme/widgets.git"
        """)
    )
    config = load_config(cfg, warn_issues=False)
    assert any("embeds a username" in issue for issue in config.config_issues)


def test_multi_repo_list_emits_sections_per_repo() -> None:
    """`repos` is always a list; every entry gets both remote-spelling
    sections, all sharing one username and one store line."""
    p = GitHubCredentialProvider(
        config_name="wf-bot", repos=["acme/widgets", "acme/gadgets"]
    )
    assert p.credential_lines("tok") == ["https://wf-bot:tok@github.com"]
    urls = [url for url, _u in p.gitconfig_sections()]
    assert urls == [
        "https://github.com/acme/widgets",
        "https://github.com/acme/widgets.git",
        "https://github.com/acme/gadgets",
        "https://github.com/acme/gadgets.git",
    ]


# -- real git against the generated materials ----------------------------------


def _fill(home: Path, url_line: str) -> tuple[int, str, str]:
    import os
    import subprocess

    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "credential", "fill"],
        input=f"{url_line}\n\n",
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def test_generated_materials_work_with_real_git(tmp_path: Path) -> None:
    """The invocation contract, pinned against git itself: the include's
    "!"-prefixed helper line must not produce git's
    "'credential-~/...' is not a git command" error (a helper value
    without "!" or an absolute path gets "git credential-" prepended),
    the org context must select the scoped token for a plain URL, and a
    foreign embedded username must draw the helper's warning."""
    import shutil

    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")

    providers = {
        "gh": GitHubCredentialProvider(config_name="gh"),
        "acme-bot": GitHubCredentialProvider(config_name="acme-bot", owner="acme"),
    }
    m = build_credential_materials(providers, {"gh": "tokF", "acme-bot": "tokS"})
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git-credentials").write_text(m.store_content)
    (home / ".agentworks-git-scopes.gitconfig").write_text(m.gitconfig_content)
    helper = home / ".agentworks-git-cred-warn.sh"
    helper.write_text(m.warn_helper_script)
    helper.chmod(0o700)
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper = store\n"
        "[include]\n\tpath = ~/.agentworks-git-scopes.gitconfig\n"
    )

    # Plain URL under the scoped org: context injects the username,
    # store supplies the scoped token, no warning, no invocation error.
    rc, out, err = _fill(home, "url=https://github.com/acme/anything.git")
    assert "is not a git command" not in err, err
    assert rc == 0, err
    assert "username=acme-bot" in out
    assert "password=tokS" in out
    assert "bypasses git credential scoping" not in err

    # Plain URL outside the org: the fallback line wins.
    rc, out, err = _fill(home, "url=https://github.com/other/repo.git")
    assert rc == 0, err
    assert "password=tokF" in out

    # Foreign embedded username: the warn helper speaks, right above
    # the (expected) auth failure.
    rc, out, err = _fill(home, "url=https://alice@github.com/acme/x.git")
    assert "is not a git command" not in err, err
    assert "bypasses git credential scoping" in err
    assert rc != 0  # store has no alice line; prompts disabled
