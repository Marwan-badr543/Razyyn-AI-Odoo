# Architecture: how the Odoo module maps onto the Frappe app

The Razyyn agent is written once and speaks to every ERP through an app the
customer installs on their own system. This document is the translation table
between the Frappe app's patterns and the Odoo ones, so that a change made on
either side can be found on the other.

**What is already done**, and verified end-to-end against live Odoo 17 and 18
by `scripts/verify_erp_app.py` in the agent repository:

- the six read routes and the identity query;
- the read-only SQL guard, against the shared hostile corpus rendered in Odoo's
  own table names;
- the seven write-gateway endpoints — the same protocol the Frappe app speaks,
  driven by the same single client in the agent — including a real create,
  idempotent replay, posting, and read-back;
- write governance: the Agent Write Policy, the append-only audit log, per-run
  document and amount ceilings;
- the in-Odoo chat page at `/razyyn/chat`, with per-user conversation isolation
  enforced by global record rules.

**What is deliberately still a stub**: the outbound messaging providers
(`messaging_config` / `send_message` answer honestly that no channel is
configured rather than half-sending). Porting the Frappe app's Gmail/Telegram/
Slack provider layer is its own piece of work.

Read `README.md` first for how to install and verify; this file is the map.

---

## 1. High-Level Architecture & Concept Mapping

The Frappe app is a full-stack ERP integration consisting of a rich ChatGPT/Claude-style desk interface, authentication proxy, self-service ERP connection provisioning, write governance policies, and message logging.

Below is the direct translation between Frappe framework patterns and Odoo 17 standards:

| Component | Frappe Implementation | Odoo 17 Equivalent |
| :--- | :--- | :--- |
| **App Identity** | `hooks.py` (`required_apps = ["erpnext"]`) | `__manifest__.py` (`"application": True`, `"depends": ["base", "mail", "web", "account"]`) |
| **Full-Page UI** | Page `agent_chat` (`agent_chat.js`, `.css`, `.json`) | OWL Component Client Action (`tag: 'razyyn_agent_chat'`, XML template, SCSS) |
| **Data Storage** | DocTypes (`Agent Settings`, `Agent Chats`, `Agent Write Policy`, etc.) | Python Models inheriting `models.Model` or `models.TransientModel` |
| **Single Settings** | `issingle: 1` DocType (`Agent Write Policy`) | Singleton record / `res.config.settings` model or dedicated single-record table |
| **API Endpoints** | Whitelisted methods (`@frappe.whitelist()`) | Odoo Controllers (`@http.route(..., type='json', auth='user')`) / Model `@api.model` methods |
| **Security & Access** | `Custom DocPerm` / `role` permissions | `security/ir.model.access.csv` + `security/security.xml` (`res.groups`) |
| **Background Crons** | `scheduler_events` in `hooks.py` | `data/ir_cron_data.xml` records on `ir.cron` |
| **Static Assets** | `public/js/mermaid.min.js`, `agent_chat.css` | `assets` bundle in `__manifest__.py` under `web.assets_backend` |

---

## 2. Directory Structure of the Target Odoo Module

Structure the module under `razyyn_ai/` as follows:

