"""
The helper source guards use to find code (_sources.py) must fail loudly when
it finds nothing, or every guard built on it passes on nothing.
"""

import ast

import pytest

import _sources


def test_every_module_is_found():
    names = {p.name for p in _sources.MODULES}
    assert {"em_controller.py", "em_api.py", "em_esphome.py", "em_state.py"} <= names
    assert len(names) > 50


def test_find_def_finds_a_function_wherever_it_lives():
    path, node = _sources.find_def("forget_oww_models")
    assert path.name == "em_state.py"
    assert isinstance(node, ast.FunctionDef)


def test_find_def_fails_on_a_missing_function():
    with pytest.raises(AssertionError, match="not defined"):
        _sources.find_def("no_such_function_anywhere")


def test_find_def_fails_on_a_name_defined_twice():
    # __init__ is defined by many classes: an ambiguous name is not an answer.
    with pytest.raises(AssertionError, match="defined"):
        _sources.find_def("__init__")


def test_code_only_drops_comments_and_docstrings_but_keeps_strings():
    text = ('def f():\n'
            '    """mentions forbidden()"""\n'
            '    # forbidden() in a comment\n'
            '    return "kept"\n')
    code = _sources.code_only(text)
    assert "forbidden" not in code
    assert "'kept'" in code


def test_code_only_takes_an_indented_method():
    assert "x = 1" in _sources.code_only("    def m(self):\n        x = 1\n")
