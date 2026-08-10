from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

__all__ = (
    "BUILD_PATH",
    "CSP",
    "Document",
    "EXPECTED_FILES",
    "NOTICE",
    "Path",
    "REPO_ROOT",
    "RepositoryFixture",
    "SHELL_TEMPLATES",
    "WEBSITE",
    "contextlib",
    "contrast",
    "io",
    "math",
    "mock",
    "os",
    "parse",
    "re",
    "shutil",
    "site_builder",
    "snapshot",
    "subprocess",
    "tempfile",
    "unittest",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE = REPO_ROOT / "website"
BUILD_PATH = WEBSITE / "build.py"
SPEC = importlib.util.spec_from_file_location("site_builder", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load website builder")
site_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(site_builder)

NOTICE = (
    "Guided onboarding is not yet published. You can still explore the repository, PyPI package, "
    "rationale, and security model."
)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
EXPECTED_FILES = frozenset(
    {
        Path("404.html"),
        Path("index.html"),
        Path("lander/index.html"),
        Path("manifesto/index.html"),
        Path("security/index.html"),
        Path("assets/agw-favicon.svg"),
        Path("assets/agw-rocket.svg"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander-world.js"),
        Path("static/lander.css"),
        Path("static/site.css"),
    }
)
SHELL_TEMPLATES = (
    "index.html",
    "manifesto.html",
    "security.html",
    "lander.html",
    "404.html",
)


def relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.start_tags: list[tuple[str, dict[str, str | None]]] = []
        self.end_tags: list[str] = []
        self.ids: list[str] = []
        self.headings: list[str] = []
        self._heading: list[str] | None = None
        self._id_stack: list[str] = []
        self.text_by_id: dict[str, str] = {}
        self.all_text: list[str] = []
        self._element_stack: list[tuple[str, dict[str, str | None]]] = []
        self.elements: list[
            tuple[
                str,
                dict[str, str | None],
                tuple[tuple[str, dict[str, str | None]], ...],
            ]
        ] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.start_tags.append((tag, attributes))
        self.elements.append((tag, attributes, tuple(self._element_stack)))
        self._element_stack.append((tag, attributes))
        element_id = attributes.get("id") or ""
        self._id_stack.append(element_id)
        if element_id:
            self.ids.append(element_id)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.end_tags.append(tag)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading is not None:
            self.headings.append(" ".join("".join(self._heading).split()))
            self._heading = None
        if self._id_stack:
            self._id_stack.pop()
        for index in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[index][0] == tag:
                del self._element_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self._heading is not None:
            self._heading.append(data)
        for element_id in reversed(self._id_stack):
            if element_id:
                self.text_by_id[element_id] = self.text_by_id.get(element_id, "") + data
                break

    def tags(self, name: str) -> list[dict[str, str | None]]:
        return [attributes for tag, attributes in self.start_tags if tag == name]


def parse(source: str) -> Document:
    document = Document()
    document.feed(source)
    return document


def snapshot(path: Path) -> dict[str, bytes]:
    return {item.relative_to(path).as_posix(): item.read_bytes() for item in path.rglob("*") if item.is_file()}


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copy2(REPO_ROOT / "README.md", self.root / "README.md")
        for contract in site_builder.DOCUMENT_CONTRACTS:
            destination = self.root / contract.source
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / contract.source, destination)
        shutil.copytree(WEBSITE / "templates", self.root / "website/templates")
        shutil.copytree(WEBSITE / "assets", self.root / "website/assets")
        shutil.copytree(WEBSITE / "static", self.root / "website/static")

    def build(self, site_base: str = "/") -> Path:
        output = Path(self.temporary.name) / "site"
        site_builder.build_site(self.root, output, site_base)
        return output
