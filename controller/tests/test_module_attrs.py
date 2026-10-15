"""
Every `module.name` a controller module reads must exist in that module.

Splitting em_api moves names between modules, and a name another module
reads through the old one is invisible to pyflakes: to em_api,
`from version import VERSION as CONTROLLER_VERSION` was an unused import,
and removing it left em_controller's `api.CONTROLLER_VERSION` to raise
AttributeError at startup. The suite passed, because nothing in it runs
em_controller.main(). This reads every module's syntax tree instead: for
each `import em_x [as y]`, every `y.attr` it loads must be defined, assigned
or imported at the top level of em_x (or declared `global` in it).
"""

import ast
from pathlib import Path

import _sources


def _defined(tree: ast.Module) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # Top-level conditionals and try/except imports still define
            # module attributes.
            for field in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(node, field, []) or []:
                    stack.extend(child.body if isinstance(child, ast.ExceptHandler) else [child])
    return names


def test_the_modules_were_found():
    names = {m.stem for m in _sources.MODULES}
    assert {"em_api", "em_controller", "em_state"} <= names


def test_every_module_attribute_read_exists():
    trees = {m.stem: _sources.tree(m) for m in _sources.MODULES}
    defined = {name: _defined(tree) for name, tree in trees.items()}
    missing = []
    for name, tree in sorted(trees.items()):
        aliases = {a.asname or a.name: a.name
                   for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for a in node.names if a.name in trees}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                    and node.attr not in defined[aliases[node.value.id]]):
                missing.append(f"{name}.py:{node.lineno}: {node.value.id}.{node.attr} "
                               f"is not defined in {aliases[node.value.id]}")
    assert not missing, "\n".join(missing)


def test_tests_read_only_what_the_modules_define():
    """
    The same rule over the test files. A test that reaches a module through
    another (em_api.em_payloads) or reads a name the split moved breaks when
    an import is tidied away, and one that assigns a name onto a module first
    passes on state nothing in production reads. Names the test file itself
    assigns or monkeypatches, and dunders, are its own business.
    """
    trees = {m.stem: _sources.tree(m) for m in _sources.MODULES}
    defined = {name: _defined(tree) for name, tree in trees.items()}
    tests = sorted(Path(__file__).resolve().parent.glob("test_*.py"))
    assert len(tests) > 50
    missing = []
    for path in tests:
        tree = ast.parse(path.read_text())
        aliases = {a.asname or a.name: a.name
                   for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for a in node.names if a.name in trees}
        own = {(n.value.id, n.attr) for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
               and isinstance(n.value, ast.Name)}
        own |= {(c.args[0].id, c.args[1].value) for c in ast.walk(tree)
                if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "setattr"
                and len(c.args) >= 2 and isinstance(c.args[0], ast.Name)
                and isinstance(c.args[1], ast.Constant)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases
                    and not node.attr.startswith("__")
                    and (node.value.id, node.attr) not in own
                    and node.attr not in defined[aliases[node.value.id]]):
                missing.append(f"{path.name}:{node.lineno}: {node.value.id}.{node.attr} "
                               f"is not defined in {aliases[node.value.id]}")
    assert not missing, "\n".join(missing)
