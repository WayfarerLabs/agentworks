"""Fine-grained PAT scoping for github credentials (issue #166).

Selection lives in the agentworks credential helper. The provisioned
include turns on ``credential.useHttpPath`` so git hands the helper the
remote's host and path; the helper picks the most specific credential
(exact repo, then owner, then the host default, then the first store
line) and serves the matching path-less store line, keyed by a
per-credential username. Verified against git 2.39.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.capabilities.descriptor import descriptor_for
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider
from agentworks.config import load_config
from agentworks.errors import ConfigError, ValidationError
from agentworks.git_credentials import (
    CredentialMaterials,
    build_credential_materials,
    materialize_credential_lines,
)
from agentworks.plugins.azure.azdo import AzDOCredentialProvider
from agentworks.schema import RefOwner, iter_field_docs
from agentworks.vms.initializer import resolve_git_credential_providers

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


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
#
# Validation and reference extraction are the CORE's now, derived from the
# provider's declared model, so these go through the core entry points
# rather than through classmethods the providers no longer have.


def _validate(blob: dict[str, object], name: str = "github", owner_name: str = "t") -> None:
    validate_capability_config(
        kind="git-credential-provider",
        config={"name": name, **blob},
        owner=RefOwner(kind="git-credential", name=owner_name),
    )


def _refs(blob: dict[str, object], owner_name: str = "t", name: str = "github") -> list[tuple[str, str]]:
    return [
        (ref.kind, ref.name)
        for ref in capability_config_references(
            kind="git-credential-provider",
            config={"name": name, **blob},
            owner=RefOwner(kind="git-credential", name=owner_name),
        )
    ]


@pytest.mark.parametrize(
    "blob",
    [
        {},
        {"repos": ["acme/widgets"]},
        {"owner": "acme"},
        {"repos": ["acme/widgets"], "owner": "acme"},
        # A deliberate loosening, recorded rather than hidden: the shipped
        # validator rejected ``repos: []`` and the model does not, because
        # an empty list and an absent field mean the same thing to every
        # consumer (``store_username`` and ``helper_entry`` both test
        # truthiness). A ``min_length=1`` would restore the rejection; it is
        # not worth an error an operator can only hit by writing something
        # inert.
        {"repos": []},
    ],
)
def test_valid_scopes_accepted(blob: dict[str, object]) -> None:
    # Extraction yields the token-secret reference the provider sources its
    # PAT from (default git-token-<name>); validation passing (no raise)
    # means the scope is well-formed.
    _validate(blob)
    assert _refs(blob) == [("secret", "git-token-t")]


def test_token_override_in_provider_config() -> None:
    assert _refs({"token": "my-secret"}, owner_name="gh") == [("secret", "my-secret")]


def test_every_secret_token_spelling_extracts_the_same_secret_edge() -> None:
    """Omitted, scalar shorthand, and the full secret arm all reach the
    graph through the model's SecretRef declaration."""
    assert _refs({}, owner_name="gh") == [("secret", "git-token-gh")]
    assert _refs({"token": "my-secret"}, owner_name="gh") == [("secret", "my-secret")]
    assert _refs({"token": {"mode": "secret", "secret": "my-secret"}}, owner_name="gh") == [("secret", "my-secret")]


def test_token_acquisition_stays_a_one_arm_union_defaulting_only_to_secret() -> None:
    """The ambition ceiling and omission-history rule as model facts.

    A future minted mechanism grows this arm set additively, but it may
    not become the default for declarations that omit ``token``.
    """
    from agentworks.capabilities.git_credential.github import GitHubConfig

    token = next(doc for doc in iter_field_docs(GitHubConfig) if doc.path == ("token",))
    assert [arm.tag for arm in token.union_arms] == ["secret"]
    assert token.default == {"mode": "secret"}
    assert not token.required


def test_shipped_providers_use_the_version_2_token_acquisition_contract() -> None:
    assert descriptor_for("git-credential-provider").contract_version == 2
    assert GitHubCredentialProvider.contract_version == 2
    assert AzDOCredentialProvider.contract_version == 2


