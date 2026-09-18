# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""The module installs what reading a scanned document needs. These say so.

Every case here is a way the install can report success and leave the customer
with no reader, because that is the failure mode that costs a day: a pip that
exits 0 having written somewhere unimportable, a migration directory named so
that Odoo can never reach it, a manifest field that refuses the install it was
meant to describe.

No Odoo needed: dependencies.py is standard library on purpose.
    cd tests && python3 -m unittest test_dependencies -v
"""

from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

_HERE = pathlib.Path(__file__).resolve().parent
_MODULE = _HERE.parent / "razyyn_ai"

_spec = importlib.util.spec_from_file_location(
    "razyyn_dependencies_under_test", _MODULE / "services" / "dependencies.py"
)
dependencies = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dependencies
_spec.loader.exec_module(dependencies)


def _package(name):
    return dependencies.Package(module=name, requirement=name, purpose="testing")


def _pip_is_installed():
    """Say pip is there.

    The tests may be run by a system python whose distribution ships pip
    separately, and "there is no pip" is a real branch of ensure() that would
    otherwise swallow the branch under test.
    """
    return mock.patch.object(
        dependencies.importlib.util, "find_spec", return_value=object()
    )


def _outside_a_virtual_environment():
    return mock.patch.multiple(dependencies.sys, prefix="/usr", base_prefix="/usr")


class WhichPipIsAsked(unittest.TestCase):
    """The interpreter running Odoo, and no other."""

    def test_never_a_pip_from_the_path(self):
        # Two Odoo installs share this machine, each with its own virtual
        # environment. A bare `pip` resolves to whichever is first on PATH,
        # which installs the reader into the other product.
        for command in dependencies._pip_commands():
            self.assertEqual(command[:3], [sys.executable, "-m", "pip"])

    def test_inside_a_virtual_environment_there_is_no_user_fallback(self):
        # THE SILENT ONE. A virtual environment sets ENABLE_USER_SITE False, so
        # `pip install --user` writes to ~/.local, exits 0, and the import
        # still fails -- an install that reports success and does nothing.
        with mock.patch.multiple(dependencies.sys, prefix="/venv", base_prefix="/usr"):
            commands = dependencies._pip_commands()

        self.assertEqual(len(commands), 1)
        self.assertNotIn("--user", commands[0])
        self.assertNotIn("--break-system-packages", commands[0])

    def test_outside_one_it_falls_back_twice_in_order(self):
        with _outside_a_virtual_environment():
            commands = dependencies._pip_commands()

        self.assertEqual(len(commands), 3)
        self.assertNotIn("--user", commands[0])
        self.assertIn("--user", commands[1])
        # Last: it overrides a refusal the distribution meant (PEP 668).
        self.assertIn("--break-system-packages", commands[2])

    def test_pip_is_never_asked_a_question(self):
        for command in dependencies._pip_commands():
            self.assertIn("--no-input", command)


class WhatCountsAsInstalled(unittest.TestCase):
    """pip's exit code is not the answer. The import is."""

    def setUp(self):
        self._packages = mock.patch.object(
            dependencies, "PYTHON_PACKAGES", (_package("alpha"), _package("beta"))
        )
        self._packages.start()
        self.addCleanup(self._packages.stop)
        self._binaries = mock.patch.object(dependencies, "SYSTEM_BINARIES", ())
        self._binaries.start()
        self.addCleanup(self._binaries.stop)
        self._env = mock.patch.dict(
            os.environ, {dependencies.SKIP_ENVIRONMENT_VARIABLE: ""}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_nothing_missing_runs_no_pip(self):
        with mock.patch.object(dependencies, "missing_packages", return_value=[]), \
             mock.patch.object(dependencies, "_run") as run:
            report = dependencies.ensure()

        run.assert_not_called()
        self.assertEqual(report.already_present, ["alpha", "beta"])
        self.assertTrue(report.python_ready)

    def test_a_pip_that_exits_zero_but_cannot_be_imported_is_not_success(self):
        # Exactly the --user-inside-a-venv trap, and the reason success is
        # decided by asking for the import again rather than by a return code.
        attempts = []
        still_missing = [_package("alpha")]

        def run(command, requirements):
            attempts.append(command)
            return dependencies.Attempt(command + requirements, 0, "Successfully installed")

        with _outside_a_virtual_environment(), _pip_is_installed(), \
             mock.patch.object(dependencies, "missing_packages", side_effect=lambda: still_missing), \
             mock.patch.object(dependencies, "_run", side_effect=run):
            report = dependencies.ensure()

        self.assertEqual(len(attempts), 3, "it stopped at the first exit code of 0")
        self.assertFalse(report.python_ready)
        self.assertEqual(report.failed, ["alpha"])

    def test_it_stops_at_the_first_one_that_actually_worked(self):
        answers = [[_package("alpha")], []]

        def missing():
            return answers.pop(0) if answers else []

        def run(command, requirements):
            return dependencies.Attempt(command + requirements, 0, "ok")

        with _outside_a_virtual_environment(), _pip_is_installed(), \
             mock.patch.object(dependencies, "missing_packages", side_effect=missing), \
             mock.patch.object(dependencies, "_run", side_effect=run) as run_mock:
            report = dependencies.ensure()

        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(report.installed, ["alpha"])
        self.assertTrue(report.python_ready)

    def test_a_failed_first_attempt_is_followed_by_the_next(self):
        codes = [1, 0]

        def run(command, requirements):
            return dependencies.Attempt(command + requirements, codes.pop(0), "output")

        # ensure() asks once at the start and again after each attempt that
        # pip said worked; the first attempt fails, so it asks twice in all.
        answers = [[_package("alpha")], []]

        with _outside_a_virtual_environment(), _pip_is_installed(), \
             mock.patch.object(dependencies, "missing_packages", side_effect=lambda: answers.pop(0)), \
             mock.patch.object(dependencies, "_run", side_effect=run) as run_mock:
            report = dependencies.ensure()

        self.assertEqual(run_mock.call_count, 2)
        self.assertTrue(report.python_ready)

    def test_a_missing_pip_says_so_rather_than_failing_like_a_network_error(self):
        # `python -m ensurepip` and "check the proxy" are different days' work.
        with mock.patch.object(dependencies, "missing_packages", return_value=[_package("alpha")]), \
             mock.patch.object(dependencies.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(dependencies, "_run") as run:
            report = dependencies.ensure()

        run.assert_not_called()
        self.assertIn("pip is not installed", report.last_error())

    def test_the_skip_switch_stops_the_fetch_and_still_reports(self):
        with mock.patch.dict(os.environ, {dependencies.SKIP_ENVIRONMENT_VARIABLE: "1"}), \
             mock.patch.object(dependencies, "missing_packages", return_value=[_package("alpha")]), \
             mock.patch.object(dependencies, "_run") as run:
            report = dependencies.ensure()

        run.assert_not_called()
        self.assertTrue(report.skipped)
        self.assertEqual(report.failed, ["alpha"])
        self.assertEqual(report.already_present, ["beta"])


class NothingHereEndsAnInstall(unittest.TestCase):
    """A customer with no route to PyPI keeps the whole product."""

    def test_a_timeout_is_recorded_not_raised(self):
        with mock.patch.object(
            dependencies.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=1),
        ):
            attempt = dependencies._run([sys.executable, "-m", "pip"], ["alpha"])
        self.assertFalse(attempt.succeeded)
        self.assertIn("PyPI", attempt.output)

    def test_an_interpreter_that_will_not_start_is_recorded_not_raised(self):
        with mock.patch.object(dependencies.subprocess, "run", side_effect=OSError("no such file")):
            attempt = dependencies._run(["/nowhere/python", "-m", "pip"], ["alpha"])
        self.assertFalse(attempt.succeeded)
        self.assertIn("no such file", attempt.output)

    def test_ensure_survives_a_subprocess_that_explodes(self):
        with mock.patch.object(dependencies, "missing_packages", return_value=[_package("alpha")]), \
             mock.patch.object(dependencies, "_run", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                # _run itself is allowed to be the thing that throws; ensure()
                # calls it through subprocess, which is why _run swallows.
                dependencies._run([sys.executable], ["alpha"])

        with mock.patch.object(dependencies, "PYTHON_PACKAGES", (_package("alpha"),)), \
             mock.patch.object(dependencies, "SYSTEM_BINARIES", ()), \
             mock.patch.object(dependencies, "missing_packages", return_value=[_package("alpha")]), \
             _pip_is_installed(), \
             mock.patch.object(dependencies.subprocess, "run", side_effect=OSError("boom")):
            report = dependencies.ensure()   # must not raise
        self.assertFalse(report.python_ready)


class ProgramsPipCannotSupply(unittest.TestCase):
    def test_one_apt_line_names_each_package_once(self):
        # pdftoppm and pdftotext both come from poppler-utils; naming it twice
        # in the line shown to an administrator looks like a mistake.
        absent = [
            dependencies.Binary("pdftoppm", "poppler-utils", ""),
            dependencies.Binary("pdftotext", "poppler-utils", ""),
            dependencies.Binary("tesseract", "tesseract-ocr tesseract-ocr-ara", ""),
        ]
        with mock.patch.object(dependencies, "missing_binaries", return_value=absent):
            line = dependencies.system_install_command()

        self.assertEqual(line.count("poppler-utils"), 1)
        self.assertIn("tesseract-ocr-ara", line)
        self.assertTrue(line.startswith("sudo apt-get install -y "))

    def test_nothing_missing_means_no_command_at_all(self):
        with mock.patch.object(dependencies, "missing_binaries", return_value=[]):
            self.assertEqual(dependencies.system_install_command(), "")

    def test_a_missing_program_alone_makes_it_not_ready(self):
        with mock.patch.object(dependencies, "missing_packages", return_value=[]), \
             mock.patch.object(dependencies, "missing_binaries",
                               return_value=[dependencies.Binary("tesseract", "tesseract-ocr", "")]):
            state = dependencies.status()
            described = dependencies.describe()

        self.assertFalse(state["ready"])
        self.assertIn("tesseract", described)


class TheModuleIsWiredToRunIt(unittest.TestCase):
    """The mechanism above is worth nothing if Odoo never reaches it."""

    def setUp(self):
        self.manifest = ast.literal_eval(
            re.search(r"\{.*\}", (_MODULE / "__manifest__.py").read_text(), re.DOTALL).group(0)
        )

    def test_the_manifest_names_the_hook_and_the_module_defines_it(self):
        self.assertEqual(self.manifest.get("post_init_hook"), "post_init_hook")
        source = (_MODULE / "__init__.py").read_text()
        self.assertIn("def post_init_hook(env)", source)

    def test_the_manifest_does_not_gate_on_the_packages_it_installs(self):
        # `external_dependencies` raises a UserError BEFORE installation
        # begins, so declaring pytesseract there refuses the very install whose
        # hook fetches pytesseract.
        self.assertNotIn("external_dependencies", self.manifest)

    def test_every_migration_directory_can_be_reached_on_both_series(self):
        # A directory named `17.0.1.1.0` is taken as already carrying its
        # series, so under Odoo 18 it is compared against 18.0.x and can never
        # run -- silently. A series-less name is adapted to whichever series is
        # running. See odoo/modules/migration.py, convert_version + compare.
        for directory in sorted((_MODULE / "migrations").iterdir()):
            if not directory.is_dir():
                continue
            self.assertRegex(
                directory.name, r"^\d+\.\d+(\.\d+)?$",
                f"migrations/{directory.name} carries an Odoo series and will "
                f"never run on the other one; name it without the series.",
            )

    def test_an_upgrade_installs_them_too_not_only_a_fresh_install(self):
        # Odoo runs post_init_hook only `if new_install:`. Every existing
        # customer takes the migration path instead -- and they are the ones
        # who have been reading the README's pip line and not running it.
        scripts = list((_MODULE / "migrations").glob("*/post-migration.py"))
        self.assertTrue(scripts, "no post-migration script installs the packages")
        self.assertTrue(
            any("dependencies" in path.read_text() for path in scripts),
            "no post-migration script reaches services/dependencies.py",
        )
        def parts(version):
            # The manifest carries its series ("17.0.1.4.0"), the directories
            # deliberately do not ("1.3.0"). Compare the last three, which is
            # the only half the two have in common.
            return tuple(int(piece) for piece in version.split(".")[-3:])

        newest = max(
            (directory.name for directory in (_MODULE / "migrations").iterdir()
             if directory.is_dir()),
            key=parts,
        )
        # AT LEAST, not exactly. Odoo runs the script in directory X when the
        # installed version is below X and the manifest is at or above it, so
        # a manifest ahead of the newest migration is the ordinary state --
        # not every release adds one. An `endswith` here read as the same
        # check and was not: it failed the moment a release bumped the version
        # for a change that needed no migration, which is most of them.
        self.assertGreaterEqual(
            parts(self.manifest["version"]), parts(newest),
            f"manifest version {self.manifest['version']} does not reach "
            f"migrations/{newest}; a migration only runs when the manifest "
            f"version is at or above the directory's.",
        )

    def test_requirements_txt_says_the_same_thing_as_the_code(self):
        listed = [
            line.strip()
            for line in (_MODULE / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(listed, [p.requirement for p in dependencies.PYTHON_PACKAGES])


class WhatIsActuallyDeclared(unittest.TestCase):
    def test_the_two_packages_the_reader_imports_are_the_two_declared(self):
        declared = {package.module for package in dependencies.PYTHON_PACKAGES}
        source = (_MODULE / "services" / "ocr.py").read_text()
        for name in ("pytesseract", "pdf2image"):
            self.assertIn(name, source)
            self.assertIn(name, declared)

    def test_every_program_the_reader_runs_itself_is_declared(self):
        # A drift guard: if ocr.py starts shelling out to something new, it has
        # to be declared here too, or a server without it fails at the moment a
        # customer sends a document rather than at install.
        source = (_MODULE / "services" / "ocr.py").read_text()
        declared = {binary.command for binary in dependencies.SYSTEM_BINARIES}
        run_directly = set(re.findall(r'subprocess\.run\(\s*\[\s*"([a-zA-Z0-9_-]+)"', source))
        self.assertTrue(run_directly, "found no subprocess calls to check")
        self.assertLessEqual(run_directly, declared, f"undeclared: {run_directly - declared}")

    def test_the_program_pdf2image_runs_is_declared_too(self):
        # pdftoppm is never named in our code -- pdf2image calls it. Without it
        # pdf2image imports perfectly and raises on the first scanned PDF.
        self.assertIn("pdftoppm", {b.command for b in dependencies.SYSTEM_BINARIES})

    def test_nothing_speculative_is_declared(self):
        # An entry for something Odoo already ships means a pip run at every
        # install that had nothing to do.
        self.assertEqual(len(dependencies.PYTHON_PACKAGES), 2)


if __name__ == "__main__":
    unittest.main()


class TheOldConnectorsRecordsAreRepaired(unittest.TestCase):
    """The e-mail the old code wrote only into a label, recovered.

    The migration itself needs a database; the rule it applies -- which labels
    carry an address and which do not -- does not, and getting that wrong
    writes a nonsense address onto a customer's connection.
    """

    def setUp(self):
        source = (_MODULE / "migrations" / "1.3.0" / "post-migration.py").read_text()
        namespace = {}
        exec(compile(source, "post-migration.py", "exec"), namespace)
        self.pattern = namespace["_LABELLED"]

    def test_it_reads_the_address_out_of_the_label_the_old_code_wrote(self):
        found = self.pattern.match("Chat: someone@razyyn.test")
        self.assertTrue(found)
        self.assertEqual(found.group(1), "someone@razyyn.test")

    def test_it_leaves_every_other_label_alone(self):
        for label in (
            "Razyyn AI Agent",
            "Razyyn Platform Connection",
            "Razyyn AI: someone@razyyn.test",   # today's spelling, email column set
            "Chat: not an address",
            "Chat:",
            "",
        ):
            self.assertIsNone(self.pattern.match(label), label)
