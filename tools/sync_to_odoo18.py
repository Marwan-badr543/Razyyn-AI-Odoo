#!/usr/bin/env python3
"""Generate the Odoo 18 module from the Odoo 17 one, and check it stays generated.

WHY THIS EXISTS RATHER THAN TWO HAND-KEPT COPIES
    ``project_rules.md`` §5 already records what happens when one product is
    shipped for two framework versions and the copies are maintained by hand:
    the Frappe chat UI lives in two benches, and every change has to be applied
    to both "immediately, to prevent divergence". That works because a person
    is watching. It stops working the moment a change is small enough to look
    harmless in one tree — and the symptom is not a failed build, it is one
    version of the product quietly behaving differently from the other.

    So the Odoo 17 module is the SOURCE, the Odoo 18 module is GENERATED, and
    the difference between them is three rules written down below rather than a
    habit. Editing the 18 tree by hand is a mistake this script will tell you
    about.

THE THREE RULES, AND WHY EACH ONE IS REAL
    1. The manifest names the series it targets. Cosmetic, but a module
       reporting 17.0 inside an Odoo 18 database misleads anyone reading the
       Apps list about what they are running.

    2. ``ir.cron`` lost ``numbercall`` in Odoo 18 — a recurring job simply
       recurs. A <field> naming a column the model no longer has does not get
       ignored; it aborts the entire module install. On 17 the field is
       REQUIRED in the other direction: the default is 1, so a job written
       without it runs exactly once and the customer's generated reports are
       never cleaned up again.

    3. Odoo 18 renamed the list view's tag from ``<tree>`` to ``<list>``, and
       each version accepts only its own. 17's ir.ui.view offers no 'list'
       type; 18 refuses 'tree' outright with "Invalid view type". There is no
       spelling that works on both, which is exactly why this is a transform
       and not a shared file.

USAGE
    python3 tools/sync_to_odoo18.py            # regenerate the 18 tree
    python3 tools/sync_to_odoo18.py --check    # fail if it has drifted

``--check`` is the one to put in CI. It regenerates into a temporary directory
and compares, so it reports drift without silently overwriting whatever the
person who introduced it was in the middle of.
"""

from __future__ import annotations

import argparse
import filecmp
import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE.parent / "razyyn_ai"
DEFAULT_TARGET = pathlib.Path(
    "/home/marwan/Programming/odoo/odoo_18/odoo/odoo/custom_addons/razyyn-odoo-18/razyyn_ai"
)

#: Copied beside the module, unchanged, because they are ABOUT the module and
#: none of the three rules applies to them.
#:
#: `tests` is here because it drifted. The two copies were kept in step by
#: hand, which worked until it did not: the Odoo 18 tree was five test cases
#: behind, and five cases fewer is not a failure anybody sees -- the suite still
#: says OK. The whole argument of this script applied to the files that are
#: supposed to catch the thing this script prevents.
#: `TESTING_THE_CREATE_DESK.md` is here for the same reason as the README:
#: it is written for the customer, it names both ports, and a copy that
#: says something the other version no longer does is a defect like any
#: other. Nothing in it is version-specific, so it is copied unchanged.
#: `.pylintrc` is here because it is the rules the COPIED CODE is written to.
#: The 18 tree kept its own by hand, and the two fell out of step the moment
#: the 17 source stopped using `self.env._()` — an idiom that does not exist
#: before Odoo 18 and crashed every error path that reached it. The generated
#: code then used `_()` while the generated tree's linter still demanded
#: `env._()`, so the only tree that could lint clean was the one whose code was
#: broken. A linter config that disagrees with the code it lints is drift like
#: any other, and this is the file that says so.
VERBATIM = ("tests", "requirements.txt", "README.md",
            "TESTING_THE_CREATE_DESK.md", ".pylintrc")

