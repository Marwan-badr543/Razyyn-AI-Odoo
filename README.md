# Razyyn AI for Odoo 18

> **`razyyn_ai/` here is GENERATED. Do not edit it.**
>
> Every file under `razyyn_ai/` is produced from the Odoo 17 module by
> `razyyn-odoo-17/tools/sync_to_odoo18.py`. Make the change there and re-run
> that script; anything edited here is overwritten the next time anyone does.
> `sync_to_odoo18.py --check` fails if the two have drifted, which is what
> stops that from happening silently.

The Odoo half of the Razyyn accounting agent. An Odoo customer installs this
module the same way an ERPNext customer installs the Frappe app, and the agent
then works against their Odoo exactly as it does against ERPNext.

This is one piece of the Razyyn stack:

- the agent backend (FastAPI + LangGraph) — `accountant_agent/`
- the same bridge for ERPNext/Frappe — `Razyyn-AI-Frappe` (v14 and v15 branches)
- **the source this tree is generated from** — `razyyn-odoo-17/`

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
node acceptance.js    # the page, the client action, every menu
node chat_flow.js     # a turn: streaming, reload mid-run, table export
BASE=http://localhost:8079 DB=razyyn18 node acceptance.js   # the same, on 18
```

14/14 and 9/9 on both series. See `tools/browser/README.md`.

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
