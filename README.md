# razyyn-odoo-17

The Odoo-side half of the `ODOO` adapter declared in
`razyyn/agent/erp/adapters.py`. Installing `razyyn_agent_connector` into an
Odoo 17 database is what turns `OdooAdapter.available = True` from a
declaration into something a customer can actually connect.

This is the third piece of the Razyyn stack, alongside:
- `razyyn/` — the agent backend (FastAPI + LangGraph)
- `razyyn-frappe-15/` — the same bridge, for ERPNext/Frappe customers

## What it does

Six HTTP routes, authenticated by a per-connection API key
(`razyyn.agent.settings`), never by an Odoo user session:

| Route | Purpose |
|---|---|
| `POST /agent_api/execute_query` | Run one read-only SELECT |
| `POST /agent_api/get_schema` | Describe one model's fields |
| `POST /agent_api/upload_file` | Attach a generated report to a session |
| `POST /agent_api/request_clarification` | Post a clarifying question |
| `POST /agent_api/messaging_config` | What this site can send through |
| `POST /agent_api/send_message` | Send one message on the company's behalf |

## The security boundary

Every statement `execute_query` runs is checked by
`razyyn_agent_connector/services/query_guard.py` against
`razyyn/docs/erp/SQL_GUARD_CONTRACT.md` before it touches the database — the
same contract the Frappe app's `agent_api_service.py` has enforced since it
shipped. `query_guard.py` has **zero `odoo` imports** on purpose, so it is
independently verifiable:

```bash
python3 -m pytest tests/test_query_guard.py -q
```

runs in any environment — CI, a laptop with no Odoo installed, anywhere —
because that file sits outside the `razyyn_agent_connector/` package on
purpose (see its own docstring for why: `models/__init__.py` and
`controllers/__init__.py` import `odoo`, so a test file collected as *part of*
that package pulls that import in even if the test itself never touches
`odoo`).

## Verified end-to-end (2026-08-27)

Installed on a real Odoo 17 + PostgreSQL 15 instance (Docker: `odoo:17.0`),
module loaded clean, and all six routes walked over real HTTP with a real
`razyyn.agent.settings` API key:

- `execute_query` — real SELECT against `res_partner`, correct rows back
- `get_schema` — real field introspection on `res.partner`
- Guard: `UPDATE` refused, `res_users.password` column refused,
  `razyyn_agent_settings` (this module's own credential table) refused
- A failing query (bad column name) returns a clean driver-error message,
  and the *next* query on the same connection still works — the
  `cr.savepoint()` fix holds
- A query ending in a trailing SQL comment still gets its row cap applied —
  the LIMIT-wrapper fix holds
- `request_clarification` — auto-creates its session, saves the message
- `upload_file` — multipart upload, real `ir.attachment` created
- `messaging_config` / `send_message` — honest empty-channels / 409 refusal

One real bug this run caught that no amount of review would have:
**`ir.cron`'s `code` field runs through Odoo's `safe_eval` sandbox, which
forbids `import` outright** — the original `data/ir_cron_data.xml` did
`from odoo.addons.razyyn_agent_connector.services import agent_api_service`
directly in the cron code and failed the whole module install with
`forbidden opcode(s) ... IMPORT_NAME, IMPORT_FROM`. Fixed by moving the
import into an ordinary method (`AgentChatSession.
cron_cleanup_expired_generated_files`) that the cron calls via the `model`
the sandbox already binds — see that method's docstring.

`RAZYYN_ODOO_ADAPTER_VERIFIED=true` (in `razyyn/agent/erp/adapters.py`) can
now be set with actual evidence behind it, not just code review. Re-run this
walkthrough after any change to `controllers/`, `models/`, or
`services/agent_api_service.py` before relying on that flag again — none of
this is enforced by CI yet.

## Known gaps — read before relying on this in production

1. **No CI wired up for the live-Odoo walkthrough above.** It was run by
   hand against a throwaway Docker stack; a change to the controller or
   service layer could silently regress it. `tests/test_query_guard.py`
   (the security-critical part) DOES run in CI-style automation — see above
   — but the HTTP/ORM plumbing does not yet have an automated equivalent.
2. **Messaging is a stub, not a port.** `messaging_config`/`send_message`
   answer honestly (no channels configured / refused) rather than
   half-implementing the Frappe app's Gmail/WhatsApp/Telegram provider layer
   (`z_plan/project_next_features.md` #3 in the `razyyn` repo). Porting that
   is a separate, larger piece of work.
3. **Write support (Creator Agent) is out of scope.** `agent/agent_create/
   adapters/` in the `razyyn` backend is ERPNext-only today
   (`erpnext_adapter.py`); this connector is read-only, matching what
   `OdooAdapter` in `agent/erp/` actually declares. Posting into Odoo through
   the agent is future work, not silently dropped — see
   `razyyn/api/services/erp_onboarding.py`, which is untouched by this
   change.
4. **The `{"message": ...}` reply envelope is a wart, not a design choice.**
   `razyyn/agent/tools/tools.py` and `document_generator.py` unwrap every ERP
   reply with `.get("message", {})` — a Frappe-ism baked into the caller, not
   into the port. This app's controller conforms to that shape rather than
   the cleaner bare-JSON one, so the agent's existing, already-tested parsing
   works unmodified. Fixing `tools.py` to parse replies the way
   `agent/tools/messaging.py` already does (adapter-agnostic: unwrap
   `"message"` if present, use the bare body otherwise) would remove the need
   for this — worth doing, not done here to keep this change's blast radius
   to the new adapter alone.

## Installing

```bash
cp -r razyyn_agent_connector /path/to/odoo/addons/
# then, from the Odoo instance:
odoo-bin -d your_db -i razyyn_agent_connector --test-enable --stop-after-init
```

Once installed, create a `razyyn.agent.settings` record (Settings → Technical
→ Razyyn Agent Connections, or `env['razyyn.agent.settings'].generate()`),
copy the plaintext key it returns — shown once — and hand `site_url` + that
key to the customer's Razyyn onboarding flow the same way an ERPNext customer
hands over their Frappe API key today.
