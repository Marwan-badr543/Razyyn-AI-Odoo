// What a customer actually does, driven in a real browser.
const { open, signIn, sleep } = require("./cdp.js");

const BASE = process.env.BASE || "http://localhost:8069";
const DB = process.env.DB || "profession";
const LOGIN = process.env.LOGIN || "razyyn_test@test.local";
const PW = process.env.PW || "razyyn_test_pw_1";
const SHOTS = process.env.SHOTS || "/tmp";
const TAG = process.env.TAG || "o17";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const page = await open();
  try {
    const uid = await signIn(page, BASE, DB, LOGIN, PW);

    // Odoo 18 routes actions at /odoo/action-<xmlid>; 17 uses the hash router.
    const version = await page.evaluate(`
      const res = await fetch("${BASE}/web/webclient/version_info", {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {} }),
      });
      const body = await res.json();
      return body.result.server_version_info[0];
    `);
    const actionUrl = (xmlid) => version >= 18
      ? `${BASE}/odoo/action-${xmlid}`
      : `${BASE}/web#action=${xmlid}`;
    console.log("Odoo major version", version);
    check("sign in to Odoo", Number.isInteger(uid), "uid " + uid);

    // ── The chat page itself ────────────────────────────────────────────
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(3000);

    const shape = await page.evaluate(`
      return {
        connected: !!document.querySelector(".agent-chat-view"),
        auth_card: !!document.querySelector(".agent-auth-card"),
        sidebar: !!document.querySelector(".agent-chat-sidebar, .agent-sidebar"),
        composer: !!document.querySelector("#agent-chat-input, .agent-chat-input textarea, textarea"),
        newchat: !!document.querySelector(".agent-new-chat-btn, .new-chat-btn"),
        text: (document.body.innerText || "").slice(0, 200),
      };
    `);
    check("chat page renders", shape.connected || shape.auth_card, JSON.stringify(shape).slice(0, 200));
    check("no console errors on load", page.errors().length === 0,
          page.errors().slice(0, 2).join(" | ").slice(0, 300));
    await page.screenshot(`${SHOTS}/${TAG}-1-chat.png`);

    if (shape.auth_card) {
      check("already signed in to Razyyn", false, "showing the sign-in card");
    } else {
      check("already signed in to Razyyn", true, "");
    }

    // ── History survives a reload ───────────────────────────────────────
    const before = await page.evaluate(`
      return document.querySelectorAll(".agent-message, .agent-chat-message").length;
    `);
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(3500);
    const after = await page.evaluate(`
      const chats = document.querySelectorAll(".agent-chat-item, .chat-list-item");
      return { chats: chats.length,
               messages: document.querySelectorAll(".agent-message, .agent-chat-message").length };
    `);
    check("conversations listed after a reload", after.chats > 0, `${after.chats} in the sidebar`);

    // ── The client action inside Odoo's own web client ───────────────────
    await page.goto(actionUrl("razyyn_ai.action_razyyn_chat"));
    await sleep(5000);
    const embedded = await page.evaluate(`
      const frame = document.querySelector("iframe.razyyn_chat_frame");
      return {
        frame: !!frame,
        navbar: !!document.querySelector(".o_main_navbar"),
        menus: Array.from(document.querySelectorAll(".o_menu_sections a, .o-dropdown"))
                 .map(function (a) { return (a.innerText || "").trim(); })
                 .filter(Boolean).slice(0, 12),
      };
    `);
    check("chat opens inside Odoo, not a bare tab", embedded.frame && embedded.navbar,
          JSON.stringify(embedded).slice(0, 220));
    await page.screenshot(`${SHOTS}/${TAG}-2-embedded.png`);

    // ── Every model reachable from the menu ─────────────────────────────
    const actions = [
      ["Conversations", "razyyn_ai.action_razyyn_chat_session"],
      ["Messages", "razyyn_ai.action_razyyn_chat_message"],
      ["Write Policy", "razyyn_ai.action_razyyn_agent_write_policy"],
      ["Write Audit Logs", "razyyn_ai.action_razyyn_agent_write_log"],
      ["Connections", "razyyn_ai.action_razyyn_agent_settings"],
      ["Messaging Channels", "razyyn_ai.action_razyyn_agent_messaging_settings"],
      ["Message Logs", "razyyn_ai.action_razyyn_agent_message_log"],
      ["Stream Events", "razyyn_ai.action_razyyn_chat_event"],
      ["All Odoo Models", "razyyn_ai.action_razyyn_all_models"],
    ];

    // IS IT IN THE MENU, which the loop below cannot answer. Each pass of that
    // loop types the action into the URL, and an action opens from a URL
    // whether or not anything ever offers it to the customer. Every one of
    // these nine passed while six of them were missing from the menu this
    // person actually sees: they sat under a heading carrying a `groups` the
    // administrator was not in, and Odoo hides a menu whose parent is hidden.
    //
    // /web/webclient/load_menus is the web client's own call -- the same tree
    // it draws the menu from, for whoever is signed in, after `groups` and the
    // access rules have had their say.
    const menu = await page.evaluate(`
      const res = await fetch("${BASE}/web/webclient/load_menus/acceptance-check",
                              { credentials: "same-origin" });
      const tree = await res.json();
      const root = Object.values(tree).find(function (entry) {
        return entry && entry.xmlid === "razyyn_ai.menu_razyyn_root";
      });
      if (!root) return { found: false, labels: [], first: "" };
      const labels = [];
      (function walk(ids) {
        for (const id of ids || []) {
          const node = tree[id] || tree[String(id)];
          if (!node) continue;
          labels.push(node.name);
          walk(node.children);
        }
      })(root.children);
      const firstChild = (root.children || [])
        .map(function (id) { return tree[id] || tree[String(id)]; })
        .filter(Boolean)
        .sort(function (a, b) { return (a.sequence || 0) - (b.sequence || 0); })[0];
      return { found: true, labels: labels, first: firstChild ? firstChild.name : "" };
    `);
    check("the app is in the menu at all", menu.found, menu.labels.length + " entries");
    const absent = actions.map(function (pair) { return pair[0]; })
                          .filter(function (label) { return !menu.labels.includes(label); });
    check("every Razyyn screen is in this person's menu", absent.length === 0,
          absent.length ? "missing: " + absent.join(", ") : menu.labels.join(", "));
    // Clicking the app opens its first child. A list of models is not what
    // somebody clicking "Razyyn AI" is asking for.
    check("the app opens on the chat", menu.first === "Agent Chat", menu.first);
    for (const [label, xmlid] of actions) {
      // Blank in between, because on 17 the action lives in the URL HASH:
      // going from #action=A to #action=B changes no document, so the browser
      // fires no navigation and the page under test never moves. The test then
      // grades the previous screen.
      await page.goto("about:blank");
      await page.goto(actionUrl(xmlid));
      await sleep(2600);
      const ok = await page.evaluate(`
        const err = document.querySelector(".o_error_dialog, .o_dialog_error");
        const view = document.querySelector(".o_list_view, .o_form_view, .o_kanban_view, .o_view_nocontent");
        return { ok: !!view && !err,
                 title: (document.querySelector(".o_breadcrumb, .o_control_panel .o_last_breadcrumb_item")||{}).innerText || "" };
      `);
      check(`menu: ${label}`, ok.ok, ok.title.trim().slice(0, 40));
    }
    await page.screenshot(`${SHOTS}/${TAG}-3-model.png`);

    // ── Any model in this Odoo, reachable from the menu ─────────────────
    //
    // ERPNext has an awesome bar; Odoo has nothing of the kind, so a model is
    // reachable only if some module put a menu in front of it. The app carries
    // its own index instead. This checks the index lists real models AND that
    // opening one actually lands on its records -- a list of dead ends would
    // be worse than no list.
    await page.goto("about:blank");
    await page.goto(actionUrl("razyyn_ai.action_razyyn_all_models"));
    await sleep(3200);
    const index = await page.evaluate(`
      const rows = Array.from(document.querySelectorAll(".o_data_row"));
      const text = rows.map(function (r) { return (r.innerText || ""); }).join(" | ");
      return { rows: rows.length, has_open: /Open Records/i.test(document.body.innerText),
               sample: text.slice(0, 120) };
    `);
    check("the menu lists the models on this Odoo", index.rows > 20,
          `${index.rows} listed`);

    // Search for one by its technical name, the way the agent names it, then
    // open it.
    const opened = await page.evaluate(`
      const answer = await fetch("/web/dataset/call_kw", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          model: "ir.model", method: "search_read",
          args: [[["model", "=", "res.partner"]], ["id", "razyyn_browsable"]],
          kwargs: { limit: 1 } }}),
      }).then(function (r) { return r.json(); });
      const row = (answer.result || [])[0];
      if (!row) return { found: false };
      const act = await fetch("/web/dataset/call_kw", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          model: "ir.model", method: "action_razyyn_open_records",
          args: [[row.id]], kwargs: {} }}),
      }).then(function (r) { return r.json(); });
      return { found: true, browsable: row.razyyn_browsable, action: act.result || act.error };
    `);
    check("opening a model from the index opens that model's records",
          !!opened.action && opened.action.res_model === "res.partner",
          JSON.stringify(opened.action).slice(0, 140));
    await page.screenshot(`${SHOTS}/${TAG}-5-all-models.png`);

    // ── Whether this server can read a scanned invoice, said where an
    //    administrator will see it. The packages arrive with the module, so
    //    this is the screen that reports the cases where they did not.
    //
    //    The record is reached by URL rather than by clicking a row: which
    //    element of a list opens a form differs between 17 and 18, and a click
    //    that quietly does nothing grades the list view instead of the form.
    await page.goto("about:blank");
    await page.goto(actionUrl("razyyn_ai.action_razyyn_agent_settings"));
    await sleep(2600);
    const settingsId = await page.evaluate(`
      const answer = await fetch("/web/dataset/call_kw", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          model: "razyyn.agent.settings", method: "search",
          args: [[]], kwargs: { limit: 1 },
        }}),
      }).then(function (r) { return r.json(); });
      return (answer.result || [])[0] || 0;
    `);
    const formUrl = version >= 18
      ? `${BASE}/odoo/action-razyyn_ai.action_razyyn_agent_settings/${settingsId}`
      : `${BASE}/web#id=${settingsId}&model=razyyn.agent.settings&view_type=form`;
    await page.goto("about:blank");
    await page.goto(formUrl);
    await sleep(3000);
    await page.evaluate(`
      const tab = Array.from(document.querySelectorAll(".o_notebook .nav-link, .nav-link"))
        .find(function (a) { return /Diagnostics/i.test(a.innerText || ""); });
      if (tab) tab.click();
      return !!tab;
    `);
    await sleep(1200);
    const ocr = await page.evaluate(`
      const form = document.querySelector(".o_form_view");
      const text = form ? (form.innerText || "") : "";
      const button = Array.from(document.querySelectorAll("button"))
        .find(function (b) { return /Install Document Reading/i.test(b.innerText || ""); });
      return {
        on_a_form: !!form,
        names_the_section: /Reading Scanned Documents/i.test(text),
        says_ready: /(^|\\n)Ready\\. Scanned and photographed/.test(text),
        offers_install: !!button,
      };
    `);
    check("settings say whether scans can be read", ocr.on_a_form && ocr.names_the_section,
          JSON.stringify(ocr));
    // The packages are on this server, so it must say so AND must not be
    // offering to install what is already installed.
    check("it reports ready, and hides the install button when it is",
          ocr.says_ready && !ocr.offers_install, JSON.stringify(ocr));
    await page.screenshot(`${SHOTS}/${TAG}-4-document-reading.png`);

    const failures = results.filter((r) => !r.ok);
    console.log(`\n${results.length - failures.length}/${results.length} checks passed`);
    process.exitCode = failures.length ? 1 : 0;
  } finally {
    await page.close();
  }
})().catch((e) => { console.error("DRIVER FAILED:", e.message); process.exit(1); });
