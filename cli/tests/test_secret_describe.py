"""Tests for ``agw secret describe`` (Phase 1e of the Resource Registry SDD).

Per FRD R10, four sections: header (name, kind, origin, description),
usages (one row per matching requirement, deduplicated by source+text),
backend mappings (per-active-backend disposition), resolution preview
(which active backend would resolve, or "not available").
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import describe_secret


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


# -- Header section ---------------------------------------------------------


def test_operator_declared_secret_shows_file_and_line(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key for the operator's service"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.name == "api-key"
    assert desc.kind == "secret"
    assert desc.description == "API key for the operator's service"
    # operator-declared origin carries structured file + line fields;
    # the renderer formats them as separate sub-lines. The describe
    # service returns the raw Origin.
    assert desc.origin is not None
    assert desc.origin.variant == "operator-declared"
    assert desc.origin.file is not None
    assert str(desc.origin.file).endswith(cfg.name)
    assert desc.origin.line is not None
    assert desc.origin.line > 0


def test_auto_declared_secret_shows_first_requirement_source(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A secret referenced from `[admin.env]` but not declared in
    ``[secrets.*]`` auto-declares; the origin carries the structured
    source tuple and the description is synthesized so the list view
    has something meaningful to show.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        API_KEY = { secret = "auto-key" }

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "auto-key")

    assert desc.origin is not None
    assert desc.origin.variant == "auto-declared"
    assert desc.origin.source == ("admin-template", "default")
    # Description synthesized at finalize time from the first
    # requirement's usage text + source. Reads as "what this is for,
    # who's asking". No "(and N more)" suffix when there's only one
    # source.
    assert desc.description == "(auto) the API_KEY env var for admin-template/default"


def test_auto_declared_description_suffix_counts_other_sources(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An auto-declared secret required by N distinct sources gets a
    ``" (and N-1 more)"`` suffix on the synthesized description (Origin
    names the first source; the suffix accounts for the rest). N
    counts distinct ``(kind, name)`` source tuples; duplicate references
    from the same source (e.g. multiple env-block lookups in one
    template) do not inflate the count.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        SHARED_KEY = { secret = "shared" }

        [vm_templates.azure-prod]
        cpus = 2

        [vm_templates.azure-prod.env]
        TEMPLATE_KEY = { secret = "shared" }
        OTHER_KEY = { secret = "shared" }

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "shared")

    # Two distinct sources require this secret: admin-template/default
    # and vm-template/azure-prod. Whichever the framework walks first
    # is named in the description (publish order, not asserted here);
    # the second contributes to "(and 1 more)". The two references
    # inside azure-prod's env block share a source and do not inflate
    # the count.
    assert desc.origin is not None
    assert desc.origin.variant == "auto-declared"
    assert desc.description.startswith("(auto) ")
    assert desc.description.endswith("(and 1 more)")
    # First-named source is one of the two requiring templates.
    assert (
        " for admin-template/default " in desc.description
        or " for vm-template/azure-prod " in desc.description
    )


# -- Usages section ---------------------------------------------------------


def test_multiple_usages_render_one_row_each(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A secret referenced by three sources shows three usage rows; the
    sources are distinct so the dedupe step does nothing.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.shared-key]
        description = "Used by admin and a template"

        [admin.env]
        ADMIN_KEY = { secret = "shared-key" }

        [vm_templates.azure-prod]
        cpus = 2

        [vm_templates.azure-prod.env]
        TEMPLATE_KEY = { secret = "shared-key" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "shared-key")

    assert len(desc.references) == 2
    sources = sorted(u.source for u in desc.references)
    assert sources == [
        ("admin-template", "default"),
        ("vm-template", "azure-prod"),
    ]
    # Usage prose reflects the env-var key.
    texts = sorted(u.usage for u in desc.references)
    assert texts == ["the ADMIN_KEY env var", "the TEMPLATE_KEY env var"]


def test_no_usages_for_unreferenced_operator_declared_secret(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An operator-declared secret nothing references has an empty
    ``usages`` tuple.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.lonely-key]
        description = "Declared but not used"
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "lonely-key")
    assert desc.references == ()


# -- Backend mappings section ----------------------------------------------


def test_backend_mappings_show_each_active_backend(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """One mapping per active backend in the resolver chain order. The
    env-var backend shows its derived identifier; the prompt backend has
    no static identifier.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    backends = [m.backend for m in desc.backend_mappings]
    assert backends == ["env-var", "prompt"]

    env_var = next(m for m in desc.backend_mappings if m.backend == "env-var")
    assert env_var.would_attempt
    assert env_var.identifier == "AW_SECRET_API_KEY"

    prompt = next(m for m in desc.backend_mappings if m.backend == "prompt")
    assert prompt.would_attempt
    assert prompt.identifier is None


