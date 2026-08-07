"""``agw resource migrate``'s manifest-upgrade half.

Rewriting manifests the operator wrote and keeps editing is a different
promise from emitting new ones: the file must come back with its
comments, quoting, key order, and unrelated documents intact, and the
run must be safe to interrupt or re-run. These pin that promise, plus the
whole-tree property that makes the upgrade correct at all (the retired
shape does not load, so no run may leave one behind).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from agentworks.config import load_config
from agentworks.errors import ConfigError, StateError
from agentworks.manifests import load_manifests
from agentworks.migrate import execute_plan, plan_migration
from agentworks.migrate.render import render_dry_run, render_preview

_COMMENTED_LEGACY = """\
# The homelab, at the top of the file.
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gpu-box   # the shared box
spec:
  # which platform runs here
  platform: lima   # trailing note on the selector
  # the ssh target
  # (over the tailnet)
  platform_config:
    vm_host: "me@gpu-box"   # quoted on purpose
---
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: ado
spec:
  provider: azdo
  provider_config:
    org: my-org
"""

_CANONICAL = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: already-fine
spec:
  platform:
    name: lima
    vm_host: me@fine   # untouched
"""


def _write_config(tmp_path: Path, *, toml_resources: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [paths]
        backups = "{(tmp_path / "backups").as_posix()}"

        """)
        + toml_resources
    )
    return cfg


def _resources(tmp_path: Path, **files: str) -> Path:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    for name, text in files.items():
        (resources / f"{name}.yaml").write_text(text)
    return resources


def _run(cfg: Path, selectors: list[str] | None = None, **kwargs: object):  # noqa: ANN202 - test helper
    """Plan and execute one migration, the way the command does."""
    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, selectors or [], all_resources=not selectors, **kwargs)  # type: ignore[arg-type]
    return plan, execute_plan(plan, config)


def test_upgrade_rewrites_the_retired_shape_preserving_comments(tmp_path: Path) -> None:
    """The whole promise in one file: every comment (file-leading,
    same-line, own-line, nested) survives, the explicit quoting survives,
    and the result loads.

    Comments that sat between the two retired keys now sit inside the
    tagged table, so they are indented a level deeper: left at the spec's
    indentation they would read as a comment on the spec rather than on
    the capability.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)
    _run(cfg)

    assert (resources / "sites.yaml").read_text() == dedent("""\
        # The homelab, at the top of the file.
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box   # the shared box
        spec:
          # which platform runs here
          platform:        # trailing note on the selector
            # the ssh target
            # (over the tailnet)
            name: lima
            vm_host: "me@gpu-box"   # quoted on purpose
        ---
        apiVersion: agentworks/v1
        kind: git-credential
        metadata:
          name: ado
        spec:
          provider:
            name: azdo
            org: my-org
        """)

    manifests = load_manifests(resources)
    assert not manifests.issues
    site, credential = manifests.entries
    assert site.resource.platform.tagged == {"name": "lima", "vm_host": "me@gpu-box"}
    assert credential.resource.provider.tagged == {"name": "azdo", "org": "my-org"}


def test_quoting_that_carries_a_type_survives_the_rewrite(tmp_path: Path) -> None:
    """The rewrite must not change a value's TYPE.

    An operator writes ``subscription_id: "0000"`` because they mean the
    string; re-emitted bare, YAML reads it back as the integer 0. Nothing
    downstream would report that as a rewrite bug, only as a config that
    stopped working, so it is pinned on the value that reaches the row.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        sites=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: azure-dev
            spec:
              platform: azure-vm
              platform_config:
                subscription_id: "0000"
                resource_group: agw
                region: eastus
            """),
    )

    _run(cfg)

    assert '    subscription_id: "0000"\n' in (resources / "sites.yaml").read_text()
    (entry,) = load_manifests(resources).entries
    assert entry.resource.platform.config["subscription_id"] == "0000"


def test_a_yaml_1_1_boolean_spelling_survives_verification(tmp_path: Path) -> None:
    """A faithful rewrite must not fail verification over YAML dialects.

    ``verify_ssl: no`` is the string ``"no"`` under YAML 1.2 (ruamel) and
    ``False`` under YAML 1.1 (the manifest loader). While the pre-side
    read the original with ruamel and the post-side read the rewrite with
    the loader, this exact document produced correct output and then
    aborted with "content differs after migration", blaming the migrator
    for a config it had reproduced byte for byte, and rolled back, leaving
    the operator an unloadable config and a remediation that refused to
    run. ``on``/``off``/``y``/``n`` and leading-zero integers are the same
    class; ``verify_ssl`` is a real proxmox field.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        sites=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: proxmox
            spec:
              platform: proxmox
              platform_config:
                api_url: https://pve:8006
                node: pve1
                token_id: agw@pam!agw
                template_vmid: 9000
                verify_ssl: no
            """),
    )

    _run(cfg)

    assert "    verify_ssl: no\n" in (resources / "sites.yaml").read_text()
    (entry,) = load_manifests(resources).entries
    assert entry.resource.platform.config["verify_ssl"] is False


