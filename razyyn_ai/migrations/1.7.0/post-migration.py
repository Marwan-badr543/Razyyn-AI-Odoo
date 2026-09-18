# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Fold several messaging configurations back into the one that is read.

WHAT WENT WRONG AND WHY THIS IS NOT OPTIONAL
    The messaging configuration was reachable as an ordinary list, so nothing
    stopped an administrator pressing New and setting the agent up a second
    time. Only one of those records was ever read — every caller resolves the
    configuration by taking the first one there is — so the mailbox password,
    the bot token and the destinations typed into the others did nothing at
    all, and the only symptom was the agent going on saying that email "is not
    set up here".

    Creating a second one is refused from now on. This carries the sites that
    already have them: rather than deleting the extras, everything they hold
    is moved onto the record that is actually read, so nothing an administrator
    typed is lost and the agent immediately sees all of it.

HOW THE MERGE DECIDES
    The surviving record is the one the agent reads — the lowest id. For every
    setting, a value already on the survivor wins; a box the survivor left
    empty is filled from the extras, newest first, because the most recently
    edited record is the one somebody was actually working in. Destinations are
    not a choice at all: every one of them is moved across, since each is a
    place the company meant the agent to be able to reach.

SAFE TO RUN TWICE. With one record left there is nothing to merge and this
returns having done nothing.
"""

import logging

logger = logging.getLogger(__name__)

#: Everything an administrator can type on the form. Written out rather than
#: read from the table so that a column added later is a deliberate decision
#: here — silently merging a field nobody has thought about is how a merge
#: turns into a surprise.
_SETTINGS_COLUMNS = (
    "name",
    "email_enabled",
    "email_from",
    "email_sender_name",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "smtp_password",
    "telegram_enabled",
    "telegram_bot_token",
    "slack_enabled",
    "slack_bot_token",
)

#: Values that mean "nobody filled this in". A False switch counts: leaving
#: Telegram off is the default state, not a decision that should outrank a
#: record where somebody turned it on.
_EMPTY = (None, "", False, 0)


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT id FROM razyyn_agent_messaging_settings ORDER BY id")
    ids = [row[0] for row in cr.fetchall()]
    if len(ids) < 2:
        return

    keeper, extras = ids[0], ids[1:]
    columns = _present_columns(cr)

    # Newest first: the record somebody edited most recently is the likeliest
    # to hold what they meant to be using.
    cr.execute(
        "SELECT id, {} FROM razyyn_agent_messaging_settings "
        "WHERE id = ANY(%s) ORDER BY write_date DESC NULLS LAST, id DESC".format(
            ", ".join(columns)
        ),
        (extras,),
    )
    donors = cr.fetchall()

    cr.execute(
        "SELECT {} FROM razyyn_agent_messaging_settings WHERE id = %s".format(
            ", ".join(columns)
        ),
        (keeper,),
    )
    kept = dict(zip(columns, cr.fetchone()))

    filled = {}
    for donor in donors:
        values = dict(zip(columns, donor[1:]))
        for column in columns:
            if kept.get(column) in _EMPTY and column not in filled:
                if values.get(column) not in _EMPTY:
                    filled[column] = values[column]

    if filled:
        cr.execute(
            "UPDATE razyyn_agent_messaging_settings SET {} WHERE id = %s".format(
                ", ".join(f"{column} = %s" for column in filled)
            ),
            list(filled.values()) + [keeper],
        )

    cr.execute(
        "UPDATE razyyn_agent_messaging_destination SET settings_id = %s "
        "WHERE settings_id = ANY(%s)",
        (keeper, extras),
    )
    moved = cr.rowcount

    # The abandoned records go, but their identity goes with them: an
    # ir.model.data row pointing at a deleted id is what makes the next module
    # upgrade fail.
    cr.execute(
        "DELETE FROM ir_model_data WHERE model = %s AND res_id = ANY(%s)",
        ("razyyn.agent.messaging.settings", extras),
    )
    cr.execute(
        "DELETE FROM razyyn_agent_messaging_settings WHERE id = ANY(%s)", (extras,)
    )

    logger.info(
        "Razyyn AI: folded %s extra messaging configuration(s) into #%s "
        "(%s setting(s) carried across, %s destination(s) moved).",
        len(extras), keeper, len(filled), moved,
    )


def _present_columns(cr):
    """The settings columns this database actually has.

    A database upgraded from far enough back may not have every one of them
    yet, and naming a missing column would abort the upgrade over a merge.
    """
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'razyyn_agent_messaging_settings'"
    )
    present = {row[0] for row in cr.fetchall()}
    return [column for column in _SETTINGS_COLUMNS if column in present]
