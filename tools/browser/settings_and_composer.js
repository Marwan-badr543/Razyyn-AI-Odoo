// Copyright (c) 2026, Marwan Badr and contributors
// For license information, please see LICENSE
//
// The three things on screen that no HTTP check can see.
//
// WHY THESE THREE
//     Each was reported by a customer as MISSING when the code that draws it
//     was present and correct in the tree:
//
//       * the company-knowledge card on Agent Settings — a widget that throws
//         during setup leaves a blank tab, and Odoo 18 removed the `rpc`
//         service the first version used, so it rendered on 17 and threw on 18
//       * the High Thinking switch beside Scan — the chat assets were cached by
//         URL for seven days, so a customer on module 1.7.0 was running the
//         chat window from 1.2.0
//       * the thinking badge reading "Finance Manager" — same cache, and the
//         name lives in a lookup whose first branch never fires
//
//     Over HTTP all three were perfect. They are only visible once a page has
//     rendered, which is what this file is for.
//
// Usage:
//     node settings_and_composer.js                          # Odoo 17 on 8069
//     BASE=http://localhost:8078 node settings_and_composer.js   # Odoo 18

const { open, sleep } = require("./cdp");
const BASE = process.env.BASE || "http://localhost:8069";
const SHOTS = process.env.SHOTS || "/tmp";
// The same local test account the other checks use. It must hold
// **Razyyn AI Accountant / Manager**, because the card under test is on the
// connection form and that form is Manager-only. Override with LOGIN/PW.
const LOGIN = process.env.LOGIN || "razyyn_test@test.local";
const PW = process.env.PW || "razyyn_test_pw_1";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail || "" });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " :: " + detail : ""}`);
}

(async () => {
  const page = await open(BASE);
  try {
    await page.goto(`${BASE}/web/login`);
    await page.evaluate(`
      const set = (s, v) => { const e = document.querySelector(s); if (e) { e.value = v;
        e.dispatchEvent(new Event("input", { bubbles: true })); } };
      set("input[name=login]", ${JSON.stringify(LOGIN)});
      set("input[name=password]", ${JSON.stringify(PW)});
      document.querySelector("form.oe_login_form, form").submit();
      return true;
    `);
    await sleep(7000);

    // ── 1. the settings card ────────────────────────────────────────────────
    const settingsId = await page.evaluate(`
      const r = await fetch("/web/dataset/call_kw", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          model: "razyyn.agent.settings", method: "search_read",
          args: [[["user_id", "=", 2]], ["id"]], kwargs: { limit: 1 } } }),
      }).then((x) => x.json());
      return (r.result && r.result[0] && r.result[0].id) || 0;
    `);
    const ODOO18 = /^18/.test(await page.evaluate(`return (window.odoo && odoo.info && odoo.info.server_version) || ""`));
    await page.goto(ODOO18
      ? `${BASE}/odoo/action-razyyn_ai.action_razyyn_agent_settings/${settingsId}`
      : `${BASE}/web#id=${settingsId}&model=razyyn.agent.settings&view_type=form`);
    await sleep(6000);
    // The card lives on its own notebook page; click it.
    await page.evaluate(`
      const tab = Array.from(document.querySelectorAll(".o_notebook .nav-link"))
        .find((t) => /Company Knowledge/i.test(t.textContent));
      if (tab) tab.click();
      return !!tab;
    `);
    await sleep(4000);
    const card = await page.evaluate(`
      const root = document.querySelector(".razyyn_knowledge");
      if (!root) return { present: false };
      const options = root.querySelectorAll("select option").length;
      return {
        present: true,
        heading: (root.querySelector("h4") || {}).textContent || "",
        countries: options,
        hasUpload: !!Array.from(root.querySelectorAll("button"))
          .find((b) => /Upload policy|Replace policy/i.test(b.textContent)),
        hasChoose: !!Array.from(root.querySelectorAll("button"))
          .find((b) => /Choose a PDF/i.test(b.textContent)),
        policy: (root.querySelector(".razyyn_knowledge_doc strong") || {}).textContent || "",
        text: (root.innerText || "").replace(/\s+/g, " ").slice(0, 200),
      };
    `);
    await page.screenshot(`${SHOTS}/ui-1-settings.png`);
    check("the settings form shows the company-knowledge card", card.present, card.heading);
    check("the country list is the platform's (249 entries)", card.countries > 200,
          `${card.countries} options`);
    check("the upload controls are there", card.hasChoose && card.hasUpload);
    check("the indexed policy is named", /policy/i.test(card.policy || ""), card.policy);

    // ── 2 & 3. the chat window ──────────────────────────────────────────────
    await page.goto(`${BASE}/razyyn/chat`);
    await sleep(9000);
    const chat = await page.evaluate(`
      const q = (s) => document.querySelector(s);
      const think = q(".agent-think-btn");
      const scan = q(".agent-scan-btn");
      // The badge name comes from agent_display_name on the page object.
      const page_ = window.frappe && window.frappe.pages && window.frappe.pages["agent-chat"];
      const chatObj = page_ && page_.agent_chat_instance;
      let manager = "", desk = "";
      try {
        manager = chatObj.agent_display_name("master");
        desk = chatObj.agent_display_name("create");
      } catch (e) { manager = "ERR:" + e.message; }
      return {
        think: !!think,
        scan: !!scan,
        thinkLabel: think ? think.innerText.trim() : "",
        thinkAfterScan: !!(scan && think && scan.nextElementSibling === think),
        manager,
        desk,
        sourceHasFinanceManager:
          /Finance Manager/.test(document.documentElement.innerHTML) ? true : "check-js",
      };
    `);
    await page.screenshot(`${SHOTS}/ui-2-chat.png`);
    check("the chat composer has the Scan switch", chat.scan);
    check("the chat composer has the High Thinking switch", chat.think, chat.thinkLabel);
    check("High Thinking sits beside Scan", chat.thinkAfterScan);
    check("the manager badge reads Finance Manager",
          /Finance Manager/i.test(chat.manager || ""), `master -> "${chat.manager}", create -> "${chat.desk}"`);

    const errors = page.errors().filter((e) => !/favicon|ERR_/.test(e));
    check("no JavaScript errors on either page", errors.length === 0, errors.slice(0, 3).join(" | "));
  } finally {
    const failed = results.filter((r) => !r.ok).length;
    console.log(`\nRESULT ${results.length - failed}/${results.length} passed`);
    process.exit(failed ? 1 : 0);
  }
})();