def test_an_explicit_null_secret_name_survives_the_rewrite(tmp_path: Path) -> None:
    """The rewrite must not erase the evidence for the one input whose
    MEANING this release changed.

    ``token_secret: null`` used to be a hard error telling the operator to
    omit the key; absent and null mean the same thing now, so that same
    document quietly resolves to the default-named secret and declares a
    dependency on it. That is a decision the operator still has to make,
    and the migrator's own summary says the registry is unchanged, so
    normalizing the line to a bare ``token_secret:`` (which is what
    ruamel does left alone) would take away the one thing left to grep
    for. Every spelling comes back as it went in.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        sites=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: proxmox
            spec:
              platform: proxmox
              platform_config:
                api_url: https://pve:8006
                node: pve1
                token_id: agw@pam!agw
                template_vmid: 9000
                token_secret: null
            """),
        more=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: tilde
            spec:
              platform: proxmox
              platform_config:
                api_url: https://pve:8006
                node: pve1
                token_id: agw@pam!agw
                template_vmid: 9000
                token_secret: ~
            """),
    )

    _run(cfg)

    assert "    token_secret: null\n" in (resources / "sites.yaml").read_text()
    assert "    token_secret: ~\n" in (resources / "more.yaml").read_text()
    # And the null still means what it meant: the default-named secret.
    entries = {entry.name: entry.resource for entry in load_manifests(resources).entries}
    assert entries["proxmox"].platform.config["token_secret"] is None


def test_a_bare_null_key_does_not_grow_a_spelling(tmp_path: Path) -> None:
    """The other direction of the same promise. Preserving ``null`` must
    not mean INVENTING one: a key the operator left bare stays bare, and
    picks up no trailing whitespace on the way through."""
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        sites=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: proxmox
            spec:
              platform: proxmox
              platform_config:
                api_url: https://pve:8006
                node: pve1
                token_id: agw@pam!agw
                template_vmid: 9000
                token_secret:
            """),
    )

    _run(cfg)

    assert "    token_secret:\n" in (resources / "sites.yaml").read_text()


def test_rerunning_the_upgrade_is_a_no_op(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)
    _run(cfg)
    upgraded = (resources / "sites.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)
    assert plan.nothing_to_do
    assert plan.rewrites == []
    assert (resources / "sites.yaml").read_bytes() == upgraded


def test_canonical_manifests_are_not_rewritten(tmp_path: Path) -> None:
    """A file with nothing retired in it is not a planned write at all,
    so its bytes (and its mtime) are never at risk."""
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, fine=_CANONICAL, legacy=_COMMENTED_LEGACY)
    before = (resources / "fine.yaml").read_bytes()

    plan, _ = _run(cfg)

    assert [r.path for r in plan.rewrites] == [resources / "legacy.yaml"]
    assert (resources / "fine.yaml").read_bytes() == before


def test_the_upgrade_is_whole_tree_not_selector_scoped(tmp_path: Path) -> None:
    """A run scoped to one TOML resource still upgrades every retired
    manifest.

    This is the load-bearing asymmetry. The retired shape does not load,
    so a partially upgraded tree is not a valid state to leave an operator
    in; it would also break this run's own verification, which rebuilds
    the registry from the whole resources directory.
    """
    cfg = _write_config(tmp_path, toml_resources="[vm_templates.dev]\ncpus = 8\n")
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)

    plan, result = _run(cfg, ["vm-template/dev"])

    assert [u.name for u in plan.units] == ["dev"]
    assert plan.rewritten_resources == ("vm-site/gpu-box", "git-credential/ado")
    assert result.replaced == [resources / "sites.yaml"]
    assert not load_manifests(resources).issues