```text
razyyn_ai/
├── __init__.py
├── __manifest__.py
├── controllers/
│   ├── __init__.py
│   ├── agent_api.py              # External machine API (Query, Schema, Upload, Clarify)
│   └── chat_client_api.py        # Web client RPC endpoints (Auth, Chat turns, Sessions)
├── data/
│   └── ir_cron_data.xml          # Background cleanup and health-check crons
├── models/
│   ├── __init__.py
│   ├── agent_settings.py         # User credentials, JWT, and connection state
│   ├── agent_chat_session.py     # Conversation threads (Agent Chats)
│   ├── agent_chat_message.py     # Messages history (human / ai / attachments)
│   ├── agent_write_policy.py     # Write limits, allowed models, blocked accounts
│   ├── agent_write_log.py        # Audit trail of mutations executed by agent
│   ├── agent_messaging_settings.py # Multi-channel messaging configs
│   └── agent_message_log.py      # Communication logs (Telegram, WhatsApp, Email)
├── security/
│   ├── security.xml              # User groups (User, Manager)
│   └── ir.model.access.csv       # ACL rules
├── services/
│   ├── __init__.py
│   ├── agent_api_service.py      # Core execution and introspection logic
│   ├── agent_connection_service.py # 1-Click platform self-service connect
│   ├── query_guard.py            # Read SQL safety guard
│   └── write_guard.py            # Write policy validator and ledger safety guard
├── static/
│   ├── description/
│   │   └── icon.png              # App icon for the Odoo Apps dashboard
│   ├── lib/
│   │   ├── mermaid.min.js        # Mermaid diagram library
│   │   └── marked.min.js         # Markdown parser
│   └── src/
│       ├── components/
│       │   └── agent_chat/
│       │       ├── agent_chat.js     # OWL Component (Main Chat Interface)
│       │       ├── agent_chat.xml    # QWeb Template for the Chat UI
│       │       └── agent_chat.scss   # Modern ChatGPT/Claude-style styling
│       └── views/
│           └── agent_chat_view.js    # Client Action registration
└── views/
    ├── agent_settings_views.xml  # Form & List views for Settings
    ├── agent_write_policy_views.xml # Policy configuration screen
    ├── agent_write_log_views.xml # Audit trail tree & search views
    ├── agent_messaging_views.xml # Messaging configuration and logs
    ├── client_actions.xml        # Window action for OWL Chat Client Action
    └── menus.xml                 # App menu and sidebar navigation
```

---

## 3. Step 1: Manifest & Application Setup (`__manifest__.py`)

Update `razyyn_ai/__manifest__.py` to declare the module as a top-level Odoo application and bundle all UI assets:

```python
{
    "name": "Razyyn AI Accountant",
    "version": "17.0.2.0.0",
    "summary": "Autonomous AI Accounting Assistant & Chat Interface for Odoo",
    "description": """
        Full AI Accounting Suite:
        - Interactive Chat Interface (Ask, Analyse, Audit modes)
        - Self-Service 1-Click ERP Platform Connection
        - Safe Read Query Execution & Schema Introspection
        - Write Governance Policy, Limits & Audit Logs
        - File Attachment & Financial Report Processing
    """,
    "author": "Marwan Badr",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "depends": ["base", "mail", "web", "account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/agent_settings_views.xml",
        "views/agent_write_policy_views.xml",
        "views/agent_write_log_views.xml",
        "views/agent_messaging_views.xml",
        "views/client_actions.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "razyyn_ai/static/lib/marked.min.js",
            "razyyn_ai/static/lib/mermaid.min.js",
            "razyyn_ai/static/src/components/agent_chat/agent_chat.scss",
            "razyyn_ai/static/src/components/agent_chat/agent_chat.xml",
            "razyyn_ai/static/src/components/agent_chat/agent_chat.js",
            "razyyn_ai/static/src/views/agent_chat_view.js",
        ],
    },
    "installable": True,
    "application": True,
}
```

---

## 4. Step 2: Data Models & Persistence

Create models corresponding to each Frappe DocType:

### 4.1. `razyyn.agent.settings` (User Credentials & Connection)
Mirrors Frappe `Agent Settings`:
* `user_id`: `Many2one('res.users', required=True, default=lambda self: self.env.user)`
* `email`: `Char` (Username / Email on the remote Agent Server)
* `access_token`: `Char` (JWT access token for the platform, encrypted or stored securely)
* `custom_instructions`: `Text` (System prompt override for the accountant)
* `erp_connection_id`: `Char` (Connection UUID registered with the platform)
* `agent_erp_user_id`: `Many2one('res.users')` (System user representing the AI)
* `write_recording_enabled`: `Boolean` (Toggle whether AI writes are saved)
* `write_connected_on`: `Datetime`
* `write_last_error`: `Text`

### 4.2. `razyyn.agent.chat.session` (Agent Chats)
Mirrors Frappe `Agent Chats`:
* `session_id`: `Char(required=True, index=True, default=lambda: str(uuid.uuid4()))`
* `title`: `Char(default='New Conversation')`
* `user_id`: `Many2one('res.users', required=True, default=lambda self: self.env.user)`
* `last_update`: `Datetime(default=fields.Datetime.now)`
* `message_ids`: `One2many('razyyn.agent.chat.message', 'session_id')`

