#!/usr/bin/env python3
"""Copy the chat page's browser code and transcript logic out of the Frappe app.

WHY THE ODOO MODULE DOES NOT HAVE A CHAT UI OF ITS OWN
    It had one: seven hundred lines of hand-written page against the Frappe
    app's eight thousand. It could not stream, it saved nothing, and it reached
    only one of the agent's desks. Writing a second one would have produced the
    same outcome a year later, because two user interfaces for one product drift
    the moment either is touched -- which is the defect this repository has now
    shipped twice (the write adapter, and Odoo 17 against Odoo 18).

    So there is ONE chat interface. It lives in the Frappe app, it is written
    against seventeen `frappe.*` calls, and `razyyn_platform.js` answers those
    seventeen calls on Odoo. This script copies it across; `--check` fails a
    build when the copy has fallen behind. Improving the chat window means
    editing the Frappe app, running this, and both products have the change.

WHAT IS REWRITTEN ON THE WAY THROUGH, AND IT IS ONLY ASSET PATHS
    Frappe serves the libraries from `/assets/accountant_agent/js/` and three of
    them from a CDN. Odoo serves all of them from `/razyyn_ai/static/lib/`. The
    Subresource Integrity hashes travel unchanged and `--check` verifies the
    vendored bytes against them, so a mis-vendored library is a build failure
    rather than a chat window that silently renders no tables.

Usage:
    python3 tools/sync_from_frappe.py            # copy
    python3 tools/sync_from_frappe.py --check    # fail if it has drifted
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "razyyn_ai"

#: Where the Frappe app might be. First one that has the chat page wins.
DEFAULT_FRAPPE_APPS = (
    "~/frappe/my-bench/apps/accountant_agent",
    "~/frappe/frappe-bench-v14/apps/accountant_agent",
    "~/frappe-bench/apps/accountant_agent",
)

PAGE = "accountant_agent/accountant_agent/page/agent_chat"

#: The page's entry point. It opens with Frappe's own `{% include %}`
#: directives, which Frappe's asset builder resolves into one file before the
#: browser ever sees it -- and which therefore also declare the ORDER the parts
#: must load in. This script resolves them the same way, so the load order is
#: the Frappe app's rather than a list somebody kept in step by hand here.
ENTRY_POINT = "agent_chat.js"

#: Copied byte-for-byte, asset paths aside.
BROWSER_FILES = (ENTRY_POINT, "agent_chat.css")

#: `{% include "<app>/<path>/<file>.js" %}` -- Frappe's build-time include.
INCLUDE = re.compile(r"^\{%\s*include\s+\"([^\"]+)\"\s*%\}\s*$", re.MULTILINE)

#: Frappe asset URL -> Odoo asset URL. Applied to every browser file.
URL_REWRITES = (
    ("/assets/accountant_agent/js/", "/razyyn_ai/static/lib/"),
    ("https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js",
     "/razyyn_ai/static/lib/chart.umd.js"),
    ("https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js",
     "/razyyn_ai/static/lib/purify.min.js"),
    ("https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js",
     "/razyyn_ai/static/lib/marked.min.js"),
)

#: The marked regions of agent_chat.py, in the order they are written out.
TRANSCRIPT_BLOCKS = ("transcript-markup", "transcript-readable", "transcript-answer")

TRANSCRIPT_HEADER = '''\
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

# GENERATED FILE -- DO NOT EDIT.
#
# Copied verbatim out of the Frappe app by tools/sync_from_frappe.py, from the
# blocks marked "SHARED WITH THE ODOO MODULE" in
#   accountant_agent/accountant_agent/page/agent_chat/agent_chat.py
#
# WHY IT IS SHARED RATHER THAN REWRITTEN HERE
#     What these functions do is not plumbing -- it is the difference between a
#     customer reading their answer and a customer reading
#     `{"type": "clarification", "questions": [{"id": ...`. Each rule in here
#     was put there by a specific complaint about a specific screen, and a
#     second implementation would have to be told about each of those
#     complaints again, one regression at a time.
#
# To change any of it: edit the Frappe app, then run
#   python3 tools/sync_from_frappe.py
# `--check` fails the build when this file and the Frappe source disagree.

import json
import re
from base64 import b64decode, b64encode
from html import escape, unescape

from odoo import _

'''


#: Reading the words out of a photographed invoice. Copied whole, because it is
#: already almost pure: two imports come out and a short preamble goes in, and
#: the four hundred lines of Tesseract handling in between are untouched.
OCR_DROP_LINES = ("from accountant_agent.agent_config import get_ocr_languages\n",)

#: Placed at the very top, where only comments may go: the file's own docstring
#: and its `from __future__` import must stay the first statements in it.
OCR_BANNER = """\
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE
#
# GENERATED FILE -- DO NOT EDIT. Copied from the Frappe app's ocr.py by
# tools/sync_from_frappe.py, which replaces its two framework imports with the
# shim below and changes nothing else.
#
# WHY IT IS COPIED RATHER THAN REWRITTEN
#     Reading a figure off a photograph correctly is the whole value of this
#     file, and every number in it -- the page layout mode, the two-pages-at-once
#     batching, the confidence threshold below which a picture is declared to
#     have no words in it -- was measured rather than chosen. A second
#     implementation would be a second set of guesses.

