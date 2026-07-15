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
from agentworks.capabilities.git_credential.azdo import AzDOCredentialProvider
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.git_credentials import CredentialMaterials, build_credential_materials
from agentworks.manifests import load_manifests
from agentworks.vms.initializer import resolve_git_credential_providers


def _gh(
    config_name: str,
    *,
    owner: str | None = None,
    repos: tuple[str, ...] | list[str] = (),
    secret_name: str | None = None,
    description: str | None = None,
) -> GitHubCredentialProvider:
    """Construct a github provider from the pre-capability kwarg shape:
    the scope fields and token-secret override now live in the bound
    provider_config blob."""
    config: dict[str, object] = {}
    if owner is not None:
        config["owner"] = owner
    if repos:
        config["repos"] = list(repos)
    if secret_name is not None:
        config["token"] = secret_name
    return GitHubCredentialProvider(config_name, config, description=description)


def _azdo(
    config_name: str,
    org: str,
    *,
    secret_name: str | None = None,
    description: str | None = None,
) -> AzDOCredentialProvider:
    config: dict[str, object] = {"org": org}
    if secret_name is not None:
        config["token"] = secret_name
    return AzDOCredentialProvider(config_name, config, description=description)


# -- provider_config validation ----------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [{}, {"repos": ["acme/widgets"]}, {"repos": ["acme/widgets", "acme/gadgets"]}, {"owner": "acme"}],
)
def test_valid_scopes_accepted(blob: dict[str, object]) -> None:
    # validate_config returns the token-secret reference the provider
    # sources its PAT from (default git-token-<name>); scope validation
    # passing means no error.
    refs = GitHubCredentialProvider.validate_config("git-credential/t", blob)
    assert [(r.kind, r.name) for r in refs] == [("secret", "git-token-t")]


def test_token_override_in_provider_config() -> None:
    refs = GitHubCredentialProvider.validate_config(
        "git-credential/gh", {"token": "my-secret"}
    )
    assert [(r.kind, r.name) for r in refs] == [("secret", "my-secret")]


def test_empty_token_rejected() -> None:
    with pytest.raises(ConfigError, match="non-empty secret name"):
        GitHubCredentialProvider.validate_config("git-credential/gh", {"token": ""})


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
    p = _gh(config_name="gh")
    assert p.credential_lines("tok") == ["https://x-access-token:tok@github.com"]
    entry = p.helper_entry()
    assert entry.repos == () and entry.owner is None


def test_repo_scope_selected_by_path(tmp_path: Path) -> None:
    """Selection lives in the helper: an exact repo match (with or
    without the .git suffix, with or without a leading slash) picks the
    repo-scoped credential over the owner scope and the fallback."""
    providers = {
        "widgets-bot": _gh(
            config_name="widgets-bot", repos=["acme/widgets"]
        ),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
        "gh": _gh(config_name="gh"),
    }
    m = build_credential_materials(
        providers, {"widgets-bot": "tokR", "acme-bot": "tokO", "gh": "tokF"}
    )
    home = _write_home(tmp_path, m)
    for qpath in ("acme/widgets.git", "acme/widgets", "/acme/widgets.git"):
        out, _err = _run_helper(
            m.helper_script, home, "get",
            f"protocol=https\nhost=github.com\npath={qpath}\n",
        )
        assert "password=tokR" in out, qpath
    # Owner scope catches everything else under acme -- including repos
    # nobody declared anywhere (the ad hoc clone case).
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=acme/undeclared.git\n",
    )
    assert "password=tokO" in out
    # Anything else on the host: the unscoped default.
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    assert "password=tokF" in out
    # No path at all (useHttpPath overridden / other tooling): serve
    # the default, but WARN -- the operator may have stepped on the
    # setting scoping depends on.
    out, err = _run_helper(
        m.helper_script, home, "get", "protocol=https\nhost=github.com\n"
    )
    assert "password=tokF" in out
    assert "no repository path" in err
    assert "useHttpPath" in err
    # With a path present, no such warning.
    _out, err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    assert err == ""


def test_multi_repo_list_selects_each(tmp_path: Path) -> None:
    providers = {
        "wf-bot": _gh(
            config_name="wf-bot", repos=["acme/widgets", "acme/gadgets"]
        ),
        "gh": _gh(config_name="gh"),
    }
    m = build_credential_materials(providers, {"wf-bot": "tokR", "gh": "tokF"})
    home = _write_home(tmp_path, m)
    for repo in ("acme/widgets", "acme/gadgets"):
        out, _err = _run_helper(
            m.helper_script, home, "get",
            f"protocol=https\nhost=github.com\npath={repo}.git\n",
        )
        assert "password=tokR" in out, repo