### 4.3. `razyyn.agent.chat.message` (Agent Chat History)
Mirrors Frappe `Agent Chat History`:
* `session_id`: `Many2one('razyyn.agent.chat.session', ondelete='cascade', required=True)`
* `sender`: `Selection([('human', 'Human'), ('ai', 'AI Agent')], required=True)`
* `content`: `Text(required=True)`
* `agent_mode`: `Selection([('ask', 'Ask'), ('analyse', 'Analyse'), ('audit', 'Audit')])`
* `attachment_ids`: `Many2many('ir.attachment')`
* `raw_tool_calls`: `Text` (JSON string of SQL or tool actions)
* `created_at`: `Datetime(default=fields.Datetime.now)`

### 4.4. `razyyn.agent.write.policy` (Write Governance Policy)
Mirrors Frappe `Agent Write Policy` (`issingle: 1`):
* `enabled`: `Boolean(string="Enable Agent Writes", default=False)`
* `dry_run_only`: `Boolean(string="Dry Run Only", default=False)`
* `require_approval`: `Boolean(string="Require Human Approval", default=True)`
* `restrict_to_listed_models`: `Boolean(default=False)`
* `allowed_model_ids`: `Many2many('ir.model', string="Allowed Models")`
* `max_documents_per_run`: `Integer(default=0)` (0 = unlimited)
* `max_total_amount_per_run`: `Float(string="Max Total Amount", default=0.0)`
* `posting_date_max_days_back`: `Integer(default=0)`
* `posting_date_max_days_forward`: `Integer(default=0)` (0 = forbids future dating)
* `allowed_company_ids`: `Many2many('res.company')`
* `blocked_account_ids`: `Many2many('account.account', string="Blocked Accounts")`

### 4.5. `razyyn.agent.write.log` (Audit Trail)
Mirrors Frappe `Agent Write Log`:
* `timestamp`: `Datetime(default=fields.Datetime.now)`
* `user_id`: `Many2one('res.users')`
* `session_id`: `Char`
* `target_model`: `Char`
* `target_record_id`: `Integer`
* `action_type`: `Selection([('create', 'Create'), ('write', 'Write'), ('cancel', 'Cancel')])`
* `status`: `Selection([('in_flight', 'In Flight'), ('success', 'Success'), ('rejected', 'Rejected'), ('failed', 'Failed')])`
* `payload`: `Text`
* `rejection_reason`: `Text`

---

## 5. Step 3: Backend Services & Controllers

### 5.1. Chat Client Controller (`controllers/chat_client_api.py`)
Provides JSON RPC routes for the OWL Chat UI:

1. **Auth & Proxy Routes:**
   * `/razyyn/auth/register`: Forwards registration payload to `{AGENT_SERVER_URL}/users/`.
   * `/razyyn/auth/login`: Posts to `{AGENT_SERVER_URL}/auth/login`, stores JWT in `razyyn.agent.settings`, returns token.
   * `/razyyn/auth/status`: Checks if current user has active credentials.
2. **Session Management Routes:**
   * `/razyyn/chat/sessions`: Lists sessions for the current logged-in user.
   * `/razyyn/chat/create_session`: Initializes a new conversation with a UUID.
   * `/razyyn/chat/rename_session`: Updates session title.
   * `/razyyn/chat/delete_session`: Removes session and associated messages.
   * `/razyyn/chat/history`: Fetches paginated messages for a session (bounded by `MAX_HISTORY_PAGE_SIZE = 200`).
3. **Chat Execution Turn:**
   * `/razyyn/chat/send_turn`:
     - Validates session ownership.
     - Formats conversation payload bounded by `MAX_HISTORY_MESSAGES = 40`.
     - Handles file attachments (enforcing mode limits: Ask ≤ 1MB, Analyse ≤ 25MB, Audit ≤ 50MB).
     - Sends request to `{AGENT_SERVER_URL}/chat/turn` with bearer token.
     - Persists user prompt and AI response in `razyyn.agent.chat.message`.
     - Returns response including any clarifying question blocks or file download links.

