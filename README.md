# Razyyn AI for Odoo 18

> **`razyyn_ai/` here is GENERATED. Do not edit it.**
>
> Every file under `razyyn_ai/` is produced from the Odoo 17 module by
> `razyyn-odoo-17/tools/sync_to_odoo18.py`. Make the change there and re-run
> that script; anything edited here is overwritten the next time anyone does.
> `sync_to_odoo18.py --check` fails if the two have drifted, which is what
> stops that from happening silently.

[![Odoo Version](https://img.shields.io/badge/Odoo-17.0%20%7C%2018.0-714B67.svg?style=flat-square)](https://odoo.com)
[![Python Version](https://img.shields.io/badge/Python-3.10%2B-green.svg?style=flat-square)](https://python.org)
[![License](https://img.shields.io/badge/License-LGPL--3-amber.svg?style=flat-square)](https://www.gnu.org/licenses/lgpl-3.0.html)
[![Status](https://img.shields.io/badge/Production-Ready-success.svg?style=flat-square)](#verifying-an-install)

> **Your complete finance department, powered by AI — inside Odoo.**
>
> Learn more at [razyyn.com](https://razyyn.com).

Razyyn AI adds a natural-language finance team to your Odoo backend. It answers
accounting questions, analyses your financials, audits your ledgers, reconciles
bank statements, and prepares accounting entries for your review — all without
leaving Odoo, and never touching your books without your explicit approval.

It works the way your finance department is supposed to: securely, and in line
with **your company's own policies, your country's tax and regulatory rules, and
standard accounting principles (IFRS / GAAP)** — all configurable, so the agent
reasons and acts the way your organisation actually operates.

This is one piece of the Razyyn stack:

- the agent backend (FastAPI + LangGraph) — `accountant_agent/`
- the same bridge for ERPNext/Frappe — `Razyyn-AI-Frappe` (v14 and v15 branches)
- **the source this tree is generated from** — `razyyn-odoo-17/`

**It is the same product, not a lookalike.** The chat window is the Frappe app's
own, copied file for file; the read protocol, the write gateway, the governance
model and the desks behind them are shared. An Odoo customer and an ERPNext
customer get the same agent.

---

## Table of Contents

**For accountants and administrators**

- [What Razyyn AI Does](#what-razyyn-ai-does)
- [Prerequisites & System Requirements](#prerequisites--system-requirements)
- [Installation Guide](#installation-guide)
- [Connecting Your Odoo](#connecting-your-odoo)
- [Configuration Guide](#configuration-guide)
  - [1. Connections (Agent Settings)](#1-connections-agent-settings)
  - [2. Write Policy (Guardrails for Ledger Writes)](#2-write-policy-guardrails-for-ledger-writes)
  - [3. Messaging Channels (Email, Telegram & Slack)](#3-messaging-channels-email-telegram--slack)
  - [4. Odoo Groups & Record Access](#4-odoo-groups--record-access)
- [Auditability & Observability](#auditability--observability)
- [Usage Examples](#usage-examples)
- [Troubleshooting & FAQs](#troubleshooting--faqs)
- [Security, Privacy & Compliance](#security-privacy--compliance)

**For developers and integrators**

- [What it does](#what-it-does) — the routes, the chat, messaging, OCR
- [The security boundary](#the-security-boundary)
- [Verifying an install](#verifying-an-install)
- [Odoo 17 and 18](#odoo-17-and-18)
- [Installing (from source)](#installing)
- [License & Support](#license--support)

---

## What Razyyn AI Does

Accounting work is complex and high-stakes — a simple chatbot is not enough to
reconcile accounts, spot irregularities, or post ledger entries reliably. Razyyn
AI is built specifically for the job, directly inside your Odoo backend:

- **Ask accounting questions & look things up.** Chart of accounts, customer
  balances, vendor status, tax rates, IFRS/GAAP guidance — answered against your
  live data.
- **Analyse your finances.** Ratios, ageing, budget variances and trend analysis,
  with interactive charts rendered right in the chat.
- **Audit your ledgers.** Flags anomalies, duplicate payments, round-sum
  transactions, off-hours postings and other internal-control red flags, with
  severity ratings and remediation notes.
- **Reconcile bank statements.** Upload a statement (CSV/Excel/PDF) and Razyyn AI
  matches it against your ledger, classifying differences and proposing
  settlement entries.
- **Read your documents.** Extracts data from uploaded invoices, receipts and
  contracts (PDF, DOCX, CSV, Excel, images) — on your own server.
- **Prepare and post entries — with your approval.** Journal entries, payments,
  customer invoices, vendor bills, contacts and more are proposed as a clear card
  showing every debit, credit, tax and party. Nothing is written to your books
  until you click **Approve**.
- **Export & deliver results.** Generates PDF, Excel, CSV and TXT deliverables,
  and can send reports and alerts by **Email**, **Telegram** or **Slack**.
- **Zero-credential connection.** No API keys to copy or paste — your Odoo
  connects to Razyyn AI securely in one click.
- **Fail-closed by default.** Out of the box the agent has zero write
  permissions. Every write is bound by both your Odoo access rights and a
  server-enforced Write Policy you control.
- **Compliant by configuration.** Teach the agent your company's policies, your
  country's rules and your chart conventions, and every answer, entry and audit
  finding respects them.
- **Secure end-to-end.** Encrypted credentials, an append-only audit trail and
  strict tenant data isolation protect your books at every step.

---

## Prerequisites & System Requirements

| Component | Requirement |
|---|---|
| **Odoo** | Version 17.0 (this module) or 18.0 (the generated `razyyn-odoo-18`) |
| **Odoo apps** | `base`, `mail`, `web`, `account` — `account` is what makes this an accounting install |
| **Python** | Python 3.10, 3.11 or 3.12 — whichever interpreter runs Odoo |
| **Database** | PostgreSQL 14+ (Odoo's own requirement) |
| **Python dependencies** | `requests`, plus `pytesseract` and `pdf2image` for reading scans — **fetched automatically when the module installs**; see [Reading photographed invoices](#reading-photographed-invoices) |
| **System packages (for OCR)** | `tesseract-ocr` with the language data you need (e.g. `eng`, `ara`) and `poppler-utils`, installed with your OS package manager |
| **Browser support** | Modern Chrome, Firefox, Safari, Edge (desktop & tablet) |

---

## Installation Guide

### Step 1: Put the module on your addons path

Clone the branch that matches your Odoo — `Razyyn-AI-Odoo-v17` for Odoo 17,
`Razyyn-AI-Odoo-v18` for Odoo 18:

```bash
git clone -b Razyyn-AI-Odoo-v17 https://github.com/Marwan-badr543/Razyyn-AI-Odoo.git razyyn-odoo
cp -r razyyn-odoo/razyyn_ai /path/to/odoo/custom_addons/
```

Or add the checkout directory to `addons_path` in your `odoo.conf`:

```ini
addons_path = /path/to/odoo/addons,/path/to/razyyn-odoo
```

### Step 2: Install the module

```bash
odoo-bin -c odoo.conf -d your_database -i razyyn_ai --stop-after-init
```

Installing also fetches the two OCR packages into the interpreter running Odoo.
Nothing here can fail the install: a server with no route to PyPI still gets the
module, the chat and every desk — it simply cannot read the words out of a
photograph until the packages are there.

### Step 3: Restart Odoo and open the app

```bash
odoo-bin -c odoo.conf
```

**Razyyn AI** appears in the apps menu. Its assets need no build step: the chat
window is served as static files whose URLs carry a hash of their own contents,
so a module upgrade can never leave a browser running last week's chat.

> [!IMPORTANT]
> **Upgrading an existing install** — always run `-u razyyn_ai` after pulling a
> new version. The Python and static files are picked up on restart, but the
> screens (`views/*.xml`) ship through a module upgrade, as they do for every
> Odoo module.

### Upgrading from `razyyn_agent_connector`

The module was renamed. Run `migrations/rename_from_razyyn_agent_connector.sql`
against the database once, with Odoo stopped, before starting it with
`-u razyyn_ai`. It moves the ownership records and keeps every chat session,
every audit-log row and the customer's API key. Skip it on a fresh install.

---

## Connecting Your Odoo

Traditional integrations force administrators to generate API keys and copy
secrets between apps. Razyyn AI eliminates this with **zero-credential,
self-service onboarding**:

```
  [Razyyn AI → Agent Chat] ──► [Sign In / Register] ──► [Click "1-Click Connect"] ──► [Ready to Operate]
```

1. Log in to Odoo as an **Administrator** (Settings → Users → your user must
   hold **Razyyn AI Accountant / Manager**).
2. Open **Razyyn AI → Agent Chat**.
3. If you have not created an account yet, register your email; otherwise sign in
   with your Razyyn credentials.
4. Open **Razyyn AI → Razyyn AI Models → Connections** and press
   **1-Click Connect**:
   - The module creates a dedicated system user `razyyn_ai@razyyn.internal`.
   - It issues a unique API key and stores only a salted hash of it.
   - It registers the connection with Razyyn AI over your authenticated session,
     declaring this ERP as **Odoo** so the agent speaks PostgreSQL and Odoo's own
     model names from the first question.
   - **Connection Status** becomes *Connected (Read Only)*.
5. When you are ready for the agent to record entries, press **Enable Recording**
   and switch the **Write Policy** on.

> [!NOTE]
> Connecting establishes identity only. Out of the box the agent holds **zero
> business permissions** and **recording is disabled**. You keep absolute control
> over what it can reach.

---

## Configuration Guide

### 1. Connections (Agent Settings)

> **Menu:** Razyyn AI → Razyyn AI Models → Connections
> **Model:** `razyyn.agent.settings` | **Access:** Razyyn AI Manager

One record links one Odoo user to their Razyyn AI account.

| Field | Type | What it is | How to set it |
|---|---|---|---|
| **Connection Name** (`label`) | Char | A friendly name for this connection. | Anything you like; it is what the list shows. |
| **Razyyn Account Email** (`email`) | Char | The email registered with Razyyn AI. | Filled in automatically when you sign in from the chat page. |
| **Company** (`company_id`) | Many2one | Whose data this key may reach. | Defaults to your active company. |
| **Agent ERP User** (`agent_erp_user_id`) | Many2one | The dedicated Odoo user the agent acts as. | Created for you by **1-Click Connect**. |
| **Custom Instructions** (`custom_instructions`) | Text | Your accounting rules, sent with every chat turn. | See below — this is the field that makes the agent *yours*. |
| **Allow Agent Recording** (`write_recording_enabled`) | Boolean | Whether the platform may send writes at all. | Toggle with **Enable / Disable Recording**. |
| **Connection Status** (`connection_status`) | Char | *Disconnected*, *Connected (Read Only)*, or *Connected & Recording Enabled*. | Read-only. |
| **Last Verification Error** (`write_last_error`) | Text | Why the last handshake failed, if it did. | Read-only; on the **Description & Diagnostics** tab. |

#### Teaching the agent your company, country and accounting rules

`Custom Instructions` is where you make Razyyn AI operate as **your** finance
department rather than a generic one. Everything typed here is applied to every
question, analysis, audit and entry. Cover three layers:

1. **Company policy** — your internal SOPs and thresholds.
   > *"Default company is 'Acme Global FZE'. Default analytic account is 'Head
   > Office'. Vendor bills above $5,000 require a Finance Manager's approval
   > before posting. Always use FIFO for inventory valuation."*
2. **Country & regulatory rules** — the tax regime you operate under.
   > *"We operate under UAE VAT law (5% standard rate). Apply reverse charge for
   > imported services. Retain supporting documents for 5 years per FTA
   > requirements. Do not backdate entries into a closed VAT period."*
3. **Accounting principles** — the framework your statements follow.
   > *"Report under IFRS. Recognise subscription revenue over the service period
   > per IFRS 15. Classify leases per IFRS 16."*

> [!TIP]
> Keep instructions short, specific and stated as rules rather than prose. The
> agent applies them literally, so precise thresholds and named accounts work far
> better than general guidance.

#### Company Knowledge tab

Two things the agent reads before answering a question about how **your**
business keeps its books:

- **Country whose accounting rules apply** — chosen from the list the platform
  publishes, so a mistyped two-letter code can never select the wrong
  jurisdiction. Press **Save** after choosing.
- **Your accounting policy** — one searchable-text PDF (up to 40 MB) holding your
  policy, your approval rules and your chart conventions. It is forwarded
  straight to Razyyn AI, which extracts the text and indexes it; **no copy is
  kept in Odoo**, in the database or in the filestore. The card reports how many
  pages and searchable sections were indexed, and **Remove** stops the agent
  applying it immediately.

Scans and photographs of pages are not accepted here — the agent reads the text
of this document, it does not look at the picture.

#### Description & Diagnostics tab

Says whether this server can read scanned documents, names anything missing, and
offers **Install Document Reading** to try the fetch again. The reasons a fetch
fails — a proxy, a firewall, a mirror being down — are all things fixed
afterwards.

---

### 2. Write Policy (Guardrails for Ledger Writes)

> **Menu:** Razyyn AI → Razyyn AI Models → Write Policy
> **Model:** `razyyn.agent.write.policy` | **Access:** Razyyn AI Manager

The Write Policy is your organisation's server-side safety harness. It is
enforced inside your own Odoo on every write attempt, read fresh on every run and
never cached — **no write that violates it can be committed**.

#### A. Master switches

| Field | Default | What it does | Best practice |
|---|---|---|---|
| **Enable Agent Writes** (`enabled`) | `False` | Master kill switch. While off, the agent is 100% read-only. | Leave off until your setup is reviewed. |
| **Dry Run Only** (`dry_run_only`) | `False` | The agent validates every document and rolls the transaction back before it saves. | **Turn on for the first two weeks** to verify accuracy without risk. |
| **Require Human Approval** (`require_approval`) | `True` | A person must press **Approve** on the proposal card in chat before anything is written. | Keep on for maximum governance. |

#### B. Permitted models

| Field | What it does |
|---|---|
| **Restrict To Listed Models** (`restrict_to_listed_models`) | When off (default), the agent may prepare any document its Odoo user is allowed to. When on, writes are limited strictly to the list below. |
| **Allowed Models** (`allowed_model_ids`) | The explicit whitelist — e.g. `account.move`, `account.payment`, `res.partner`. |

#### C. Blast-radius limits

Controls the maximum exposure of any single run.

| Field | Default | Behaviour | Suggested |
|---|---|---|---|
| **Max Documents Per Run** (`max_documents_per_run`) | `0` | Most records one run may write. `0` = unlimited. | `50` for batch imports. |
| **Max Total Amount Per Run** (`max_total_amount_per_run`) | `0` | Largest cumulative value one run may record. `0` = unlimited. | Your practice's single-batch threshold. |
| **Posting Date — Max Days Back** (`posting_date_max_days_back`) | `0` | Refuses backdating beyond N days. `0` = unlimited. *(Odoo's own lock dates always apply as well.)* | `30`, to keep the agent out of closed periods. |
| **Posting Date — Max Days Forward** (`posting_date_max_days_forward`) | `0` | **Unlike the others, `0` here forbids future dating entirely.** | Leave at `0` unless advance-dated documents are needed. |

#### D. Scope restrictions

- **Allowed Companies** (`allowed_company_ids`) — in a multi-company database,
  the companies the agent may touch. Empty means every company its Odoo user can
  reach.
- **Blocked Accounts** (`blocked_account_ids`) — accounts the agent may **never**
  debit or credit under any circumstance (retained earnings, suspense, statutory
  VAT control accounts).

> [!NOTE]
> A posted document is never edited. Odoo refuses it and so does this module: a
> posted entry is corrected by reversing it and recording a corrected one, which
> is what the agent will propose.

---

### 3. Messaging Channels (Email, Telegram & Slack)

> **Menu:** Razyyn AI → Razyyn AI Models → Messaging Channels
> **Model:** `razyyn.agent.messaging.settings` | **Access:** Razyyn AI Manager

Lets Razyyn AI deliver statements, audit summaries and alerts over three
channels, each on its own tab. Credentials live in this Odoo and never leave it —
the agent asks for an outcome ("send this to the finance group") and is never
handed a token.

#### Tab 1: Email (SMTP)

| Field | Value / setup |
|---|---|
| **Email Enabled** (`email_enabled`) | Switch on to allow outbound email. |
| **Send As** (`email_from`) | The mailbox the agent sends from, e.g. `finance@yourcompany.com`. |
| **Sender Name** (`email_sender_name`) | The name recipients see, e.g. `Acme Finance Agent`. |
| **Mail Server** (`smtp_host`) | Your SMTP host, e.g. `smtp.gmail.com`, `smtp.office365.com`. |
| **Port** (`smtp_port`) | Usually `587` for STARTTLS, `465` for SSL. |
| **Security** (`smtp_security`) | `STARTTLS`, `SSL`, or `None` (only inside your own network). |
| **Username** (`smtp_username`) | Usually the sending address itself. |
| **Password** (`smtp_password`) | An **App Password** if your provider requires one — never the mailbox owner's personal password. Stored encrypted. |
| **Last Email Error** (`email_last_error`) | Read-only; the exact SMTP response when a send fails. |

#### Tab 2: Telegram

1. In Telegram, message **@BotFather** and send `/newbot`; copy the HTTP API
   token.
2. Add the bot to your group or channel as an **Administrator** with permission
   to post.
3. Send a test message, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and read
   `"chat":{"id": -1001234567890}`. **Group IDs are negative — keep the minus
   sign.**
4. Fill in **Telegram Enabled** (`telegram_enabled`) and **Bot Token**
   (`telegram_bot_token`), then add the chat on the Destinations tab.

#### Tab 3: Slack

1. Create a Slack app, give it the `chat:write` and `files:write` bot scopes, and
   install it to the workspace.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) into **Slack Bot Token**
   (`slack_bot_token`) and switch **Slack Enabled** (`slack_enabled`) on.
3. Invite the bot to each channel with `/invite @your-bot-name`, then add the
   channel on the Destinations tab.

#### Destinations — the list the agent may send to

> **Model:** `razyyn.agent.messaging.destination`

A Telegram bot cannot start a conversation and a Slack bot cannot post in a
channel it was never invited to, so **the places the agent may send to are a list
this company keeps**, each on its own channel's tab:

| Field | What it is |
|---|---|
| **Channel** (`channel`) | `email`, `telegram` or `slack`. |
| **Name** (`label`) | The name an accountant would say out loud — `Finance Team`, `CFO`, `Auditors`. |
| **Address** (`address`) | The mailbox, chat id or channel id. |
| **Default** (`is_default`) | Used when the request names no destination. |
| **Notes** (`notes`) | Free text for whoever maintains the list. |

A destination that is not on the list is **refused, not invented**.

---

### 4. Odoo Groups & Record Access

> **Settings → Users & Companies → Users**, under **Razyyn AI Accountant**

| Group | Who it is for | What it grants |
|---|---|---|
| **User** (`group_razyyn_user`) | Every internal employee who should be able to use the chat. | The chat page, their own conversations, and the model browser. |
| **Manager** (`group_razyyn_manager`) | Administrators and the finance lead. | Everything above, plus the Write Policy, the audit logs, the connection records and the messaging credentials. |

Conversations are private per user, enforced by global record rules rather than
by menus — a User sees their own chat sessions and messages and nobody else's.

What the agent may **do** in your books is decided twice, and both must allow it:
by the Odoo access rights of `razyyn_ai@razyyn.internal` (give it
`Accounting / Billing` or a narrower custom group, and use record rules to fence
it to a company, journal or analytic account), and by the Write Policy above.

---

## Auditability & Observability

### 1. Write Audit Logs

> **Menu:** Razyyn AI → Razyyn AI Models → Write Audit Logs
> **Model:** `razyyn.agent.write.log`

An append-only record of **every** write attempt — committed, refused or failed:

- **Idempotency key** — the cryptographic key that makes a retried request safe;
  a network retry can never duplicate an entry.
- **Action** — `create`, `update`, `submit`, `cancel` or `amend`.
- **Status** — `in_flight`, `success`, `failed` or `rejected`.
- **Target** — the model and record id, plus the document state that resulted.
- **Who** — the session and the person who approved it.
- **Payload** — exactly what was sent, and the system's own answer or refusal.

### 2. Message Logs

> **Menu:** Razyyn AI → Razyyn AI Models → Message Logs
> **Model:** `razyyn.agent.message.log`

Every outbound email, Telegram and Slack message — delivered, refused or failed:
channel, destination, status, the provider's own message id (the receipt),
subject, body preview, attachment names, and who asked for it and who approved
it. An outbound record holding only successes cannot answer *"did the agent email
that to our client?"*, which is the question an auditor actually asks.

### 3. Conversations, Messages and Stream Events

> **Menu:** Razyyn AI → Razyyn AI Models → Conversations / Messages / Stream Events

The saved transcript of every chat, and the live progress rows that let a reload
mid-run rejoin the work instead of losing it.

---

## Usage Examples

Open **Razyyn AI → Agent Chat**.

### 💬 General inquiries & accounting guidance
> *"What is our total outstanding receivable across all customers as of today,
> and who are our top 3 overdue debtors?"*

> *"Explain how we should account for software subscription revenue under
> IFRS 15, and draft the required journal entry pattern."*

### 📊 Financial analysis & visualisations
> *"Analyse our sales performance for Q1 and Q2 this year broken down by product
> category. Render a comparison graph and export the detailed variance analysis
> to Excel."*

> *"Calculate our current ratio, quick ratio and debt-to-equity ratio from the
> latest balance sheet, and highlight any liquidity risks."*

### 🔍 Forensic auditing & internal controls
> *"Audit all journal entries posted in the last 30 days. Flag round-sum
> transactions, entries posted outside business hours, and potential duplicate
> payments to vendors."*

> *"Perform a Benford's Law analysis on our vendor payments this fiscal year.
> Summarise anomalies in an audit table and send the findings to the Finance Team
> on Telegram."*

### ⚖️ Bank & ledger reconciliation
*(Attach `bank_statement_august.csv` with the paperclip, or drag and drop it)*
> *"Reconcile this bank statement against our 'Bank' journal for August 2026.
> Identify uncredited deposits, unpresented cheques and missing bank charges."*

### ✍️ Recording and posting
> *"Record a customer payment of $3,500 from 'Apex Global' against invoice
> 'INV/2026/00120' into the Bank journal. Email me the confirmation once it is
> posted."*

> *Razyyn AI reads your ledger, composes the document, checks your Odoo will
> accept it, shows you a card with every debit and credit, and waits for you to
> press **Approve**. The receipt afterwards names the document the way your own
> screen names it — a journal entry has no number until it is posted, so a draft
> is reported as a draft and a posted entry by its number.*

### 🎚️ The two switches beside the message box

- **Scan & Extract Data** — on by default. Reads the words out of photographed
  and scanned attachments on your own server. Turn it off when you want the agent
  to *look at* a picture rather than read it.
- **High Thinking** — off by default. Puts your question to the advisory board — a
  team leader and three senior consultants deliberating in parallel — before the
  work is planned. Slower and more thorough; right for a genuinely hard matter,
  needless for a lookup.

---

## Troubleshooting & FAQs

### 1. The agent says recording is disabled
- **Cause:** Recording is off on the connection, or the Write Policy master
  switch is off.
- **Fix:** Open **Connections** and check the status reads *Connected & Recording
  Enabled*, then open **Write Policy** and tick **Enable Agent Writes**.

### 2. A write was refused: *"Account X is blocked by policy"*
- **Cause:** The entry touched an account in **Blocked Accounts**.
- **Fix:** If the entry is legitimate, remove the account from the list, or have
  an authorised person post it by hand.

### 3. The agent says a document type does not exist
- **Cause:** It asked for something this database genuinely does not have.
- **Fix:** The refusal names what the system *does* have — Odoo's own labels are
  understood, so "Journal Entry" finds `account.move` without you translating
  anything. If the list looks wrong, the module that provides that document is
  probably not installed.

### 4. The chat looks out of date after an upgrade
- **Cause:** Almost certainly not the browser. The chat's assets carry a hash of
  their own contents in their URL, so an upgrade changes the URL and the browser
  fetches the new file.
- **Fix:** Run `-u razyyn_ai`. The *screens* (`views/*.xml`) ship through a module
  upgrade even though the chat assets cache-bust themselves.

### 5. Telegram error: *"chat not found"*
- **Cause:** The chat id is missing its leading minus sign, or the bot was never
  added to the group.
- **Fix:** Group ids start with `-100…`. Check the bot is an administrator there.

### 6. Email fails to send
- **Cause:** Wrong username/password, or the **Security** mode does not match the
  port.
- **Fix:** Use an **App Password** if your provider requires one, match
  `STARTTLS` to `587` and `SSL` to `465`, and read **Last Email Error** for the
  server's exact response.

### 7. Slack error: *"channel_not_found"* or *"not_in_channel"*
- **Cause:** The bot is not in the channel, or the id is wrong.
- **Fix:** `/invite @your-bot-name` in the channel, then check the id on the
  Destinations tab.

### 8. Two Odoos on one machine fight over the login
- **Cause:** Cookies ignore the port, so both servers write the same `session_id`
  for `localhost`.
- **Fix:** Reach one as `127.0.0.1` and the other as `localhost`. A customer with
  one Odoo never meets this.

---

## Security, Privacy & Compliance

- **Read-only SQL, checked before it runs.** Every statement the agent sends is
  validated by `services/query_guard.py` — one `SELECT` (or `WITH … SELECT`) at a
  time, no writes, no stacked statements, no reaching into credential columns.
- **Encrypted, hashed credentials.** The API key is stored only as a salted
  PBKDF2-SHA256 hash; SMTP passwords and bot tokens are held in Odoo's own
  encrypted fields. Re-issuing a key retires the old one in the same call.
- **Append-only audit trail.** Write Audit Logs and Message Logs cannot be edited
  or deleted from the interface.
- **Governed writes.** The Write Policy is read fresh on every run, and a posted
  document is never edited in place.
- **Strict tenant isolation.** The Razyyn backend enforces row-level security so
  one customer's data can never be reached from another's session.
- **No model training.** Accounting data sent for reasoning is ephemeral and is
  never used to train or fine-tune a model.
- **Compliance-aware by configuration.** The agent works within the company
  policy, country rules and accounting framework you configure, and every write
  is bound in addition by your Odoo access rights and the Write Policy.

---

## What it does

### Seven endpoints the agent reads through

| Route | Purpose |
|---|---|
| `POST /agent_api/execute_query` | Run one read-only SELECT |
| `POST /agent_api/get_schema` | Describe one model's real columns |
| `POST /agent_api/upload_file` | Attach a generated report to a session |
| `POST /agent_api/request_clarification` | Put a question to the customer |
| `POST /agent_api/messaging_config` | What this site can send through |
| `POST /agent_api/send_message` | Send one message on the company's behalf |

### Seven more the agent writes through

`/agent_write/{get_document_spec, search_documents, get_write_policy,
get_write_log, list_documents, preflight, write_batch}` — the Razyyn write
gateway protocol, the same one the Frappe app implements. The agent has ONE
client for both (`agent/agent_create/adapters/gateway.py`); a vendor is a
subclass with three answers, not a second implementation.

### The chat, inside Odoo

Razyyn AI is a page of the Odoo application: the menu, the breadcrumbs and every
other screen stay where they are while the customer works in it.

**It is the Frappe app's chat window, not a second one.** Eight thousand lines of
it are copied into `static/src/chat/frappe/` by `tools/sync_from_frappe.py`, and
the seventeen `frappe.*` calls it makes are answered by
`static/src/chat/razyyn_platform.js`. So the chat an Odoo customer uses is
byte-for-byte the one an ERPNext customer uses: the same streaming, the same
saved conversations, the same clarification cards, the same export of a table to
Excel or PDF. Improving it means editing the Frappe app and running the sync;
`--check` fails the build if the two have drifted.

**Signing in is not a lookup.** If the platform accepts an account's password,
this Odoo remembers the session — creating the connection record if there is
none, and adopting the user's existing one if its e-mail column was never
filled in. It used to refuse any account without a matching row here, which
locked out everybody whose Razyyn account was made in the web app, on an
ERPNext bench, or on another Odoo; signing up instead is refused by the
platform with "This ERP is already linked to an account", so there was no way
in at all. `tools/browser/sign_in.js` is the check that starts from signed out.

Live progress is written to `razyyn.agent.chat.event` before it is sent and
delivered over `GET /razyyn/chat/events`, which replays from the last id the
browser saw. A turn belongs to a background worker, not to a browser tab, so
closing the tab does not abandon the work and reloading mid-run rejoins it.

### Sending a message on the company's behalf

The agent can send what it has just produced — a reconciliation, a VAT summary,
a generated file — through **email, Telegram, or Slack**, from credentials that
live in this Odoo and never leave it. Set it up in **Razyyn AI → Messaging
Channels**.

The agent asks this site for an outcome ("send this to the finance group") and
is never handed a token. It is also never handed a chat id: a Telegram bot
cannot start a conversation and a Slack bot cannot post in a channel it was
never invited to, so the places it may send to are a list the company keeps on
the **Destinations** tab, each with the name an accountant would say out loud.
A destination that is not on the list is refused, not invented.

A send is treated like a ledger write, for the same reason: an email to a
client's auditor cannot be unsent.

- **An idempotency key**, so a retried request does not send the same thing
  twice. Only a delivered message blocks a repeat — an attempt that was refused
  can be retried once an administrator has fixed whatever it complained about.
- **A receipt carrying the provider's own message id.** The agent may tell a
  customer something was sent only when there is an id behind it; "nothing
  raised" is not evidence of delivery.
- **A row in Message Logs for every attempt**, delivered, refused or failed. An
  outbound record holding only successes cannot answer "did the agent email
  that to our client?", which is the question an auditor actually asks.

Email goes through this system's own outgoing mail server, so there is nothing
extra to install; Telegram and Slack are plain HTTPS. The last failure on each
channel is shown on the settings form, because the person who can fix a bad
token is an administrator looking at that screen, not the accountant reading
the chat.

`tools/create_desk_check.py` sends a real message to a real chat as part of its
run, so this is checked against the provider rather than against a mock.

### Reading photographed invoices

The Scan & Extract button turns a photographed or scanned document into text on
this server, so the picture never leaves the practice and a purpose-built reader
— rather than a language model looking at a photograph — is what reads the
figures.

It needs two Python packages Odoo does not ship, and **installing the module
fetches them for you**. There is no pip line to run. `post_init_hook` calls
`razyyn_ai/services/dependencies.py`, which installs `pytesseract` and
`pdf2image` into whichever interpreter is running Odoo — `sys.executable`, so
two Odoo installs on one machine each get their own. Upgrading an existing
database takes `migrations/1.2.0/post-migration.py` instead, because Odoo runs a
`post_init_hook` only on a first install.

Nothing here can fail an install. A site with no route to PyPI gets the module,
the chat and every desk; what it loses is reading a photograph, which already
has a fallback — the picture goes to the model to look at.

The manifest deliberately has **no `external_dependencies`**. That field is a
gate, not a request: `check_external_dependencies` raises before installation
begins, so naming `pytesseract` there would refuse the very install that fetches
`pytesseract`.

Two things are detected rather than installed, because they are programs rather
than packages and installing them means `apt-get` as root from inside a module
install:

```bash
sudo apt install tesseract-ocr tesseract-ocr-ara poppler-utils   # or your distro's
```

**Razyyn AI → Settings → Description & Diagnostics** says whether this server
can read scans, names anything missing, and offers *Install Document Reading* to
try again — the reasons a fetch fails (a proxy, a firewall, a mirror down) are
all things fixed afterwards.

To manage the packages yourself, set `RAZYYN_SKIP_DEPENDENCY_INSTALL=1`; the
module still reports what is missing, it simply stops trying to fetch it.
`razyyn_ai/requirements.txt` lists them for deployments that install a module's
requirements themselves, and a test fails if that file and the code disagree.

### Reaching any record from the menu

ERPNext customers type into the awesome bar and land on any DocType list in the
system. Odoo has no equivalent: a model is reachable only if some installed
module happened to put a menu in front of it, and most of them have none. So
**Razyyn AI → Browse Odoo → All Models** carries the index — every model on the
database, searchable by the words on the screen or by the technical name the
agent uses, with **Open Records** on each row.

It grants nothing. Opening a model goes through Odoo's ordinary access and
record rules, exactly as a menu would; what it removes is the accident of
whether anybody wrote a menu.

### Two Odoos on one machine

If you run Odoo 17 and Odoo 18 side by side, reach one of them as `127.0.0.1`
and the other as `localhost`. Cookies ignore the port, so both servers write
the same `session_id` cookie for the host `localhost` — touching one replaces
the other's session, and the login page still open in the other tab then fails
with **400 Bad Request**. Different hostnames, different cookies. A customer
with one Odoo never meets this.

## The security boundary

Every statement `execute_query` runs is checked by `razyyn_ai/services/
query_guard.py` before it reaches the database, against the same contract the
Frappe app has enforced since it shipped. The guard has **zero `odoo` imports**
on purpose, so it can be verified anywhere:

```bash
cd tests && python3 -m unittest test_query_guard -v     # 24 cases, no Odoo needed
cd tests && python3 -m unittest discover                # all 84, no Odoo needed
```

What the agent may WRITE is decided by the **Agent Write Policy** record —
allowed models, blocked accounts, per-run document and amount ceilings, posting
date bounds — read fresh on every run, never cached.

Who may use the **chat** is every internal employee; which conversations each
of them sees is enforced by global `ir.rule` records. The **governance**
records (the write policy, the audit log, the messaging credentials) are
Manager-only.

## Verifying an install

The one command that matters, run from the agent repository against a live
site:

```bash
python scripts/verify_erp_app.py --erp ODOO \
    --site-url https://the-customers-odoo --api-key "$KEY"
```

It walks the six read routes as the agent's own tools call them, checks the
page-size contract, fires the shared hostile SQL corpus rendered in Odoo's own
table names, and exercises all seven write-gateway methods. Nothing is written
unless you add `--write`. It exits non-zero on any failure, so it belongs in
CI and in any upgrade runbook.

**Both series pass it in full** — 55/55, including a real create, replay, post
and read-back, a reference given the way a person names it, and a document
recorded and posted in one batch. That is the evidence behind
`RAZYYN_ODOO_ADAPTER_VERIFIED` defaulting to on.

### And the half of it that needs a browser

`verify_erp_app.py` proves the module answers the agent correctly. It cannot
see anything that only exists once a page has rendered, and two of the worst
defects this module has had were exactly that shape: on Odoo 18 every list in
the app opened as an error dialog, and a chat tab that reloaded mid-turn dropped
the answer on the floor. Over HTTP both were perfect.

```bash
cd tools/browser && npm install
node acceptance.js             # the page, the client action, every menu
node chat_flow.js              # a turn: streaming, reload mid-run, table export
node settings_and_composer.js  # the knowledge card, High Thinking, the badge
BASE=http://localhost:8079 DB=razyyn18 node acceptance.js   # the same, on 18
```

22/22, 9/9 and 9/9 on both series. See `tools/browser/README.md`.

`settings_and_composer.js` exists because all three of the things it looks at
were reported MISSING while the code that draws them was present and correct in
the tree — a widget that throws during setup leaves a blank tab, and the chat's
assets were cached by URL for seven days, so a customer on module 1.7.0 was
running the chat window from 1.2.0. Over HTTP all three were perfect.

### And the chat itself

```bash
python3 tools/sync_from_frappe.py            # copy it across
python3 tools/sync_from_frappe.py --check    # fail if it has drifted   (for CI)
```

`--check` also fails when a vendored library's bytes do not match the integrity
hash the page asks the browser for, and when the chat calls a server method this
module has not routed.

## Odoo 17 and 18

The Odoo 17 module is the SOURCE. The Odoo 18 module is GENERATED from it:

```bash
python3 tools/sync_to_odoo18.py            # regenerate
python3 tools/sync_to_odoo18.py --check    # fail if it has drifted   (for CI)
```

Three things genuinely differ between the series and are transformed
automatically: the manifest's version, `ir.cron`'s `numbercall` (removed in
18), and the list view's tag (`<tree>` in 17, `<list>` in 18 — each version
accepts only its own). Everything else is byte-identical, and `--check` is what
keeps it that way. **Edit the Odoo 17 tree; never the Odoo 18 one.**

## Installing

```bash
cp -r razyyn_ai /path/to/odoo/addons/
odoo-bin -d your_db -i razyyn_ai --stop-after-init
```

Then create a connection — Settings → Technical → Razyyn AI Connections, or
`env['razyyn.agent.settings'].generate()` — and hand the site URL and the
plaintext key it returns (shown once) to the customer's Razyyn onboarding,
exactly as an ERPNext customer hands over their Frappe API key.

### Upgrading from `razyyn_agent_connector`

The module was renamed. Run `migrations/rename_from_razyyn_agent_connector.sql`
against the database once, with Odoo stopped, before starting it with
`-u razyyn_ai`. It moves the ownership records and keeps every chat session,
every audit-log row and the customer's API key. Skip it on a fresh install.

---

## License & Support

- **License:** LGPL-3.
- **Publisher:** [Razyyn AI](https://razyyn.com).
- **Repository:** [Razyyn-AI-Odoo](https://github.com/Marwan-badr543/Razyyn-AI-Odoo)
  (branches `Razyyn-AI-Odoo-v17` and `Razyyn-AI-Odoo-v18`).
- **Website, plans & support:** [razyyn.com](https://razyyn.com).