def test_azdo_org_routes_by_first_segment(tmp_path: Path) -> None:
    providers = {
        "ado": _azdo(config_name="ado", org="my-org"),
    }
    m = build_credential_materials(providers, {"ado": "tokA"})
    home = _write_home(tmp_path, m)
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=dev.azure.com\npath=my-org/proj/_git/repo\n",
    )
    assert "username=my-org" in out
    assert "password=tokA" in out


def test_github_default_hardcoded_without_unscoped_cred(tmp_path: Path) -> None:
    """With only scoped creds, github.com still has the x-access-token
    default baked in -- a later hand-added (add-git-credential) line
    keeps serving; absent that line, the helper just misses."""
    providers = {
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    m = build_credential_materials(providers, {"acme-bot": "tokO"})
    home = _write_home(tmp_path, m)
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    # No unscoped line exists: legacy fallback serves the first host
    # line (better a scoped token than a guaranteed failure -- exactly
    # what credential-store did).
    assert "password=tokO" in out
    (home / ".git-credentials").write_text(
        (home / ".git-credentials").read_text()
        + "https://x-access-token:added@github.com\n"
    )
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    assert "password=added" in out


# -- cross-credential materials ----------------------------------------------


def test_unscoped_lines_precede_scoped() -> None:
    """Ordering still matters for the legacy first-host-line fallback:
    the unscoped default precedes scoped lines regardless of provider
    dict order."""
    providers = {
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
        "gh": _gh(config_name="gh"),
    }
    m = build_credential_materials(providers, {"acme-bot": "tokA", "gh": "tokB"})
    lines = m.store_content.splitlines()
    assert lines == [
        "https://x-access-token:tokB@github.com",
        "https://acme-bot:tokA@github.com",
    ]


def test_scope_collision_is_loud() -> None:
    providers = {
        "bot-a": _gh(config_name="bot-a", owner="acme"),
        "bot-b": _gh(config_name="bot-b", owner="acme"),
    }
    with pytest.raises(ConfigError, match="both claim scope"):
        build_credential_materials(providers, {"bot-a": "x", "bot-b": "y"})


def test_include_is_only_usehttppath() -> None:
    """The include carries exactly the useHttpPath switch -- selection
    lives in the helper, so no context sections exist, scoped or not."""
    for providers, tokens in (
        ({"gh": _gh(config_name="gh")}, {"gh": "t"}),
        (
            {"a": _gh(config_name="a", owner="acme")},
            {"a": "t"},
        ),
    ):
        m = build_credential_materials(providers, tokens)
        assert m.gitconfig_content.startswith("# Managed by agentworks")
        assert "useHttpPath = true" in m.gitconfig_content
        assert 'credential "' not in m.gitconfig_content


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
    entry = providers["widgets-bot"].helper_entry()
    assert entry.repos == ("acme/widgets",)
    assert entry.username == "widgets-bot"


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
    """A repo under one credential and its org under another is fine --
    exact repo beats owner in the helper's selection order (pinned by
    execution in test_repo_scope_selected_by_path)."""
    providers = {
        "widgets-bot": _gh(
            config_name="widgets-bot", repos=["acme/widgets"]
        ),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    build_credential_materials(providers, {"widgets-bot": "x", "acme-bot": "y"})


# -- the credential helper ------------------------------------------------------


def _run_helper(
    script: str, home: Path, op: str, query: str
) -> tuple[str, str]:
    import os
    import subprocess

    path = home / "helper.sh"
    path.write_text(script)
    path.chmod(0o700)
    result = subprocess.run(
        ["sh", str(path), op],
        input=query,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "HOME": str(home)},
    )
    assert result.returncode == 0  # the helper NEVER blocks the chain
    return result.stdout, result.stderr


def _scoped_materials() -> CredentialMaterials:
    providers = {
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
        "gh": _gh(config_name="gh"),
    }
    return build_credential_materials(providers, {"acme-bot": "tokS", "gh": "tokF"})


def _write_home(tmp_path: Path, m: CredentialMaterials) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".git-credentials").write_text(m.store_content)
    return home


def test_helper_get_serves_scoped_and_fallback(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    out, err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\nusername=acme-bot\n",
    )
    assert "username=acme-bot" in out
    assert "password=tokS" in out
    assert err == ""
    out, err = _run_helper(
        m.helper_script, home, "get", "protocol=https\nhost=github.com\n"
    )
    # Username-less query takes the FIRST line (unscoped fallback).
    assert "username=x-access-token" in out
    assert "password=tokF" in out