### 5.2. 1-Click Platform Self-Service Connect (`services/agent_connection_service.py`)
Mirrors Frappe `connect.py`:
* **Provisioning User:** Ensure a dedicated system user (`accountant_agent@razyyn.internal`) with the "Razyyn Agent" role exists.
* **Mints Credentials:** Generates API Key / Secret pair on the agent user.
* **Platform Registration:** Sends a POST request to `{AGENT_SERVER_URL}/api/create/connections` with `site_url`, `api_key`, `api_secret`, and `label`.
* **Verification & Recording:** Validates bi-directional handshake, saves connection ID, and manages the `write_recording_enabled` switch.
* **Rotate & Disconnect:** Handles safe credential rotation and complete disconnection.

### 5.3. Write Guard Service (`services/write_guard.py`)
Mirrors Frappe `agent_write_service.py`:
* Before any write operation executes:
  1. Checks if `Agent Write Policy.enabled` is `True`.
  2. If `dry_run_only` is `True`, validates data and simulates posting without committing.
  3. Validates model against `allowed_model_ids`.
  4. Validates posting dates against `posting_date_max_days_back` and `posting_date_max_days_forward`.
  5. Checks accounting lines to prevent modifications to `blocked_account_ids` (e.g., Retained Earnings, Tax Control).
  6. Enforces limits on `max_documents_per_run` and `max_total_amount_per_run`.
  7. Creates an audit record in `razyyn.agent.write.log`.

---

## 6. Step 4: Frontend Chat Interface (OWL Component)

Build a modern, responsive single-page chat experience in Odoo 17 using the **OWL (Odoo Web Library)** framework.

### 6.1. Component Architecture (`static/src/components/agent_chat/`)

1. **`agent_chat.js` (State & Business Logic):**
   * Uses `useState` for reactive UI state:
     - `currentSessionId`: Active chat thread.
     - `sessions`: List of user conversations.
     - `messages`: Array of messages in active thread.
     - `agentMode`: `'ask'` | `'analyse'` | `'audit'`.
     - `isGenerating`: Boolean flag controlling streaming/typing indicator and Stop button.
     - `attachments`: List of staged files.
     - `clarificationModal`: Object holding active clarifying questions from the agent.
   * Interacts with Odoo backend via `useService("rpc")` and `useService("notification")`.
   * Integrates `marked.js` for Markdown rendering and `mermaid.js` for dynamic chart rendering.

2. **`agent_chat.xml` (QWeb Template):**
   * **Left Sidebar:**
     - Header: "New Chat" button and search bar.
     - Session List: Scrollable history items with title, date, active indicator, edit/rename button, and delete confirmation.
     - Footer: User profile and Connection Status badge.
   * **Main Chat Container:**
     - **Header Bar:** Agent Mode Selector pill buttons (Ask, Analyse, Audit), Connection Status indicator, and Settings drawer button.
     - **Message Stream:**
       - Human bubble (right-aligned) with attachment preview tags.
       - AI bubble (left-aligned) with avatar, Markdown-formatted content, code block copy buttons, Mermaid diagrams, and clarifying question prompt cards.
       - Typing / Thinking animation indicator when waiting for server.
     - **Interactive Clarification Cards:** Embedded action buttons/options when the AI requests user input on an accounting transaction.
     - **Composer & Input Bar:**
       - File drag-and-drop overlay.
       - Attachment chips showing file name, size, and remove button.
       - Auto-resizing textarea with `Enter` (send) and `Shift+Enter` (new line) shortcuts.
       - Action buttons: Attach File, Agent Mode toggle, Send button / Stop Generation button.

3. **`agent_chat.scss` (Styling):**
   * ChatGPT/Claude aesthetics with clean border radii, subtle elevation shadows, smooth transition animations, and dark/light mode support.

### 6.2. Client Action Registration (`static/src/views/agent_chat_view.js` & `views/client_actions.xml`)

Register the OWL component as an Odoo client action:

```javascript
/** @odoo-module **/
import { registry } from "@web/core/registry";
import { AgentChatComponent } from "../components/agent_chat/agent_chat";

registry.category("actions").add("razyyn_agent_chat", AgentChatComponent);
```

