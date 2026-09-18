# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

"""One thing that happened during a turn, written down before it is sent.

WHY THE LIVE PROGRESS IS IN A TABLE AND NOT ONLY ON A SOCKET
    On ERPNext the chat receives its progress over socket.io, and the Frappe
    page says plainly why that is not enough: *"Realtime is an acceleration
    path, not the source of truth."* A laptop that sleeps, a proxy that
    reconnects, a customer who reloads the page in the middle of a five-minute
    audit -- each of those loses packets, and what the customer sees is a
    bubble that spins for ever while the work completes invisibly behind it.

    So every event is a row here before it goes anywhere. The browser sends
    back the last id it saw and is given everything after it.

    WHAT THAT COVERS, EXACTLY: the reconnect. The stream is closed by the
    server every fifty seconds on purpose and reopened about a second later,
    and it also drops when a proxy blinks or a laptop sleeps. Those gaps are
    what the rows bridge, and they are measured in seconds.

    IT DOES NOT COVER A RELOAD, whatever an older comment said. The browser
    keeps its marker in memory only, so a reloaded page has none, and the
    stream then starts at the newest row deliberately -- replaying from zero
    would redraw a finished run on top of a new one. A reload is recovered
    instead by rebuilding the transcript and asking the agent for the run
    state, neither of which comes from this table.

WHY NOT IN MEMORY
    Because a site with more than one Odoo worker runs the turn in one process
    and serves the reconnect from another. An in-memory queue works perfectly
    in development, where there is one process, and loses every reconnect in
    production, where there are six. That is the worst shape a defect can have.

WHY IT IS SAFE TO DELETE
    These rows are a delivery buffer, not a record: the transcript is in
    razyyn.agent.chat.message and the ledger writes are in
    razyyn.agent.write.log. Nothing here is the only copy of anything -- the
    answer, a question, an error and a cancellation are all in the transcript
    before the matching event is ever sent, and the chat window rebuilds any of
    them from there.

WHO ACTUALLY REMOVES THEM
    The turn itself, a minute after it ends -- see schedule_event_cleanup in
    services/chat_turn_service.py. That is the path that runs, and it leaves
    the table empty between conversations rather than holding hours of a
    customer's ledger traffic for nothing.

    The cron below is the BACKSTOP, for the turns that never got to sweep up:
    a restarted server, a stopped worker, a thread killed mid-answer. It is not
    the main path, and on a healthy site it finds nothing.
"""

import logging

from odoo import api, fields, models, tools

_logger = logging.getLogger(__name__)

#: How old a row must be before the backstop will touch it.
#:
#: THIS IS WHAT PROTECTS A CONVERSATION THAT IS STILL RUNNING. The sweep goes
#: by AGE, not by conversation, and a row that has not been delivered yet is
#: seconds old -- it is read within half a second of being written. So an hour
#: is not a guess at how long a turn takes: it is a margin so wide that no row
#: still on its way to a browser can possibly be caught by it, even if the
#: cleanup happens to run in the middle of the customer's longest audit.
#:
#: It does not need to outlast a turn. A three-hour audit's first minutes were
#: delivered three hours ago; only the newest rows are still in flight, and
#: those are the ones an age cutoff can never reach.
EVENT_RETENTION_HOURS = 1


class AgentChatEvent(models.Model):
    _name = "razyyn.agent.chat.event"
    _description = "Razyyn AI Chat Stream Event"
    _order = "id asc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade",
        help="Whose browser this event is for. Nobody else is ever sent it.",
    )
    session_id = fields.Char(
        required=True, index=True,
        help="The conversation this belongs to, as the browser knows it.",
    )
    event = fields.Char(
        required=True,
        help="The event name the chat window listens for, e.g. agent_message_chunk.",
    )
    payload = fields.Text(
        help="The event body, as JSON. Sent to the browser exactly as stored.",
    )

    def init(self):
        """The index the live stream reads by, and it is not optional.

        Every open chat window asks the same question twice a second: "what has
        happened for this user since event N?". Indexed on the user alone,
        PostgreSQL has to walk every event that user has produced in the
        retention window -- and during a turn that is one row per word of the
        answer, so the cost of the question grows as the answer is written.
        Indexed on the pair, it steps straight to the first new row and reads
        only what it is going to send.
        """
        tools.create_index(
            self._cr,
            "razyyn_agent_chat_event_user_id_id_idx",
            self._table,
            ["user_id", "id"],
        )

    @api.model
    def cron_prune_delivered_events(self):
        """Drop events old enough that nothing can still be waiting for them.

        The backstop, not the main path: each turn removes its own events a
        minute after it ends. What is left here belongs to turns that died
        before they could -- and on this machine that was not hypothetical, a
        whole day of rows was found sitting in a live database because the
        cron runner had not been up.
        """
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), hours=EVENT_RETENTION_HOURS)
        stale = self.sudo().search([("create_date", "<", cutoff)])
        count = len(stale)
        if count:
            stale.unlink()
            _logger.info("Razyyn AI: pruned %s delivered chat events", count)
        return count