def test_an_append_targeting_an_upgraded_file_is_one_write(tmp_path: Path) -> None:
    """A TOML unit whose per-kind target is a file being upgraded folds
    into that file's single replacement, rather than two writes racing
    each other's digest guard."""
    cfg = _write_config(
        tmp_path, toml_resources='[azure]\nsubscription_id = "0000"\nresource_group = "g"\nregion = "r"\n'
    )
    resources = _resources(tmp_path, **{"vm-sites": _COMMENTED_LEGACY})

    plan, result = _run(cfg)

    assert plan.writes == []
    assert result.appended == []
    assert result.replaced == [resources / "vm-sites.yaml"]
    names = {d["metadata"]["name"] for d in yaml.safe_load_all((resources / "vm-sites.yaml").read_text()) if d}
    assert names == {"gpu-box", "ado", "azure"}

    # The coalesced documents are still accounted for in the preview. They
    # have no FileWrite of their own any more, so without this the unit
    # would be listed as migrating with nothing shown receiving it.
    (rewrite,) = plan.rewrites
    assert rewrite.appended == 1
    preview = render_preview(plan)
    assert f"  append to {resources / 'vm-sites.yaml'} (1 document(s), within its upgrade below)" in preview
    assert "  vm-site/azure to resources/vm-sites.yaml" in preview


def test_dry_run_writes_nothing_and_shows_the_diff(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)
    before = (resources / "sites.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)
    lines = render_dry_run(plan, full=True)

    assert (resources / "sites.yaml").read_bytes() == before
    assert "Upgrading 2 manifest resource(s) off the retired capability shape:" in lines
    assert "-  platform: lima   # trailing note on the selector" in lines
    assert "+    name: lima" in lines


def test_a_concurrent_edit_refuses_the_rewrite_and_keeps_the_edit(tmp_path: Path) -> None:
    """The digest guard is what makes a plan safe to sit on: an edit
    landing between planning and execution stops the write instead of
    overwriting the operator."""
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)
    edited = _COMMENTED_LEGACY.replace("me@gpu-box", "me@elsewhere")
    (resources / "sites.yaml").write_text(edited)

    with pytest.raises(StateError, match="it changed after migration planning"):
        execute_plan(plan, config)
    assert (resources / "sites.yaml").read_text() == edited


def test_a_verification_failure_rolls_the_manifest_back(tmp_path: Path) -> None:
    """The rewrite is covered by the same all-or-nothing contract the
    TOML side has: if the post-run registry does not match, the operator's
    original file comes back byte for byte."""
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)
    original = (resources / "sites.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)
    # A pre-row nothing can match: verification must fail AFTER the file
    # is replaced, which is the case rollback exists for.
    plan.pre_rows[("vm-site", "ghost")] = object()

    with pytest.raises(StateError, match="migration verification failed"):
        execute_plan(plan, config)
    assert (resources / "sites.yaml").read_bytes() == original


def test_the_original_is_snapshotted_beside_the_config_backup(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY)
    original = (resources / "sites.yaml").read_bytes()

    _plan, result = _run(cfg)

    assert result.yaml_backup_path is not None
    snapshot = result.yaml_backup_path / "resources" / "sites.yaml"
    assert snapshot.read_bytes() == original


def test_a_name_key_in_the_retired_config_is_refused_before_any_write(tmp_path: Path) -> None:
    """``name`` is the tagged table's discriminator, so a config key of
    that name cannot be folded in. Refused during planning, so nothing is
    written and the operator's file is untouched."""
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        sites=dedent("""\
            apiVersion: agentworks/v1
            kind: vm-site
            metadata:
              name: odd
            spec:
              platform: future-platform
              platform_config:
                name: sneaky
            """),
    )
    before = (resources / "sites.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError, match="collides with the tagged table's discriminator"):
        plan_migration(config, [], all_resources=True)
    assert (resources / "sites.yaml").read_bytes() == before


@pytest.mark.parametrize(
    ("kind", "field", "config_field", "capability"),
    [
        ("vm-site", "platform", "platform_config", "lima"),
        ("git-credential", "provider", "provider_config", "github"),
        ("session-template", "harness_integration", "harness_integration_config", "claude-code"),
    ],
)
def test_a_non_table_retired_config_is_refused_rather_than_dropped(
    tmp_path: Path, kind: str, field: str, config_field: str, capability: str
) -> None:
    """Folding a sibling that is not a table would DELETE it.

    There are no keys to move, so the fold emits the tagged table and
    drops the operator's value, in a file they never named (the upgrade
    is whole-tree, so any run reaches it). Verification is structurally
    blind to it, because the pre-side folds through the same function and
    loses the key too, so the run reported the resources as verified over
    a file it had just edited down. The refusal is the only thing
    that covers this class, and decode's message for the same shape says
    in as many words that the migrator refuses it.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(
        tmp_path,
        odd=dedent(f"""\
            apiVersion: agentworks/v1
            kind: {kind}
            metadata:
              name: odd
            spec:
              {field}: {capability}
              {config_field}: not-a-table
            """),
    )
    before = (resources / "odd.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError, match=f"spec.{config_field} is 'not-a-table' rather than a table"):
        plan_migration(config, [], all_resources=True)
    assert (resources / "odd.yaml").read_bytes() == before


def test_multi_document_markers_and_unrelated_documents_survive(tmp_path: Path) -> None:
    """Explicit stream markers, a leading `---`, a trailing `...`, and
    documents of other kinds all come back untouched. ruamel does not
    round-trip marker spelling itself, so this is the pin on the
    marker-restoring emit path."""
    cfg = _write_config(tmp_path)
    text = dedent("""\
        --- # first
        apiVersion: agentworks/v1
        kind: vm-template
        metadata:
          name: dev
        spec:
          cpus: 8
        ---
        apiVersion: agentworks/v1
        kind: vm-site
        metadata:
          name: gpu-box
        spec:
          platform: lima
          platform_config:
            vm_host: me@gpu-box
        ... # done
        """)
    resources = _resources(tmp_path, mixed=text)

    _run(cfg)

    rewritten = (resources / "mixed.yaml").read_text()
    assert rewritten.startswith("--- # first\n")
    assert rewritten.rstrip().endswith("... # done")
    assert "  cpus: 8\n" in rewritten
    assert not load_manifests(resources).issues


def test_discovery_finds_every_retired_document_across_the_tree(tmp_path: Path) -> None:
    from agentworks.migrate.manifest_upgrade import discover_legacy_documents

    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY, fine=_CANONICAL)

    found = discover_legacy_documents(resources)

    assert [(d.path.name, d.token) for d in found] == [
        ("sites.yaml", "vm-site/gpu-box"),
        ("sites.yaml", "git-credential/ado"),
    ]


def test_an_unparseable_manifest_stops_the_run_during_planning(tmp_path: Path) -> None:
    """A file that does not parse is reported with its position, before
    anything is written.

    Not a scan giving up quietly: verification rebuilds the registry from
    the whole directory, so an unparseable file makes the run impossible
    whatever the upgrade does. Failing here leaves the operator one fix
    and an untouched tree, where skipping it would rewrite the other files
    first and die afterwards.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_COMMENTED_LEGACY, broken="{{{ not yaml")
    before = (resources / "sites.yaml").read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError, match=r"broken.yaml:1: invalid YAML"):
        plan_migration(config, [], all_resources=True)
    assert (resources / "sites.yaml").read_bytes() == before


