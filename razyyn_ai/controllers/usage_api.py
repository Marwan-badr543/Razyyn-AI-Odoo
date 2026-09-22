# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""What this account has spent of its monthly budget, for the card on Agent Settings.

WHY IT IS PLAIN HTTP AND NOT ``type="json"``
    The same reason the knowledge card's four routes are. A JSON route has to be
    reached through Odoo's ``rpc`` service, and that service was REMOVED in Odoo
    18 -- so a card that rendered on 17 threw "Service rpc is not available" on
    18 and took the whole settings page with it. One product on two framework
    generations: the fewer framework services the shared code touches, the fewer
    ways the two can diverge. ``fetch`` is not going anywhere.

WHY THERE IS NO ``csrf`` TOKEN ON IT
    It reads. It changes nothing, and it is scoped to the caller's own signed-in
    account -- ``auth="user"`` plus the account read from this Odoo's own stored
    token, never a parameter the caller could choose.
"""

from __future__ import annotations

import json

from odoo import http
from odoo.http import request

from ..services import usage_service


class RazyynUsage(http.Controller):

    @http.route("/razyyn/usage", type="http", auth="user",
                methods=["GET"], csrf=False)
    def usage(self, **_kwargs):
        """Always answers, and always in one shape.

        A platform that cannot be reached reports a zeroed figure with `known`
        false rather than an error status: the card is a warning about a balance,
        and a red box shown whenever the platform is busy is exactly the moment
        the customer came to look at it.
        """
        body = usage_service.usage_or_unknown(request.env, request.env.uid)
        return request.make_response(
            json.dumps({"ok": True, **body}),
            headers=[("Content-Type", "application/json"),
                     ("Cache-Control", "no-store")],
        )
