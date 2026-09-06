"""Reading first-party source, either from the working tree or at a git ref.

Every command here answers a question about one snapshot of the repository.
Most ask it of HEAD; `carry` and `reanchor` ask it of the commit an older map's
line numbers were measured at, because a line number only means something
against the tree it was read from. One class serves both so there is a single
spelling of "parse this file" rather than one per point in history.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

PROD_ROOT = "cli/agentworks"
TEST_ROOT = "cli/tests"
WEB_ROOT = "website/tests"


class Tree:
    """One snapshot of the repository, listed and parsed on demand.

    `ref` is a git commit-ish, or None for the working tree.

    A file that is listed but does not parse is fatal, not skipped. Skipping
    would silently shrink the estate: the file's sites would not exist to be
    reported unowned, so the "every site is claimed by exactly one row" check
    would pass over a file nobody had looked at.
    """

    def __init__(self, ref: str | None = None) -> None:
        self.ref = ref
        self._parsed: dict[str, ast.Module] = {}
        self._listed: dict[tuple[str, ...], list[str]] = {}
        if ref is not None:
            done = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], capture_output=True, text=True)
            if done.returncode != 0:
                raise SystemExit(f"{ref!r} is not a commit in this repository")

    def __str__(self) -> str:
        return self.ref or "the working tree"

    def files(self, *roots: str) -> list[str]:
        """Every `.py` file under `roots` in this snapshot, in git's order.

        The working tree includes files git does not track yet, because a test
        file added but not staged holds real sites and an estate that cannot see
        it reports the same "every site is claimed" as a complete one. Ignored
        files stay out. Paths come back NUL-separated, so a path containing
        whitespace cannot split into two.
        """
        if roots not in self._listed:
            if self.ref is None:
                command = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *roots]
            else:
                command = ["git", "ls-tree", "-r", "-z", "--name-only", self.ref, "--", *roots]
            done = subprocess.run(command, capture_output=True, text=True)
            if done.returncode != 0:
                raise SystemExit(f"cannot list {', '.join(roots)} at {self}: {done.stderr.strip()}")
            names = sorted({f for f in done.stdout.split("\0") if f.endswith(".py")})
            self._listed[roots] = names
        return self._listed[roots]

    def read(self, path: str) -> str | None:
        """The file's text, or None when it does not exist in this snapshot."""
        if self.ref is None:
            try:
                return Path(path).read_text(encoding="utf-8")
            except OSError:
                return None
        done = subprocess.run(["git", "show", f"{self.ref}:{path}"], capture_output=True, text=True)
        return done.stdout if done.returncode == 0 else None

    def exists(self, path: str) -> bool:
        return self.read(path) is not None

    def parse(self, path: str) -> ast.Module:
        """Parse one file in this snapshot, or stop.

        See the class docstring for why an unparsed file is fatal rather than
        skipped.
        """
        if path not in self._parsed:
            text = self.read(path)
            if text is None:
                raise SystemExit(f"{path}: absent from {self}, so it cannot be parsed")
            try:
                self._parsed[path] = ast.parse(text, path)
            except SyntaxError as exc:
                message = f"{path}: cannot parse at {self} ({exc}); the estate would be short by this file"
                raise SystemExit(message) from exc
        return self._parsed[path]


def exc_name(node: ast.AST | None) -> str | None:
    """The name an exception expression names, ignoring how it is called."""
    if isinstance(node, ast.Call):
        return exc_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def template(node: ast.AST | None) -> str | None:
    r"""A string expression with its interpolations blanked to \x00, so a fixed
    part can still be matched against a `match=` needle."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\x00" for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = template(node.left), template(node.right)
        return None if left is None or right is None else left + right
    return None


def call_name(node: ast.Call) -> str:
    """The bare name a call invokes, whether it is spelled plain or attributed."""
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""
