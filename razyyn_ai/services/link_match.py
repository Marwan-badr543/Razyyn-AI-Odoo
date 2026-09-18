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


def spellings_of(text: str) -> list:
    """The ways one reference may have been written, best first.

    A DOCUMENT'S DISPLAYED NAME IS NOT ALWAYS SOMETHING THE VENDOR WILL MATCH.
        Odoo builds a journal entry's displayed name out of two columns —
        `MISC/2026/09/0004 (RAZYYN-TEST-B)` is its number and its reference —
        and `name_search` matches on either column ALONE. It does not match the
        string it just produced. So the loop failed to close: this module tells
        the agent a document is called "MISC/2026/09/0004 (RAZYYN-TEST-B)", the
        agent names it back exactly as it was told, and the module answers that
        no such record exists.

        It is the same string a person reads off their own screen, which is the
        other half of the problem: an accountant copying a reference out of Odoo
        copies the whole of it.

    So a reference that resolves as written is used as written, and only a
    reference that resolves to NOTHING is retried on its parts. The trailing
    parenthesis is the only structure looked for, because it is the only one
    a displayed name is built with here — and a name that happens to contain
    brackets still gets its literal spelling tried first.
    """
    whole = str(text or "").strip()
    if not whole:
        return []
    tried = [whole]
    if whole.endswith(")") and "(" in whole:
        head, _, tail = whole.rpartition("(")
        head = head.strip()
        tail = tail[:-1].strip()
        for part in (head, tail):
            if part and part not in tried:
                tried.append(part)
    return tried
