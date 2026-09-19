# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""Reaching any record in this Odoo from inside Razyyn AI.

WHY THIS EXISTS
    On ERPNext the customer types into the awesome bar and lands on any DocType
    list in the system. Odoo has no equivalent: a model is reachable only if
    some installed module happened to put a menu in front of it, and most of
    them have none. An accountant who wants to look at, say, `account.full.
    reconcile` while the agent is talking about it has nowhere to click.

    So the app carries its own index. Every model on this database is listed,
    and opening one opens its records.

IT GRANTS NOTHING
    The list is navigation, not permission. Opening a model goes through Odoo's
    ordinary access rules and record rules, exactly as a menu would: a user who
    may not read `account.move` is refused by Odoo at the same place and with
    the same message as anywhere else. What this removes is the accident of
    whether somebody wrote a menu, not the boundary.
"""

from __future__ import annotations

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class IrModel(models.Model):
    _inherit = "ir.model"

    razyyn_browsable = fields.Boolean(
        string="Has Records", compute="_compute_razyyn_browsable", store=True,
        help="Whether this model holds records that can be opened and listed.",
    )

    @api.depends("model", "transient")
    def _compute_razyyn_browsable(self):
        """Whether opening this model would show anything at all.

        STORED, because it is the list's filter and a domain cannot run Python.
        Recomputed whenever the module is upgraded, which is when the set of
        installed models can have changed.

        Three kinds are excluded. A TRANSIENT model is a wizard -- its rows are
        scratch that Odoo sweeps up on a schedule. An ABSTRACT model defines
        behaviour other models reuse and has no table of its own (`mail.thread`
        is one, and it is in this table). A model with no `_auto` table has
        nowhere to read from. Listing any of them puts dead ends in front of
        somebody looking for their invoices.
        """
        for record in self:
            target = self.env.get(record.model)
            record.razyyn_browsable = bool(
                target is not None
                and not record.transient
                and not target._abstract
                and target._auto
            )

    def action_razyyn_open_records(self):
        """Open this model's records, the way a menu would."""
        self.ensure_one()
        target = self.env.get(self.model)
        if target is None:
            raise UserError(self.env._("The model %s is not installed on this database.", self.model))
        if target._abstract:
            raise UserError(self.env._(
                "%s is an abstract model: it defines behaviour that other models "
                "reuse and has no records of its own.", self.model))

        return {
            "type": "ir.actions.act_window",
            "name": self.name or self.model,
            "res_model": self.model,
            # `list` on Odoo 18, `tree` on 17 -- the view mode is one of the
            # three things tools/sync_to_odoo18.py rewrites, and it is written
            # here in the 17 spelling because the Odoo 17 tree is the source.
            "view_mode": "tree,form",
            "target": "current",
            "context": {"create": False} if self._is_read_only_here(target) else {},
        }

    @staticmethod
    def _is_read_only_here(target) -> bool:
        """Whether this user may only look.

        Offering a New button that then refuses is worse than not offering one.

        ASKED TWO WAYS BECAUSE ODOO 18 IS RETIRING THE FIRST.
        `check_access_rights(..., raise_exception=False)` still answers on 18,
        but `check_access` is what replaces it and it takes no such argument --
        so a single call written for either version breaks on the other the
        moment the old one goes. A `except: return True` around that would not
        fail loudly; it would quietly mark every model in the index read-only.

        AND WHEN NEITHER ANSWERS, ASSUME THEY MAY. Hiding a button somebody
        could have used is the worse mistake: Odoo still refuses the create
        itself, with a message that says who to ask.
        """
        checker = getattr(target, "check_access_rights", None)
        if checker is not None:
            try:
                return not checker("create", raise_exception=False)
            # Not a swallow: this signature didn't match, so control falls
            # through to the next checker below, per the docstring above.
            except TypeError:  # pylint: disable=except-pass
                pass
            except Exception:
                return True

        checker = getattr(target, "check_access", None)
        if checker is not None:
            try:
                checker("create")
                return False
            except AccessError:
                return True
            except Exception:
                return False

        return False