"""

#: Goes in where `import frappe` came out, so the body below it is untouched.
OCR_SHIM = """\
import logging as _logging

_logger = _logging.getLogger(__name__)


class _FrappeShim:
    \"\"\"The two calls ocr.py makes into its framework, answered by Odoo's log.\"\"\"

    @staticmethod
    def log_error(title="", message=""):
        _logger.warning("Razyyn AI OCR: %s: %s", title, message)


frappe = _FrappeShim()

#: Tesseract language codes. Set by ocr_service.py from the site's own
#: configuration before any reading starts -- the page workers that do the
#: reading must not reach into the database themselves.
OCR_LANGUAGES = "eng+ara"


def get_ocr_languages() -> str:
    return OCR_LANGUAGES
"""


def _ocr_module(source: str) -> str:
    """The Frappe app's ocr.py, with its framework imports swapped for a shim."""
    if "import frappe\n" not in source:
        raise SystemExit(
            "ocr.py no longer imports frappe the way this script expects. Look at "
            "what changed before syncing -- the generated module would not import."
        )
    body = source.replace("import frappe\n", OCR_SHIM, 1)
    for line in OCR_DROP_LINES:
        body = body.replace(line, "", 1)
    return OCR_BANNER + body


def _find_frappe_app(explicit: str | None) -> Path:
    candidates = [explicit] if explicit else list(DEFAULT_FRAPPE_APPS)
    if not explicit and os.environ.get("RAZYYN_FRAPPE_APP"):
        candidates.insert(0, os.environ["RAZYYN_FRAPPE_APP"])
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if (path / PAGE / "agent_chat.js").is_file():
            return path
    raise SystemExit(
        "Could not find the Frappe app. Pass --frappe-app /path/to/apps/accountant_agent "
        "or set RAZYYN_FRAPPE_APP.\nLooked in: " + ", ".join(str(c) for c in candidates)
    )


