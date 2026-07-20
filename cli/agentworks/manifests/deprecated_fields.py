"""Deprecated-field notices for manifest specs (FRD R11).

A general, decoupled compatibility shim: when a formerly-valid spec
field is retired or relocated, an operator whose manifests still carry
it gets a clear, actionable message rather than a silent success or an
opaque schema error.

The mechanism ONLY detects a deprecated field's presence at the spec top
level and emits its message. It never relocates a value (no move
semantics), never inspects where the field should go, and does not hook
into the real schema validation. That keeps it removable wholesale:
delete this module, its one call in ``decode_document``, and its doctor
call site.

This is distinct from, and does NOT touch, the permanent TOML flat-field
hoist (``config._session_harness_pair``, FRD R6), which is an intentional
remapping with a different lifecycle.

Disposition is per-field:

- ``error``: fail the load, pointing at the new shape. Chosen whenever
  ignoring the field would silently change runtime behavior.
- ``warn``: emit a notice and ignore the field. Chosen only for
  genuinely vestigial fields.

``agw doctor`` reads the same table (``manifest_deprecation_notices``) to
surface ``warn``-level usage proactively; ``error``-level usage already
fails the load and is reported by doctor's config-load check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class DeprecatedField:
    """One retired-or-relocated spec field and how to react to it.

    ``message`` is a fragment that reads naturally after
    ``"<kind> spec field(s) <names> "`` (e.g. "are the 'shell' harness's
    config; ..."), so fields that share a message group into one line.
    """

    name: str
    level: Literal["error", "warn"]
    message: str


# The retired ``shell`` flat fields (FRD R2/R11): dropping any of them
# would silently downgrade a template to a bare login shell, so all three
# are ``error``. They share one message, so a document carrying several
# of them groups into one line, reproducing the single grouped message
# the bespoke reject in ``_decode_session_template`` emitted before this
# facility replaced it.
_SHELL_FLAT_FIELD_MESSAGE = (
    "are the 'shell' harness's config; set harness: shell and move them "
    "under spec.harness_config"
)

# Per-kind deprecated-field table, keyed by kind string. Seeding a new
# entry is the whole authoring surface: no code changes elsewhere.
DEPRECATED_FIELDS: dict[str, tuple[DeprecatedField, ...]] = {
    "session-template": (
        DeprecatedField("command", "error", _SHELL_FLAT_FIELD_MESSAGE),
        DeprecatedField("restart_command", "error", _SHELL_FLAT_FIELD_MESSAGE),
        DeprecatedField("required_commands", "error", _SHELL_FLAT_FIELD_MESSAGE),
    ),
}


def _grouped_notices(kind: str, matched: list[DeprecatedField]) -> list[str]:
    """Format the matched fields into notices, grouping fields that share
    a message onto one line (nicer than one line per field, and it
    reproduces the bespoke reject's single grouped message)."""
    by_message: dict[str, list[str]] = {}
    for field in matched:
        by_message.setdefault(field.message, []).append(field.name)
    return [
        f"{kind} spec field(s) {', '.join(sorted(names))} {message}"
        for message, names in by_message.items()
    ]


def check_deprecated_fields(kind: str, spec: dict[str, object]) -> list[str]:
    """Detect deprecated fields present at ``spec``'s top level.

    Called by ``decode_document`` before per-kind delegation. Any
    ``error``-level match raises ``ConfigError`` (the fields never reach
    the loader). ``warn``-level matches are stripped from ``spec`` (so the
    per-kind decoder never sees them, keeping the "ignore the field"
    guarantee self-contained rather than trusting a decoder's unknown-key
    policy) and returned as notices for the caller's warning channel.
    ``spec`` is ``decode_document``'s local ``dict(doc.spec)`` copy, so the
    strip never mutates the source document.
    """
    entries = DEPRECATED_FIELDS.get(kind)
    if not entries:
        return []
    present = [field for field in entries if field.name in spec]
    errors = [field for field in present if field.level == "error"]
    if errors:
        raise ConfigError("; ".join(_grouped_notices(kind, errors)))
    warns = [field for field in present if field.level == "warn"]
    for field in warns:
        spec.pop(field.name, None)
    return _grouped_notices(kind, warns)


def manifest_deprecation_notices(resources_dir: Path) -> list[str]:
    """Scan the operator's manifests for ``warn``-level deprecated fields
    (``agw doctor``'s proactive surface).

    Best-effort and tolerant: unreadable files and malformed envelopes
    are skipped, because their errors surface through doctor's own
    config-load check. Only ``warn``-level usage is reported here;
    ``error``-level usage fails the load and is reported there.

    Each notice is prefixed with the document's ``file:line`` and matches
    the string ``decode_document`` puts in ``ManifestSet.issues``
    verbatim, so doctor can render it as a dedicated finding without
    double-reporting it as a generic manifest issue.
    """
    from agentworks.manifests.envelope import validate_envelope
    from agentworks.manifests.loader import _iter_documents, _iter_manifest_files

    notices: list[str] = []
    for path in _iter_manifest_files(resources_dir):
        try:
            documents = list(_iter_documents(path))
        except ConfigError:
            continue
        for value, location in documents:
            try:
                doc = validate_envelope(value, location)
            except ConfigError:
                continue
            entries = DEPRECATED_FIELDS.get(doc.kind)
            if not entries:
                continue
            warns = [
                field
                for field in entries
                if field.name in doc.spec and field.level == "warn"
            ]
            notices.extend(
                f"{doc.where}: {line}"
                for line in _grouped_notices(doc.kind, warns)
            )
    return notices
