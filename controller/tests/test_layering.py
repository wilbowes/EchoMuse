"""
Which controller modules may import which.

The split of em_api left it as the app's assembly, routes in em_routes_*,
and shared pieces beneath them. The point was code that can be tested and
understood a module at a time, and that only lasts while the imports keep
pointing down. A base module that pulls in the HTTP layer or the voice
pipeline cannot be imported by a test without them, which is how em_api and
em_controller went untested for so long (#306, #739). Three rules:

  1. Only em_controller imports em_api: em_api assembles the app, and the
     process that serves it is the one thing that should start it.
  2. Route modules are imported by em_api, which registers them, and by
     other route modules. Never by the pipeline or a shared module: logic
     both need belongs below them (em_credentials exists for that reason).
  3. The base modules never reach em_api, a route module, em_controller or
     em_esphome, directly or through anything they import.

Imports under `if TYPE_CHECKING:` are exempt: they are not imported at run
time.
"""

import ast

import _sources

BASE = ("em_state", "em_http", "em_events", "em_shell", "em_tasks",
        "em_dbwriter", "em_db")
ABOVE_BASE = ("em_api", "em_controller", "em_esphome")


def _type_checking_only(tree: ast.Module) -> set[int]:
    lines = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"):
            for sub in ast.walk(node):
                lines.add(getattr(sub, "lineno", 0))
    return lines


def _imports() -> dict[str, set[str]]:
    names = {m.stem for m in _sources.MODULES}
    graph = {}
    for module in _sources.MODULES:
        tree = _sources.tree(module)
        skip = _type_checking_only(tree)
        found = set()
        for node in ast.walk(tree):
            if getattr(node, "lineno", 0) in skip:
                continue
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names if a.name in names}
            elif isinstance(node, ast.ImportFrom) and node.module in names:
                found.add(node.module)
        graph[module.stem] = found
    return graph


def _is_route(name: str) -> bool:
    return name.startswith("em_routes_")


def test_the_graph_was_found():
    graph = _imports()
    assert all(b in graph for b in BASE)
    assert sum(_is_route(m) for m in graph) >= 10
    assert "em_routes_auth" in graph["em_api"]


def test_only_em_controller_imports_em_api():
    graph = _imports()
    importers = sorted(m for m, deps in graph.items() if "em_api" in deps)
    assert importers == ["em_controller"], importers


def test_route_modules_are_imported_only_by_the_api():
    graph = _imports()
    wrong = sorted(f"{m} imports {r}" for m, deps in graph.items()
                   for r in deps if _is_route(r)
                   and m != "em_api" and not _is_route(m))
    assert not wrong, ("route modules are for em_api to register; move what "
                       "the importer needs below them: " + "; ".join(wrong))


def test_base_modules_never_reach_the_layers_above():
    graph = _imports()

    def reach(start: str) -> dict[str, list[str]]:
        """Every module reachable from `start`, with the path to it."""
        paths, todo = {start: [start]}, [start]
        while todo:
            here = todo.pop()
            for nxt in graph.get(here, ()):
                if nxt not in paths:
                    paths[nxt] = paths[here] + [nxt]
                    todo.append(nxt)
        return paths

    wrong = []
    for base in BASE:
        for target, path in reach(base).items():
            if target in ABOVE_BASE or _is_route(target):
                wrong.append(" -> ".join(path))
    assert not wrong, "base modules reach upward: " + "; ".join(wrong)
