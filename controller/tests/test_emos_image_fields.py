"""
The emOS build POST must read every field the wizard sends it.

`/api/provision/emos_image` parses its multipart body with a hand-written
if/elif chain over `field.name`. A field with no branch is not an error and is
not logged — it is skipped, and the handler proceeds with a value of None.

That is what happened to `system_part` (#545), for the life of the feature.
The wizard resolved system_a/system_b through TWRP's by-name map, logged the
partition it had chosen, and appended it to the POST; the handler had no
branch, so `parts["system_part"]` was never set, the validation below it could
never fire, `build_emos_image` was called with `system_part=None`, and the
packer's stamping code — which is correct, and tested in test_emos_build.py —
was never reached. Every emOS image built for an amonet v2 device carried no
`emos.system=` on its cmdline, and emOS fell back to the p13 it hardcoded
before the stamp existed. On a device whose donor slot is B that is the wrong
userspace, and it boots, so nothing reports a fault.

Three things all claimed it was working: the emos-v0.6 release notes, the
wizard's own transcript naming the partition, and the controller log, which
says "with no /system stamp (older wizard)" — a message that blamed the
caller for the handler's omission.

Nothing could have caught it. The packer's tests pass `system_part` directly,
the dashboard tests cover `chooseBootSlots` (which resolves the value
correctly), and no test crossed the boundary between them. So this one asserts
across it: what dashboard.jsx appends must be what em_api.py reads.

Pure source analysis — the suite cannot import em_api, which needs aiohttp.
The Python side is read with `ast` and the JS side from the function body, so
a comment naming a field cannot satisfy either half (see the
source-guards-match-their-own-prose rule).
"""

import ast
import re
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[1]
HANDLER = "_post_provision_emos_image"


def _api_tree() -> ast.Module:
    return ast.parse((CONTROLLER / "em_api.py").read_text())


def _declared_fields() -> tuple:
    """EMOS_IMAGE_FIELDS as the module actually defines it."""
    for node in _api_tree().body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "EMOS_IMAGE_FIELDS"
               for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("em_api.EMOS_IMAGE_FIELDS not found")


def _handled_fields() -> set:
    """The field names the handler's parse loop actually branches on.

    Every string compared against `field.name`, taken from the syntax tree, so
    a name that appears only in a comment or a docstring is not counted.
    """
    handler = next(
        (n for n in ast.walk(_api_tree())
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == HANDLER),
        None)
    assert handler is not None, f"em_api.{HANDLER} not found"

    def _is_field_name(node) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "name"
                and isinstance(node.value, ast.Name)
                and node.value.id == "field")

    found = set()
    for node in ast.walk(handler):
        if not isinstance(node, ast.Compare) or not _is_field_name(node.left):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant):
                found.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                found.update(e.value for e in comparator.elts
                             if isinstance(e, ast.Constant))
            # A bare Name — `field.name not in EMOS_IMAGE_FIELDS` — is the
            # allowlist check itself, not a branch. Skipped deliberately.
    return found


def _wizard_fields() -> set:
    """Every `fd.append('<name>', …)` inside runBuildEmos, from its body.

    Sliced from the function's own text rather than the whole file, so another
    multipart POST elsewhere in the dashboard cannot contribute a name.
    """
    src = (CONTROLLER / "static" / "dashboard.jsx").read_text()
    start = src.index("async function runBuildEmos(")
    # The next declaration at the same indentation ends the body.
    end = re.search(r"\n  (?:async )?function ", src[start + 1:])
    assert end, "could not find the end of runBuildEmos"
    body = src[start:start + 1 + end.start()]
    assert "fd.append(" in body, "runBuildEmos no longer builds a FormData"
    return set(re.findall(r"fd\.append\(\s*'([^']+)'", body))


def test_the_handler_reads_every_field_the_wizard_sends():
    """
    THE #545 REGRESSION. A field appended by the wizard and not read by the
    handler is discarded in silence, and the image is built without it.
    """
    missing = _wizard_fields() - set(_declared_fields())
    assert not missing, (
        f"dashboard.jsx sends {sorted(missing)} to /api/provision/emos_image "
        f"and em_api.EMOS_IMAGE_FIELDS does not list them, so they are "
        f"dropped without an error and the image is built as though they were "
        f"never sent")


def test_system_part_specifically_crosses_the_boundary():
    """
    Named on its own because it is the one that went missing, and because
    everything downstream of it — the stamp, init's mount, the release notes —
    is correct and useless without it.
    """
    assert "system_part" in _wizard_fields(), (
        "runBuildEmos must append system_part, or no image can carry an "
        "emos.system= stamp")
    assert "system_part" in _handled_fields(), (
        "the parse loop must branch on system_part, or the value is dropped "
        "and every v2 image mounts emOS's hardcoded fallback partition")


def test_every_allowed_field_has_a_branch_that_stores_it():
    """
    The allowlist and the parse loop are two lists that must not drift: a name
    added to EMOS_IMAGE_FIELDS with no branch is admitted past the warning and
    then silently ignored, which is exactly the fault this file is about, one
    step further in.
    """
    unhandled = set(_declared_fields()) - _handled_fields()
    assert not unhandled, (
        f"EMOS_IMAGE_FIELDS names {sorted(unhandled)} but the parse loop has "
        f"no branch reading them — they are accepted and then dropped")


def test_an_unknown_field_is_logged_rather_than_skipped():
    """
    The warning is the only reason the next occurrence of this is findable at
    all: a dropped field leaves no other trace, in the log or in the image.
    """
    handled = _handled_fields()
    assert handled, "the parse loop no longer branches on field.name"
    src = (CONTROLLER / "em_api.py").read_text()
    start = src.index(f"async def {HANDLER}")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    assert "EMOS_IMAGE_FIELDS" in body, (
        "the parse loop must check the allowlist")
    assert re.search(r"log\.warning\([^)]*field\.name", body, re.S), (
        "an unrecognised multipart field must be logged with its name — "
        "without it, a field the wizard sends and the handler ignores is "
        "invisible at every layer")
