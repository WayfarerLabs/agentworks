"""The load precondition on ``agw resource migrate``.

An operator rehearsing the upgrade ran the guide's recipe, which leads
with ``--dry-run --full``. The dry run SUCCEEDED, printed the complete
correct diff, and ended "Dry run: nothing was written."; the real run
then refused over a manifest with nothing to do with the migration,
because only the real run loaded the whole resources directory. A dry run
that reports success where the real run fails is worse than no dry run,
because the operator believes it.

These pin the two halves of the answer: both paths reach the same
verdict, and the refusal says which order to work in.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.migrate import execute_plan, plan_migration

_LEGACY_SITE = """\
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gpu-box
spec:
  platform: lima
  platform_config:
    vm_host: me@gpu-box
"""

# Unrelated to the migration in every sense: a different kind, a
# different file, and nothing the upgrade half would touch. `key_url`
# must be a string, so this is the ordinary hand-edit class the phase
# introduced.
_BROKEN_APT = """\
apiVersion: agentworks/v1
kind: apt-source
metadata:
  name: my-repo
spec:
  key_url: 42
  key_path: /etc/apt/keyrings/my-repo.gpg
  source: deb https://apt.example.com/debian bookworm main
  source_file: my-repo.list
"""


def _write_config(tmp_path: Path, resources: str = "") -> Path:
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
        + resources
    )
    return cfg


def _resources(tmp_path: Path, **files: str) -> Path:
    resources = tmp_path / "resources"
    resources.mkdir(exist_ok=True)
    for name, text in files.items():
        (resources / f"{name}.yaml").write_text(text)
    return resources


def _cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]):  # noqa: ANN202 - test helper
    from typer.testing import CliRunner

    from agentworks.cli import app

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    return CliRunner().invoke(app, args)


def test_the_dry_run_refuses_exactly_what_the_real_run_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression itself. One tree, both commands, same verdict.

    Pinned through the CLI rather than the planner because the defect was
    a path difference: the dry run RETURNED before the code that loads
    the tree, so a planner-level test would have passed while the command
    stayed broken.
    """
    _write_config(tmp_path)
    _resources(tmp_path, sites=_LEGACY_SITE, apt=_BROKEN_APT)

    dry = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--dry-run", "--full"])
    real = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--yes"])

    assert dry.exit_code != 0, dry.stdout
    assert real.exit_code != 0, real.stdout
    assert "apt-source/my-repo" in str(dry.exception)
    assert "apt-source/my-repo" in str(real.exception)
    assert "Dry run: nothing was written." not in dry.stdout


def test_the_refusal_says_to_fix_the_unrelated_problems_first(tmp_path: Path) -> None:
    """The other half of the finding: the operator was told about a file
    the migration does not touch, by a command that had already said
    "Applying migration...", with nothing anywhere saying which to do
    first. The refusal has to carry the ordering itself."""
    cfg = _write_config(tmp_path)
    _resources(tmp_path, sites=_LEGACY_SITE, apt=_BROKEN_APT)

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError) as excinfo:
        plan_migration(config, [], all_resources=True)

    message, hint = str(excinfo.value), excinfo.value.hint or ""
    assert "does not load" in message
    assert "apt-source/my-repo" in message
    assert "has to load before a migration can be verified" in hint
    assert "re-run `agw resource migrate`" in hint
    assert "Nothing has been written." in hint


def test_a_refused_run_writes_nothing(tmp_path: Path) -> None:
    """Refusing during planning is what makes the promise cheap: there is
    no backup to take, no partial state, and nothing to roll back."""
    cfg = _write_config(tmp_path)
    resources = _resources(tmp_path, sites=_LEGACY_SITE, apt=_BROKEN_APT)
    before = {path: path.read_bytes() for path in resources.iterdir()}
    config_before = cfg.read_bytes()

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError):
        plan_migration(config, [], all_resources=True)

    assert {path: path.read_bytes() for path in resources.iterdir()} == before
    assert cfg.read_bytes() == config_before
    assert not (tmp_path / "backups").exists()


def test_the_check_reads_the_tree_this_run_would_produce(tmp_path: Path) -> None:
    """Not the tree as it stands.

    A legacy sibling document does not load TODAY, and it is the one
    problem an operator must NOT go fix by hand, because removing it is
    the migration's whole job. Checking the current tree would report it
    and refuse forever.
    """
    cfg = _write_config(tmp_path)
    _resources(tmp_path, sites=_LEGACY_SITE)

    config = load_config(cfg, warn_issues=False, resources=False)
    plan = plan_migration(config, [], all_resources=True)

    assert plan.rewrites  # the legacy document is why there is work to do
    execute_plan(plan, config)


def test_the_check_covers_documents_this_run_would_create(tmp_path: Path) -> None:
    """The overlay has to carry files that do not exist yet.

    A TOML-declared resource whose name is already taken by a manifest is
    a duplicate the moment the emitted document lands. Nothing refuses
    that at plan time, so without the created file in the overlay the
    collision would surface only from the real run, after the writes,
    which is the shape of the bug this whole check exists to close.
    """
    cfg = _write_config(
        tmp_path,
        resources=dedent("""\
            [secrets.npm-token]
            description = "from toml"
            """),
    )
    _resources(
        tmp_path,
        secrets=dedent("""\
            apiVersion: agentworks/v1
            kind: secret
            metadata:
              name: npm-token
              description: from yaml
            spec: {}
            """),
    )

    config = load_config(cfg, warn_issues=False, resources=False)
    with pytest.raises(ConfigError, match="duplicate secret"):
        plan_migration(config, [], all_resources=True)


def test_nothing_to_migrate_still_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The precondition is a precondition of DOING the work. A config with
    nothing left to migrate answers that, rather than complaining about a
    manifest this command was not asked to touch."""
    _write_config(tmp_path)
    _resources(tmp_path, apt=_BROKEN_APT)

    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert "Nothing to migrate" in result.stdout


def test_a_clean_dry_run_says_which_check_it_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Honesty in the passing case too. The dry run reaches the load
    precondition but not the registry-equivalence check, and says so
    rather than letting "nothing was written" imply a clean bill of
    health."""
    _write_config(tmp_path)
    _resources(tmp_path, sites=_LEGACY_SITE)

    result = _cli(tmp_path, monkeypatch, ["resource", "migrate", "--all", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Dry run: nothing was written." in result.stdout
    assert "the config loads and the registry builds with this migration applied" in result.stdout
    assert "needs the files on disk" in result.stdout