def test_empty_token_rejected_by_validation() -> None:
    with pytest.raises(ConfigError):
        _validate({"token": ""}, owner_name="gh")


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(None, id="v0.13-outer-null"),
        pytest.param({"mode": "stored", "secret": "my-secret"}, id="pre-release-stored"),
        pytest.param({"mode": "minted"}, id="unknown-minted"),
    ],
)
def test_retired_and_unknown_token_acquisition_shapes_are_rejected(token: object) -> None:
    with pytest.raises(ConfigError):
        _validate({"token": token}, owner_name="gh")


def test_extraction_total_on_malformed_config() -> None:
    """Extraction never raises: a malformed ``token`` field omits the
    (now-underivable) token edge, and a malformed scope does not raise
    here either, because validation owns the raising."""
    assert _refs({"token": ""}, owner_name="gh") == []
    assert _refs({"token": 3}, owner_name="gh") == []
    # A malformed scope still yields the default token edge (its identity
    # does not depend on the scope fields).
    assert _refs({"repos": "not-a-list"}, owner_name="gh") == [("secret", "git-token-gh")]


# One case per RULE, not one per way of breaking it. Counting the rules is
# the part that needs care: ``^[A-Za-z0-9._-]+$`` is TWO rules wearing one
# regex, a charset and a non-emptiness quantifier, so it needs two cases.
# An earlier pass read it as one and dropped the emptiness pair; relaxing
# `+` to `*` then survived the entire suite. That is not a cosmetic hole.
# ``store_username`` tests ``if self.config.repos or self.config.owner``,
# so an empty-string owner is FALSY and a credential the operator wrote as
# owner-scoped would silently fall through to the unscoped github.com
# token. Scope-widening in the credential system, arrived at silently.
#
# The genuinely one-per-rule collapses stand: `no-slash` and `a/b/c` both
# violate the single ``GitHubRepo`` shape and produce the one message, and
# `repo` / `org` both hit closed-world.
@pytest.mark.parametrize(
    ("blob", "match"),
    [
        ({"repos": ["no-slash"]}, "must match `"),
        ({"owner": "acme/"}, "must match `"),
        # The quantifier, not the charset. See the note above.
        ({"owner": ""}, "must match `"),
        ({"repos": ["/leading"]}, "must match `"),
        ({"repos": "acme/widgets"}, "repos: must be a list"),
        ({"repo": "acme/widgets"}, "unknown field; expected one of: name, owner, repos, token"),
        ({"repos": [123]}, r"repos\[0\]: must be a string"),
        ({"owner": 123}, "owner: must be a string"),
    ],
)
def test_invalid_scopes_rejected(blob: dict[str, object], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _validate(blob)


# -- per-credential emission --------------------------------------------------


def test_unscoped_store_line_unchanged() -> None:
    """Loads-today: unscoped credentials keep the released host-level
    line verbatim (x-access-token username)."""
    p = _gh(config_name="gh")
    assert p.credential_lines("tok") == ["https://x-access-token:tok@github.com"]
    entry = p.helper_entry()
    assert entry.repos == () and entry.owner is None


@pytest.mark.parametrize(
    "provider",
    [_gh("gh"), _azdo("azdo", "acme")],
    ids=["github", "azdo"],
)
@pytest.mark.parametrize(
    "separator",
    [pytest.param("\n", id="lf"), pytest.param("\r", id="cr"), pytest.param("\0", id="nul")],
)
def test_core_credential_materialization_rejects_line_unsafe_token_for_shipped_provider(
    provider: GitHubCredentialProvider | AzDOCredentialProvider,
    separator: str,
) -> None:
    token = f"git-sink-sentinel{separator}injected"

    with pytest.raises(ValidationError) as caught:
        materialize_credential_lines(provider, token)

    assert "cannot be used for Git authentication and credential storage" in str(caught.value)
    assert "git-sink-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_direct_builder_rejects_line_injected_by_conformant_provider() -> None:
    class _InjectingProvider(GitHubCredentialProvider):
        def credential_lines(self, token: str) -> list[str]:
            return [f"https://x-access-token:{token}@github.com\nhttps://injected.invalid"]

    from agentworks.capabilities.conformance import conformance_error
    from agentworks.capabilities.git_credential.kinds import (
        GIT_CREDENTIAL_PROVIDER_DESCRIPTOR,
    )

    assert conformance_error(GIT_CREDENTIAL_PROVIDER_DESCRIPTOR, _InjectingProvider) is None

    with pytest.raises(ValidationError) as caught:
        build_credential_materials(
            {"gh": _InjectingProvider("gh", {})},
            {"gh": "git-provider-output-sentinel"},
        )

    assert "cannot be used for Git authentication and credential storage" in str(caught.value)
    assert "git-provider-output-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_repo_scope_selected_by_path(tmp_path: Path) -> None:
    """Selection lives in the helper: an exact repo match (with or
    without the .git suffix, with or without a leading slash) picks the
    repo-scoped credential over the owner scope and the fallback."""
    providers = {
        "widgets-bot": _gh(config_name="widgets-bot", repos=["acme/widgets"]),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
        "gh": _gh(config_name="gh"),
    }
    m = build_credential_materials(providers, {"widgets-bot": "tokR", "acme-bot": "tokO", "gh": "tokF"})
    home = _write_home(tmp_path, m)
    for qpath in ("acme/widgets.git", "acme/widgets", "/acme/widgets.git"):
        out, _err = _run_helper(
            m.helper_script,
            home,
            "get",
            f"protocol=https\nhost=github.com\npath={qpath}\n",
        )
        assert "password=tokR" in out, qpath
    # Owner scope catches everything else under acme, including repos
    # nobody declared anywhere (the ad hoc clone case).
    out, _err = _run_helper(
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\npath=acme/undeclared.git\n",
    )
    assert "password=tokO" in out
    # Anything else on the host: the unscoped default.
    out, _err = _run_helper(
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    assert "password=tokF" in out
    # No path at all (useHttpPath overridden / other tooling): serve
    # the default, but WARN: the operator may have stepped on the
    # setting scoping depends on.
    out, err = _run_helper(m.helper_script, home, "get", "protocol=https\nhost=github.com\n")
    assert "password=tokF" in out
    assert "no repository path" in err
    assert "useHttpPath" in err
    # With a path present, no such warning.
    _out, err = _run_helper(
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    assert err == ""


def test_multi_repo_list_selects_each(tmp_path: Path) -> None:
    providers = {
        "wf-bot": _gh(config_name="wf-bot", repos=["acme/widgets", "acme/gadgets"]),
        "gh": _gh(config_name="gh"),
    }
    m = build_credential_materials(providers, {"wf-bot": "tokR", "gh": "tokF"})
    home = _write_home(tmp_path, m)
    for repo in ("acme/widgets", "acme/gadgets"):
        out, _err = _run_helper(
            m.helper_script,
            home,
            "get",
            f"protocol=https\nhost=github.com\npath={repo}.git\n",
        )
        assert "password=tokR" in out, repo


def test_one_credential_combines_exact_repos_with_its_owner_scope(tmp_path: Path) -> None:
    """``repos`` plus ``owner`` is the union of both scope sets.

    The existing helper already composes the two fields: exact-repository
    matches are checked first, then the owner covers every other repo under
    it. The provider should pass both through rather than rejecting the
    composition at validation.
    """
    combined = _gh(config_name="acme-bot", repos=["other/widgets"], owner="acme")
    entry = combined.helper_entry()
    assert entry.repos == ("other/widgets",)
    assert entry.owner == "acme"

    providers = {"acme-bot": combined, "gh": _gh(config_name="gh")}
    materials = build_credential_materials(providers, {"acme-bot": "tokC", "gh": "tokF"})
    home = _write_home(tmp_path, materials)
    for path in ("other/widgets.git", "acme/undeclared.git"):
        out, _err = _run_helper(
            materials.helper_script,
            home,
            "get",
            f"protocol=https\nhost=github.com\npath={path}\n",
        )
        assert "password=tokC" in out, path
    out, _err = _run_helper(
        materials.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\npath=other/elsewhere.git\n",
    )
    assert "password=tokF" in out


def test_azdo_org_routes_by_first_segment(tmp_path: Path) -> None:
    providers = {
        "ado": _azdo(config_name="ado", org="my-org"),
    }
    m = build_credential_materials(providers, {"ado": "tokA"})
    home = _write_home(tmp_path, m)
    out, _err = _run_helper(
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=dev.azure.com\npath=my-org/proj/_git/repo\n",
    )
    assert "username=my-org" in out
    assert "password=tokA" in out


def test_scoped_only_out_of_scope_serves_nothing(tmp_path: Path) -> None:
    """With only scoped creds and no unscoped fallback, an out-of-scope
    URL serves NO credential (a scoped credential never serves outside its
    scope) and prints a diagnosis. A later hand-added UNSCOPED line (as
    add-git-credential does) becomes the host default and serves."""
    providers = {
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    m = build_credential_materials(providers, {"acme-bot": "tokO"})
    home = _write_home(tmp_path, m)
    out, err = _run_helper(
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\npath=other/repo.git\n",
    )
    # No scope matches, no unscoped credential: serve nothing (no leak).
    assert "password=" not in out
    assert "no credential is scoped to github.com/other/repo" in err
    # add-git-credential appends an UNSCOPED line; it becomes the fallback.
    (home / ".git-credentials").write_text(
        (home / ".git-credentials").read_text() + "https://x-access-token:added@github.com\n"
    )
    out, _err = _run_helper(
        m.helper_script,
        home,
        "get",
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
    with pytest.raises(ConfigError):
        build_credential_materials(providers, {"bot-a": "x", "bot-b": "y"})


def test_include_is_only_usehttppath() -> None:
    """The include carries exactly the useHttpPath switch: selection
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
          provider:
            name: github
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
    """The scope-shape check moved into the finalize ``validate`` pass
    (R3), so a malformed provider_config fails at build_registry, and the
    manifest file:line survives the move (re-attached from the resource
    origin rather than the decode prefix)."""
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
          name: bad
        spec:
          provider:
            name: github
            repos: [not-a-repo]
        """)
    )
    with pytest.raises(ConfigError) as exc:
        build_registry(load_config(cfg, warn_issues=False))
    assert "creds.yaml" in str(exc.value)


# -- more-specific-wins is NOT a collision ------------------------------------


def test_repo_and_owner_scopes_on_same_org_coexist() -> None:
    """A repo under one credential and its org under another is fine --
    exact repo beats owner in the helper's selection order (pinned by
    execution in test_repo_scope_selected_by_path)."""
    providers = {
        "widgets-bot": _gh(config_name="widgets-bot", repos=["acme/widgets"]),
        "acme-bot": _gh(config_name="acme-bot", owner="acme"),
    }
    build_credential_materials(providers, {"widgets-bot": "x", "acme-bot": "y"})


# -- the credential helper ------------------------------------------------------


def _run_helper(script: str, home: Path, op: str, query: str) -> tuple[str, str]:
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
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\nusername=acme-bot\n",
    )
    assert "username=acme-bot" in out
    assert "password=tokS" in out
    assert err == ""
    out, err = _run_helper(m.helper_script, home, "get", "protocol=https\nhost=github.com\n")
    # Username-less query takes the FIRST line (unscoped fallback).
    assert "username=x-access-token" in out
    assert "password=tokF" in out


def test_helper_get_ignores_other_hosts(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    out, err = _run_helper(m.helper_script, home, "get", "protocol=https\nhost=gitlab.com\n")
    assert out == ""
    assert err == ""


def test_helper_warns_on_foreign_username(tmp_path: Path) -> None:
    m = _scoped_materials()
    home = _write_home(tmp_path, m)
    _out, err = _run_helper(
        m.helper_script,
        home,
        "get",
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
        m.helper_script,
        home,
        "erase",
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
        m.helper_script,
        home,
        "erase",
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
        m.helper_script,
        home,
        "get",
        "protocol=https\nhost=github.com\nusername=alice\n",
    )
    assert "bypasses" not in err
    out, _err = _run_helper(m.helper_script, home, "get", "protocol=https\nhost=github.com\n")
    assert "password=tokF" in out
    _out, err = _run_helper(
        m.helper_script,
        home,
        "erase",
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
    target.write_file.side_effect = lambda path, content, mode="600", **kw: writes.append((path, content, mode))
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
    assert "--replace-all credential.helper '!~/.agentworks-git-cred-helper.sh'" in cmd
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


# The former ``test_toml_github_scope_keys_warn_and_unscope`` was removed here:
# it pinned the flat-TOML ``[git_credentials.*]`` behavior of warning that
# github scope keys (``repos``) are manifest-only and dropping them to an empty
# ``provider_config``. config.toml now hard-errors on any resource section
# (ADR 0022), so a ``[git_credentials.*]`` block never loads to warn-and-unscope;
# the manifest scope path is covered by ``test_resolve_threads_scope_from_manifest``
# and ``test_manifest_scope_validation_has_file_line`` above.


# -- review_remote (provider-owned repo URL advice) ----------------------------


def test_github_review_remote_flags_embedded_username() -> None:
    gh = _gh("bot", owner="acme")
    # Embedded username on github.com overrides scoping: flagged.
    assert gh.review_remote("https://alice@github.com/acme/widgets.git")
    # Plain https remote: fine.
    assert gh.review_remote("https://github.com/acme/widgets.git") == []
    # Not github's host, or not http(s): not github's concern.
    assert gh.review_remote("https://alice@dev.azure.com/acme/_git/x") == []
    assert gh.review_remote("git@github.com:acme/widgets.git") == []


def test_azdo_review_remote_allows_org_username() -> None:
    azdo = _azdo("bot", "acme")
    # The org as username is the standard, self-consistent AzDO form: fine.
    assert azdo.review_remote("https://acme@dev.azure.com/acme/proj/_git/r") == []
    assert azdo.review_remote("https://dev.azure.com/acme/proj/_git/r") == []
    # A username that is NOT the org would not be served: flagged.
    assert azdo.review_remote("https://someone@dev.azure.com/acme/proj/_git/r")
    # Not AzDO's host: not its concern.
    assert azdo.review_remote("https://acme@github.com/acme/widgets") == []


def test_remote_advisories_unions_and_filters(tmp_path: Path) -> None:
    from agentworks.git_credentials import remote_advisories

    registry = _registry_with_scoped_cred(tmp_path)  # one github credential
    # A github URL with an embedded username draws exactly one advisory.
    got = remote_advisories(registry, "https://alice@github.com/acme/widgets.git")
    assert len(got) == 1
    # Plain remotes and non-http(s) remotes draw nothing.
    assert remote_advisories(registry, "https://github.com/acme/widgets.git") == []
    assert remote_advisories(registry, "git@github.com:acme/widgets.git") == []


def test_announce_git_credentials_reinforces_names(
    captured_output: CapturedOutput,
) -> None:
    """Preflight echoes one ``Checking git-credential/<name>...`` line per
    credential, in the ``<kind>/<name>`` form matching the vm-site /
    vm-template context lines."""
    from agentworks.vms.initializer import announce_git_credentials

    announce_git_credentials({"github": _gh("github", owner="acme"), "azdo_ifc": _azdo("azdo_ifc", "org")})
    assert "Checking git-credential/github..." in captured_output.info
    assert "Checking git-credential/azdo_ifc..." in captured_output.info
    # Not the old friendly-label / display-name form.
    assert not any(m.startswith("Git credentials:") for m in captured_output.info)


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
    diagnosis: the exact behaviors credential-store got wrong."""
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

    # And the credential still serves afterward, no self-destruct.
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
        m.helper_script,
        home,
        "erase",
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

    with pytest.raises(ConfigError):
        build_credential_materials({"s": _Sneaky("s", {})}, {"s": "t"})


def test_azdo_org_charset_validated() -> None:
    with pytest.raises(ConfigError):
        _validate({"org": "my org"}, name="azdo")


def test_two_unscoped_creds_first_wins(tmp_path: Path) -> None:
    """Two unscoped credentials on one host are NOT a scope collision
    (released configs may carry them): first-wins by store order, and
    the second is effectively shadowed, pinned as intended behavior."""
    providers = {
        "gh1": _gh(config_name="gh1"),
        "gh2": _gh(config_name="gh2"),
    }
    m = build_credential_materials(providers, {"gh1": "tok1", "gh2": "tok2"})
    home = _write_home(tmp_path, m)
    out, _err = _run_helper(
        m.helper_script,
        home,
        "get",
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
