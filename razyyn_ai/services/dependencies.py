# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What the module needs from PyPI, obtained when the module is installed.

WHY THE MODULE INSTALLS ITS OWN PACKAGES
    Reading a photographed invoice needs two packages Odoo does not ship. Until
    now the answer was a line in the README, which means every customer's first
    scanned invoice is read as a picture instead of as words, and nobody finds
    out why until they ask. An accounting product whose headline feature is
    "send me the invoice" cannot open with a shell command.

    So this runs at install: the two packages are fetched into whichever
    interpreter is running Odoo, and the administrator does nothing.

WHY `external_dependencies` IS NOT IN THE MANIFEST, AND MUST NOT BE ADDED
    That field is a GATE, not a request. `ir.module.module.check_external_
    dependencies` raises a UserError before installation begins
    (addons/base/models/ir_module.py), so declaring `pytesseract` there would
    refuse the very install whose job is to fetch `pytesseract`. The packages
    are named here instead, where something can act on them.

WHAT IS NOT DONE HERE, DELIBERATELY: apt
    Tesseract itself and poppler are operating-system packages. Installing
    those means `apt-get` as root, taking the dpkg lock, from inside a module
    install -- a system-wide change nobody asked for, which can fail half
    applied. They are DETECTED instead: what is missing and the exact command
    that supplies it are recorded, logged, and shown on the settings page.

NOTHING IN HERE RAISES
    A customer behind a proxy with no PyPI must still get a working module and
    a chat window; what they lose is reading a photograph, which already
    degrades on its own -- the picture goes to the model to look at. A failed
    fetch that aborted the install would cost them the entire product to save a
    feature that has a fallback.

Pure standard library on purpose: it runs before anything is installed, and
tests/test_dependencies.py loads it with no Odoo present.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import os
import shutil
import site
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

#: A fetch from PyPI that has not finished in this long is not going to.
#: An air-gapped site gets a recorded reason rather than an install that hangs.
INSTALL_TIMEOUT_SECONDS = 600

#: Set this to anything non-empty to manage the packages yourself. The module
#: still reports what is missing; it simply stops trying to fetch it.
SKIP_ENVIRONMENT_VARIABLE = "RAZYYN_SKIP_DEPENDENCY_INSTALL"


@dataclass(frozen=True)
class Package:
    """One thing pip can supply."""

    #: What `import` calls it, which is not always what pip calls it.
    module: str
    #: What pip calls it.
    requirement: str
    #: What stops working without it, in a sentence an administrator can read.
    purpose: str

    def present(self) -> bool:
        try:
            return importlib.util.find_spec(self.module) is not None
        except (ImportError, ValueError):
            # A half-removed distribution leaves a spec that cannot be built.
            # Missing is the honest reading, and reinstalling is the cure.
            return False


@dataclass(frozen=True)
class Binary:
    """One thing pip cannot supply, because it is a program, not a package."""

    #: The command, as it must be found on PATH.
    command: str
    #: The distribution packages that provide it, space separated.
    packages: str
    purpose: str

    def present(self) -> bool:
        return bool(shutil.which(self.command))


#: Fetched automatically. Keep this list to what is genuinely absent from an
#: Odoo install -- Pillow, for one, is already there, and a speculative entry
#: means a pip run at every install that had nothing to do.
PYTHON_PACKAGES: tuple[Package, ...] = (
    Package(
        module="pytesseract",
        requirement="pytesseract",
        purpose="reading the words out of a photographed or scanned invoice",
    ),
    Package(
        module="pdf2image",
        requirement="pdf2image",
        purpose="turning the pages of a scanned PDF into pictures that can be read",
    ),
)

#: Detected and reported, never installed. See the module docstring.
SYSTEM_BINARIES: tuple[Binary, ...] = (
    Binary(
        command="tesseract",
        packages="tesseract-ocr tesseract-ocr-ara",
        purpose="the reader itself, and the Arabic alphabet for it",
    ),
    Binary(
        command="pdftoppm",
        packages="poppler-utils",
        purpose="rendering a page of a PDF so it can be read",
    ),
    Binary(
        command="pdfinfo",
        packages="poppler-utils",
        purpose="counting a PDF's pages -- without it a scan is read as having none",
    ),
    Binary(
        command="pdftotext",
        packages="poppler-utils",
        purpose="lifting the text a PDF already carries, so it is never re-read as a picture",
    ),
)


@dataclass
class Attempt:
    """One pip invocation and what came of it."""

    command: list[str]
    returncode: int | None
    output: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass
class Report:
    """What the install found and what it managed to do about it."""

    installed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    skipped: bool = False
    #: Binaries that are not on PATH, as (command, packages).
    missing_binaries: list[tuple[str, str]] = field(default_factory=list)

    @property
    def python_ready(self) -> bool:
        return not self.failed

    @property
    def ocr_ready(self) -> bool:
        return self.python_ready and not self.missing_binaries

    def last_error(self) -> str:
        for attempt in reversed(self.attempts):
            if not attempt.succeeded:
                return attempt.output.strip()[-2000:]
        return ""


def _in_virtual_environment() -> bool:
    """Whether this interpreter has a site-packages of its own to write into.

    THE WHOLE FALLBACK CHAIN TURNS ON THIS. A virtual environment sets
    ENABLE_USER_SITE to False, so `pip install --user` inside one puts the
    package in ~/.local, which is not on sys.path -- pip reports success and
    the import still fails. That is the worst outcome available: a silent one.
    """
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _pip_commands() -> list[list[str]]:
    """The ways to ask pip, best first.

    Always `sys.executable -m pip`, never a `pip` found on PATH: two Odoo
    installs on one machine have two interpreters, and the one that matters is
    the one running this code.
    """
    base = [sys.executable, "-m", "pip", "install", "--no-input"]
    if _in_virtual_environment():
        # Its site-packages is ours to write to. Nothing else can apply.
        return [base]
    return [
        base,
        # No permission to write to the interpreter's own site-packages.
        base + ["--user"],
        # PEP 668: a distribution-managed Python refuses installs outright.
        # Last, because it overrides a refusal the distribution meant.
        base + ["--break-system-packages"],
    ]