def test_helper_get_ignores_other_hosts(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    out, err = _run_helper(
        m.helper_script, home, "get", "protocol=https\nhost=gitlab.com\n"
    )
    assert out == ""
    assert err == ""


def test_helper_warns_on_foreign_username(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    _out, err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\nusername=alice\n",
    )
    assert "embeds username 'alice'" in err
    assert "bypasses git credential scoping" in err


def test_helper_erase_deletes_nothing_and_diagnoses(tmp_path: Path) -> None:
    """The reason the helper exists: git invokes erase after a rejected
    auth; credential-store DELETED the provisioned line. Ours keeps the
    file untouched and names the credential and secret to fix."""
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    before = (home / ".git-credentials").read_text()
    out, err = _run_helper(
        m.helper_script, home, "erase",
        "protocol=https\nhost=github.com\nusername=acme-bot\npassword=tokS\n",
    )
    assert (home / ".git-credentials").read_text() == before
    assert out == ""
    assert "rejected git credential 'acme-bot'" in err
    assert "secret 'git-token-acme-bot'" in err
    assert "agw agent reinit" in err


def test_helper_erase_silent_for_foreign_credentials(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    _out, err = _run_helper(
        m.helper_script, home, "erase",
        "protocol=https\nhost=example.com\nusername=alice\npassword=x\n",
    )
    assert err == ""


def test_helper_without_scopes_serves_but_never_warns(tmp_path: Path) -> None:
    """With no scoped credentials there is no scoping to bypass: the
    embedded-username warning is omitted, but get/erase still work."""
    providers = {"gh": _gh(config_name="gh")}
    m = build_credential_materials(providers, {"gh": "tokF"})
    home = _write_home(tmp_path, m)
    _out, err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\nusername=alice\n",
    )
    assert "bypasses" not in err
    out, _err = _run_helper(
        m.helper_script, home, "get", "protocol=https\nhost=github.com\n"
    )
    assert "password=tokF" in out
    _out, err = _run_helper(
        m.helper_script, home, "erase",
        "protocol=https\nhost=github.com\nusername=x-access-token\n",
    )
    assert "rejected git credential 'gh'" in err


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
        "gh": _gh(config_name="gh"),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    # Runup off so this stays focused on the materials write, not the
    # network probe (runup_and_filter is tested separately).
    cfg = MagicMock()
    cfg.defaults.runup_git_credentials = False
    _configure_git_credentials(
        "vm1",
        target,
        providers,
        MagicMock(),
        git_tokens={"gh": "t1", "acme-bot": "t2"},
        config=cfg,
    )

    by_path = {path: (content, mode) for path, content, mode in writes}
    store, store_mode = by_path["~/.git-credentials"]
    assert store.splitlines()[0] == "https://x-access-token:t1@github.com"
    assert store_mode == "600"
    include, include_mode = by_path["~/.agentworks-git-scopes.gitconfig"]
    assert "useHttpPath = true" in include
    assert include_mode == "600"
    helper, helper_mode = by_path["~/.agentworks-git-cred-helper.sh"]
    assert helper.startswith("#!/bin/sh")
    assert helper_mode == "700"
    (cmd,) = runs
    assert (
        "--replace-all credential.helper '!~/.agentworks-git-cred-helper.sh'" in cmd
    )
    assert "credential.helper store" not in cmd
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
    """The full invocation contract, pinned against git itself: our
    helper (registered as "!<path>" in credential.helper) serves get
    for scoped, fallback, and foreign-username cases, and a rejected
    auth leaves the store file BYTE-IDENTICAL while printing the
    diagnosis -- the exact behaviors credential-store got wrong."""
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")

    providers = {
        "gh": _gh(config_name="gh"),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    m = build_credential_materials(providers, {"gh": "tokF", "acme-bot": "tokS"})
    home = tmp_path / "home"
    home.mkdir()
    (home / ".git-credentials").write_text(m.store_content)
    (home / ".agentworks-git-scopes.gitconfig").write_text(m.gitconfig_content)
    helper = home / ".agentworks-git-cred-helper.sh"
    helper.write_text(m.helper_script)
    helper.chmod(0o700)
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper = !~/.agentworks-git-cred-helper.sh\n"
        "[include]\n\tpath = ~/.agentworks-git-scopes.gitconfig\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def run(op: str, url_line: str) -> tuple[int, str, str]:
        result = subprocess.run(
            ["git", "credential", op],
            input=f"{url_line}\n\n",
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr

    # Plain URL under the scoped org: context injects the username, our
    # helper supplies the scoped token, no warning, no invocation error.
    rc, out, err = run("fill", "url=https://github.com/acme/anything.git")
    assert "is not a git command" not in err, err
    assert rc == 0, err
    assert "username=acme-bot" in out
    assert "password=tokS" in out
    assert "bypasses git credential scoping" not in err

    # Plain URL outside the org: the fallback line wins.
    rc, out, err = run("fill", "url=https://github.com/other/repo.git")
    assert rc == 0, err
    assert "password=tokF" in out

    # Foreign embedded username: the helper warns; fill fails (no such
    # credential; prompts disabled).
    rc, out, err = run("fill", "url=https://alice@github.com/acme/x.git")
    assert "is not a git command" not in err, err
    assert "bypasses git credential scoping" in err
    assert rc != 0

    # THE erase contract: a rejected credential leaves the store file
    # byte-identical and produces the diagnosis (credential-store would
    # have silently deleted the line here).
    before = (home / ".git-credentials").read_text()
    rc, out, err = run(
        "reject",
        "url=https://acme-bot:tokS@github.com",
    )
    assert rc == 0, err
    assert (home / ".git-credentials").read_text() == before
    assert "rejected git credential 'acme-bot'" in err
    assert "secret 'git-token-acme-bot'" in err

    # And the credential still serves afterward -- no self-destruct.
    rc, out, err = run("fill", "url=https://github.com/acme/anything.git")
    assert rc == 0, err
    assert "password=tokS" in out


# -- shell-safety of the generated helper --------------------------------------


def test_hostile_secret_name_cannot_inject(tmp_path: Path) -> None:
    """The reviewer's canary: a token secret name carrying a command
    substitution must come out as inert text in the erase diagnosis --
    values are single-quote-escaped, never expanded."""
    hostile = "x$(touch " + str(tmp_path / "pwned") + ")"
    providers = {
        "gh": _gh(config_name="gh", secret_name=hostile),
    }
    m = build_credential_materials(providers, {"gh": "tok"})
    home = _write_home(tmp_path, m)
    _out, err = _run_helper(
        m.helper_script, home, "erase",
        "protocol=https\nhost=github.com\nusername=x-access-token\n",
    )
    assert not (tmp_path / "pwned").exists()
    assert hostile in err  # printed literally, not executed


def test_unsafe_scope_values_rejected_at_build() -> None:
    """Case labels and word lists must be glob- and quote-inert; the
    generator refuses anything else loudly (defense in depth behind the
    per-provider charset validation)."""
    from agentworks.capabilities.git_credential.base import HelperEntry

    class _Sneaky(GitHubCredentialProvider):
        def helper_entry(self) -> HelperEntry:
            return HelperEntry(host="github.com", username="a b")

    with pytest.raises(ConfigError, match="unsafe"):
        build_credential_materials(
            {"s": _Sneaky("s", {})}, {"s": "t"}
        )


def test_azdo_org_charset_validated() -> None:
    with pytest.raises(ConfigError, match="organization name"):
        from agentworks.capabilities.git_credential.azdo import AzDOCredentialProvider as A

        A.validate_config("t", {"org": "my org"})


def test_two_unscoped_creds_first_wins(tmp_path: Path) -> None:
    """Two unscoped credentials on one host are NOT a scope collision
    (released configs may carry them): first-wins by store order, and
    the second is effectively shadowed -- pinned as intended behavior."""
    providers = {
        "gh1": _gh(config_name="gh1"),
        "gh2": _gh(config_name="gh2"),
    }
    m = build_credential_materials(providers, {"gh1": "tok1", "gh2": "tok2"})
    home = _write_home(tmp_path, m)
    out, _err = _run_helper(
        m.helper_script, home, "get",
        "protocol=https\nhost=github.com\npath=any/repo.git\n",
    )
    assert "password=tok1" in out


def test_add_git_credential_never_downgrades_helper() -> None:
    """The add-git-credential path must not revert credential.helper to
    store on a helper-provisioned VM (that would reintroduce the
    erase-on-rejection self-destruct for every credential); on an old
    VM without the helper script, store keeps working until reinit."""
    import inspect as _inspect

    from agentworks.vms import manager

    src = _inspect.getsource(manager.add_git_credential)
    assert "if [ -x {GIT_CRED_HELPER_PATH} ]" in src
    assert "--replace-all credential.helper '!{GIT_CRED_HELPER_PATH}'" in src
    # And no unconditional downgrade remains.
    assert 'run("git config --global credential.helper store")' not in src