#: The README is generated too, with a fourth rule applied to two lines of it.
#: It was drifting the worst of anything here -- the Odoo 18 copy still
#: described a chat page this repository replaced, and was about to describe a
#: pip command the module now runs itself. A document that tells a customer to
#: do something the product stopped needing is a defect like any other.
_README_TITLE = "# Razyyn AI for Odoo 17"

_README_BANNER = """# Razyyn AI for Odoo 18

> **`razyyn_ai/` here is GENERATED. Do not edit it.**
>
> Every file under `razyyn_ai/` is produced from the Odoo 17 module by
> `razyyn-odoo-17/tools/sync_to_odoo18.py`. Make the change there and re-run
> that script; anything edited here is overwritten the next time anyone does.
> `sync_to_odoo18.py --check` fails if the two have drifted, which is what
> stops that from happening silently."""

_README_SIBLING = """- **the same module for Odoo 18** \u2014 `razyyn-odoo-18/`, GENERATED from this one
  (see "Odoo 17 and 18" below)"""

_README_SIBLING_18 = "- **the source this tree is generated from** \u2014 `razyyn-odoo-17/`"


def _transform_readme(text: str) -> str:
    """Rule 4: the title, its banner, and the bullet pointing at the other tree.

    Both swaps are asserted rather than attempted. If someone rewrites either
    line in the Odoo 17 README, this fails loudly at build time -- which is the
    whole point, because the alternative is an Odoo 18 README that quietly
    stops being generated from anything.
    """
    for marker in (_README_TITLE, _README_SIBLING):
        if marker not in text:
            raise SystemExit(
                "sync_to_odoo18: the Odoo 17 README no longer contains the line "
                f"this script rewrites for Odoo 18:\n\n{marker}\n\n"
                "Restore it, or update _transform_readme."
            )
    text = text.replace(_README_TITLE, _README_BANNER, 1)
    return text.replace(_README_SIBLING, _README_SIBLING_18, 1)

#: Never copied: build artefacts and version control.
_SKIP_DIRS = {"__pycache__", ".git"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}

_VIEW_MODE = re.compile(
    r"<field name=\"view_mode\">[^<]*</field>"
)

_CRON_NUMBERCALL = re.compile(
    r"[ \t]*<field name=\"numbercall\">-?\d+</field>\n"
)

_CRON_REPLACEMENT = (
    "            <!-- No `numbercall` here: Odoo 18 removed the field from\n"
    "                 ir.cron (a recurring job simply recurs), and a <field>\n"
    "                 naming a column the model no longer has aborts the whole\n"
    "                 module install. Generated by tools/sync_to_odoo18.py —\n"
    "                 edit the Odoo 17 copy, not this one. -->\n"
)

_GENERATED_BANNER = (
    "<!-- GENERATED from the Odoo 17 module by tools/sync_to_odoo18.py.\n"
    "     Edit razyyn-odoo-17/razyyn_ai/{name} and re-run that script;\n"
    "     changes made here are overwritten. -->\n"
)


def _transform_xml(text: str, name: str) -> str:
    """Rule 2 and rule 3, plus a banner saying where this file came from."""
    text = _CRON_NUMBERCALL.sub(_CRON_REPLACEMENT, text)

    # `<tree` / `</tree>` only as a TAG — never inside an attribute value or a
    # word like "treeview", which a blind string replace would corrupt.
    text = re.sub(r"<tree(\s|>|/)", r"<list\1", text)
    text = text.replace("</tree>", "</list>")

    # AND THE VIEW MODE, which is the same rename in a different place and was
    # missed. Odoo 18 refuses an action whose `view_mode` says "tree" with
    # "View types not defined tree found in act_window action" -- so every list
    # in the app opened as an error dialog, on Odoo 18 only. Nothing that
    # speaks to the module over HTTP can see this; it takes a browser.
    text = _VIEW_MODE.sub(
        lambda match: match.group(0).replace("tree", "list"), text
    )

    if text.lstrip().startswith("<?xml"):
        head, _, rest = text.partition("\n")
        return head + "\n" + _GENERATED_BANNER.format(name=name) + rest
    return _GENERATED_BANNER.format(name=name) + text


