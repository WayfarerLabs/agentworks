"""Tests for ``agw secret describe`` (Phase 1e of the Resource Registry SDD).

Per FRD R10, four sections: header (name, kind, origin, description),
usages (one row per matching requirement, deduplicated by source+text),
source mappings (one per active source, including its backend disposition),
resolution preview (which active source would resolve, or "not available").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import describe_secret
from agentworks.secrets.preview import PreviewCategory, SkippedSource
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_ENV_PROMPT = """
[secret_config]
sources = ["env-var", "prompt"]
"""
_ENV_ONLY = """
[secret_config]
sources = ["env-var"]
"""


def _write_cfg(
    tmp_path: Path,
    *,
    settings: str = "",
    admin_env: dict[str, object] | None = None,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` plus this file's ``admin_env`` sugar.

    Nineteen of the calls below seed the ``default`` admin-template's env
    block, which is where an operator's secret-referencing env lives, so it
    is worth a spelling here. It is not shared vocabulary: one file wanting
    it does not make it everyone's.
    """
    docs: list[ManifestDoc | str] = list(manifests)
    if admin_env is not None:
        docs.append(ManifestDoc("admin-template", "default", {"env": admin_env}))
    return write_cfg(tmp_path, *docs, settings=settings, filename="c.toml")


# -- Header section ---------------------------------------------------------