def test_the_upgrade_verifies_against_the_pre_rewrite_rows(tmp_path: Path) -> None:
    """Verification is real, not a comparison of the rewrite with itself:
    the pre-side decodes the ORIGINAL document's parsed values, the
    post-side decodes the text ruamel emitted, so an emission that lost a
    key fails.
    """
    cfg = _write_config(tmp_path)
    _resources(tmp_path, sites=_COMMENTED_LEGACY)

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)

    assert set(plan.pre_rows) == {("vm-site", "gpu-box"), ("git-credential", "ado")}
    lossy = plan.rewrites[0].new_text.replace('    vm_host: "me@gpu-box"   # quoted on purpose\n', "")
    plan.rewrites[0] = type(plan.rewrites[0])(
        path=plan.rewrites[0].path,
        old_bytes=plan.rewrites[0].old_bytes,
        old_digest=plan.rewrites[0].old_digest,
        new_text=lossy,
        new_digest=sha256(lossy.encode()).hexdigest(),
        resources=plan.rewrites[0].resources,
    )
    with pytest.raises(StateError, match="migration verification failed"):
        execute_plan(plan, config)


_LEGACY_SESSION_TEMPLATE = """\
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: htop
spec:
  harness_integration: shell
  harness_integration_config:
    command: htop
"""


def test_the_session_template_surface_upgrades_too(tmp_path: Path) -> None:
    """The third host surface, which no shipped release ever EMITTED in
    the sibling shape and which an operator can still type from `harness:`
    muscle memory.

    Decode refuses that document and names `agw resource migrate --all` as
    the remedy, so the remedy has to do something for it. Pinned end to
    end rather than by the table entry alone: the entry is what makes the
    upgrade look for the pair, and this is what proves the rewrite it
    produces loads.
    """
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sessions=_LEGACY_SESSION_TEMPLATE)

    with pytest.raises(ConfigError, match="names the capability as a string"):
        load_manifests(resources)

    _run(cfg)

    assert (resources / "sessions.yaml").read_text() == dedent("""\
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration:
            name: shell
            command: htop
        """)
    (entry,) = load_manifests(resources).entries
    assert entry.resource.harness_integration.tagged == {"name": "shell", "command": "htop"}
