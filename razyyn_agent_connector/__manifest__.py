# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

{
    "name": "Razyyn Agent Connector",
    "version": "17.0.1.0.0",
    "summary": "Read-only JSON bridge that lets the Razyyn accounting agent "
                "query this database safely, the same contract the ERPNext "
                "app already serves.",
    "description": """
Razyyn Agent Connector
=======================

Installs the Odoo-side half of the ODOO adapter declared in
razyyn/agent/erp/adapters.py. Everything this module exposes is read-only
and every statement it executes is checked against
razyyn/docs/erp/SQL_GUARD_CONTRACT.md before it runs - see
services/query_guard.py, which is a direct port of the guard the Frappe
app (razyyn-frappe-15/accountant_agent/agent_api/services/agent_api_service.py)
has enforced since that repo's own agent API shipped.

The agent is reached through six routes:

- /agent_api/execute_query - run one read-only SELECT
- /agent_api/get_schema - describe one model's fields
- /agent_api/upload_file - attach a generated report to a session
- /agent_api/request_clarification - post a clarifying question
- /agent_api/messaging_config - what this site can send through
- /agent_api/send_message - send one message on the company's behalf

Authenticated by a per-connection API key (razyyn.agent.settings), never
by an Odoo user session - the agent is a machine caller, not a logged-in
user.
    """,
    "author": "Marwan Badr",
    "license": "LGPL-3",
    "category": "Extra Tools",
    "depends": ["base", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
    ],
    "installable": True,
    "application": False,
}