def test_operator_declared_secret_shows_file_and_line(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", description="API key for the operator's service")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.name == "api-key"
    assert desc.kind == "secret"
    assert desc.description == "API key for the operator's service"
    # operator-declared origin carries structured file + line fields;
    # the renderer formats them as separate sub-lines. The describe
    # service returns the raw Origin. The declaring file is the YAML
    # manifest in the resources/ dir now, not config.toml.
    assert desc.origin is not None
    assert desc.origin.variant == "operator-declared"
    assert desc.origin.file is not None
    assert desc.origin.file.parent.name == "resources"
    assert desc.origin.line is not None
    assert desc.origin.line > 0


def test_auto_declared_secret_shows_first_requirement_source(tmp_path: Path) -> None:
    """A secret referenced from `[admin.env]` but not declared in
    ``[secrets.*]`` auto-declares; the origin carries the structured
    source tuple and the description is synthesized so the list view
    has something meaningful to show.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        admin_env={"API_KEY": {"secret": "auto-key"}},
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


def test_auto_declared_description_suffix_counts_other_sources(tmp_path: Path) -> None:
    """An auto-declared secret required by N distinct sources gets a
    ``" (and N-1 more)"`` suffix on the synthesized description (Origin
    names the first source; the suffix accounts for the rest). N
    counts distinct ``(kind, name)`` source tuples; duplicate references
    from the same source (e.g. multiple env-block lookups in one
    template) do not inflate the count.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        admin_env={"SHARED_KEY": {"secret": "shared"}},
        manifests=[
            ManifestDoc(
                "vm-template",
                "azure-prod",
                {"cpus": 2, "env": {"TEMPLATE_KEY": {"secret": "shared"}, "OTHER_KEY": {"secret": "shared"}}},
            )
        ],
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
    assert " for admin-template/default " in desc.description or " for vm-template/azure-prod " in desc.description


# -- Usages section ---------------------------------------------------------


def test_multiple_usages_render_one_row_each(tmp_path: Path) -> None:
    """A secret referenced by three sources shows three usage rows; the
    sources are distinct so the dedupe step does nothing.
    """
    cfg = _write_cfg(
        tmp_path,
        admin_env={"ADMIN_KEY": {"secret": "shared-key"}},
        manifests=[
            ManifestDoc("secret", "shared-key", description="Used by admin and a template"),
            ManifestDoc("vm-template", "azure-prod", {"cpus": 2, "env": {"TEMPLATE_KEY": {"secret": "shared-key"}}}),
        ],
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


def test_no_usages_for_unreferenced_operator_declared_secret(tmp_path: Path) -> None:
    """An operator-declared secret nothing references has an empty
    ``usages`` tuple.
    """
    cfg = _write_cfg(
        tmp_path,
        manifests=[ManifestDoc("secret", "lonely-key", description="Declared but not used")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "lonely-key")
    assert desc.references == ()


# -- Backend mappings section ----------------------------------------------


def test_source_mappings_show_each_active_source(tmp_path: Path) -> None:
    """One mapping per active source in precedence order. Each mapping retains
    the selected backend implementation fact: env-var shows its derived
    identifier, while prompt has no static identifier.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", description="API key")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    backends = [m.backend for m in desc.source_mappings]
    assert backends == ["env-var", "prompt"]

    env_var = next(m for m in desc.source_mappings if m.backend == "env-var")
    assert env_var.would_attempt
    assert env_var.identifier == "AW_SECRET_API_KEY"

    prompt = next(m for m in desc.source_mappings if m.backend == "prompt")
    assert prompt.would_attempt
    assert prompt.identifier is None


def test_source_mapping_respects_operator_override(tmp_path: Path) -> None:
    """An operator's ``backend_mappings.env-var = "CUSTOM"`` overrides
    the framework default.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_ONLY,
        manifests=[
            ManifestDoc("secret", "api-key", {"backend_mappings": {"env-var": "CUSTOM_API_KEY"}}, description="API key")
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    env_var = next(m for m in desc.source_mappings if m.backend == "env-var")
    assert env_var.identifier == "CUSTOM_API_KEY"


def test_source_mapping_respects_opt_out(tmp_path: Path) -> None:
    """An operator's ``backend_mappings.env-var = false`` skips that source
    for this secret; ``would_attempt`` is False.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", {"backend_mappings": {"env-var": False}}, description="API key")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    env_var = next(m for m in desc.source_mappings if m.backend == "env-var")
    assert env_var.would_attempt is False
    # Prompt still attempts.
    prompt = next(m for m in desc.source_mappings if m.backend == "prompt")
    assert prompt.would_attempt


# -- Resolution preview section --------------------------------------------


def test_resolution_preview_picks_env_var_when_var_is_set(tmp_path: Path, monkeypatch) -> None:
    """Env-var first in the chain; the var is actually set. Preview
    reports env-var. This is the case where the operator's shell already
    holds the value and ``vm create`` will resolve silently.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", description="API key")],
    )
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-shell")
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.category is PreviewCategory.ATTEMPTABLE
    assert desc.resolution.source == "env-var"


def test_resolution_preview_is_pure_when_env_var_is_unset(tmp_path: Path, monkeypatch) -> None:
    """Preview reports mapping applicability and never reads the environment."""
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", description="API key")],
    )
    monkeypatch.delenv("AW_SECRET_API_KEY", raising=False)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.category is PreviewCategory.ATTEMPTABLE
    assert desc.resolution.source == "env-var"


def test_resolution_preview_falls_through_to_prompt(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        manifests=[ManifestDoc("secret", "api-key", {"backend_mappings": {"env-var": False}}, description="API key")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    assert desc.resolution.category is PreviewCategory.ATTEMPTABLE
    assert desc.resolution.source == "prompt"


# -- Readiness-aware describe (R9.1 / R9.6) ---------------------------------


def test_not_ready_source_annotated_and_skipped_in_preview(tmp_path: Path, monkeypatch) -> None:
    """R9.1 / R9.6: mapped source ``work-op`` selects the onepassword backend,
    which is not-ready without ``op`` on PATH. The source keeps its Backend
    mappings row with a not-ready annotation, is shown as skipped in the
    Resolution preview, and does not count toward "would attempt via X": the
    chain falls through to prompt. Readiness is offline (no store probe)."""
    monkeypatch.setattr("shutil.which", lambda name: None)  # op absent -> not ready
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["work-op", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret-source", "work-op", {"backend": {"name": "onepassword"}}),
            ManifestDoc(
                "secret",
                "api-key",
                {"backend_mappings": {"work-op": "op://Vault/api/field"}},
                description="API key",
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    op = next(m for m in desc.source_mappings if m.backend == "onepassword")
    assert op.source == "work-op"
    assert op.would_attempt is True
    assert op.not_ready_reason == "op CLI not installed"
    assert SkippedSource(source="work-op", reason="op CLI not installed") in desc.resolution.skipped_not_ready
    assert desc.resolution.source == "prompt"  # not-ready op does not count
    assert desc.resolution.category is PreviewCategory.ATTEMPTABLE


def test_render_shows_not_ready_annotation_and_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """The rendered describe view carries the not-ready annotation on the
    mapping and the ``skipped: not ready`` line in the preview."""
    from agentworks.secrets.inspect import render_secret_description

    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["onepassword", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret-source", "onepassword", {"backend": {"name": "onepassword"}}),
            ManifestDoc(
                "secret",
                "api-key",
                {"backend_mappings": {"onepassword": "op://Vault/api/field"}},
                description="API key",
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    render_secret_description(describe_secret(config, registry, "api-key"))

    out = capsys.readouterr().out
    assert ("onepassword (onepassword, declared): op://Vault/api/field (not ready: op CLI not installed)") in out
    assert "skipped onepassword: not ready: op CLI not installed" in out
    assert "would attempt via prompt" in out


def test_resolution_preview_summary_names_full_fallthrough_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """Three active, ready sources: the summary names all three, not just
    the winner. The preview picks its winner by mapping applicability alone
    (no value is ever read), so naming only the winner reads as more
    certain than it is when a later source in the chain is what actually
    resolves at runtime (field evidence: sources = ["env-var", "personal-op",
    "prompt"], env var unset, summary read "would attempt via env-var" and
    was taken as the final answer even though the chain kept going)."""
    from agentworks.secrets.inspect import render_secret_description

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/op")
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["env-var", "personal-op", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret-source", "personal-op", {"backend": {"name": "onepassword"}}),
            ManifestDoc(
                "secret",
                "api-key",
                {"backend_mappings": {"personal-op": "op://Vault/api/field"}},
                description="API key",
            ),
        ],
    )
    monkeypatch.delenv("AW_SECRET_API_KEY", raising=False)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")
    render_secret_description(desc)

    out = capsys.readouterr().out
    preview_section = out.split("Resolution preview:", 1)[1]
    assert "env-var" in preview_section
    assert "personal-op" in preview_section
    assert "prompt" in preview_section


def test_interactive_optimism_preview_unchanged_under_readiness(tmp_path: Path, monkeypatch) -> None:
    """LLD e acceptance line: the interactive-optimism preview is UNCHANGED.
    Readiness is the offline layer UNDER interactive-optimism: with an earlier
    not-ready onepassword source skipped, a ready ``prompt`` source is STILL
    previewed as resolving on its backend's ``would_attempt`` alone (never
    probed for a TTY or interaction). Readiness (offline) and interactivity
    (optimistic) stay orthogonal; no surface conflates them."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["onepassword", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret-source", "onepassword", {"backend": {"name": "onepassword"}}),
            ManifestDoc(
                "secret",
                "api-key",
                {"backend_mappings": {"onepassword": "op://Vault/api/field"}},
                description="API key",
            ),
        ],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")

    # The prompt is previewed optimistically (describe passes
    # interactive_available=True), exactly as before this phase.
    assert desc.resolution.source == "prompt"
    assert desc.resolution.category is PreviewCategory.ATTEMPTABLE


def test_resolution_preview_not_available_when_no_source_attempts(tmp_path: Path) -> None:
    """A secret opted out of every active source has no resolution attempt;
    the preview reports "not available".

    Construction: a chain with only ``env-var`` (no prompt fallback)
    and an explicit ``backend_mappings.env-var = false`` opt-out.
    ``validate_chain`` (at build_registry) hard-errors when an
    OPERATOR-declared secret is unreachable, so the decl is
    hand-published as auto-declared (the origin the reachability check
    exempts); the chain comes from config as always.
    """
    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.resources import Origin, Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.sources import publish_builtin_secret_sources

    cfg = _write_cfg(tmp_path, settings=_ENV_ONLY)
    config = load_config(cfg, warn_issues=False)

    from agentworks.manifests import builtin as builtin_manifests

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    # The bundled vm-site rows reference the vm-platform capability
    # rows; publish every platform row directly, bypassing the
    # host-support gate (this test is about secret previews, not this
    # host's OS/tooling).
    from tests.conftest import publish_all_platforms

    publish_all_platforms(registry)
    publish_capability_rows(registry, descriptor_for("secret-backend"))
    publish_builtin_secret_sources(registry)
    decl = SecretDecl(
        name="api-key",
        description="API key",
        backend_mappings={"env-var": False},
    )
    registry.add(
        "secret",
        "api-key",
        decl,
        Origin.auto_declared(source=("test", "api-key")),
    )
    registry.finalize()

    desc = describe_secret(config, registry, "api-key")
    assert desc.resolution.category is PreviewCategory.UNAVAILABLE
    assert desc.resolution.source is None


# -- Renderer outputs the four sections -------------------------------------


def test_render_emits_header_usages_mappings_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch,
) -> None:
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_PROMPT,
        admin_env={"ADMIN_KEY": {"secret": "api-key"}},
        manifests=[ManifestDoc("secret", "api-key", description="API key for the operator's service")],
    )
    # Resolution preview now reflects runtime presence -- set the var so
    # the assertion ``would attempt via env-var`` is meaningful.
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
    assert "env-var (env-var, synthesized default): AW_SECRET_API_KEY" in out
    assert "prompt (prompt, synthesized default): (prompt at resolution time)" in out
    # Resolution preview: names the winner and, since prompt is also active
    # and reachable, the fall-through chain behind it (structural check on
    # the preview section only, not the connecting prose).
    assert "Resolution preview:" in out
    preview_section = out.split("Resolution preview:", 1)[1]
    assert "env-var" in preview_section
    assert "prompt" in preview_section


# -- Used-by (Phase 3c dynamic dimension) -----------------------------------


def test_describe_secret_used_by_is_none_without_db(tmp_path: Path) -> None:
    """Without ``db``, ``describe_secret`` leaves ``used_by = None`` and
    the renderer omits the ``Used by:`` section. Preserves the
    pre-Phase-3c behavior for callers that don't care about the
    dynamic dimension.
    """
    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_ONLY,
        manifests=[ManifestDoc("secret", "api-key", description="k")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    desc = describe_secret(config, registry, "api-key")
    assert desc.used_by is None


def test_describe_secret_used_by_populated_with_db(tmp_path: Path) -> None:
    """With ``db``, ``used_by`` is a tuple of ``InstanceRef``. For an
    admin-mode session referencing this secret via ``[admin.env]``,
    the tuple has one entry pointing at the session.
    """
    from agentworks.db import Database, SessionMode

    cfg = _write_cfg(tmp_path, admin_env={"API_KEY": {"secret": "shared-key"}})
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "used_by_test.db")
    db.insert_vm("vm-1", site="lima", hostname="lima--vm-1")
    db.insert_workspace("ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1")
    db.insert_session(
        "sess-1",
        "ws-1",
        template="default",
        mode=SessionMode.ADMIN,
        socket_path="/tmp/sess-1.sock",
    )
    db._conn.commit()

    desc = describe_secret(config, registry, "shared-key", db=db)
    assert desc.used_by is not None
    assert [(r.instance_kind, r.instance_name) for r in desc.used_by] == [("session", "sess-1")]


def test_render_emits_used_by_section_when_populated(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The renderer emits ``Used by (per current config):`` between
    ``Referenced by:`` and ``Backend mappings:`` when the description
    carries a non-``None`` ``used_by`` tuple.
    """
    from agentworks.db import Database, SessionMode
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(tmp_path, admin_env={"API_KEY": {"secret": "shared-key"}})
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    db = Database(tmp_path / "render_used_by.db")
    db.insert_vm("vm-1", site="lima", hostname="lima--vm-1")
    db.insert_workspace("ws-1", workspace_path="/tmp/ws-1", vm_name="vm-1", linux_group="ws-ws-1")
    db.insert_session(
        "sess-1",
        "ws-1",
        template="default",
        mode=SessionMode.ADMIN,
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
        settings=_ENV_ONLY,
        manifests=[ManifestDoc("secret", "dead-key", description="Declared but no live session reaches it")],
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
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``used_by`` is ``None`` (no db passed), the renderer omits
    the section entirely -- backend mappings follows the reference
    list directly.
    """
    from agentworks.secrets.inspect import render_secret_description

    cfg = _write_cfg(
        tmp_path,
        settings=_ENV_ONLY,
        manifests=[ManifestDoc("secret", "api-key", description="k")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    desc = describe_secret(config, registry, "api-key")
    render_secret_description(desc)
    out = capsys.readouterr().out

    assert "Referenced by:" in out
    assert "Used by" not in out
    assert "Backend mappings:" in out


# -- Names that reach the renderer unvalidated ------------------------------


def test_an_auto_declared_name_cannot_add_a_line_to_the_rendering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator's choice of name cannot lengthen describe's output.

    The header renders the secret's name, and that name reached no naming
    validator: the ``secret`` kind auto-declares any name a reference uses,
    with no name restriction (``secrets/kinds.py``), so a manifest can put a
    line separator in it. Splitting the header gives the operator a second
    line that reads like one of ours.

    ``ResolutionPreview`` screens the name on the way through, so describe
    refuses before the renderer runs. The assertion is the property that
    survives either answer, measured against the same manifest under a plain
    name: an unavailable source chain keeps every other line identical, so
    any growth is the split.
    """
    from agentworks.secrets.inspect import render_secret_description

    def _rendered_line_count(directory: Path, name: str) -> int:
        directory.mkdir()
        cfg = write_cfg(
            directory,
            ManifestDoc("admin-template", "default", {"env": {"API_KEY": {"secret": name}}}),
            settings="[secret_config]\nsources = []\n",
            filename="c.toml",
        )
        config = load_config(cfg, warn_issues=False)
        registry = build_registry(config)
        try:
            render_secret_description(describe_secret(config, registry, name))
        except ValueError:
            return 0
        return len(capsys.readouterr().out.splitlines())

    baseline = _rendered_line_count(tmp_path / "plain", "api-key")
    assert baseline > 0
    assert _rendered_line_count(tmp_path / "forged", "api-key\nSecret: forged") <= baseline


# -- Missing-name behavior --------------------------------------------------


def test_describe_secret_raises_not_found_for_unknown_name(tmp_path: Path) -> None:
    """The service-layer function raises ``NotFoundError`` for an
    unknown secret name (typed at the service layer per the project's
    service-layer-is-the-authority rule; CLI / future web/API clients
    render uniformly).
    """
    from agentworks.errors import NotFoundError

    cfg = _write_cfg(tmp_path)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    with pytest.raises(NotFoundError) as exc:
        describe_secret(config, registry, "no-such-secret")
    assert exc.value.entity_kind == "secret"
    assert exc.value.entity_name == "no-such-secret"
    assert exc.value.hint is not None
    assert "agw secret list" in exc.value.hint