def _resolve_includes(source: str, app: Path, page: Path) -> str:
    """Inline Frappe's `{% include %}` directives, in the order they are written.

    This is what Frappe's asset builder does before serving the page, so the
    result is the file the browser actually runs on ERPNext -- same parts, same
    order. Resolving it here rather than listing the parts in the Odoo page
    template means the order can never be got wrong on one product and right on
    the other: there is only one place it is written down, and it is upstream.
    """
    banner = [
        "/* GENERATED by tools/sync_from_frappe.py -- DO NOT EDIT.",
        " *",
        " * The Razyyn chat window, assembled out of the Frappe app exactly as",
        " * Frappe's own asset builder assembles it: its `{% include %}` lines,",
        " * resolved in the order they are written. To change anything in here,",
        " * edit the Frappe app and run the sync.",
        " */",
        "",
    ]
    parts = list(banner)

    def take(relative: str) -> None:
        path = (app / relative).resolve()
        if not path.is_file():
            raise SystemExit(
                f"{ENTRY_POINT} includes {relative}, which does not exist. The chat "
                f"window would load with a piece missing."
            )
        parts.append(f"/* ─── {path.name} ─────────────────────────────── */")
        parts.append(path.read_text(encoding="utf-8").rstrip("\n"))
        parts.append("")

    for relative in INCLUDE.findall(source):
        take(relative)

    parts.append(f"/* ─── {ENTRY_POINT} ─────────────────────────────── */")
    parts.append(INCLUDE.sub("", source).lstrip("\n"))
    return "\n".join(parts)


def _rewrite(text: str) -> str:
    for frappe_url, odoo_url in URL_REWRITES:
        text = text.replace(frappe_url, odoo_url)
    return text


def _extract_blocks(source: str) -> str:
    """The marked regions of agent_chat.py, concatenated, in declared order."""
    out = [TRANSCRIPT_HEADER]
    for tag in TRANSCRIPT_BLOCKS:
        pattern = re.compile(
            r"^# --- .*BEGIN " + re.escape(tag) + r"\n(.*?)^# --- END " + re.escape(tag) + r"\n",
            re.DOTALL | re.MULTILINE,
        )
        found = pattern.search(source)
        if not found:
            raise SystemExit(
                f"The Frappe source has no block marked '{tag}'. Someone removed the\n"
                f"markers, or renamed the block. Re-mark it in agent_chat.py -- the\n"
                f"Odoo chat cannot render a question without this code."
            )
        out.append(found.group(1).strip("\n") + "\n")
    return "\n\n".join(out)


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


#: Every local library the copied page loads. The gap between `src` and
#: `integrity` must not cross another `script.src`, or a library loaded without
#: a hash inherits the next one's and is reported as mis-vendored when it is
#: perfectly fine.
_LIB_REFERENCE = re.compile(r"[\"'](/razyyn_ai/static/lib/[^\"']+)[\"']")
_LIB_WITH_INTEGRITY = re.compile(
    r"script\.src\s*=\s*'(/razyyn_ai/static/lib/[^']+)';"
    r"(?:(?!script\.src)[\s\S])*?"
    r"script\.integrity\s*=\s*'(sha384-[^']+)';"
)


def _referenced_libraries(texts: dict[str, str]) -> set[str]:
    """Every /razyyn_ai/static/lib/ path the copied page asks the browser for."""
    found: set[str] = set()
    for text in texts.values():
        found.update(_LIB_REFERENCE.findall(text))
    return found


def _integrity_expectations(texts: dict[str, str]) -> dict[str, str]:
    """Local library path -> the SRI hash the copied JS asks the browser for."""
    wanted: dict[str, str] = {}
    for text in texts.values():
        for src, integrity in _LIB_WITH_INTEGRITY.findall(text):
            wanted[src] = integrity
    return wanted


#: Every `<app>.<path>.<method>` the copied chat window asks its server for.
_SERVER_METHOD = re.compile(r"accountant_agent\.[A-Za-z0-9_.]*\.([a-z_]+)")

#: The list razyyn_platform.js will answer. A method missing from it does not
#: fail quietly: the shim throws, naming itself and the route to add. But it
#: throws in the BROWSER, at the moment a customer uses that feature -- which
#: for the plan-usage badge was after everything else had been signed off.
_KNOWN_METHODS = re.compile(r"KNOWN_METHODS = new Set\(\[(.*?)\]\)", re.DOTALL)


