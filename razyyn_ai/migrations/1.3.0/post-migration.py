# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Put the e-mail back in the column the old connector only wrote to the label.

WHAT WENT WRONG
    Before the module was renamed, signing up through the chat created the
    connection record like this:

        generate(label=f"Chat: {email}")

    -- so the address the customer signed up with survived only inside a label,
    and `razyyn_agent_settings.email` was left empty. Everything that looks a
    connection up by e-mail therefore missed it, and the two that matter most
    are the two a customer meets first: signing in, and being told whether they
    are already signed in. On this developer's own database it was the `admin`
    user, who could not sign in at all -- the platform accepted the password,
    Odoo answered HTTP 200, and the page did not move.

    Signing in no longer depends on finding a matching row (see
    `_session_holder` in controllers/chat_client_api.py), so this is not what
    makes sign-in work. It is here because the data is simply wrong, and a
    customer who opens Razyyn AI → Connections should see which account each
    connection is for rather than a blank column beside a label that says it.

ONLY WHERE THE LABEL SAYS IT AND THE COLUMN DOES NOT
    Nothing is overwritten and nothing is guessed. A row whose e-mail is
    already set is left exactly as it is.
"""

import logging
import re

_logger = logging.getLogger(__name__)

#: What the old code wrote: the literal prefix, then the address.
_LABELLED = re.compile(r"^\s*Chat:\s*(\S+@\S+?)\s*$")


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT id, label FROM razyyn_agent_settings
         WHERE COALESCE(email, '') = ''
           AND label LIKE 'Chat: %%'
    """)
    rows = cr.fetchall()

    repaired = 0
    for record_id, label in rows:
        found = _LABELLED.match(label or "")
        if not found:
            continue
        cr.execute(
            "UPDATE razyyn_agent_settings SET email = %s WHERE id = %s",
            (found.group(1), record_id),
        )
        repaired += 1

    if repaired:
        _logger.info(
            "Razyyn AI: recovered the account address on %s connection "
            "record(s) that only carried it in their label.", repaired,
        )
