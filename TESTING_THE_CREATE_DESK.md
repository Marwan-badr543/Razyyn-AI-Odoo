# Testing the create desk and messaging yourself

Everything below is something you can do from the chat window or from one
screen in Odoo. It applies to Odoo 17 and Odoo 18 alike — the two modules are
generated from one source, so there is nothing to test twice for a difference
in behaviour, only for a difference in your data.

---

## 0. Before anything else: two settings decide whether any of this works

**Razyyn AI → Write Policy → Enabled.** It is **off** on a fresh install. Until
it is on, every write is refused with "Agent writes are switched off in this
system's Agent Write Policy". That refusal reaches you in the chat, so you will
not be left guessing — but it is the first thing to check on a new site.

**Razyyn AI → Messaging Channels.** Telegram needs three things: the switch on,
a bot token, and at least one row on the **Destinations** tab. A token with no
destination cannot reach anybody, and the agent will tell you Telegram is not
set up, because that is the truth.

---

## 1. The trap you already hit: where a document actually is

You asked the agent to record entries, it said they were recorded, and the
model was empty when you looked. **Both entries were written.** Two things hid
them, and both are Odoo behaving normally.

### The company switcher

Your entries went into **EG Company**, because that is the company the accounts
they use belong to. Your user's active company was **YourCompany**. Odoo filters
every list by the companies ticked in the switcher at the top right, so the
journal-entries list could not have shown them.

> **Test it:** top right, tick **EG Company**. Open **Accounting → Accounting →
> Journal Entries**. They are there.

### A draft journal entry has no number

Until an entry is posted, Odoo has not given it one — its name is literally `/`
and its screen calls it *Draft Entry (\* 128)*. So there is no document number
to search for. Search by the **reference text** instead ("Purchase of building
for cash"), or just sort by date.

### What changed because of this

The agent used to report a document by its internal reference only —
"account.move 126" — which is not something your screen ever shows. It now
tells you what the document is called on your own screen **and which company it
is in**, for every action, including when it repeats one it had already done.
Both ERPs do this now, so an ERPNext report reads the same way.

### Three places to see what the agent wrote

| Where | What it shows |
|---|---|
| **Accounting → Journal Entries** | the documents themselves — with the right company ticked |
| **Razyyn AI → Write Audit Logs** | every attempt, recorded or refused, with the model, the record id and the reason |
| **Razyyn AI → Razyyn AI Models** | everything this module stores, in one list |

Write Audit Logs is the one to reach for when you are unsure. It has a row for
every single thing the agent tried, whatever the outcome.

---

## 2. Chat test cases — recording, changing, posting, reversing

Do these in order in one conversation. After each, check the document in
Accounting with the right company selected.

| # | Say this | What should happen |
|---|---|---|
| 1 | "Record a journal entry dated today: debit Buildings 1,000,000 and credit Cash 1,000,000, reference 'building purchase'." | It asks for anything it needs, then reports it **recorded** — naming the document and the company, and saying it is a draft. |
| 2 | "Change the reference on that entry to 'building purchase — head office'." | Reported as **changed**. Open it: the reference is the new one. |
| 3 | "Post it." | Reported as **posted**, and the entry now has a real number. |
| 4 | "Change its reference again." | **Refused**, saying a posted document cannot be edited and that the way to correct one is to reverse it and record a corrected one. This refusal is the point of the test. |
| 5 | "Cancel that entry." | Reported as **reversed**. |
| 6 | "Record an entry for 500 to Office Supplies against Cash, and post it." | **One message, two actions.** Both should happen: recorded *and* posted, not left in draft. |
| 7 | Repeat test 6 word for word. | It should say it had **already done that** and name the same document. If a second entry appears, that is a bug — tell me. |
| 8 | "Record an entry: debit Cash 100, credit Sales 90." | **Refused before anything is written**, because it does not balance. |

### Things worth trying that are less obvious

- **Ask for something in a company you cannot see.** Tick only YourCompany, then
  ask for an entry using EG Company accounts. The receipt should still tell you
  it went to EG Company — that sentence is the whole fix from §1.
- **Ask it to record five entries at once.** Set **Write Policy → Max documents
  per run** to 2 first. The whole request should be refused with the ceiling
  named, and *nothing* recorded — not two of the five.
- **Switch Write Policy → Dry run only on.** It should validate and record
  nothing, and say so plainly.

---

## 3. Chat test cases — sending a Telegram message

Your bot and your chat (`marwan`) are already set up on both Odoo 17 and
Odoo 18.

| # | Say this | What should happen |
|---|---|---|
| 1 | "Send a message on Telegram to marwan saying the month close is done." | It arrives in your chat. The reply quotes a **reference number** — that is Telegram's own message id. |
| 2 | Repeat test 1 word for word. | It should say it had **already sent that** and give the same reference. Only one message should arrive. |
| 3 | "Send that to the auditors on Telegram." | **Refused**, listing the destinations that do exist. A bot cannot start a conversation, so it can only send where it has already been added. |
| 4 | "What can you send through?" | It should say Telegram can reach `marwan`, and that email and Slack are not set up here — naming what is missing rather than just saying no. |
| 5 | Produce something first ("give me a trial balance as Excel"), then "send that file to marwan on Telegram". | The file arrives as a document, with the message as its caption. |

Then open **Razyyn AI → Message Logs**. There should be a row for every one of
those, **including the refusal in test 3**. A refused or failed attempt is
recorded exactly like a delivered one; only the status differs.

### Two behaviours worth knowing

- **A refusal can be retried; a delivery cannot be repeated.** If a send fails
  because of a setting, fix the setting and ask again — it will go. If it
  already went, asking again returns the original receipt and sends nothing.
- **Email is different from Telegram on purpose.** Telegram and Slack can only
  reach places you listed, so the agent sends without asking. Email can reach
  any address, so the agent shows you the message and sends only when you
  approve it.

---

## 4. The automated check, if you want to run it yourself

```bash
python3 tools/create_desk_check.py --base-url http://localhost:8069 --api-key <key>
```

54 checks against a running site: the write policy, a document type's real
fields, finding accounts and journals, validating before writing, recording,
reading back what was recorded, changing a draft, posting, refusing to edit a
posted document, reversing, repeating an instruction, "record and post" as one
request, a batch where one document fails, the audit log, listing, refusing an
unauthenticated caller, what the site can send through, and **one real Telegram
message**.

Use `--base-url http://localhost:8079` for Odoo 18. The key is a Razyyn
connection's API key.

The module's own tests run with Odoo:

```bash
python3 odoo-bin -c odoo.conf -d <database> -u razyyn_ai \
    --test-enable --stop-after-init --test-tags /razyyn_ai
```

47 of them, and they cover the parts that should never be exercised against a
live site — writing switched off, check-only mode, the per-run ceilings.

---

## 5. What a refusal should always look like

However a request fails, the answer should name **what did not happen** and
**why**, in words you can act on. If you ever see the agent say something was
recorded, posted, or sent without being able to tell you where to find it or
what its reference is, that is worth reporting — every one of those sentences
is built from a receipt that carries the document's name, its company and the
provider's own message id, and it is not allowed to claim any of them without
one.
