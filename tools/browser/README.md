# The checks that need a browser

`scripts/verify_erp_app.py` proves the module answers the agent correctly. It
cannot see anything that only exists once a page has rendered — and two of the
worst defects in this module's history were exactly that shape:

- Odoo 18 renamed the list view type, so **every list in the app opened as an
  error dialog**. Over HTTP the module was perfect.
- The chat window's answer was written into a bubble created by the page load
  that sent the message, so **a reloaded tab dropped the answer on the floor**.

So these run a real Chrome against a real Odoo.

```bash
npm install                     # once: the CDP client

node acceptance.js              # the app: page, client action, every menu,
                                #   and whether this server can read a scan
node chat_flow.js               # a turn: streaming, reload, table export

RAZYYN_EMAIL=you@example.com RAZYYN_PASS=secret \
  node sign_in.js               # signing in, starting from signed OUT

# Odoo 18, or any other site
BASE=http://localhost:8079 DB=razyyn18 node acceptance.js

# Run two at once — each needs its own debugger port
CDP_PORT=9301 node acceptance.js &
CDP_PORT=9302 BASE=http://localhost:8079 DB=razyyn18 node acceptance.js &
```

## Why `sign_in.js` is separate

`acceptance.js` signs in to Odoo as a user who is ALREADY connected to Razyyn,
so it never once pressed Connect — and two defects lived in that blind spot:

- A refused sign-in came back as **HTTP 200 with the reason in the body** (that
  is how Odoo answers a `type="json"` route), and the chat window's catch block
  only `console.error`s it, because on Frappe the customer has already been
  shown a dialog. So a wrong password did nothing visible at all.
- Signing in refused any account without a matching row in this database,
  which locked out everyone whose Razyyn account was made anywhere else — and
  signing up instead is refused by the platform with "This ERP is already
  linked to an account".

So this one starts by disconnecting, and reaches the signed-out state the same
way a customer does. It needs a Razyyn account it may sign in as
(`RAZYYN_EMAIL` / `RAZYYN_PASS`) and, optionally, an Odoo user to be
(`LOGIN` / `PW`, defaulting to the harness account named in `acceptance.js`).

Two Odoo users exist on the development databases for this: `razyyn_login_check
@test.local` and `razyyn_signup_check@test.local`. Neither is needed in
production and both can be removed.

## One more, run by hand

`press_install.js` proves the other half of the reading status: that the page
says so when the packages are NOT there, and that the button puts them back. It
is not in the standard suite because it only means anything against a server
that is missing them, so remove them first:

```bash
/path/to/odoo/venv/bin/python -m pip uninstall -y pytesseract pdf2image
CDP_PORT=9316 node press_install.js      # leaves them installed again
```

It fails if the page claims to be ready when it is not, which is the failure
that matters: a diagnostic that only ever says "fine" is worse than none.

Both suites exit non-zero on failure and write screenshots to `$SHOTS` (default
`/tmp`). They sign in as `razyyn_test@test.local`; override with `LOGIN` and
`PW`. The account needs the Razyyn AI Manager group to reach the governance
menus — an ordinary employee is not shown them, which is itself the behaviour
`acceptance.js` would catch if it regressed.
