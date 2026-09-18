# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Which record holds a signed-in customer's Razyyn session."""

from __future__ import annotations


def settings_for_session(env, user_id: int, email: str):
    """The record this user's Razyyn session is kept on, made if there is none.

    SIGNING IN IS NOT A LOOKUP, AND TREATING IT AS ONE LOCKED PEOPLE OUT.
    The record holds nothing that authorises anything: the platform has just
    checked the password, and what goes here is that person's own tokens. So
    "I cannot find a row with this e-mail on it" is not a reason to refuse a
    sign-in the platform accepted -- it is a reason to write the row.

    It refused three ways, all of them real:

    * A connection made by the version of this module before the rename kept
      the e-mail in its LABEL and left the column empty ("Chat: someone@..."),
      so the person it belonged to could never match it again.
    * Anyone whose Razyyn account was made anywhere else -- the web app, an
      ERPNext bench, another Odoo -- had no row here at all. Signing up instead
      is refused by the platform with "This ERP is already linked to an
      account", so there was no way in at all.
    * A second colleague on the same Odoo, for the same reason.

    The record it settles on is deliberately the user's FIRST, because that is
    the one `connect_write_access` reads. Writing the session anywhere else
    leaves 1-Click Connect saying "please sign in to Razyyn first" to somebody
    who just did -- which is the same defect the 1.1.0 migration was written to
    put an end to.
    """
    settings_model = env["razyyn.agent.settings"].sudo()

    named = settings_model.search(
        [("user_id", "=", user_id), ("email", "=", email)], limit=1,
    ) if email else settings_model.browse(())
    if named:
        return named

    mine = settings_model.search(
        [("user_id", "=", user_id)], order="id asc", limit=1,
    )
    if mine:
        return mine

    record, _key = settings_model.generate(
        user_id=user_id, label=f"Razyyn AI: {email}", email=email,
    )
    return record
