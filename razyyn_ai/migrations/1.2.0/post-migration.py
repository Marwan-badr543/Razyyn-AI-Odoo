# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Fetch the reader's packages on an upgrade too, not only on a fresh install.

WHY THIS EXISTS ALONGSIDE post_init_hook
    Odoo runs a post_init_hook only when a module is installed for the first
    time -- `if new_install:` in odoo/modules/loading.py. Every database that
    already has Razyyn AI therefore takes this path instead, and that is most
    of them: an existing customer upgrading is exactly the person who has been
    reading the README's pip line and not running it.

WHY THE DIRECTORY IS NAMED `1.2.0` AND NOT `17.0.1.2.0`
    A version-less name is adapted to whichever series is running, and compared
    on the module part alone (odoo/modules/migration.py, `convert_version` and
    `compare`). A name carrying `17.0.` is taken as already complete, so under
    Odoo 18 it is compared against 18.0.x and can never be reached -- the
    script does not fail, it simply never runs. `1.1.0` beside this one was
    named that way and was dead on Odoo 18 until it was renamed.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    try:
        from odoo.addons.razyyn_ai.services import dependencies
    except Exception as exc:  # pragma: no cover - a broken import is logged, not fatal
        _logger.warning("Razyyn AI: could not check the reader's packages: %s", exc)
        return

    dependencies.ensure(_logger)