def test_backend_mapping_respects_operator_override(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An operator's ``backend_mappings.env-var = "CUSTOM"`` overrides
    the framework default.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"
        backend_mappings.env-var = "CUSTOM_API_KEY"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    env_var = next(m for m in desc.backend_mappings if m.backend == "env-var")
    assert env_var.identifier == "CUSTOM_API_KEY"


def test_backend_mapping_respects_opt_out(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """An operator's ``backend_mappings.env-var = false`` skips that
    backend for this secret; ``would_attempt`` is False.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"
        backend_mappings.env-var = false

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    env_var = next(m for m in desc.backend_mappings if m.backend == "env-var")
    assert env_var.would_attempt is False
    # Prompt still attempts.
    prompt = next(m for m in desc.backend_mappings if m.backend == "prompt")
    assert prompt.would_attempt


# -- Resolution preview section --------------------------------------------


def test_resolution_preview_picks_env_var_when_var_is_set(
    tmp_path: Path, ssh_keys: tuple[Path, Path], monkeypatch
) -> None:
    """Env-var first in the chain; the var is actually set. Preview
    reports env-var. This is the case where the operator's shell already
    holds the value and ``vm create`` will resolve silently.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-shell")
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.available
    assert desc.resolution.resolved_by == "env-var"


def test_resolution_preview_falls_through_when_env_var_is_unset(
    tmp_path: Path, ssh_keys: tuple[Path, Path], monkeypatch
) -> None:
    """Env-var is configured (would_attempt is True) but the operator
    hasn't set ``AW_SECRET_API_KEY`` in their shell. Preview must not
    claim env-var would resolve -- it must fall through to the next
    backend (prompt), matching what would actually happen at runtime.
    Regression test: the prior implementation only checked
    ``would_attempt`` and reported env-var as the resolver, misleading
    operators whose shell didn't hold the value.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    monkeypatch.delenv("AW_SECRET_API_KEY", raising=False)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.available
    assert desc.resolution.resolved_by == "prompt"


def test_resolution_preview_falls_through_to_prompt(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"
        backend_mappings.env-var = false

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.available
    assert desc.resolution.resolved_by == "prompt"


def test_resolution_preview_not_available_when_no_backend_attempts(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """A secret opted out of every active backend resolves via no
    backend; the preview reports "not available".

    Construction: a chain with only ``env-var`` (no prompt fallback)
    and an explicit ``backend_mappings.env-var = false`` opt-out.
    ``validate_chain`` (at build_registry) hard-errors when an
    OPERATOR-declared secret is unreachable, so the decl is
    hand-published as auto-declared (the origin the reachability check
    exempts); the chain comes from config as always.
    """
    from agentworks.resources import Origin, Registry
    from agentworks.secrets.backends import publish_to as publish_backends
    from agentworks.secrets.base import SecretDecl

    cfg = _write_cfg(
        tmp_path,
        """\
        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)

    from agentworks.capabilities import vm_platform as vm_platforms
    from agentworks.manifests import builtin as builtin_manifests

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    # The bundled vm-site rows reference the vm-platform capability
    # rows, so the manual sequence needs the platform publisher too.
    vm_platforms.publish_to(registry)
    publish_backends(registry)
    decl = SecretDecl(
        name="api-key",
        description="API key",
        backend_mappings={"env-var": False},
    )
    registry.add(
        "secret", "api-key", decl,
        Origin.auto_declared(source=("test", "api-key")),
    )
    registry.finalize()

    desc = describe_secret(config, registry, "api-key")
    assert desc.resolution.available is False
    assert desc.resolution.resolved_by is None


# -- Renderer outputs the four sections -------------------------------------


def test_render_emits_header_usages_mappings_preview(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch,
) -> None:
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key for the operator's service"

        [admin.env]
        ADMIN_KEY = { secret = "api-key" }

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    # Resolution preview now reflects runtime presence -- set the var so
    # the assertion ``would resolve via env-var`` is meaningful.
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-shell")
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")
    render_secret_description(desc)

    out = capsys.readouterr().out
    # Header
    assert "Secret: api-key" in out
    assert "Kind: secret" in out
    assert "Description: API key for the operator's service" in out
    # Origin is one line: variant + parenthetical with the file:line.
    assert "Origin: operator-declared (" in out
    # Description comes before Origin (Description is the primary info).
    assert out.index("Description:") < out.index("Origin:")
    # References (inbound)
    assert "Referenced by:" in out
    assert "admin-template/default" in out
    assert "the ADMIN_KEY env var" in out
    # Backend mappings
    assert "Backend mappings:" in out
    assert "env-var: AW_SECRET_API_KEY" in out
    assert "prompt: (prompt at resolution time)" in out
    # Resolution preview
    assert "Resolution preview:" in out
    assert "would resolve via env-var" in out


# -- Used-by (Phase 3c dynamic dimension) -----------------------------------


def test_describe_secret_used_by_is_none_without_db(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """Without ``db``, ``describe_secret`` leaves ``used_by = None`` and
    the renderer omits the ``Used by:`` section. Preserves the
    pre-Phase-3c behavior for callers that don't care about the
    dynamic dimension.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")
    assert desc.used_by is None


def test_describe_secret_used_by_populated_with_db(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """With ``db``, ``used_by`` is a tuple of ``InstanceRef``. For an
    admin-mode session referencing this secret via ``[admin.env]``,
    the tuple has one entry pointing at the session.
    """
    from agentworks.db import Database, SessionMode

    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        API_KEY = { secret = "shared-key" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "used_by_test.db")
    db.insert_vm("vm-1", site="lima", hostname="lima--vm-1")
    db.insert_workspace(
        "ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1"
    )
    db.insert_session(
        "sess-1", "ws-1", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    desc = describe_secret(config, registry, "shared-key", db=db)
    assert desc.used_by is not None
    assert [(r.instance_kind, r.instance_name) for r in desc.used_by] == [
        ("session", "sess-1")
    ]


def test_render_emits_used_by_section_when_populated(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The renderer emits ``Used by (per current config):`` between
    ``Referenced by:`` and ``Backend mappings:`` when the description
    carries a non-``None`` ``used_by`` tuple.
    """
    from agentworks.db import Database, SessionMode
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        """\
        [admin.env]
        API_KEY = { secret = "shared-key" }
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "render_used_by.db")
    db.insert_vm("vm-1", site="lima", hostname="lima--vm-1")
    db.insert_workspace(
        "ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1"
    )
    db.insert_session(
        "sess-1", "ws-1", template="default", mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    desc = describe_secret(config, registry, "shared-key", db=db)
    render_secret_description(desc)
    out = capsys.readouterr().out

    assert "Used by (per current config):" in out
    assert "session/sess-1" in out
    # Section ordering: Referenced by -> Used by -> Backend mappings.
    assert out.index("Referenced by:") < out.index("Used by (per current config):")
    assert out.index("Used by (per current config):") < out.index("Backend mappings:")


def test_render_used_by_empty_shows_friendly_message(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty ``used_by`` tuple (db provided but no sessions reach the
    secret) renders as a friendly ``(no live sessions reach this
    secret)`` line rather than an empty section.
    """
    from agentworks.db import Database
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.dead-key]
        description = "Declared but no live session reaches it"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    # DB with no sessions -- dead-key's used_by is empty (but non-None).
    db = Database(tmp_path / "no_sessions.db")

    desc = describe_secret(config, registry, "dead-key", db=db)
    assert desc.used_by == ()
    render_secret_description(desc)
    out = capsys.readouterr().out
    assert "Used by (per current config):" in out
    assert "(no live sessions reach this secret)" in out


def test_render_omits_used_by_section_when_none(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``used_by`` is ``None`` (no db passed), the renderer omits
    the section entirely -- backend mappings follows the reference
    list directly.
    """
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    desc = describe_secret(config, registry, "api-key")
    render_secret_description(desc)
    out = capsys.readouterr().out

    assert "Referenced by:" in out
    assert "Used by" not in out
    assert "Backend mappings:" in out


# -- Missing-name behavior --------------------------------------------------


def test_describe_secret_raises_not_found_for_unknown_name(
    tmp_path: Path, ssh_keys: tuple[Path, Path]
) -> None:
    """The service-layer function raises ``NotFoundError`` for an
    unknown secret name (typed at the service layer per the project's
    service-layer-is-the-authority rule; CLI / future web/API clients
    render uniformly).
    """
    from agentworks.errors import NotFoundError

    cfg = _write_cfg(tmp_path, "", ssh_keys)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    with pytest.raises(NotFoundError) as exc:
        describe_secret(config, registry, "no-such-secret")
    assert exc.value.entity_kind == "secret"
    assert exc.value.entity_name == "no-such-secret"
    assert exc.value.hint is not None
    assert "agw secret list" in exc.value.hint
