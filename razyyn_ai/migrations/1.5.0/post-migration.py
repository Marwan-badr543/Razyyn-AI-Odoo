# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Carry a Telegram chat id that was typed into the old single box.

WHAT CHANGED AND WHY THIS IS NEEDED
    Telegram used to be one enabled flag, one token and one "default chat id".
    The agent cannot work from that: it is offered destinations BY NAME so an
    accountant can say "send it to the finance group", and it refuses to send
    anywhere that is not on the list. So a chat id now lives in a row that has
    a name of its own.

    An administrator who had already filled the old box must not have to find
    their chat id again — and worse, must not be left with Telegram switched on
    and no destination, which reads to the agent as "not set up". This copies
    what they typed into the new shape, naming the destination after the
    configuration record if they gave it a name of their own.

SAFE TO RUN TWICE. A destination carrying that address already is left alone
rather than duplicated: a second row with the same chat id would make the
default ambiguous and the agent would start asking which one to use.
"""

import logging

logger = logging.getLogger(__name__)

#: The name the record carries when nobody has renamed it. Using it as a
#: destination label would be worse than useless — the agent would offer the
#: accountant "send it to Agent Messaging Configuration".
_UNNAMED = "Agent Messaging Configuration"


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'razyyn_agent_messaging_settings'
           AND column_name = 'telegram_default_chat_id'
    """)
    if not cr.fetchone():
        return

    cr.execute("""
        SELECT id, name, telegram_default_chat_id
          FROM razyyn_agent_messaging_settings
         WHERE telegram_default_chat_id IS NOT NULL
           AND btrim(telegram_default_chat_id) <> ''
    """)
    for settings_id, name, chat_id in cr.fetchall():
        chat_id = chat_id.strip()
        cr.execute("""
            SELECT id FROM razyyn_agent_messaging_destination
             WHERE settings_id = %s AND channel = 'telegram' AND address = %s
        """, (settings_id, chat_id))
        if cr.fetchone():
            continue

        label = (name or "").strip()
        if not label or label == _UNNAMED:
            label = "Default"

        cr.execute("""
            INSERT INTO razyyn_agent_messaging_destination
                (settings_id, channel, label, address, is_default, sequence,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, 'telegram', %s, %s, TRUE, 10, 1, 1, NOW(), NOW())
        """, (settings_id, label, chat_id))
        logger.info(
            "Razyyn AI: Telegram chat id carried over as the destination '%s'.",
            label,
        )