def _run(command: list[str], requirements: list[str]) -> Attempt:
    full = command + requirements
    try:
        completed = subprocess.run(
            full,
            capture_output=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
            # pip is not being read by a person; colour codes in a log field
            # shown on a settings form are noise.
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return Attempt(full, None, f"No answer from PyPI within {INSTALL_TIMEOUT_SECONDS} seconds.")
    except OSError as exc:
        return Attempt(full, None, str(exc))

    output = "\n".join(
        part.decode("utf-8", errors="replace").strip()
        for part in (completed.stdout, completed.stderr)
        if part
    )
    return Attempt(full, completed.returncode, output)


def _teach_the_interpreter_about(command: list[str]) -> None:
    """Make what pip just wrote importable in THIS process, without a restart.

    A `--user` install lands somewhere sys.path has never heard of, and every
    install lands in a directory whose contents Python has already cached as
    "what is in here". Both are corrected here, so the very first chat turn
    after the install can read a picture.
    """
    if "--user" in command:
        try:
            user_site = site.getusersitepackages()
        except Exception:
            user_site = ""
        if user_site and user_site not in sys.path:
            sys.path.append(user_site)
    importlib.invalidate_caches()


def _lock_path() -> str:
    """One pip at a time per interpreter.

    `odoo -i razyyn_ai -d one,two` installs into two databases in one process,
    and two pips writing one site-packages is how a half-written package
    happens.
    """
    # NOT hash(): Python salts string hashing per process, so two workers --
    # the case this lock exists for -- would each compute a different filename
    # and neither would ever wait for the other.
    stamp = hashlib.sha256(sys.prefix.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"razyyn-ai-dependencies-{stamp}.lock")


class _OnePipAtATime:
    def __enter__(self):
        self._handle = None
        try:
            import fcntl

            self._handle = open(_lock_path(), "w")
            fcntl.flock(self._handle, fcntl.LOCK_EX)
        except Exception:
            # Not on a platform with flock, no room to make the file, or a
            # lock left in /tmp by a different operating-system user that this
            # one may not open. The lock is insurance against a rare race, not
            # a correctness requirement; going without it beats refusing to
            # install, and a second pip is still pip -- it does not corrupt.
            self._release()
        return self

    def __exit__(self, *_exception):
        self._release()
        return False

    def _release(self):
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def missing_packages() -> list[Package]:
    return [package for package in PYTHON_PACKAGES if not package.present()]


def missing_binaries() -> list[Binary]:
    return [binary for binary in SYSTEM_BINARIES if not binary.present()]


def system_install_command() -> str:
    """The one line an administrator runs for whatever is missing, or "".

    Named for Debian and Ubuntu, which is what Odoo is deployed on; the package
    names are the same on any Debian derivative and recognisable everywhere
    else.
    """
    packages = []
    for binary in missing_binaries():
        for name in binary.packages.split():
            if name not in packages:
                packages.append(name)
    if not packages:
        return ""
    return "sudo apt-get install -y " + " ".join(packages)


def ensure(logger: logging.Logger | None = None) -> Report:
    """Fetch anything missing that pip can supply. Never raises.

    Returns a Report whether it did anything or not, so a caller can put the
    outcome in front of the administrator instead of leaving it in a log.
    """
    log = logger or _logger
    report = Report()

    # Asked once, through the same door the check after an install uses, so a
    # test exercises the real decision rather than a second copy of it.
    absent = {package.requirement for package in missing_packages()}
    for package in PYTHON_PACKAGES:
        target = report.failed if package.requirement in absent else report.already_present
        target.append(package.requirement)

    report.missing_binaries = [(b.command, b.packages) for b in missing_binaries()]

    if report.failed and os.environ.get(SKIP_ENVIRONMENT_VARIABLE, "").strip():
        report.skipped = True
        log.info(
            "Razyyn AI: %s is set, so %s will not be fetched. Reading scanned "
            "documents needs it: %s",
            SKIP_ENVIRONMENT_VARIABLE,
            " and ".join(report.failed),
            " ".join(p.requirement for p in PYTHON_PACKAGES),
        )
        return _finish(report, log)

    if not report.failed:
        return _finish(report, log)

    if importlib.util.find_spec("pip") is None:
        # Slim container images strip pip. The message has to say that, because
        # the fix -- `python -m ensurepip` or rebuilding the image -- has
        # nothing in common with the fix for a network failure.
        report.attempts.append(
            Attempt(
                [sys.executable, "-m", "pip"],
                None,
                "pip is not installed in the interpreter running Odoo "
                f"({sys.executable}). Run `{sys.executable} -m ensurepip` or install "
                f"the packages into that interpreter: {' '.join(report.failed)}",
            )
        )
        return _finish(report, log)

    wanted = list(report.failed)
    log.info(
        "Razyyn AI: fetching %s into %s so scanned documents can be read.",
        " and ".join(wanted), sys.executable,
    )

    with _OnePipAtATime():
        for command in _pip_commands():
            attempt = _run(command, wanted)
            report.attempts.append(attempt)
            if not attempt.succeeded:
                log.warning(
                    "Razyyn AI: `%s` did not install the reader (exit %s).",
                    " ".join(attempt.command), attempt.returncode,
                )
                continue

            _teach_the_interpreter_about(command)
            # pip's exit code says the fetch worked; only an import says the
            # package is USABLE from here, which is the thing that matters and
            # the thing a --user install gets wrong.
            report.failed = [p.requirement for p in missing_packages()]
            report.installed = [r for r in wanted if r not in report.failed]
            if not report.failed:
                break

    return _finish(report, log)


def _finish(report: Report, log: logging.Logger) -> Report:
    """Say plainly what a customer has and has not got. Once, at install."""
    if report.installed:
        log.info("Razyyn AI: installed %s.", ", ".join(report.installed))

    for requirement in report.failed:
        purpose = next(
            (p.purpose for p in PYTHON_PACKAGES if p.requirement == requirement),
            "reading scanned documents",
        )
        log.warning(
            "Razyyn AI: %s could not be installed, so %s falls back to sending the "
            "picture to the model as it is. Install it into %s to restore it.",
            requirement, purpose, sys.executable,
        )

    command = system_install_command()
    if command:
        log.warning(
            "Razyyn AI: %s not found on this server. Reading scanned documents "
            "needs them; they are operating-system packages, so this module does "
            "not install them for you. Run: %s",
            ", ".join(name for name, _packages in report.missing_binaries),
            command,
        )
    return report


def status() -> dict:
    """What is present right now, for the settings page. Installs nothing."""
    packages_missing = missing_packages()
    binaries_missing = missing_binaries()
    return {
        "ready": not packages_missing and not binaries_missing,
        "interpreter": sys.executable,
        "packages_missing": [p.requirement for p in packages_missing],
        "binaries_missing": [b.command for b in binaries_missing],
        "system_install_command": system_install_command(),
    }


def describe(report: Report | None = None) -> str:
    """The status as a few lines of English, for a field on a form."""
    now = status()
    lines = []

    if now["ready"]:
        lines.append("Ready. Scanned and photographed documents are read as words.")
    else:
        lines.append(
            "Not ready. Scanned documents will be sent to the model as pictures "
            "instead of being read, which is less accurate."
        )

    if now["packages_missing"]:
        lines.append("")
        lines.append("Missing Python packages: " + ", ".join(now["packages_missing"]))
        lines.append(f"  {sys.executable} -m pip install " + " ".join(now["packages_missing"]))

    if now["binaries_missing"]:
        lines.append("")
        lines.append("Missing programs: " + ", ".join(now["binaries_missing"]))
        lines.append("  " + now["system_install_command"])

    if report is not None and report.failed:
        error = report.last_error()
        if error:
            lines.append("")
            lines.append("What the install said:")
            lines.append(error)

    return "\n".join(lines)
