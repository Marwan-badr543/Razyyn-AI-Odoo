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


def _backfill_pre_multiuser_settings_columns(cr):
    """Bring a pre-multi-user `razyyn_agent_settings` up to this shape.

    A company-wide-key-only install (predating `user_id`/`email`/
    `access_token`/`refresh_token` on this table) never went through the ORM
    schema sync that would normally have added them, so the fold below finds
    columns that do not exist yet instead of columns that are merely empty.
    Add them here, nullable, so the fold's own UPDATE/INSERT works; Odoo's
    _auto_init tightens them (NOT NULL, index, FK) right after this script
    returns, the same as it would for a column it added itself.

    The existing company-wide row is left with `user_id` NULL rather than
    guessed at: nothing about a company-wide key says which user it belongs
    to, and `_auto_init`'s own field-default pass will fill it (as it would
    for any other pre-existing row missing a newly-required field) rather
    than this script inventing an owner.
    """
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'razyyn_agent_settings'
    """)
    existing = {row[0] for row in cr.fetchall()}
    for column in ("user_id", "email", "access_token", "refresh_token"):
        if column not in existing:
            cr.execute(f"ALTER TABLE razyyn_agent_settings ADD COLUMN {column} varchar")
    if "user_id" not in existing:
        # Added as varchar above like the others; retype to the integer FK
        # column an M2O actually needs before anything queries it as one.
        cr.execute("""
            ALTER TABLE razyyn_agent_settings
             ALTER COLUMN user_id TYPE integer USING NULLIF(user_id, '')::integer
        """)

    # The fold's INSERT below never sets company_id, and that column is
    # NOT NULL with no default on a company-wide-key-era table. A DEFAULT
    # lets the plain INSERT satisfy it; _auto_init does not mind a column
    # already having one.
    cr.execute("SELECT id FROM res_company ORDER BY id LIMIT 1")
    row = cr.fetchone()
    if row:
        cr.execute(
            "ALTER TABLE razyyn_agent_settings ALTER COLUMN company_id SET DEFAULT %s",
            (row[0],),
        )

    # Same era gap for the API-key columns: on a company-wide-key-only
    # table they are NOT NULL (every row had to have a key). The fold's
    # INSERT below deliberately leaves them unset -- a session folded in
    # from the chat has no machine API key until the user generates one
    # via AgentSettings.generate() -- so they must be nullable by the time
    # it runs, same as on every schema this migration was written against.
    for column in ("api_key_salt", "api_key_hash", "api_key_lookup_hash"):
        cr.execute(
            "ALTER TABLE razyyn_agent_settings ALTER COLUMN %s DROP NOT NULL" % column
        )


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'razyyn_chat_login'
    """)
    if not cr.fetchone():
        return

    _backfill_pre_multiuser_settings_columns(cr)

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