def _transform_manifest(text: str) -> str:
    """Rule 1."""
    text = re.sub(r'"version":\s*"17\.', '"version": "18.', text)
    return text.replace("Razyyn AI for Odoo 17", "Razyyn AI for Odoo 18")


def generate(source: pathlib.Path, target: pathlib.Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for path in sorted(source.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue

        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".xml":
            destination.write_text(
                _transform_xml(path.read_text(encoding="utf-8"), path.name),
                encoding="utf-8",
            )
        elif path.name == "__manifest__.py":
            destination.write_text(
                _transform_manifest(path.read_text(encoding="utf-8")), encoding="utf-8"
            )
        else:
            shutil.copy2(path, destination)


def generate_verbatim(source_root: pathlib.Path, target_root: pathlib.Path) -> None:
    """Copy VERBATIM across. No transform: they reference the module by a
    relative path, which is the same relative path in either tree."""
    for name in VERBATIM:
        source = source_root / name
        target = target_root / name
        if not source.exists():
            continue
        if name == "README.md":
            target.write_text(_transform_readme(source.read_text(encoding="utf-8")),
                              encoding="utf-8")
        elif source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(
                source, target,
                ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc", "*.pyo"),
            )
        else:
            shutil.copy2(source, target)


def _verbatim_differences(source_root: pathlib.Path, target_root: pathlib.Path) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        staged = pathlib.Path(tmp)
        generate_verbatim(source_root, staged)
        out: list[str] = []
        for name in VERBATIM:
            expected, actual = staged / name, target_root / name
            if not expected.exists():
                continue
            if not actual.exists():
                out.append(f"missing from the Odoo 18 repository: {name}")
            elif expected.is_dir():
                out.extend(f"{name}/{line}" for line in _differences(expected, actual))
            elif expected.read_bytes() != actual.read_bytes():
                out.append(f"differs: {name}")
        return out


def _differences(left: pathlib.Path, right: pathlib.Path) -> list[str]:
    """Every file that differs between two generated trees, recursively."""
    out: list[str] = []

    def walk(comparison: filecmp.dircmp, prefix: str) -> None:
        for name in comparison.left_only:
            out.append(f"only in the generated tree: {prefix}{name}")
        for name in comparison.right_only:
            out.append(f"only in the committed tree:  {prefix}{name}")
        for name in comparison.diff_files:
            out.append(f"differs: {prefix}{name}")
        for name, sub in comparison.subdirs.items():
            walk(sub, f"{prefix}{name}/")

    walk(filecmp.dircmp(str(left), str(right)), "")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--check", action="store_true",
        help="Report drift instead of regenerating. Exits non-zero if the Odoo "
             "18 tree is not what this script would produce.",
    )
    args = parser.parse_args()

    if not SOURCE.is_dir():
        print(f"Source module not found: {SOURCE}", file=sys.stderr)
        return 2

    source_root = SOURCE.parent
    target_root = args.target.parent

    if not args.check:
        generate(SOURCE, args.target)
        generate_verbatim(source_root, target_root)
        print(f"Regenerated {args.target} and {', '.join(VERBATIM)} from {source_root}")
        return 0

    if not args.target.is_dir():
        print(f"Odoo 18 module missing entirely: {args.target}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        expected = pathlib.Path(tmp) / "razyyn_ai"
        generate(SOURCE, expected)
        drift = _differences(expected, args.target)
    drift += _verbatim_differences(source_root, target_root)

    if drift:
        print("The Odoo 18 module has drifted from the Odoo 17 source:",
              file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print("\nMake the change in razyyn-odoo-17/razyyn_ai and re-run "
              "tools/sync_to_odoo18.py.", file=sys.stderr)
        return 1

    print("Odoo 18 module is in step with the Odoo 17 source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
