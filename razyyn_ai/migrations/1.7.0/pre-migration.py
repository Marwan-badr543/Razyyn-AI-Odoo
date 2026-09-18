# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Let two menu entries change from a plain window into a server action.

WHAT BREAKS WITHOUT THIS, AND IT BREAKS FOR EVERY EXISTING SITE
    Messaging Channels and the Write Policy became single records in 1.7.0 --
    one configuration each, opened directly rather than listed. A static window
    action cannot do that, because the record's id is not known until it is
    looked up, so both menu entries had to become server actions instead.

    An external id may not change the model it points at. Odoo does not warn
    about it and it does not skip the record: the whole module update stops
    dead, with

        For external id razyyn_ai.action_razyyn_agent_write_policy when trying
        to create/update a record of model ir.actions.server found record of
        different model ir.actions.act_window

    ...and the site is left on the old version. A FRESH INSTALL NEVER SEES
    THIS -- there is no old record to clash with -- which is exactly why it has
    to be caught by upgrading a database that already has the module, and why
    it is written down here rather than trusted to be remembered.

WHY "pre" AND NOT "post"
    The clash happens while the view files are being read. Anything that runs
    afterwards runs too late to prevent it; by then the update has already
    failed. So the stale record goes before the new one is declared.

WHAT IS LOST BY DELETING THEM
    Nothing. A window action holds no configuration of its own -- it is the
    instruction "open this model in this view", and the replacement carries the
    same instruction plus the ability to work out which record to open. The
    menu entries that point at these are re-created from menus.xml moments
    later in the same update.

SAFE TO RUN TWICE, AND SAFE ON A SITE THAT NEVER HAD THE OLD RECORDS. Each id
is touched only where it still names a window action.
"""

import logging

_logger = logging.getLogger(__name__)

#: The external ids that changed model in 1.7.0, and what they used to be.
#: Anything not still recorded as that old model is left alone.
_CHANGED_MODEL = {
    "action_razyyn_agent_messaging_settings": "ir.actions.act_window",
    "action_razyyn_agent_write_policy": "ir.actions.act_window",
}

#: An action's own row lives in a table named after its kind, and the row every
#: action shares lives in ir_actions. Both go, and in that order.
_TABLE_OF = {"ir.actions.act_window": "ir_act_window"}


def migrate(cr, version):
    if not version:
        return  # a fresh install: there is nothing behind us to clear

    for external_id, old_model in _CHANGED_MODEL.items():
        cr.execute(
            """
            SELECT id, res_id FROM ir_model_data
             WHERE module = 'razyyn_ai' AND name = %s AND model = %s
            """,
            (external_id, old_model),
        )
        row = cr.fetchone()
        if not row:
            continue
        data_id, res_id = row

        # The menu entry pointing here is about to be rewritten by menus.xml,
        # but it is cleared first so that nothing is left naming a row that has
        # gone -- a menu with a dangling action is a menu that raises when it
        # is clicked, and the window between the two is a window in which an
        # administrator may well be looking at the screen.
        cr.execute(
            "UPDATE ir_ui_menu SET action = NULL WHERE action = %s",
            (f"{old_model},{res_id}",),
        )
        cr.execute(f"DELETE FROM {_TABLE_OF[old_model]} WHERE id = %s", (res_id,))
        cr.execute("DELETE FROM ir_actions WHERE id = %s", (res_id,))
        cr.execute("DELETE FROM ir_model_data WHERE id = %s", (data_id,))
        _logger.info(
            "Razyyn AI: cleared the old window action behind %s so it can become a server action",
            external_id,
        )
