// One-off: with the packages removed, does the settings page say so, and does
// the button put them back? Not part of the standard suite -- it changes what
// is installed in the venv.
const { open, signIn, sleep } = require("./cdp");
const BASE = process.env.BASE || "http://localhost:8069";
const DB = process.env.DB || "profession";
const SHOTS = process.env.SHOTS || "/tmp";
// The harness account, kept in step with acceptance.js by reading it from
// there. A copied default drifts, and the failure it produces -- an
// AccessDenied traceback out of Odoo -- reads like a defect in the product.
const acceptance = require("fs").readFileSync(__dirname + "/acceptance.js", "utf8");
const LOGIN = process.env.LOGIN || /LOGIN = process\.env\.LOGIN \|\| "([^"]+)"/.exec(acceptance)[1];
const PW = process.env.PW || /PW = process\.env\.PW \|\| "([^"]+)"/.exec(acceptance)[1];
const TAG = process.env.TAG || "press";

(async () => {
  const page = await open(BASE);
  try {
    const uid = await signIn(page, BASE, DB, LOGIN, PW);
    console.log("signed in, uid", uid);
    const version = parseInt(process.env.ODOO_VERSION || "17", 10);
    const listUrl = version >= 18
      ? `${BASE}/odoo/action-razyyn_ai.action_razyyn_agent_settings`
      : `${BASE}/web#action=razyyn_ai.action_razyyn_agent_settings`;
    await page.goto(listUrl);
    await sleep(2600);
    const id = await page.evaluate(`
      const a = await fetch("/web/dataset/call_kw", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: {
          model: "razyyn.agent.settings", method: "search", args: [[]], kwargs: { limit: 1 } }}),
      }).then(function (r) { return r.json(); });
      return (a.result || [])[0] || 0;
    `);
    const formUrl = version >= 18
      ? `${BASE}/odoo/action-razyyn_ai.action_razyyn_agent_settings/${id}`
      : `${BASE}/web#id=${id}&model=razyyn.agent.settings&view_type=form`;

    const look = async () => {
      await page.goto("about:blank");
      await page.goto(formUrl);
      await sleep(3200);
      await page.evaluate(`
        const tab = Array.from(document.querySelectorAll(".o_notebook .nav-link, .nav-link"))
          .find(function (a) { return /Diagnostics/i.test(a.innerText || ""); });
        if (tab) tab.click(); return true;
      `);
      await sleep(1000);
      return page.evaluate(`
        const form = document.querySelector(".o_form_view");
        const text = form ? (form.innerText || "") : "";
        const button = Array.from(document.querySelectorAll("button"))
          .find(function (b) { return /Install Document Reading/i.test(b.innerText || ""); });
        return {
          says_not_ready: /(^|\\n)Not ready\\./.test(text),
          says_ready: /(^|\\n)Ready\\. Scanned/.test(text),
          names_missing: /pytesseract/i.test(text) || /pdf2image/i.test(text),
          offers_install: !!button,
        };
      `);
    };

    const before = await look();
    console.log("BEFORE:", JSON.stringify(before));
    await page.screenshot(`${SHOTS}/${TAG}-1-not-ready.png`);

    await page.evaluate(`
      const button = Array.from(document.querySelectorAll("button"))
        .find(function (b) { return /Install Document Reading/i.test(b.innerText || ""); });
      if (button) button.click(); return !!button;
    `);
    let toast = "";
    for (let i = 0; i < 40 && !toast; i++) {
      await sleep(700);
      toast = await page.evaluate(`
        const n = document.querySelector(".o_notification, .o_notification_content");
        return n ? (n.innerText || "").slice(0, 200) : "";
      `);
    }
    console.log("NOTIFICATION:", JSON.stringify(toast));
    await page.screenshot(`${SHOTS}/${TAG}-2-pressed.png`);

    const after = await look();
    console.log("AFTER:", JSON.stringify(after));
    await page.screenshot(`${SHOTS}/${TAG}-3-ready.png`);

    const ok = before.says_not_ready && before.offers_install && before.names_missing
            && after.says_ready && !after.offers_install;
    console.log(ok ? "\nPASS  the page reported it, the button fixed it" : "\nFAIL");
    process.exitCode = ok ? 0 : 1;
  } finally { await page.close(); }
})().catch((e) => { console.error("DRIVER FAILED:", e.message); process.exit(1); });
