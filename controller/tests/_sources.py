"""
Finding controller code for source guards, wherever it lives.

A guard that reads one file by name goes on passing after the code it guards
has moved to another: a rule over `em_controller.py` says nothing about a
handler split out into its own module, and `"X" not in src` is true of a file
X has left. This module is what guards use instead, so a move cannot silence
one:

- `MODULES` is every first-party controller module, found by glob, so a new
  module is covered the day it is added.
- `find_def` finds a function by name across all of them and fails if it is
  defined nowhere, or more than once.
- `code_only` drops comments and docstrings, so a guard does not match the
  prose explaining why the thing it forbids is forbidden.
"""

from __future__ import annotations

import ast
import functools
import textwrap
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]

MODULES: tuple[Path, ...] = tuple(sorted(CONTROLLER.glob("em_*.py")))


@functools.lru_cache(maxsize=None)
def source(path: Path) -> str:
    return path.read_text()


@functools.lru_cache(maxsize=None)
def tree(path: Path) -> ast.Module:
    return ast.parse(source(path))


def find_def(name: str) -> tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]:
    """The one function or method called `name` in the controller.

    Fails, rather than returning None, when it is missing or defined more than
    once: a guard handed nothing must not pass on nothing. A name two modules
    both define is asked for as `module.name` (`em_ota.run_update`)."""
    module, _, func = name.rpartition(".")
    paths = [p for p in MODULES if not module or p.stem == module]
    hits = [(path, node) for path in paths for node in ast.walk(tree(path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func]
    assert hits, f"{name} is not defined in any controller module"
    assert len(hits) == 1, (
        f"{name} is defined {len(hits)} times: "
        + ", ".join(f"{p.name}:{n.lineno}" for p, n in hits))
    return hits[0]


def def_source(name: str) -> str:
    """The source of `find_def(name)`, comments included."""
    path, node = find_def(name)
    return ast.get_source_segment(source(path), node)


def code_only(text: str) -> str:
    """`text` with comments and docstrings removed."""
    parsed = ast.parse(textwrap.dedent(text))
    for node in ast.walk(parsed):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0] = ast.Pass()
    return ast.unparse(parsed)


def all_source() -> str:
    """Every controller module, as written."""
    return "\n".join(source(p) for p in MODULES)


def all_code() -> str:
    """Every controller module, comments and docstrings removed."""
    return "\n".join(code_only(source(p)) for p in MODULES)