def _check_every_method_is_answered(entry_point: str, problems: list[str]) -> None:
    """Every server call the chat window makes must have a route on this side."""
    shim = (MODULE / "static/src/chat/razyyn_platform.js").read_text(encoding="utf-8")
    declared = _KNOWN_METHODS.search(shim)
    if not declared:
        problems.append(
            "razyyn_platform.js has no KNOWN_METHODS list any more, so nothing "
            "checks that the chat window's calls are answered here."
        )
        return

    known = set(re.findall(r'"([a-z_]+)"', declared.group(1)))
    called = set(_SERVER_METHOD.findall(entry_point))
    missing = sorted(called - known)
    if missing:
        problems.append(
            "the chat window calls " + ", ".join(missing) + ", which this module "
            "does not implement. Add a /razyyn/api/<name> route in "
            "controllers/chat_client_api.py and list it in KNOWN_METHODS."
        )

    routes = (MODULE / "controllers/chat_client_api.py").read_text(encoding="utf-8")
    unrouted = sorted(
        name for name in known
        if f'"/razyyn/api/{name}"' not in routes
    )
    if unrouted:
        problems.append(
            "razyyn_platform.js promises " + ", ".join(unrouted) + ", but there "
            "is no route of that name in controllers/chat_client_api.py."
        )


def _check_vendored_libraries(texts: dict[str, str], problems: list[str]) -> None:
    integrity_of = _integrity_expectations(texts)
    for src in sorted(_referenced_libraries(texts)):
        vendored = MODULE / src[len("/razyyn_ai/"):]
        if not vendored.is_file():
            problems.append(
                f"{src} is loaded by the chat page but is not vendored at {vendored}."
            )
            continue
        integrity = integrity_of.get(src)
        if not integrity:
            continue
        actual = _sri(vendored.read_bytes())
        if actual != integrity:
            problems.append(
                f"{src} does not match the integrity hash the page asks for.\n"
                f"    page wants: {integrity}\n"
                f"    file is:    {actual}\n"
                f"    The browser will refuse to run it and the chat window will "
                f"lose whatever it provides."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frappe-app", default=None)
    parser.add_argument("--check", action="store_true",
                        help="Report drift and exit non-zero; write nothing.")
    args = parser.parse_args()

    app = _find_frappe_app(args.frappe_app)
    page = app / PAGE

    generated: dict[Path, str] = {}
    browser_texts: dict[str, str] = {}

    for name in BROWSER_FILES:
        source = (page / name).read_text(encoding="utf-8")
        if name == ENTRY_POINT:
            source = _resolve_includes(source, app, page)
        rewritten = _rewrite(source)
        browser_texts[name] = rewritten
        generated[MODULE / "static/src/chat/frappe" / name] = rewritten

    generated[MODULE / "services/transcript.py"] = _extract_blocks(
        (page / "agent_chat.py").read_text(encoding="utf-8")
    )
    generated[MODULE / "services/ocr.py"] = _ocr_module(
        (app / "accountant_agent/ocr.py").read_text(encoding="utf-8")
    )

    problems: list[str] = []
    _check_vendored_libraries(browser_texts, problems)
    _check_every_method_is_answered(browser_texts[ENTRY_POINT], problems)

    if args.check:
        for target, text in generated.items():
            if not target.is_file():
                problems.append(f"{target.relative_to(MODULE.parent)} is missing.")
            elif target.read_text(encoding="utf-8") != text:
                problems.append(
                    f"{target.relative_to(MODULE.parent)} has drifted from the Frappe app."
                )
        if problems:
            print("The Odoo chat is out of step with the Frappe app:\n")
            for problem in problems:
                print("  - " + problem)
            print(f"\nSource: {page}")
            print("Run: python3 tools/sync_from_frappe.py")
            return 1
        print(f"The Odoo chat is in step with the Frappe app at {app}.")
        return 0

    for target, text in generated.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target.relative_to(MODULE.parent)}")

    if problems:
        print("\nCopied, but the vendored libraries need attention:\n")
        for problem in problems:
            print("  - " + problem)
        return 1
    print(f"\nIn step with {app}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
