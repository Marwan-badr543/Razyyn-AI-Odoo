# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Which record a customer's words mean, out of what the vendor offered.

A module of its own, with nothing from Odoo in it, for one reason: the rule
below decides which account an entry is posted to, and a rule that
consequential must be testable directly. Living inside a service that cannot
be imported without a running Odoo, it could only ever be checked by a second
copy of itself in a test file -- which passes for ever, including after the
original changes.
"""

from __future__ import annotations


def pick_link_match(text: str, matches):
    """Which of the vendor's matches the customer meant.

    Returns the id when there is an answer, or the remaining candidates when it
    is genuinely a question. Public and free of any `env` so it can be tested
    directly -- a rule this consequential must not be checked by a second copy
    of itself in a test file.

    AN EXACT NAME IS NOT AN AMBIGUOUS ONE.
        `name_search` matches on a substring, so asking for the account called
        "Bank" on a real chart comes back with "Bank", "Bank Charges" and "Bank
        Suspense Account". Treating that as ambiguous refuses the one thing the
        customer got exactly right, and the agent then asks them which "Bank"
        they meant -- about the account they just named. Only a genuine tie,
        where two records carry the same name, is a question worth asking.
    """
    wanted = str(text or "").strip().casefold()
    exact = [
        (record_id, label) for record_id, label in matches
        if str(label or "").strip().casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0][0]
    if len(exact) > 1:
        return exact
    if len(matches) == 1:
        return matches[0][0]
    return list(matches)
