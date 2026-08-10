"""Static site asset-reference contracts."""

from __future__ import annotations

from typing import Final

FAVICON_ATTRIBUTES: Final = {
    "rel": "icon",
    "type": "image/svg+xml",
    "href": "{{SITE_BASE}}assets/agw-favicon.svg",
}


def validate_head_links(
    template_name: str,
    head_elements: list[tuple[str, dict[str, str | None]]],
    expected_canonical: str,
) -> None:
    """Validate canonical and favicon links among a template's head children."""
    canonicals = [
        attributes for tag, attributes in head_elements if tag == "link" and attributes.get("rel") == "canonical"
    ]
    if len(canonicals) != 1:
        raise ValueError(f"{template_name}: one canonical link is required")
    if canonicals[0].get("href") != expected_canonical:
        raise ValueError(f"{template_name}: canonical URL must be {expected_canonical}")
    favicons = [attributes for tag, attributes in head_elements if tag == "link" and attributes.get("rel") == "icon"]
    if favicons != [FAVICON_ATTRIBUTES]:
        raise ValueError(f"{template_name}: one exact favicon link is required")
