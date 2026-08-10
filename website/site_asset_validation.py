"""Static site asset-reference contracts."""

from __future__ import annotations

from typing import Final
from xml.etree import ElementTree

FAVICON_ATTRIBUTES: Final = {
    "rel": "icon",
    "type": "image/svg+xml",
    "href": "{{SITE_BASE}}assets/agw-favicon.svg",
}
SVG_TAG: Final = "{http://www.w3.org/2000/svg}svg"
GROUP_TAG: Final = "{http://www.w3.org/2000/svg}g"
PATH_TAG: Final = "{http://www.w3.org/2000/svg}path"


def _rel_tokens(attributes: dict[str, str | None]) -> set[str]:
    return {token.casefold() for token in str(attributes.get("rel") or "").split()}


def _parse_svg(label: str, source: str) -> ElementTree.Element:
    parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        return ElementTree.fromstring(source, parser=parser)
    except ElementTree.ParseError as error:
        raise ValueError(f"{label}: invalid SVG") from error


def _has_non_whitespace_text(elements: tuple[ElementTree.Element, ...]) -> bool:
    return any((element.text or "").strip() or (element.tail or "").strip() for element in elements)


def validate_favicon_asset(favicon_source: str, rocket_source: str) -> None:
    """Require a flame-free exact projection of the canonical rocket mark."""
    favicon = _parse_svg("assets/agw-favicon.svg", favicon_source)
    rocket = _parse_svg("assets/agw-rocket.svg", rocket_source)
    favicon_children = tuple(favicon)
    rocket_marks = tuple(element for element in rocket.iter() if element.attrib.get("id") == "agw-mark")
    if favicon.tag != SVG_TAG or favicon.attrib != {"viewBox": "0 0 240 425"} or len(favicon_children) != 1:
        raise ValueError("assets/agw-favicon.svg: root contract is invalid")
    if len(rocket_marks) != 1:
        raise ValueError("assets/agw-rocket.svg: one canonical mark is required")
    mark = favicon_children[0]
    canonical_mark = rocket_marks[0]
    paths = tuple(mark)
    canonical_paths = tuple(canonical_mark)
    all_elements = (favicon, mark, *paths)
    if (
        mark.tag != GROUP_TAG
        or mark.attrib != canonical_mark.attrib
        or len(paths) != 3
        or _has_non_whitespace_text(all_elements)
    ):
        raise ValueError("assets/agw-favicon.svg: mark structure is invalid")
    for path, canonical_path in zip(paths, canonical_paths, strict=True):
        if path.tag != PATH_TAG or path.attrib != canonical_path.attrib or tuple(path):
            raise ValueError("assets/agw-favicon.svg: mark geometry differs from the canonical rocket")


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
    favicons = [attributes for tag, attributes in head_elements if tag == "link" and "icon" in _rel_tokens(attributes)]
    if favicons != [FAVICON_ATTRIBUTES]:
        raise ValueError(f"{template_name}: one exact favicon link is required")