Declare the action in XML (`views/client_actions.xml`):

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="action_razyyn_agent_chat" model="ir.actions.client">
        <field name="name">AI Accountant Chat</field>
        <field name="tag">razyyn_agent_chat</field>
        <field name="target">current</field>
    </record>
</odoo>
```

---

## 7. Step 5: Menus & Navigation (`views/menus.xml`)

Provide clean, intuitive navigation for end users and administrators:

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Top-Level Root Menu -->
    <menuitem id="menu_razyyn_root"
              name="AI Accountant"
              web_icon="razyyn_ai,static/description/icon.png"
              sequence="20"/>

    <!-- Chat Interface (Primary Entrypoint) -->
    <menuitem id="menu_razyyn_chat"
              name="Agent Chat"
              parent="menu_razyyn_root"
              action="action_razyyn_agent_chat"
              sequence="10"/>

    <!-- Governance & Administration Menu -->
    <menuitem id="menu_razyyn_governance"
              name="Governance &amp; Settings"
              parent="menu_razyyn_root"
              sequence="20"
              groups="base.group_system"/>

    <menuitem id="menu_razyyn_write_policy"
              name="Write Policy"
              parent="menu_razyyn_governance"
              action="action_razyyn_write_policy"
              sequence="10"/>

    <menuitem id="menu_razyyn_write_log"
              name="Audit Logs"
              parent="menu_razyyn_governance"
              action="action_razyyn_write_log"
              sequence="20"/>

    <menuitem id="menu_razyyn_settings"
              name="Connection Settings"
              parent="menu_razyyn_governance"
              action="action_razyyn_agent_settings"
              sequence="30"/>
</odoo>
```

---

## 8. Step 6: Security & Permissions

1. **Security Groups (`security/security.xml`):**
   * `group_razyyn_user`: Standard users who can use the Chat UI and view their own sessions.
   * `group_razyyn_manager`: Accountants/Managers who can configure Write Policies and view Audit Logs.
   * `base.group_system`: Full admin access (connect/disconnect ERP, rotate keys).

2. **Access Rights (`security/ir.model.access.csv`):**
   * Configure granular CRUD permissions for `razyyn.agent.chat.session`, `razyyn.agent.chat.message`, `razyyn.agent.settings`, `razyyn.agent.write.policy`, and `razyyn.agent.write.log`.

---

## 9. Implementation Roadmap & Milestones

Execute the development in these ordered phases:

### Phase 1: Models, Security & Manifest
- [ ] Update `__manifest__.py` with `"application": True`, new models, and views.
- [ ] Create security groups in `security.xml` and complete `ir.model.access.csv`.
- [ ] Implement models: `razyyn.agent.settings`, `razyyn.agent.chat.session`, `razyyn.agent.chat.message`, `razyyn.agent.write.policy`, `razyyn.agent.write.log`.
- [ ] Verify clean module upgrade: `odoo-bin -u razyyn_ai -d your_db`.

### Phase 2: Backend RPC & Connection Service
- [ ] Implement `controllers/chat_client_api.py` (Auth proxy, session CRUD, chat turn forwarding).
- [ ] Implement `services/agent_connection_service.py` (1-Click ERP self-service connect, key generation, platform registration).
- [ ] Implement `services/write_guard.py` (Ledger checks, limits, dry-run simulation, audit logging).

### Phase 3: OWL Chat Interface
- [ ] Add `marked.min.js` and `mermaid.min.js` to `static/lib/`.
- [ ] Build `AgentChatComponent` in OWL (`agent_chat.js`, `agent_chat.xml`, `agent_chat.scss`).
- [ ] Implement session sidebar (create, rename, delete, search).
- [ ] Implement message stream with Markdown parsing, syntax highlighting, and Mermaid charts.
- [ ] Implement file drag-and-drop uploader with mode-specific validation.
- [ ] Implement clarifying questions interactive modal/cards.

### Phase 4: Verification & Polish
- [ ] Test 1-Click Connect handshake with the agent server.
- [ ] Test real chat conversations across Ask, Analyse, and Audit modes.
- [ ] Test file attachments upload and preview.
- [ ] Verify Write Policy refusal when writes are disabled or limits are exceeded.
- [ ] Confirm zero browser console errors and verify responsive layout on mobile/tablet viewports.
