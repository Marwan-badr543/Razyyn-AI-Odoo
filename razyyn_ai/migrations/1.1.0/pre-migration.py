# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Fold the second credential record into the first, before it is removed.

There used to be two places holding one user's Razyyn session: the connection
record, and a `razyyn.chat.login` row with the same two tokens in it. They
disagreed -- signing in through the chat wrote one and connecting for recording
read the other -- so a customer who was plainly signed in was told to sign in
first.

The second is gone. Anything it held moves here first, so an upgrade does not
sign anybody out.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'razyyn_chat_login'
    """)
    if not cr.fetchone():
        return

    # Only where the connection record has nothing: a session written by the
    # chat since is newer than anything the old row holds.
    cr.execute("""
        UPDATE razyyn_agent_settings AS settings
           SET access_token  = login.access_token,
               refresh_token = login.refresh_token,
               email         = COALESCE(NULLIF(settings.email, ''), login.email)
          FROM razyyn_chat_login AS login
         WHERE login.user_id = settings.user_id
           AND COALESCE(settings.access_token, '') = ''
           AND COALESCE(login.access_token, '') <> ''
    """)
    moved = cr.rowcount

    # A sign-in that never had a connection record at all: give it one, so the
    # customer keeps their session rather than being asked to sign in again.
    cr.execute("""
        INSERT INTO razyyn_agent_settings
                    (user_id, email, access_token, refresh_token, label, active,
                     create_uid, write_uid, create_date, write_date)
             SELECT login.user_id, login.email, login.access_token, login.refresh_token,
                    'Razyyn AI: ' || COALESCE(login.email, 'connection'), TRUE,
                    1, 1, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
               FROM razyyn_chat_login AS login
              WHERE COALESCE(login.access_token, '') <> ''
                AND NOT EXISTS (
                      SELECT 1 FROM razyyn_agent_settings AS settings
                       WHERE settings.user_id = login.user_id
                  )
    """)
    created = cr.rowcount

    if moved or created:
        _logger.info(
            "Razyyn AI: kept %s existing sessions and recreated %s while removing "
            "the duplicate credential record.", moved, created,
        )
