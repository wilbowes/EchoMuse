"""
How big the controller's modules and functions are, against a baseline that
only shrinks.

em_api reached 6,680 lines and handle_control 1,140 before anyone split
them, because nothing objected to the next 50. tests/test_size_ratchet.py
holds the line: a module over FILE_CAP lines or a function over FN_CAP is
listed in tests/size_baseline.json at its current size and may not grow;
anything not listed may not cross the cap. When a listed one shrinks by more
than SLACK, the baseline is lowered to keep the gain:

    python controller/tools/size_ratchet.py --update

Growing a listed item is a deliberate act: raise its number in the baseline
in the same change, where a reviewer sees it.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
BASELINE = CONTROLLER / "tests" / "size_baseline.json"
FILE_CAP = 1500
FN_CAP = 150
SLACK = 25


def measure() -> dict[str, dict[str, int]]:
    """Every em_*.py over FILE_CAP lines, and every function over FN_CAP."""
    files, functions = {}, {}
    for path in sorted(CONTROLLER.glob("em_*.py")):
        text = path.read_text()
        lines = text.count("\n")
        if lines > FILE_CAP:
            files[path.name] = lines
        tree = ast.parse(text)
        stack = [(node, "") for node in tree.body]
        while stack:
            node, prefix = stack.pop()
            if isinstance(node, ast.ClassDef):
                stack += [(child, f"{prefix}{node.name}.") for child in node.body]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > FN_CAP:
                    functions[f"{path.stem}.{prefix}{node.name}"] = length
    return {"files": files, "functions": functions}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true",
                        help="rewrite the baseline to today's sizes")
    args = parser.parse_args()
    now = measure()
    if args.update:
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print(f"wrote {BASELINE.relative_to(CONTROLLER.parent)}")
    else:
        print(json.dumps(now, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
